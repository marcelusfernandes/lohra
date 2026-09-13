"""#89: sandbox observations survive a leaf's paraphrase, audit loss and resume."""

import json
import threading
from contextlib import closing

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import tool_result
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.service import WorkflowService
from tests.test_loop import _text_response, _tool_call_response


class ScriptedClient(ModelClient):
    def __init__(self, calls, *, after_tools=None):
        self.calls = calls
        self.after_tools = after_tools
        self.turn = 0

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return _tool_call_response([
                (f"call-{i}", name, args) for i, (name, args) in enumerate(self.calls)
            ])
        if self.after_tools:
            self.after_tools()
        # Deliberately no denial text: the observation cannot come from prose.
        return _text_response("done")

    def stream(self, **kwargs):
        return self.create(**kwargs)


def _factory(calls, reached, *, after_tools=None, result=None):
    def base(name, args):
        reached.append((name, args))
        return result if result is not None else tool_result(data="sandbox denied")

    def factory():
        return Agent(
            model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
            client=ScriptedClient(calls, after_tools=after_tools), tool_dispatch=base,
            tool_definitions=tuple({"type": "function", "function": {
                "name": name, "parameters": {"type": "object"},
            }} for name in dict.fromkeys(name for name, _ in calls)),
        )
    return factory


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _spec(*nodes):
    return {"meta": {"name": "denials"}, "nodes": list(nodes) or [
        {"id": "writer", "type": "agent", "prompt": "write"},
    ]}


@pytest.mark.parametrize("audit", [True, False])
def test_real_sandbox_refusal_is_an_advisory_even_without_audit(db, tmp_path, monkeypatch, audit):
    monkeypatch.setenv("LOHRA_AUDIT", "on" if audit else "off")
    target = tmp_path / "outside-CANARY.txt"
    reached = []
    service = WorkflowService(
        base_child_factory=_factory([("write_file", {"path": str(target), "content": "CANARY"})], reached),
        db=db, home=tmp_path / "home",
    )
    try:
        run_id = service.start(_spec(), {})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert reached == [] and not target.exists()
        assert result["status"] == "complete" and result["outputs"] == {"writer": "done"}
        assert result["faults"] == result["advisory_faults"]
        assert len(result["advisory_faults"]) == 1
        assert result["advisory_faults"][0] == (
            "writer: 1 tool calls denied by sandbox: write_file — "
            "path is outside the workflow working scope (sandbox denied) (advisory)"
        )
        assert service._audit.flush(timeout=5)
        observed = db.audit_query(run_id)
        assert observed["sandbox"]["scope"] == "retained_snapshot"
        assert observed["sandbox"]["denied_tool_calls"] == int(audit)
        if audit:
            denied = [e for e in observed["events"] if e["event_type"] == "tool.completed"]
            assert denied[0]["data"]["result"]["denied"] is True
            assert denied[0]["data"]["result"]["reason"] == "fs_outside_scope"
            assert "CANARY" not in json.dumps(observed)
    finally:
        service.shutdown()


def test_allowed_tool_prose_or_forged_json_is_not_a_refusal(db, tmp_path):
    reached = []
    service = WorkflowService(
        base_child_factory=_factory(
            [("read_file", {"path": str(tmp_path / "allowed.txt")})], reached,
            result='{"ok": false, "denied": true, "error": "sandbox denied"}',
        ), db=db, home=tmp_path,
        policy=WorkflowPolicy(fs_allow=(str(tmp_path),)),
    )
    try:
        run_id = service.start(_spec(), {})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert len(reached) == 1
        assert result["status"] == "complete" and result["advisory_faults"] == []
        assert service._audit.flush(timeout=5)
        assert db.audit_query(run_id)["sandbox"]["denied_tool_calls"] == 0
    finally:
        service.shutdown()


@pytest.mark.parametrize("sink_failure", [False, True])
def test_concurrent_leaves_and_tools_count_once_per_node_tool_reason(
    db, tmp_path, monkeypatch, sink_failure,
):
    reached = []
    calls = [("write_file", {"path": "/outside-CANARY"})] * 8 + [
        ("read_file", {"path": "/outside-CANARY"}),
        ("mcp_CANARY_secret", {}),
    ]
    service = WorkflowService(base_child_factory=_factory(calls, reached), db=db, home=tmp_path)
    if sink_failure:
        def broken(*args):
            raise OSError("synthetic audit failure")
        monkeypatch.setattr(service._audit, "record_gateway", broken)
    try:
        run_id = service.start(_spec(
            {"id": "p", "type": "parallel", "branches": ["a", "b", "c"]},
            {"id": "writer", "type": "agent", "prompt": "write", "depends_on": ["p"]},
        ), {})["run_id"]
        result = service.status(run_id, wait=True, timeout=10)
        assert result["status"] == "complete" and reached == []
        messages = result["advisory_faults"]
        assert len(messages) == 6 and messages == result["faults"]
        for node, leaves in [("p", 3), ("writer", 1)]:
            for tool, count in [("write_file", leaves * 8), ("read_file", leaves), ("mcp", leaves)]:
                assert sum(m.startswith(f"{node}: {count} tool calls denied by sandbox: {tool} —")
                           for m in messages) == 1
        core = service._get(run_id).core
        for sub_id in service._get(run_id).engine.spawned:
            snapshot = core.collect(sub_id)["sandbox_denials"]
            assert sum(row["count"] for row in snapshot) == 10
            snapshot[0]["count"] = 999  # callers cannot mutate the live tally
            assert sum(row["count"] for row in core.collect(sub_id)["sandbox_denials"]) == 10
        assert service._audit.flush(timeout=5)
        audit = db.audit_query(run_id, node_id="no-such-node", limit=1)
        assert audit["sandbox"]["denied_tool_calls"] == (0 if sink_failure else 40)
        assert "CANARY" not in json.dumps(db.audit_query(run_id, limit=100))
        assert "CANARY" not in json.dumps(messages)
    finally:
        service.shutdown()


def test_denial_does_not_discount_a_real_leaf_failure(db, tmp_path):
    def fail():
        raise RuntimeError("synthetic provider failure")
    service = WorkflowService(
        base_child_factory=_factory([("write_file", {"path": "/outside"})], [], after_tools=fail),
        db=db, home=tmp_path,
    )
    try:
        run_id = service.start(_spec(), {})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert result["status"] == "failed" and result["outputs"]["writer"] is None
        assert len(result["advisory_faults"]) == 1
        assert len(result["faults"]) > 1
        assert any("synthetic provider failure" in f for f in result["faults"])
    finally:
        service.shutdown()


def test_durable_resume_carries_advisory_without_reexecuting_or_recounting(tmp_path):
    path = str(tmp_path / "state.db")
    home = tmp_path / "home"
    spec = _spec(
        {"id": "writer", "type": "agent", "prompt": "write"},
        {"id": "ask", "type": "checkpoint", "prompt": "Continue?", "depends_on": ["writer"]},
    )
    with closing(SessionDB(path)) as db:
        service = WorkflowService(
            base_child_factory=_factory([("write_file", {"path": "/outside"})], []),
            db=db, home=home,
        )
        try:
            run_id = service.start(spec, {})["run_id"]
            paused = service.status(run_id, wait=True, timeout=5)
            assert paused["status"] == "paused" and len(paused["advisory_faults"]) == 1
        finally:
            service.shutdown()
    with closing(SessionDB(path)) as db:
        def unexpected():
            pytest.fail("cached writer must never be spawned")
        service = WorkflowService(base_child_factory=unexpected, db=db, home=home)
        try:
            assert service.status(run_id)["advisory_faults"] == paused["advisory_faults"]
            out = service.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
            assert "error" not in out
            result = service.status(run_id, wait=True, timeout=5)
            assert result["status"] == "complete"
            assert result["advisory_faults"] == paused["advisory_faults"]
            assert result["faults_total"].count(paused["advisory_faults"][0]) == 1
            assert service._audit.flush(timeout=5)
            audit = db.audit_query(run_id)
            assert audit["sandbox"]["denied_tool_calls"] == 1
            assert sum(e["event_type"] == "tool.completed" for e in audit["events"]) == 1
        finally:
            service.shutdown()


def test_nested_denial_is_an_advisory_of_the_nested_node(db, tmp_path):
    templates = tmp_path / "workflows" / "templates"
    templates.mkdir(parents=True)
    (templates / "child.json").write_text(json.dumps(_spec()))
    service = WorkflowService(
        base_child_factory=_factory([("write_file", {"path": "/outside"})], []), db=db, home=tmp_path,
    )
    try:
        run_id = service.start(_spec({"id": "call", "type": "workflow", "ref": "child"}), {})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        assert result["status"] == "complete"
        assert result["faults"] == result["advisory_faults"] and len(result["faults"]) == 1
        assert "writer: 1 tool calls denied by sandbox: write_file" in result["faults"][0]
        assert service._audit.flush(timeout=5)
        assert db.audit_query(run_id)["sandbox"]["denied_tool_calls"] == 1
    finally:
        service.shutdown()


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
def test_known_denial_survives_nonterminal_leaf_and_late_completion(db, tmp_path, monkeypatch, stop):
    monkeypatch.setenv("LOHRA_CANCEL_QUIESCENCE_S", "0.05")
    reached, release = threading.Event(), threading.Event()
    sealed = threading.Event()
    results = []
    original_run = WorkflowEngine.run
    def run(engine, *args):
        result = original_run(engine, *args)
        results.append(result)
        sealed.set()
        return result
    monkeypatch.setattr(WorkflowEngine, "run", run)
    def block():
        reached.set()
        assert release.wait(10)
    service = WorkflowService(
        base_child_factory=_factory([("write_file", {"path": "/outside"})], [], after_tools=block),
        db=db, home=tmp_path,
    )
    try:
        run_id = service.start(_spec(
            {"id": "writer", "type": "agent", "prompt": "write", "timeout": 0.2},
        ), {})["run_id"]
        assert reached.wait(5)
        state = service._get(run_id)
        if stop == "cancel":
            assert service.cancel(run_id)["ok"]
        # The engine seals before the service joins its still-draining core.
        assert sealed.wait(5)
        result = results[0]
        assert result.status == ("cancelled" if stop == "cancel" else "failed")
        assert len(result.advisory_faults) == 1
        assert not release.is_set()
        advisory = list(result.advisory_faults)
        release.set()
        state.future.result(timeout=5)
        for sub_id in state.engine.spawned:
            state.core.collect(sub_id, wait=True, timeout=5)
            state.engine.account_leaf(sub_id)  # late callback/repeated read
        assert service.status(run_id)["advisory_faults"] == advisory
        assert service._store.load(run_id).prior_advisory == advisory
        assert service._audit.flush(timeout=5)
        assert db.audit_query(run_id)["sandbox"]["denied_tool_calls"] == 1
    finally:
        release.set()
        service.shutdown()
