"""#130 semantic transition, known #115 stamp and NULL #75 control."""

import pytest

from lohra.state import SessionDB
from lohra.workflow.cache import content_hash
from lohra.workflow.cell_stamp import policy_fingerprint
from lohra.workflow.sandbox import WorkflowPolicy
from tests.test_workflow_cache_policy import _counting, _pause_at_gate, _replays, _service


def _pre130_hash():
    # Exact payload on #115 candidate 5ab87748, verified byte-identical in
    # production and equal in fingerprint on integrated claim base c4ca5e2.
    return content_hash({
        "allow_terminal": False, "allow_search": False,
        "egress_scope": "all_hops", "egress_dns": "pinned_public",
        "mcp_authority": "registered_entry_exact_server",
        "egress_allow": [], "fs_allow": [], "mcp_allow": [],
    })


def test_author_time_enforcement_changes_unchanged_nominal_policy_fingerprint():
    assert policy_fingerprint(WorkflowPolicy()) != _pre130_hash()


@pytest.mark.parametrize("legacy_null", [False, True], ids=["known-pre130", "unknown-null"])
def test_pre130_paid_cell_replay_is_advisory_and_never_respawned(tmp_path, legacy_null):
    path = tmp_path / "state.db"
    policy = WorkflowPolicy()
    database = SessionDB(str(path))
    responder, calls = _counting("PAID")
    first = _service(database, tmp_path, responder, policy=policy)
    try:
        try:
            run_id = _pause_at_gate(first)
            assert calls == [1]
        finally:
            first.shutdown()
        with database._connection as connection:
            result = connection.execute(
                "UPDATE workflow_node_cache SET policy_hash=? WHERE run_id=?",
                (None if legacy_null else _pre130_hash(), run_id),
            )
            assert result.rowcount == 1
    finally:
        database.close()

    reopened = SessionDB(str(path))
    responder, respawns = _counting("UNEXPECTED")
    second = _service(reopened, tmp_path, responder, policy=policy)
    try:
        assert "error" not in second.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        result = second.status(run_id, wait=True, timeout=10)
        assert result["status"] == "complete"
        assert result["outputs"]["draft"] == "PAID" and respawns == [0]
        reasons = [row["data"].get("reason") for row in _replays(reopened, run_id)]
        advisories = [fault for fault in result["faults"] if "replayed under a different" in fault]
        assert reasons == ([None] if legacy_null else ["policy_changed"])
        assert len(advisories) == (0 if legacy_null else 1)
        assert not advisories or advisories[0].startswith("draft: ")
    finally:
        second.shutdown()
        reopened.close()
