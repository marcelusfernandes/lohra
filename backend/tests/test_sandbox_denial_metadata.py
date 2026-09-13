"""Structural refusal metadata, retained audit counts and snapshot watermarks."""

import json
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from lohra.orchestration.core import OrchestrationCore
from lohra.tools.sandbox_denials import DenialCounts, denial_of
from lohra.workflow.audit import gateway_audit_event, sanitize_audit_event
from lohra.workflow.causality import CausalContext
from lohra.workflow.denials import DenialTally
from lohra.workflow.sandbox import FsRoot, WorkflowPolicy, make_sandboxed_leaf_factory, sandbox_dispatch
from tests.test_workflow_sandbox_denials import ScriptedClient, _factory, db  # noqa: F401


def _frame(result):
    return {"params": {"type": "tool.complete", "payload": {
        "name": "write_file", "args": {"path": "/CANARY"}, "result": result,
    }}}


def _event(result):
    return gateway_audit_event(
        _frame(result), CausalContext(run_id="run", segment_id="seg", node_path=("writer",),
                              cell_id="cell", role="agent"),
        sub_id="leaf",
    )


@pytest.mark.parametrize("name,tainted,reason", [
    ("write_file", False, "fs_outside_scope"),
    ("read_file", False, "fs_outside_scope"),
    ("write_file", False, "fs_read_only"),
    ("web_fetch", False, "egress_not_allowed"),
    ("terminal", False, "terminal_disabled"),
    ("mcp_CANARY_secret", False, "mcp_not_allowed"),
    ("write_file", True, "tainted_fs"),
    ("read_file", True, "tainted_fs"),
    ("web_fetch", True, "tainted_egress"),
    ("web_search", True, "tainted_egress"),
    ("terminal", True, "tainted_terminal"),
    ("mcp_CANARY_secret", True, "tainted_mcp"),
])
def test_every_sandbox_decision_keeps_json_string_and_closed_metadata(tmp_path, name, tainted, reason):
    def forbidden(*args):
        pytest.fail("denied call reached underlying dispatch")
    policy = WorkflowPolicy(fs_allow=(FsRoot(tmp_path / "CANARY", writable=False),))
    dispatch = sandbox_dispatch(forbidden, working_root=tmp_path / "work", policy=policy, tainted=tainted)
    path = tmp_path / "CANARY" / "file" if reason == "fs_read_only" else tmp_path / "outside"
    result = dispatch(name, {"path": str(path), "url": "https://CANARY.test"})
    assert isinstance(result, str) and "error" in json.loads(result)
    assert json.loads(json.dumps(result)) == str(result)
    assert deepcopy(result) == result and denial_of(deepcopy(result)) == denial_of(result)
    assert denial_of(str(result)) is None  # serialization cannot mint observations
    assert denial_of(result).reason == reason
    assert denial_of(result).tool == ("mcp" if name.startswith("mcp_") else name)
    event = _event(result)
    assert event["data"]["result"]["denied"] is True
    assert event["data"]["result"]["reason"] == reason
    assert "CANARY" not in json.dumps(event)
    safe = sanitize_audit_event(event)
    assert safe["data"] == event["data"]
    assert sanitize_audit_event(safe) == safe  # every write/read repeats it


def test_counts_ignore_other_frames_and_plain_results_and_return_independent_snapshots(tmp_path):
    dispatch = sandbox_dispatch(lambda *_: "ok", working_root=tmp_path,
                                policy=WorkflowPolicy(), tainted=False)
    result = dispatch("write_file", {"path": "/outside"})
    counts = DenialCounts()
    for frame in [{}, {"params": []}, {"params": {"type": "tool.start"}},
                  {"params": {"type": "tool.complete", "payload": []}}, _frame(str(result))]:
        counts.observe(frame)
    assert counts.snapshot() == []
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(counts.observe, [_frame(result)] * 200))
    assert counts.snapshot() == [{"tool": "write_file", "reason": "fs_outside_scope", "count": 200}]


def test_watermarks_deduplicate_concurrent_older_reads_but_keep_new_denials():
    tally = DenialTally()
    row = {"tool": "write_file", "reason": "fs_outside_scope", "count": 2}
    tally.fold("leaf-1", "writer", [row, row])
    tally.fold("leaf-1", "writer", [{**row, "count": 1}])
    tally.fold("leaf-2", "writer", [row])
    assert tally.drain()[0].startswith("writer: 4 tool calls denied by sandbox")
    tally.fold("leaf-1", "writer", [row])
    assert tally.drain() == []
    tally.fold("leaf-1", "writer", [{**row, "count": 3}])
    assert tally.drain()[0].startswith("writer: 1 tool calls denied by sandbox")
    for rows in [None, {}, [None], [{**row, "tool": "CANARY"}], [{**row, "reason": "CANARY"}],
                 [{**row, "count": True}], [{**row, "count": 0}], [{**row, "count": -1}]]:
        tally.fold("leaf-3", "writer", rows)
    assert tally.drain() == []


def test_leaf_counts_accumulate_across_steered_turns(db, tmp_path):  # noqa: F811
    class RepeatingClient(ScriptedClient):
        def create(self, **kwargs):
            self.turn %= 2
            return super().create(**kwargs)
    def factory():
        agent = _factory([("write_file", {"path": "/outside"})], [])()
        agent.client = RepeatingClient([("write_file", {"path": "/outside"})])
        return agent
    core = OrchestrationCore(db, make_sandboxed_leaf_factory(
        base_factory=factory, working_root=tmp_path, policy=WorkflowPolicy(), tainted=False,
    ))
    try:
        leaf = core.spawn("write")
        first = core.collect(leaf, wait=True, timeout=5)
        assert first["sandbox_denials"][0]["count"] == 1
        core.steer(leaf, "write again")
        second = core.collect(leaf, wait=True, timeout=5)
        assert second["sandbox_denials"][0]["count"] == 2
        assert first["sandbox_denials"][0]["count"] == 1
    finally:
        core.shutdown()


def test_audit_count_is_of_retained_snapshot_independent_of_paging_or_filters(db, tmp_path):  # noqa: F811
    dispatch = sandbox_dispatch(lambda *_: "ok", working_root=tmp_path,
                                policy=WorkflowPolicy(), tainted=False)
    refusal = _event(dispatch("write_file", {"path": "/outside"}))
    legacy = _event('{"denied": true, "error": "sandbox denied"}')
    assert "denied" not in legacy["data"]["result"]
    def append(event):
        db.audit_append(event, max_events=4, now=1000, max_runs=64, retention_seconds=86400)
    append(refusal)
    append(legacy)
    append(refusal)
    first = db.audit_query("run", limit=1)
    assert first["sandbox"] == {"scope": "retained_snapshot", "denied_tool_calls": 2}
    append(refusal)
    frozen = db.audit_query("run", after_seq=1, snapshot_seq=first["page"]["snapshot_seq"],
                            node_id="absent", event_type="leaf.started", limit=1)
    assert frozen["events"] == [] and frozen["sandbox"] == first["sandbox"]
    assert db.audit_query("run")["sandbox"]["denied_tool_calls"] == 3
    for _ in range(4):
        append(legacy)
    retained = db.audit_query("run")
    assert retained["sandbox"]["denied_tool_calls"] == 0
    assert retained["integrity"]["event_markers"]["gaps"] >= 1
    absent = db.audit_query("never-recorded")
    assert absent["sandbox"]["denied_tool_calls"] == 0 and absent["availability"] == "unavailable"
