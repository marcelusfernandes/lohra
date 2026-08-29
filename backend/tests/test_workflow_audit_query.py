"""OBS-04: bounded, honest, provider-free audit queries."""

from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
from typing import Any

from lohra.state import SessionDB
from lohra.workflow.audit_query import WorkflowAuditTool


def _event(
    run_id: str,
    seq: int,
    *,
    event_type: str = "leaf.started",
    node: str = "node-a",
    sub_id: str | None = "sub-a",
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event_type": event_type,
        "provenance": "observed",
        "identity": {
            "run_id": run_id,
            "segment_id": "segment-a",
            "node_path": [node],
            "sub_id": sub_id,
            "attempt": 0,
            "turn": seq,
        },
        "data": data or {"state": "running"},
    }


def _append(db: SessionDB, event: dict[str, Any], now: float) -> int:
    return db.audit_append(
        event,
        now=now,
        max_events=2048,
        max_runs=64,
        retention_seconds=30 * 86400,
    )


def test_query_pages_a_stable_snapshot_and_filters_without_hiding_integrity(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        _append(db, _event("run-1", 0, data={"arguments": {"state": "redacted"}}), 1000)
        _append(db, _event("run-1", 1, event_type="audit.gap", node="node-b",
                           data={"reason": "queue_overflow", "dropped_count": 3}), 1001)
        _append(db, _event("run-1", 2, event_type="leaf.completed"), 1002)

        first = db.audit_query("run-1", node_id="node-a", limit=1)
        assert first["availability"] == "available"
        assert [event["seq"] for event in first["events"]] == [1]
        assert first["events"][0]["created_at"] == 1000
        assert first["page"] == {
            "after_seq": 0,
            "next_after_seq": 1,
            "snapshot_seq": 3,
            "limit_requested": 1,
            "limit_effective": 1,
            "limit_clamped": False,
            "returned": 1,
            "has_more": True,
        }
        # The node filter excludes the gap from events, never from disclosure.
        assert first["integrity"]["event_markers"]["gaps"] == 1
        assert first["integrity"]["field_markers"]["redacted"] == 1
        assert first["integrity"]["notices"][0]["event_type"] == "audit.gap"
        assert first["policy"]["raw_payloads"] == "redacted_or_excluded_at_ingest_and_read"
        assert first["policy"]["private_reasoning"] == "excluded_private_state"

        # A stable snapshot excludes a concurrent tail appended after page one.
        _append(db, _event("run-1", 3, event_type="leaf.completed"), 1003)
        second = db.audit_query(
            "run-1", node_id="node-a", after_seq=1, snapshot_seq=3, limit=10
        )
        assert [event["seq"] for event in second["events"]] == [3]
        assert second["page"]["snapshot_seq"] == 3
        assert second["page"]["has_more"] is False

        tail = db.audit_query("run-1", node_id="node-a", after_seq=3, limit=10)
        assert [event["seq"] for event in tail["events"]] == [4]
    finally:
        db.close()


def test_query_clamps_output_and_reports_pagination_truncation(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        for index in range(105):
            _append(db, _event("bounded", index), 1000 + index)
        result = db.audit_query("bounded", limit=999)
        assert len(result["events"]) == 100
        assert result["page"]["limit_requested"] == 999
        assert result["page"]["limit_effective"] == 100
        assert result["page"]["limit_clamped"] is True
        assert result["page"]["has_more"] is True
        assert result["integrity"]["pagination_truncated"] is True
    finally:
        db.close()


def test_unknown_or_expired_audit_is_explicitly_unavailable(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        unknown = db.audit_query("never-recorded")
        assert unknown["availability"] == "unavailable"
        assert unknown["integrity"]["notices"] == [
            {"event_type": "audit.unavailable", "provenance": "unavailable",
             "data": {"reason": "not_recorded"}}
        ]
    finally:
        db.close()


def _query_in_process(path: str, queue: Any) -> None:
    db = SessionDB(path)
    try:
        queue.put(db.audit_query("cross-process", limit=10))
    finally:
        db.close()


def test_query_reads_during_and_after_a_run_from_another_process(tmp_path: Path) -> None:
    path = str(tmp_path / "state.db")
    writer = SessionDB(path)
    try:
        _append(writer, _event("cross-process", 0, event_type="segment.started"), 1000)
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        child = context.Process(target=_query_in_process, args=(path, queue))
        child.start()
        child.join(10)
        assert child.exitcode == 0
        during = queue.get(timeout=2)
        assert during["availability"] == "available"
        assert [item["event_type"] for item in during["events"]] == ["segment.started"]

        _append(writer, _event("cross-process", 1, event_type="segment.completed"), 1001)
    finally:
        writer.close()

    reopened = SessionDB(path)
    try:
        after = reopened.audit_query("cross-process")
        assert [item["event_type"] for item in after["events"]] == [
            "segment.started", "segment.completed"
        ]
    finally:
        reopened.close()


def test_agent_tool_is_a_db_only_reader_with_filters_and_validation(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        _append(db, _event("tool-run", 0), 1000)
        tool = WorkflowAuditTool(db)
        assert not hasattr(tool, "_service")
        result = json.loads(tool.handle({"run_id": "tool-run", "event_type": "leaf.started"}))
        assert result["ok"] is True
        assert result["events"][0]["event_type"] == "leaf.started"
        assert "error" in json.loads(tool.handle({"run_id": "tool-run", "after_seq": -1}))
        assert "error" in json.loads(tool.handle({"run_id": "tool-run", "limit": "many"}))
    finally:
        db.close()


def test_tool_schema_is_registered_bound_and_excluded_from_children(tmp_path: Path) -> None:
    from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS
    from lohra.agent.equip import build_session_dispatch, register_all_tools
    from lohra.memory.store import MemoryStore
    from lohra.skills.store import SkillStore
    from lohra.tools import registry

    db = SessionDB(str(tmp_path / "state.db"))
    try:
        _append(db, _event("bound-run", 0), 1000)
        register_all_tools()
        names = {item["function"]["name"] for item in registry.get_definitions()}
        assert "workflow_audit" in names
        assert "workflow_audit" in _CHILD_EXCLUDED_TOOLS
        dispatch = build_session_dispatch(MemoryStore(tmp_path), SkillStore(tmp_path), db)
        result = json.loads(dispatch("workflow_audit", {"run_id": "bound-run"}))
        assert result["events"][0]["seq"] == 1
    finally:
        db.close()


def test_cli_audit_uses_the_same_read_model_and_parser(monkeypatch, tmp_path: Path, capsys) -> None:
    from lohra import cli
    from lohra.agent import client as client_module
    from lohra.memory.paths import state_db_path

    def reject_provider(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("audit query must not construct a provider client")

    monkeypatch.setattr(client_module, "build_client", reject_provider)
    monkeypatch.setenv("OPENROUTER_API_KEY", "SECRET-TOKEN-CANARY")
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    db = SessionDB(str(state_db_path()))
    try:
        _append(db, _event("cli-run", 0), 1000)
        expected = db.audit_query("cli-run", node_id="node-a", limit=7)
    finally:
        db.close()

    args = cli.build_parser().parse_args(
        ["workflow", "audit", "cli-run", "--node", "node-a", "--limit", "7"]
    )
    assert args.workflow_cmd == "audit" and args.node_id == "node-a"
    assert cli.run_workflow_cmd("audit", run_id="cli-run", node_id="node-a", limit=7) == 0
    output = capsys.readouterr().out
    assert "SECRET-TOKEN-CANARY" not in output
    assert json.loads(output) == expected


def test_query_re_sanitizes_valid_but_unsafe_or_corrupt_ledger_rows(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        _append(db, _event("defense", 0), 1000)
        canary = "RAW-PRIVATE-CANARY"
        unsafe = {
            "schema_version": 1,
            "event_type": "leaf.completed",
            "provenance": "observed",
            "identity": {"run_id": "defense", "node_path": ["node-a"]},
            "data": {"content": canary, "private_state": canary},
        }
        with db._audit_lock:  # simulate a legacy/tampered row below the writer boundary
            db._audit_connection.execute(
                "UPDATE workflow_audit_events SET payload_json = ? WHERE run_id = ? AND seq = 1",
                (json.dumps(unsafe), "defense"),
            )
            db._audit_connection.commit()
        result = db.audit_query("defense")
        assert canary not in json.dumps(result)
        assert result["integrity"]["field_markers"]["excluded_by_policy"] >= 1

        with db._audit_lock:
            db._audit_connection.execute(
                "UPDATE workflow_audit_events SET payload_json = '{broken' "
                "WHERE run_id = 'defense' AND seq = 1"
            )
            db._audit_connection.commit()
        corrupt = db.audit_query("defense")
        assert corrupt["events"][0]["event_type"] == "audit.unavailable"
        assert corrupt["integrity"]["event_markers"]["unavailable"] == 1
        assert corrupt["integrity"]["notices"][0]["data"]["reason"] == "corrupt_payload"
    finally:
        db.close()


def test_query_reports_retention_and_event_truncation_independently_of_filters(tmp_path: Path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        _append(db, _event("lossy", 0, node="old"), 1000)
        _append(db, _event("lossy", 1, event_type="audit.truncated", node="other",
                           data={"state": "truncated", "original_bytes": 4000,
                                 "limit_bytes": 2048,
                                 "original_event_type": "leaf.completed"}), 1001)
        _append(db, _event("lossy", 2, node="wanted"), 1002)
        with db._audit_lock:
            db._audit_connection.execute(
                "UPDATE workflow_audit_state SET retention_dropped = 4, "
                "dropped_before_seq = 1 WHERE run_id = 'lossy'"
            )
            db._audit_connection.commit()
        result = db.audit_query("lossy", node_id="wanted")
        assert [event["identity"]["node_path"][-1] for event in result["events"]] == ["wanted"]
        assert result["integrity"]["event_markers"] == {
            "gaps": 1, "truncated": 1, "unavailable": 0
        }
        assert {notice["event_type"] for notice in result["integrity"]["notices"]} == {
            "audit.gap", "audit.truncated"
        }
    finally:
        db.close()
