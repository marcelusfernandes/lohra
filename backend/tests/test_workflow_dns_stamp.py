"""A pre-pinning cell is replayed unchanged, with effective-policy provenance."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import registry
from lohra.web import tool as web_tool  # noqa: F401 - production registration
from lohra.workflow.cache import content_hash
from lohra.workflow.service import WorkflowService
from tests.test_workflow_sandbox_denials import ScriptedClient
from tests.web_socket_lab import PUBLIC, Lab


@pytest.mark.parametrize("unknown", [False, True])
def test_previous_dns_stamp_reopens_replays_and_new_acquisition_has_new_stamp(
    tmp_path, monkeypatch, unknown
):
    monkeypatch.delenv("LOHRA_LEAF_ALLOW_SEARCH", raising=False)
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    policy = {"egress_allow": ["public.test"]}
    (tmp_path / "workflow_policy.json").write_text(json.dumps(policy))
    spec = {
        "meta": {"name": "dns-stamp"},
        "nodes": [
            {"id": "reader", "type": "agent", "prompt": "fetch"},
        ],
    }
    spawned = []

    def factory():
        spawned.append(True)
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient([("web_fetch", {"url": "https://public.test"})]),
            tool_dispatch=registry.dispatch,
            tool_definitions=tuple(registry.get_definitions({"web"})),
        )

    path = tmp_path / "cache.db"
    db = SessionDB(path)
    first = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        with Lab(
            lambda *_: [PUBLIC], env={"LOHRA_HOME": str(tmp_path), "LOHRA_AUDIT": "on"}
        ) as lab:
            run_id = first.start(spec)["run_id"]
            assert first.status(run_id, wait=True, timeout=5)["outputs"] == {"reader": "done"}
            assert len(lab.connected) == 1
        # Exact #56/#9 stamp, immediately before #13; no invented historical version.
        old_effective = {
            "allow_terminal": False,
            "allow_search": False,
            "egress_scope": "all_hops",
            "egress_allow": ["public.test"],
            "fs_allow": [],
            "mcp_allow": [],
        }
        # Current semantics include #115; the historical payload below stays frozen.
        current_hash = content_hash({**old_effective, "egress_dns": "pinned_public",
                                     "mcp_authority": "registered_entry_exact_server"})
        row = db._connection.execute("SELECT policy_hash FROM workflow_node_cache").fetchone()
        assert row[0] == current_hash  # acquisition went through the actual owned fetcher
    finally:
        first.shutdown()
    historical = None if unknown else content_hash(old_effective)
    db._connection.execute("UPDATE workflow_node_cache SET policy_hash=?", (historical,))
    db._connection.commit()
    before = tuple(db._connection.execute("SELECT * FROM workflow_node_cache").fetchone())
    db.close()
    spawned.clear()
    db = SessionDB(path)
    second = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        with Lab(lambda *_: (_ for _ in ()).throw(AssertionError("replay resolved DNS"))) as lab:
            assert second.start(resume_run_id=run_id)["cache_preview"]["replay"] == 1
            result = second.status(run_id, wait=True, timeout=5)
            assert result["outputs"] == {"reader": "done"}
            assert spawned == lab.connected == []
        assert (
            tuple(db._connection.execute("SELECT * FROM workflow_node_cache").fetchone()) == before
        )
        assert len(result["advisory_faults"]) == int(not unknown)
        if not unknown:
            assert "harness semantics" in result["advisory_faults"][0]
            assert "nothing was recomputed" in result["advisory_faults"][0]
        assert second._audit.flush(timeout=5)
        replays = db.audit_query(run_id, event_type="cache.replayed")["events"]
        assert [row["data"].get("reason") for row in replays] == [
            None if unknown else "policy_changed"
        ]
    finally:
        second.shutdown()
        db.close()
