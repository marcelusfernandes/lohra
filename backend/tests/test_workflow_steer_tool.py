"""workflow_steer: the tool layer must forward to WorkflowService.steer."""

import json
from types import SimpleNamespace

from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS
from lohra.tools import registry
from lohra.workflow.tools import WorkflowTool, register_workflow_tool_schemas


def _dispatch():
    register_workflow_tool_schemas()
    return registry.dispatch


def test_schema_requires_exact_occurrence_coordinates_and_intercepted_fallback():
    register_workflow_tool_schemas()
    defs = {d["function"]["name"]: d["function"] for d in registry.get_definitions()}
    fn = defs["workflow_steer"]
    assert fn["parameters"]["required"] == [
        "run_id",
        "sub_id",
        "segment_id",
        "attempt",
        "turn",
        "text",
    ]
    # The interception contract: no service bound -> explicit error, not a crash.
    assert "error" in json.loads(_dispatch()("workflow_steer", {}))


def test_description_states_occurrence_local_queue_and_limits():
    register_workflow_tool_schemas()
    fn = {d["function"]["name"]: d["function"] for d in registry.get_definitions()}
    desc = fn["workflow_steer"]["description"]
    # Exact live execution occurrence + local process.
    assert "LIVE EXECUTION OCCURRENCE" in desc
    assert "THIS process" in desc
    # Queued, not an interruption; delivery is best-effort between iterations.
    assert "QUEUED, never an interruption" in desc
    assert "DELIVERY is not guaranteed" in desc
    assert "between loop iterations" in desc
    # The operator's steering limits, spelled out.
    assert "1 external steer per leaf" in desc
    assert "3 per run" in desc
    assert "2 total corrections" in desc


def _tool_with_service(out):
    calls = []

    def steer(run_id, sub_id, text, *, segment_id, attempt, turn):
        calls.append((run_id, sub_id, segment_id, attempt, turn, text))
        return out

    tool = WorkflowTool(SimpleNamespace(steer=steer))
    return tool, calls


def test_tool_forwards_exact_occurrence_and_wraps_ok_result():
    tool, calls = _tool_with_service({"ok": True, "queued": False, "receipts": {}})
    args = {
        "run_id": "r1",
        "sub_id": "s1",
        "segment_id": "seg",
        "attempt": 2,
        "turn": 3,
        "text": "hello",
    }
    out = json.loads(tool.steer(args))
    assert calls == [("r1", "s1", "seg", 2, 3, "hello")]
    assert out["ok"] is True


def test_tool_wraps_service_error_as_tool_error():
    tool, calls = _tool_with_service({"error": "boom", "exhausted": True})
    args = {
        "run_id": "r1",
        "sub_id": "s1",
        "segment_id": "seg",
        "attempt": 0,
        "turn": 0,
        "text": "hello",
    }
    out = json.loads(tool.steer(args))
    assert calls == [("r1", "s1", "seg", 0, 0, "hello")]
    assert "error" in out


def test_tool_validates_before_touching_the_service():
    tool, calls = _tool_with_service({"ok": True})
    valid = {
        "run_id": "r1",
        "sub_id": "s1",
        "segment_id": "seg",
        "attempt": 0,
        "turn": 0,
        "text": "hello",
    }
    for bad in (
        {},
        {**valid, "segment_id": ""},
        {**valid, "attempt": -1},
        {**valid, "attempt": True},
        {**valid, "turn": -1},
        {**valid, "turn": True},
        {**valid, "text": ""},
        {**valid, "text": None},
    ):
        assert "error" in json.loads(tool.steer(bad)), bad
    assert calls == []  # never reached the service


def test_steer_excluded_from_subagents_like_pause_and_cancel():
    assert "workflow_steer" in _CHILD_EXCLUDED_TOOLS
