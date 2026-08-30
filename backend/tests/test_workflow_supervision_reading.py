"""SUP-02 anti-drift contract: reading workflow status without lying about cost.

Pins the permanent minimum of the supervision-reading doctrine:

- every successful ``WorkflowService.status`` read carries an ``observation``
  block declaring PROVENANCE (``local_registry`` vs ``durable_store``) and the
  two cost facts: the local read makes **no provider call** and its JSON payload
  is ``not_separately_attributed`` — the supervisor's context spend shows up in
  the containing turn's aggregate while ``workflow_token_ledger_delta`` stays 0
  (nothing is charged to the workflow run);
- the ``no workflow run`` error path carries no observation;
- the tool descriptions never overclaim: ``workflow_audit`` makes zero provider
  calls but its returned JSON still spends supervisor context;
- ``RUN_GUIDANCE`` and the workflow-authoring skill keep the reading contract:
  status first, a CONDITIONAL terminal notification that does not wake/start a
  turn, a fixed internal wait timeout rather than a caller-selected deadline, no
  fixed blind polling, audit only on leaf-level need, and the hard limit that NO read —
  status, audit or silence — can tell "slow" from "wedged": silence only
  updates last observed state, it is unknown, never idle;
- the envelope a successful ``WorkflowTool.status`` returns is pinned end to
  end, and the ``observation`` block is semantically anti-contradicted (the old
  ``live_engine``/``durable_snapshot``/``not_metered`` names must not return).

Deliberately NOT pinned: the rollup's existing fields (other tests own those)
or full prose — short stable fragments only.
"""

import json
from pathlib import Path

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.skills.store import SkillStore, builtin_root
from lohra.state import SessionDB
from lohra.workflow import tools as wt
from lohra.workflow.audit_query import AUDIT_QUERY_SCHEMA
from lohra.workflow.rollup import observation
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

SKILL_NAME = "workflow-authoring"

STATUS_DESC = wt._STATUS_SCHEMA["description"]
AUDIT_DESC = AUDIT_QUERY_SCHEMA["description"]

_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}

# The block shape every successful status read carries: two observation sources
# and the two honest cost facts (no provider call; the JSON lands in the
# supervisor's context but is not attributed to the workflow run).
EXPECTED_OBSERVATION = {
    "source": "local_registry",
    "provider_calls": "none",
    "supervisor_context_tokens": "not_separately_attributed",
    "workflow_token_ledger_delta": 0,
}


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


@pytest.fixture
def skill_body() -> str:
    skill = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),)).get(SKILL_NAME)
    assert skill is not None
    return skill.body


# --- the observation helper ---------------------------------------------------


def test_observation_block_declares_provenance_and_both_costs():
    assert observation("local_registry") == {
        "source": "local_registry",
        "provider_calls": "none",
        "supervisor_context_tokens": "not_separately_attributed",
        "workflow_token_ledger_delta": 0,
    }
    assert observation("durable_store")["source"] == "durable_store"


def test_observation_helper_knows_exactly_two_sources():
    with pytest.raises(ValueError):
        observation("cache_replay")  # a replay is not an observation source
    # The renamed-away names must not come back — they promised a live
    # engine/snapshot split the read path does not actually guarantee.
    for misleading in ("live_engine", "durable_snapshot"):
        with pytest.raises(ValueError):
            observation(misleading)


# --- service.status: local_registry path --------------------------------------


def _service(db, home, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def test_status_of_a_run_this_process_launched_says_local_registry(tmp_path):
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert "error" not in out
        assert out["observation"]["source"] == "local_registry"
        assert out["observation"]["provider_calls"] == "none"
        assert out["observation"]["supervisor_context_tokens"] == "not_separately_attributed"
        assert out["observation"]["workflow_token_ledger_delta"] == 0
    finally:
        svc.shutdown()
        db.close()


def test_status_observation_is_a_fresh_copy_not_shared_state(tmp_path):
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)["observation"]
        second = svc.status(run_id, wait=True, timeout=10)["observation"]
        assert first == second and first is not second
        first["source"] = "tampered"
        assert svc.status(run_id, wait=True, timeout=10)["observation"]["source"] != "tampered"
    finally:
        svc.shutdown()
        db.close()


# --- service.status: durable_store path ---------------------------------------


def test_status_of_a_run_only_the_line_knows_says_durable_store(tmp_path):
    """A fresh process (empty registry) over the same file-backed DB reads the
    durable line — and must say so, not masquerade as a live read."""
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()  # the "restart": the in-process registry dies, SQLite survives

    db2 = SessionDB(str(tmp_path / "state.db"))
    svc2 = _service(db2, tmp_path, lambda _p: "R")
    try:
        out = svc2.status(run_id)
        assert "error" not in out
        assert out["observation"]["source"] == "durable_store"
        assert out["observation"]["provider_calls"] == "none"
        assert out["observation"]["supervisor_context_tokens"] == "not_separately_attributed"
        assert out["observation"]["workflow_token_ledger_delta"] == 0
    finally:
        svc2.shutdown()
        db2.close()


def test_status_of_an_unknown_run_has_no_observation(tmp_path):
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        out = svc.status("never-launched")
        assert "error" in out
        assert "observation" not in out
    finally:
        svc.shutdown()
        db.close()


def test_the_tool_error_envelope_for_an_unknown_run_carries_no_observation(tmp_path):
    from lohra.tools.registry import tool_error

    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    tool = wt.WorkflowTool(svc)
    try:
        assert json.loads(tool_error("no workflow run 'x'")) == json.loads(
            tool.status({"run_id": "x"})
        )
    finally:
        svc.shutdown()
        db.close()


# --- the tool envelope: a successful read end to end --------------------------


def test_the_tool_status_envelope_is_ok_data_and_carries_the_observation(tmp_path):
    """The whole tool envelope: the standard success envelope ('ok': true) with
    the rollup and its observation block at the top level — equal to what the
    service returned. An 'error' key would mean the tool turned a success into
    a failure; the observation must survive the envelope either way."""
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    tool = wt.WorkflowTool(svc)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        out = json.loads(tool.status({"run_id": run_id}))
        assert out["ok"] is True
        assert out["run_id"] == run_id
        assert out["observation"] == EXPECTED_OBSERVATION | {"source": "local_registry"}
        assert out["observation"]["workflow_token_ledger_delta"] == 0
    finally:
        svc.shutdown()
        db.close()


def test_the_tool_status_envelope_of_a_restarted_process_keeps_the_observation(tmp_path):
    db = SessionDB(str(tmp_path / "state.db"))
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()

    db2 = SessionDB(str(tmp_path / "state.db"))
    svc2 = _service(db2, tmp_path, lambda _p: "R")
    tool = wt.WorkflowTool(svc2)
    try:
        out = json.loads(tool.status({"run_id": run_id}))
        assert out["ok"] is True
        assert out["observation"]["source"] == "durable_store"
        assert out["observation"]["supervisor_context_tokens"] == "not_separately_attributed"
        assert out["observation"]["workflow_token_ledger_delta"] == 0
    finally:
        svc2.shutdown()
        db2.close()


# --- tool descriptions: provenance and cost -----------------------------------


def test_status_description_declares_the_observation_block():
    d = _norm(STATUS_DESC)
    assert "`observation`" in d
    assert "local_registry" in d and "durable_store" in d
    assert "provider_calls" in d and "none" in d
    assert "supervisor_context_tokens" in d and "not_separately_attributed" in d
    assert "workflow_token_ledger_delta" in d and "0" in d
    # The read is the PRIMARY path; the durable store may be another process or
    # a restart — the description must say which primary read it took.
    assert "primary" in d
    assert "another process" in d or "restart" in d
    assert "no workflow run" in d  # and the error path is still excluded there


def test_status_description_never_overclaims_a_separate_charge():
    d = _norm(STATUS_DESC)
    # The old field lied twice over: it implied a meter exists for this spend.
    assert "not_metered" not in d
    # And the payload is never charged to the workflow run itself.
    assert "not charged to the workflow run" in d


def test_audit_description_distinguishes_local_cost_from_supervisor_context():
    d = _norm(AUDIT_DESC)
    assert "zero provider calls" in d
    # The overclaim must never come back: the query is free, the READ is not.
    assert "spends no model tokens" not in d
    assert "supervisor" in d and "context" in d
    assert "not separately attributed" in d


# --- the reading contract in the guidance and the skill -----------------------


def test_guidance_pins_the_reading_contract():
    g = _norm(wt.RUN_GUIDANCE)
    assert "terminal notification" in g
    assert "callback" in g and "delivery can fail" in g
    assert "cancelled" in g
    assert "before adapting" in g
    assert "no fixed blind polling" in g
    assert "does not wake or start a turn" in g
    assert "internal and fixed" in g
    assert "not a caller-selected deliberate deadline" in g
    assert "no active watcher" in g
    assert "leaf-level" in g
    assert "after_seq" in g and "limit" in g
    assert "raw content" in g and "reasoning" in g


def test_guidance_pins_the_slow_wedged_limit_no_read_can_break():
    """The semantic pin: NO read distinguishes slow from wedged. Silence (or any
    read, really) only updates last observed state. A phrase claiming the
    opposite — 'tells slow from wedged' — must never come back."""
    g = _norm(wt.RUN_GUIDANCE)
    assert "can't tell slow from wedged" in g
    assert "last observed state" in g
    assert "tells .still working. from .wedged." not in g


def test_guidance_pins_identity_ephemeral_execution_and_cost_split():
    g = _norm(wt.RUN_GUIDANCE)
    assert "sub_id" in g
    assert "ephemeral" in g
    assert "cache replay" in g
    assert "no execution" in g
    assert "logical" in g
    assert "observation" in g
    assert "no provider call" in g
    assert "aggregate" in g
    assert "not separately attributed" in g
    assert "workflow run" in g


def test_guidance_pins_absence_gap_unknown_and_observed_metadata():
    g = _norm(wt.RUN_GUIDANCE)
    assert "unknown" in g
    assert "idle" in g
    assert "observed metadata" in g
    assert "not" in g and "current action" in g


def test_skill_teaches_status_first_reading_not_blind_polling(skill_body):
    s = _norm(skill_body)
    assert "terminal notification" in s
    assert "callback" in s and "cancelled" in s
    assert "can fail" in s
    assert "does not wake or start a turn" in s
    assert "internal and fixed" in s
    assert "caller-selected" in s and "deliberate deadline" in s
    assert "no active watcher" in s
    assert "before adapting" in s
    assert "no fixed blind polling" in s


def test_skill_never_claims_a_read_distinguishes_slow_from_wedged(skill_body):
    """The old line — 'only a read tells still working from wedged' — directly
    contradicted the investigation's own H5 finding (no surface separates a
    slow leaf from a wedged one). The skill must keep the contradiction
    honest: silence proves nothing, a read only refreshes what was observed."""
    s = _norm(skill_body)
    assert "no read" in s and "slow" in s and "wedged" in s
    assert "last observed state" in s
    assert "only a read tells" not in s
    assert 'tells "still working" from "wedged"' not in s
    assert "wedged on one node" not in s


def test_skill_teaches_audit_on_demand_with_leaf_level_need(skill_body):
    s = _norm(skill_body)
    assert "leaf-level" in s
    assert "workflow_audit" in s
    assert "after_seq" in s
    assert "raw content" in s and "reasoning" in s
    assert "zero provider calls" in s
    assert "supervisor" in s and "context" in s


def test_skill_pins_absence_gap_unknown_and_observed_metadata(skill_body):
    s = _norm(skill_body)
    assert "unknown" in s
    assert "never idle" in s
    assert "observed metadata" in s
    assert "current action" in s


def test_skill_pins_logical_identity_vs_ephemeral_execution(skill_body):
    s = _norm(skill_body)
    assert "sub_id" in s
    assert "ephemeral" in s
    assert "cache replay" in s
    assert "no execution" in s
    assert "logical" in s
    assert "run_id" in s and "node_path" in s


def test_skill_documents_the_observation_block(skill_body):
    s = _norm(skill_body)
    assert "`observation`" in s
    assert "local_registry" in s and "durable_store" in s
    assert "not_separately_attributed" in s
    assert "workflow_token_ledger_delta" in s
    # the misleading names must not survive anywhere in the skill
    assert "live_engine" not in s and "durable_snapshot" not in s


def test_skill_says_the_supervisor_turn_is_metered_in_aggregate(skill_body):
    s = _norm(skill_body)
    assert "aggregate" in s
    assert "not separately attributed" in s
    assert "workflow_token_ledger_delta" in s and "0" in s
    assert "charged to the workflow run" in s


def test_skill_file_stays_within_the_line_budget():
    skill_path = Path(builtin_root()) / SKILL_NAME / "SKILL.md"
    lines = skill_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 800
