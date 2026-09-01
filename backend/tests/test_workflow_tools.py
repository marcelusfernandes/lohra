"""Tests for the workflow tool surface + WorkflowService (Fase 8, Milestone F)."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool, register_workflow_tool_schemas
from tests.test_loop import FakeClient, _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _child_factory(reply="ok"):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return factory


def _service(db, tmp_path, reply="ok"):
    return WorkflowService(base_child_factory=_child_factory(reply), db=db, home=tmp_path)


_SPEC = {
    "meta": {"name": "demo"},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "do ${args.task}"},
        {"id": "b", "type": "agent", "prompt": "use ${a}", "depends_on": ["a"]},
    ],
}


def test_run_then_status_completes(db, tmp_path):
    svc = _service(db, tmp_path, reply="DONE")
    try:
        tool = WorkflowTool(svc)
        started = json.loads(tool.run({"spec": _SPEC, "args": {"task": "x"}}))
        assert started["ok"] is True
        run_id = started["run_id"]
        final = json.loads(tool.status({"run_id": run_id, "wait": True}))
        assert final["status"] == "complete"
        assert final["outputs"]["a"] == "DONE"
        assert final["outputs"]["b"] == "DONE"
    finally:
        svc.shutdown()


def test_invalid_spec_is_rejected_before_run(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        out = json.loads(WorkflowTool(svc).run({"spec": {"meta": {}, "nodes": []}}))
        assert "error" in out  # didactic validation error, no run started
    finally:
        svc.shutdown()


def test_run_requires_spec_object(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        assert "error" in json.loads(WorkflowTool(svc).run({"spec": "not an object"}))
        assert "error" in json.loads(WorkflowTool(svc).run({}))
    finally:
        svc.shutdown()


def test_status_unknown_run_errors(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        assert "error" in json.loads(WorkflowTool(svc).status({"run_id": "ghost"}))
        assert "error" in json.loads(WorkflowTool(svc).cancel({"run_id": "ghost"}))
    finally:
        svc.shutdown()


def test_run_isolates_under_run_dir(db, tmp_path):
    svc = _service(db, tmp_path, reply="x")
    try:
        run_id = json.loads(WorkflowTool(svc).run({"spec": _SPEC, "args": {"task": "t"}}))["run_id"]
        json.loads(WorkflowTool(svc).status({"run_id": run_id, "wait": True}))
        # ``work-<fence>``: one scratch directory per ACQUISITION (issue #12),
        # so a stale owner's leaves cannot dirty the recovering owner's scratch.
        scratch = list((tmp_path / "runs" / run_id).iterdir())
        assert [path.name for path in scratch] == ["work-1"]
    finally:
        svc.shutdown()


def test_schemas_registered_and_intercepted_fallback():
    from lohra.tools import registry

    register_workflow_tool_schemas()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"run_workflow", "workflow_status", "workflow_cancel"} <= names
    assert "error" in json.loads(registry.dispatch("run_workflow", {"spec": {}}))


def test_triad_excluded_from_subagents_and_server():
    from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS

    assert {
        "run_workflow", "workflow_status", "workflow_cancel", "workflow_templates"
    } <= _CHILD_EXCLUDED_TOOLS


def test_templates_tool_lists_after_a_clean_run(db, tmp_path):
    svc = _service(db, tmp_path, reply="DONE")
    try:
        tool = WorkflowTool(svc)
        assert json.loads(tool.templates({}))["templates"] == []  # none yet
        run_id = json.loads(tool.run({"spec": _SPEC, "args": {"task": "x"}}))["run_id"]
        json.loads(tool.status({"run_id": run_id, "wait": True}))
        listed = json.loads(tool.templates({}))["templates"]  # clean run -> template (§12.3)
        assert [t["name"] for t in listed] == ["demo"]
        fetched = json.loads(tool.templates({"name": "demo"}))
        assert fetched["spec"]["meta"]["name"] == "demo"
    finally:
        svc.shutdown()


def test_templates_tool_unknown_name_errors(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        assert "error" in json.loads(WorkflowTool(svc).templates({"name": "ghost"}))
    finally:
        svc.shutdown()


def test_nested_workflow_runs_a_template(db, tmp_path):
    # A parent workflow whose node refs a saved template runs it inline (H).
    svc = _service(db, tmp_path, reply="NESTED")
    try:
        tool = WorkflowTool(svc)
        # 1) run a clean leaf workflow -> saved as template "demo"
        rid = json.loads(tool.run({"spec": _SPEC, "args": {"task": "x"}}))["run_id"]
        json.loads(tool.status({"run_id": rid, "wait": True}))
        assert "demo" in [t["name"] for t in svc.list_templates()]
        # 2) a parent with a `workflow` node referencing "demo" runs it nested
        parent = {"meta": {"name": "parent"},
                  "nodes": [{"id": "sub", "type": "workflow", "ref": "demo", "args": {"task": "y"}}]}
        prid = json.loads(tool.run({"spec": parent}))["run_id"]
        final = json.loads(tool.status({"run_id": prid, "wait": True}))
        assert final["status"] == "complete"
        # the nested workflow's outputs are returned under the `workflow` node
        assert final["outputs"]["sub"]["a"] == "NESTED"
    finally:
        svc.shutdown()


# --- sandbox is on the REAL run path (security claim → fact) ---


def _secret_reading_factory(called):
    """A leaf that emits a read_file(~/.lohra/.env) tool call; base records reach."""
    from pathlib import Path

    from lohra.tools.registry import tool_result
    from tests.test_loop import _text_response, _tool_call_response

    def base_dispatch(name, args):
        called["base"] = True
        return tool_result(contents="SECRET")

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(
                [
                    _tool_call_response([("c1", "read_file", {"path": str(Path.home() / ".lohra" / ".env")})]),
                    _text_response("done"),
                ]
            ),
            tool_dispatch=base_dispatch,
        )

    return factory


def test_sandbox_denies_secret_read_on_the_real_run_path(db, tmp_path):
    called = {"base": False}
    svc = WorkflowService(base_child_factory=_secret_reading_factory(called), db=db, home=tmp_path)
    try:
        spec = {"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "read it"}]}
        run_id = json.loads(WorkflowTool(svc).run({"spec": spec}))["run_id"]
        json.loads(WorkflowTool(svc).status({"run_id": run_id, "wait": True}))
        # the sandbox denied read_file(~/.lohra/.env) BEFORE it reached the base dispatch
        assert called["base"] is False
    finally:
        svc.shutdown()


def test_sandbox_discriminator_without_sandbox_the_read_reaches_base(db):
    # Same leaf on a PLAIN (unsandboxed) core+engine — the read DOES reach base.
    # Proves the test above actually discriminates (would fail if sandbox vanished).
    from lohra.orchestration.core import OrchestrationCore
    from lohra.workflow.budget import Budget
    from lohra.workflow.engine import WorkflowEngine
    from lohra.workflow.schema import validate_spec

    called = {"base": False}
    core = OrchestrationCore(db, _secret_reading_factory(called))  # NOT sandboxed
    try:
        spec = validate_spec({"meta": {"name": "x"},
                              "nodes": [{"id": "a", "type": "agent", "prompt": "read it"}]})
        WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert called["base"] is True  # no sandbox -> the read reached base
    finally:
        core.shutdown()


# --- terminal/MCP containment on the REAL run path (issue #4 / F01-A) ---


def _terminal_factory(called, command="cat ~/.lohra/.env"):
    """A leaf that emits a `terminal` tool call; base records whether it ran."""
    from lohra.tools.registry import tool_result
    from tests.test_loop import _tool_call_response

    def base_dispatch(name, args):
        called["base"] = True
        return tool_result(output="SECRET")

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(
                [
                    _tool_call_response([("c1", "terminal", {"command": command})]),
                    _text_response("done"),
                ]
            ),
            tool_dispatch=base_dispatch,
        )

    return factory


def _run_one(svc, tainted=False):
    spec = {"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "run it"}]}
    try:
        run_id = svc.start(spec, {}, tainted=tainted)["run_id"]
        svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()


def test_sandbox_denies_terminal_on_the_real_run_path(db, tmp_path):
    # No workflow_policy.json -> deny-by-default: the shell never runs.
    called = {"base": False}
    _run_one(WorkflowService(base_child_factory=_terminal_factory(called), db=db, home=tmp_path))
    assert called["base"] is False


def test_operator_policy_file_opts_terminal_in_on_the_real_run_path(db, tmp_path):
    # Discriminator through the OPERATOR path: JSON -> load_policy -> service -> leaf.
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"allow_terminal": True}))
    called = {"base": False}
    _run_one(WorkflowService(base_child_factory=_terminal_factory(called), db=db, home=tmp_path))
    assert called["base"] is True  # opt-in reaches base -> the deny above discriminates


def test_tainted_run_denies_terminal_even_with_operator_opt_in(db, tmp_path):
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"allow_terminal": True}))
    called = {"base": False}
    _run_one(
        WorkflowService(base_child_factory=_terminal_factory(called), db=db, home=tmp_path),
        tainted=True,
    )
    assert called["base"] is False  # taint has no override
