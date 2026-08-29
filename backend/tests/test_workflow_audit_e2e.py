"""OBS-05: integrated adversarial campaign for workflow node auditability."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.providers.base import ProviderProfile
from lohra.state import SessionDB
from lohra.workflow.audit import AuditTrail
from lohra.workflow.audit_query import WorkflowAuditTool
from lohra.workflow.service import WorkflowService
from tests.test_loop import _text_response


class _PromptClient(ModelClient):
    """A thread-safe, prompt-derived oracle; scheduling cannot assign answers."""

    def __init__(self, *, coordinate_first_stage: bool = False) -> None:
        self._lock = threading.Lock()
        self.prompts: list[str] = []
        self._calls: Counter[str] = Counter()
        self._first_stage = threading.Barrier(4) if coordinate_first_stage else None
        self.release_slow = threading.Event()

    def create(self, **kwargs: Any) -> dict[str, Any]:
        messages = kwargs.get("messages") or []
        prompt = " ".join(
            str(message.get("content", ""))
            for message in messages
            if isinstance(message, dict)
        )
        marker = prompt.rsplit(" ", 1)[-1]
        with self._lock:
            self.prompts.append(marker)
            call = self._calls[marker]
            self._calls[marker] += 1
        coordinated = self._first_stage is not None and call == 0 and marker in {
            "slow-a", "fast-a", "slow-b", "fast-b"
        }
        if coordinated:
            self._first_stage.wait(timeout=5)
            if marker.startswith("slow-"):
                assert self.release_slow.wait(5)
        return _text_response(marker)

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs: Any) -> dict[str, Any]:
        return self.create(**kwargs)


def _factory(client: ModelClient, *, profile: ProviderProfile | None = None):
    selected = profile or get_provider_profile("anthropic")

    def build() -> Agent:
        return Agent(model="audit-test", provider=selected, client=client)

    return build


def _service(
    db: SessionDB,
    home: Path,
    client: ModelClient,
    *,
    profile: ProviderProfile | None = None,
    max_runs: int = 2,
) -> WorkflowService:
    return WorkflowService(
        base_child_factory=_factory(client, profile=profile),
        db=db,
        home=home,
        run_concurrency=4,
        max_runs=max_runs,
    )


def _events(db: SessionDB, run_id: str) -> list[dict[str, Any]]:
    result = db.audit_query(run_id, limit=100)
    assert result["availability"] == "available"
    assert result["page"]["has_more"] is False
    return result["events"]


def test_concurrent_runs_preserve_leaf_identity_out_of_order_and_through_nesting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    client = _PromptClient(coordinate_first_stage=True)
    service = _service(db, tmp_path, client)
    original_record_gateway = service._audit.record_gateway
    terminal_lock = threading.Lock()
    fast_terminals = 0

    def release_slow_after_both_fast_leaves(
        frame: dict[str, Any], context: Any, *, sub_id: str
    ) -> bool:
        nonlocal fast_terminals
        accepted = original_record_gateway(frame, context, sub_id=sub_id)
        params = frame.get("params", {})
        payload = params.get("payload", {}) if isinstance(params, dict) else {}
        terminal = (
            params.get("type") == "message.complete"
            and isinstance(payload, dict)
            and payload.get("status") == "complete"
            and context.node_path == ("pipe",)
            and context.stage_index == 0
        )
        if terminal:
            with terminal_lock:
                fast_terminals += 1
                if fast_terminals == 2:
                    client.release_slow.set()
        return accepted

    service._audit.record_gateway = release_slow_after_both_fast_leaves  # type: ignore[method-assign]
    nested = {
        "meta": {"name": "nested-template"},
        "nodes": [{"id": "inner", "type": "agent", "prompt": "nested nested-result"}],
    }
    monkeypatch.setattr(
        "lohra.workflow.service.library.get_template",
        lambda _home, ref: nested if ref == "nested-template" else None,
    )
    spec = {
        "meta": {"name": "concurrent-audit"},
        "nodes": [
            {
                "id": "pipe",
                "type": "pipeline",
                "items": "${args.items}",
                "stages": [
                    {"prompt": "first ${item}"},
                    {"prompt": "second ${stage.result}"},
                ],
            },
            {"id": "nested", "type": "workflow", "ref": "nested-template"},
        ],
    }
    try:
        run_a = service.start(spec, args={"items": ["slow-a", "fast-a"], "label": "run-a"})[
            "run_id"
        ]
        run_b = service.start(spec, args={"items": ["fast-b", "slow-b"], "label": "run-b"})[
            "run_id"
        ]
        status_a = service.status(run_a, wait=True, timeout=10)
        status_b = service.status(run_b, wait=True, timeout=10)
        assert status_a["status"] == status_b["status"] == "complete"
        assert status_a["outputs"]["pipe"] == ["slow-a", "fast-a"]
        assert status_b["outputs"]["pipe"] == ["fast-b", "slow-b"]
        assert service._audit.flush(timeout=2)

        all_sub_ids: set[str] = set()
        for run_id, fast_index in ((run_a, 1), (run_b, 0)):
            rows = _events(db, run_id)
            assert all(row["identity"]["run_id"] == run_id for row in rows)
            leaves = [row for row in rows if row["event_type"].startswith("leaf.")]
            started = [row for row in leaves if row["event_type"] == "leaf.started"]
            completed = [row for row in leaves if row["event_type"] == "leaf.completed"]
            assert len(started) == len(completed) == 5

            by_sub: dict[str, list[dict[str, Any]]] = {}
            for row in leaves:
                by_sub.setdefault(row["identity"]["sub_id"], []).append(row)
            assert len(by_sub) == 5
            assert all({item["event_type"] for item in pair} == {
                "leaf.started", "leaf.completed"
            } for pair in by_sub.values())
            assert not (all_sub_ids & set(by_sub))
            all_sub_ids.update(by_sub)

            pipeline = [row for row in started if row["identity"]["node_path"] == ["pipe"]]
            assert {
                (row["identity"]["item_index"], row["identity"]["stage_index"])
                for row in pipeline
            } == {(0, 0), (0, 1), (1, 0), (1, 1)}
            assert len({row["identity"]["cell_id"] for row in pipeline}) == 4
            nested_rows = [
                row for row in started if row["identity"]["node_path"] == ["nested", "inner"]
            ]
            assert len(nested_rows) == 1
            completion_order = [
                row["identity"]["item_index"]
                for row in completed
                if row["identity"]["node_path"] == ["pipe"]
            ]
            assert completion_order[0] == fast_index

            queried = json.loads(WorkflowAuditTool(db).handle({"run_id": run_id, "limit": 100}))
            assert queried["ok"] is True
            assert {row["identity"]["run_id"] for row in queried["events"]} == {run_id}
    finally:
        service.shutdown()
        db.close()


def test_resume_in_fresh_service_replays_cache_without_fictitious_leaf(tmp_path: Path) -> None:
    path = str(tmp_path / "state.db")
    client = _PromptClient()
    spec = {
        "meta": {"name": "resume-audit"},
        "nodes": [
            {"id": "before", "type": "agent", "prompt": "answer before"},
            {"id": "approval", "type": "checkpoint", "prompt": "continue?"},
            {"id": "after", "type": "agent", "prompt": "answer after"},
        ],
    }

    first_db = SessionDB(path)
    first_service = _service(first_db, tmp_path, client, max_runs=1)
    started = first_service.start(spec)
    run_id = started["run_id"]
    first = first_service.status(run_id, wait=True, timeout=10)
    assert first["status"] == "paused"
    assert first["reason"] == "checkpoint"
    assert first_service._audit.flush(timeout=2)
    first_service.shutdown()
    first_db.close()

    second_db = SessionDB(path)
    second_service = _service(second_db, tmp_path, client, max_runs=1)
    try:
        resumed = second_service.start(
            resume_run_id=run_id,
            checkpoint_answers={"approval": "go"},
        )
        assert resumed["run_id"] == run_id
        final = second_service.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
        assert final["outputs"]["before"] == "before"
        assert final["outputs"]["after"] == "after"
        assert client.prompts == ["before", "after"]
        assert second_service._audit.flush(timeout=2)

        rows = _events(second_db, run_id)
        segments = [row for row in rows if row["event_type"] == "segment.started"]
        assert len(segments) == 2
        assert len({row["identity"]["segment_id"] for row in segments}) == 2
        replays = [row for row in rows if row["event_type"] == "cache.replayed"]
        assert any(row["identity"]["node_path"] == ["before"] for row in replays)
        assert all(row["identity"].get("sub_id") is None for row in replays)

        before_leaves = [
            row for row in rows
            if row["event_type"] == "leaf.started"
            and row["identity"]["node_path"] == ["before"]
        ]
        after_leaves = [
            row for row in rows
            if row["event_type"] == "leaf.started"
            and row["identity"]["node_path"] == ["after"]
        ]
        assert len(before_leaves) == len(after_leaves) == 1
        assert before_leaves[0]["identity"]["segment_id"] != after_leaves[0]["identity"][
            "segment_id"
        ]
    finally:
        second_service.shutdown()
        second_db.close()


class _AttemptClient(ModelClient):
    def __init__(self) -> None:
        self.counts = {"structured": 0, "empty": 0}

    def create(self, **kwargs: Any) -> dict[str, Any]:
        prompt = " ".join(
            str(message.get("content", ""))
            for message in (kwargs.get("messages") or [])
            if isinstance(message, dict)
        )
        kind = "structured" if "structured" in prompt else "empty"
        attempt = self.counts[kind]
        self.counts[kind] += 1
        if kind == "structured":
            return _text_response('{"value": "wrong"}' if attempt == 0 else '{"value": 7}')
        return _text_response("" if attempt == 0 else "retry-result")

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs: Any) -> dict[str, Any]:
        return self.create(**kwargs)


def test_validation_turns_and_fresh_retries_have_unmixed_attempt_identity(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "attempts.db"))
    client = _AttemptClient()
    service = _service(db, tmp_path, client, max_runs=1)
    spec = {
        "meta": {"name": "attempt-audit"},
        "nodes": [
            {
                "id": "structured",
                "type": "agent",
                "prompt": "answer structured",
                "schema": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                },
            },
            {"id": "empty", "type": "agent", "prompt": "answer empty", "retries": 1},
        ],
    }
    try:
        run_id = service.start(spec)["run_id"]
        result = service.status(run_id, wait=True, timeout=10)
        assert result["status"] == "complete"
        assert result["outputs"] == {"structured": {"value": 7}, "empty": "retry-result"}
        assert service._audit.flush(timeout=2)
        started = [
            row for row in _events(db, run_id) if row["event_type"] == "leaf.started"
        ]
        structured = [
            row for row in started if row["identity"]["node_path"] == ["structured"]
        ]
        assert len(structured) == 2
        assert len({row["identity"]["sub_id"] for row in structured}) == 1
        assert {(row["identity"]["attempt"], row["identity"]["turn"]) for row in structured} == {
            (0, 0), (1, 1)
        }
        assert len({row["identity"]["cell_id"] for row in structured}) == 1

        empty = [row for row in started if row["identity"]["node_path"] == ["empty"]]
        assert len(empty) == 2
        assert len({row["identity"]["sub_id"] for row in empty}) == 2
        assert {row["identity"]["attempt"] for row in empty} == {0, 1}
        assert {row["identity"]["turn"] for row in empty} == {0}
        assert len({row["identity"]["cell_id"] for row in empty}) == 1
    finally:
        service.shutdown()
        db.close()


class _TransportClient(ModelClient):
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: dict[str, int] = {}

    def create(self, **kwargs: Any) -> dict[str, Any]:
        prompt = repr(kwargs)
        key = "fail" if "force-failure" in prompt else "tool"
        count = self.calls.get(key, 0)
        self.calls[key] = count + 1
        if key == "fail":
            raise RuntimeError("PROVIDER-SECRET-CANARY")
        if count == 0:
            return _raw_tool_call(self.mode)
        return _raw_text(self.mode, "PRIVATE-RESULT-CANARY")

    def stream(self, *, on_text=None, on_reasoning=None, **kwargs: Any) -> dict[str, Any]:
        return self.create(**kwargs)


def _raw_tool_call(mode: str) -> dict[str, Any]:
    if mode == "anthropic_messages":
        return {
            "content": [{"type": "tool_use", "id": "call-1", "name": "probe", "input": {}}],
            "stop_reason": "tool_use",
        }
    if mode == "chat_completions":
        return {
            "choices": [{"message": {"content": None, "tool_calls": [{
                "id": "call-1", "function": {"name": "probe", "arguments": "{}"}
            }]}, "finish_reason": "tool_calls"}],
        }
    return {
        "status": "completed",
        "output": [{"type": "function_call", "call_id": "call-1", "name": "probe", "arguments": "{}"}],
    }


def _raw_text(mode: str, text: str) -> dict[str, Any]:
    if mode == "anthropic_messages":
        return {
            "content": [
                {"type": "thinking", "thinking": "PRIVATE-REASONING-CANARY"},
                {"type": "text", "text": text},
            ],
            "stop_reason": "end_turn",
        }
    if mode == "chat_completions":
        return {
            "choices": [{"message": {
                "content": text, "reasoning_content": "PRIVATE-REASONING-CANARY"
            }, "finish_reason": "stop"}],
        }
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "summary": [{"text": "PRIVATE-REASONING-CANARY"}],
             "encrypted_content": "PRIVATE-ENCRYPTED-CANARY"},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ],
    }


@pytest.mark.parametrize("mode", ["anthropic_messages", "chat_completions", "responses"])
def test_provider_transports_share_observed_lifecycle_and_exclude_private_payloads(
    mode: str, tmp_path: Path
) -> None:
    db = SessionDB(str(tmp_path / f"{mode}.db"))
    client = _TransportClient(mode)
    profile = ProviderProfile(name=f"test-{mode}", api_mode=mode, default_max_tokens=128)

    def build() -> Agent:
        def dispatch(_name: str, _args: dict[str, Any]) -> str:
            return "PRIVATE-TOOL-RESULT-CANARY"

        return Agent(
            model="audit-test",
            provider=profile,
            client=client,
            tool_dispatch=dispatch,
            tool_definitions=({"type": "function", "function": {"name": "probe"}},),
        )

    service = WorkflowService(base_child_factory=build, db=db, home=tmp_path, max_runs=1)
    spec = {
        "meta": {"name": f"provider-{mode}"},
        "nodes": [
            {"id": "tool", "type": "agent", "prompt": "use-tool"},
            {"id": "failure", "type": "agent", "prompt": "force-failure", "retries": 0},
        ],
    }
    try:
        run_id = service.start(spec)["run_id"]
        result = service.status(run_id, wait=True, timeout=10)
        assert result["status"] == "degraded"
        assert service._audit.flush(timeout=2)
        rows = _events(db, run_id)
        assert Counter(row["event_type"] for row in rows) == Counter({
            "segment.started": 1,
            "node.started": 2,
            "cache.missed": 2,
            "leaf.started": 2,
            "tool.started": 1,
            "tool.completed": 1,
            "leaf.completed": 1,
            "cache.stored": 1,
            "node.completed": 1,
            "leaf.failed": 1,
            "workflow.fault": 1,
            "node.failed": 1,
            "segment.completed": 1,
        })
        leaf_rows = [row for row in rows if row["event_type"].startswith("leaf.")]
        by_path = {
            path: [row for row in leaf_rows if row["identity"]["node_path"] == [path]]
            for path in ("tool", "failure")
        }
        assert [row["event_type"] for row in by_path["tool"]] == [
            "leaf.started", "leaf.completed"
        ]
        assert [row["event_type"] for row in by_path["failure"]] == [
            "leaf.started", "leaf.failed"
        ]
        assert all(len({row["identity"]["sub_id"] for row in pair}) == 1
                   for pair in by_path.values())
        tool_rows = [row for row in rows if row["event_type"].startswith("tool.")]
        assert [row["event_type"] for row in tool_rows] == ["tool.started", "tool.completed"]
        assert {row["identity"]["sub_id"] for row in tool_rows} == {
            by_path["tool"][0]["identity"]["sub_id"]
        }
        assert any(
            row["event_type"] == "tool.completed"
            and row["data"]["tool_name_state"] == "known_tool"
            and row["data"]["tool_name"] == {"state": "observed", "characters": 5}
            and row["data"]["arguments"]["state"] == "redacted"
            and row["data"]["result"]["state"] == "redacted"
            for row in rows
        )
        encoded = json.dumps(rows)
        assert "PRIVATE-" not in encoded
        assert "PROVIDER-SECRET-CANARY" not in encoded
    finally:
        service.shutdown()
        db.close()


def test_overflow_and_truncation_remain_visible_through_filtered_query(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    service = _service(db, tmp_path, _PromptClient(), max_runs=1)
    service._audit.shutdown()
    service._audit = AuditTrail(db, queue_limit=2, max_event_bytes=512)
    original_append = db.audit_append
    release = threading.Event()
    entered = threading.Event()

    def blocked_append(*args: Any, **kwargs: Any) -> int:
        entered.set()
        assert release.wait(5)
        return original_append(*args, **kwargs)

    db.audit_append = blocked_append  # type: ignore[method-assign]
    spec = {
        "meta": {"name": "loss-boundary"},
        "nodes": [{"id": "wide", "type": "parallel", "branches": [
            f"answer item-{index}" for index in range(16)
        ]}],
    }
    try:
        run_id = service.start(spec)["run_id"]
        assert entered.wait(2)
        result = service.status(run_id, wait=True, timeout=10)
        assert result["status"] == "complete"
        release.set()
        assert service._audit.flush(timeout=5)
        query = db.audit_query(run_id, event_type="leaf.completed", limit=100)
        assert query["integrity"]["event_markers"]["gaps"] >= 1
        assert any(
            notice["event_type"] == "audit.gap"
            and notice["data"]["reason"] == "queue_overflow"
            and notice["data"]["dropped_count"] >= 1
            for notice in query["integrity"]["notices"]
        )
    finally:
        release.set()
        db.audit_append = original_append  # type: ignore[method-assign]
        service.shutdown()

    trunc_db = SessionDB(str(tmp_path / "truncated.db"))
    trunc_client = _TransportClient("anthropic_messages")

    def trunc_factory() -> Agent:
        return Agent(
            model="audit-test",
            provider=get_provider_profile("anthropic"),
            client=trunc_client,
            tool_dispatch=lambda _name, _args: "PRIVATE-TOOL-RESULT-CANARY",
            tool_definitions=({"type": "function", "function": {"name": "probe"}},),
        )

    trunc_service = WorkflowService(
        base_child_factory=trunc_factory, db=trunc_db, home=tmp_path / "truncated", max_runs=1
    )
    trunc_service._audit.shutdown()
    trunc_service._audit = AuditTrail(trunc_db, max_event_bytes=512)
    try:
        run_id = trunc_service.start({
            "meta": {"name": "truncated"},
            "nodes": [{"id": "leaf", "type": "agent", "prompt": "use-tool"}],
        })["run_id"]
        assert trunc_service.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert trunc_service._audit.flush(timeout=2)
        query = trunc_db.audit_query(run_id, event_type="leaf.completed", limit=100)
        assert query["integrity"]["event_markers"]["truncated"] >= 1
        assert any(
            notice["event_type"] == "audit.truncated"
            for notice in query["integrity"]["notices"]
        )
    finally:
        trunc_service.shutdown()
        trunc_db.close()
