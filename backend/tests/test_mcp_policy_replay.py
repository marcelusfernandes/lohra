"""#115 semantic fingerprint and #75 advisory replay; SQLite/fake clients only."""

import pytest

from lohra.state import SessionDB
from lohra.workflow.cache import content_hash
from lohra.workflow.cell_stamp import policy_fingerprint
from lohra.workflow.sandbox import WorkflowPolicy
from tests.test_workflow_cache_policy import _counting, _pause_at_gate, _replays, _service


def _prefix_semantics_hash():
    # Exact pre-#115 payload for this unchanged nominal policy on 58b2584.
    # This freezes historical data; it does not monkeypatch a future gate.
    return content_hash({"allow_terminal": False, "allow_search": False,
        "egress_scope": "all_hops", "egress_dns": "pinned_public",
        "egress_allow": [], "fs_allow": [], "mcp_allow": ["github"]})


def test_unchanged_nominal_grant_has_a_new_effective_policy_fingerprint():
    assert policy_fingerprint(WorkflowPolicy(mcp_allow=("github",))) != _prefix_semantics_hash()


def test_original_server_identities_do_not_collapse_in_the_fingerprint():
    assert policy_fingerprint(WorkflowPolicy(mcp_allow=("gh-team",))) != policy_fingerprint(
        WorkflowPolicy(mcp_allow=("gh_team",)))


def test_reordering_and_deduplicating_exact_grants_is_not_a_policy_change():
    assert policy_fingerprint(WorkflowPolicy(mcp_allow=("GitHub", "gh-team", "GitHub"))) == (
        policy_fingerprint(WorkflowPolicy(mcp_allow=("gh-team", "GitHub"))))


@pytest.mark.parametrize("legacy_null", [False, True], ids=["old-prefix-stamp", "unknown-stamp"])
def test_old_policy_replay_preserves_paid_output_without_respawn(tmp_path, legacy_null):
    database = tmp_path / "state.db"
    policy = WorkflowPolicy(mcp_allow=("github",))
    db = SessionDB(str(database))
    responder, calls = _counting("PAID")
    first = _service(db, tmp_path, responder, policy=policy)
    try:
        try:
            run_id = _pause_at_gate(first)
            assert calls == [1]
        finally:
            first.shutdown()
        # Represent the historical cell's stored stamp, retaining its content
        # hash, output, cost, harness version and durable run/checkpoint state.
        with db._connection as conn:
            cursor = conn.execute("UPDATE workflow_node_cache SET policy_hash=? WHERE run_id=?",
                                  (None if legacy_null else _prefix_semantics_hash(), run_id))
            assert cursor.rowcount == 1
    finally:
        db.close()

    reopened = SessionDB(str(database))
    responder, respawns = _counting("UNEXPECTED")
    second = _service(reopened, tmp_path, responder, policy=policy)
    try:
        assert "error" not in second.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        result = second.status(run_id, wait=True, timeout=10)
        rows = _replays(reopened, run_id)
        assert result["status"] == "complete"
        assert result["outputs"]["draft"] == "PAID" and respawns == [0]
        advisories = [f for f in result["faults"] if "replayed under a different" in f]
        assert [row["data"].get("reason") for row in rows] == ([None] if legacy_null else ["policy_changed"])
        assert len(advisories) == (0 if legacy_null else 1)
        assert not advisories or advisories[0].startswith("draft: ")
    finally:
        second.shutdown()
        reopened.close()
