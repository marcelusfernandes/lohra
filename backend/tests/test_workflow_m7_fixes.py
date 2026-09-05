"""M7 fatia A — corrections and ergonomics found by dogfooding the harness.

Seven independent fixes, each pinned here by the test that would have caught it:

- **WF-22** a resume had to re-send a spec the run already persisted, even
  though the tool's own guidance says ``run_workflow(resume_run_id=...)`` is all
  it takes (and the quota auto-resume really does replay from the persisted
  spec). The schema said ``required: ["spec"]`` and the tool refused first.
- **WF-23** the terminal rollup of a RESUMED run reported only the last
  stretch's tokens, so a 30k-token run closed with "2k spent" on screen. The
  cumulative total now rides alongside the segment figures.
- **WF-8** ``judge_panel`` gated its fan-out per phase, so a structurally
  oversized panel could pay for every attempt and only then be rejected on the
  judges. The whole shape (attempts + attempts x judges + synthesize) is checked
  once, before anything spawns.
- **WF-9** nothing pinned NODE_SPECS ≡ STRATEGIES ≡ SUPPORTED_NODE_TYPES ≡ the
  guidance, so a node type could ship validatable-but-unexecutable. The contract
  test below is what makes the engine's defensive "no strategy" branch provably
  dead (it is gone; an impossible type is an ordinary engine fault).
- **WF-21** ``fs_allow`` could not express "readable, not writable": allowlisting
  a repo so leaves could READ it also made it writable.
- **WF-10** a checkpoint answered with `""` (or an explicit null) was not
  cached, because the store gate that drops a leaf's empty output treated a
  human's answer the same way — so the NEXT resume re-asked a question the
  person had already closed.
- the authoring skill now says to keep schemas lean — a verbose spec is one tool
  call, and the model truncated its own ``nodes`` list twice in the dogfood.

Every leaf costs a deterministic 8 tokens (fake usage 5 in / 3 out). No real
sleeps: the mid-flight tests release a gate Event from the test thread.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from lohra.skills.store import SkillStore, builtin_root
from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.budget import Budget
from lohra.workflow.accounting import RunResult
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.nodes import NODE_TYPES, Node, WorkflowSpec
from lohra.workflow.sandbox import FsRoot, WorkflowPolicy, load_policy, sandbox_dispatch
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import SUPPORTED_NODE_TYPES
from lohra.workflow.strategies import STRATEGIES
from lohra.workflow.tools import _RUN_SCHEMA, RUN_GUIDANCE, WorkflowTool
from tests.test_workflow_operability import LEAF_COST, _TWO_NODE, _gate, _ok, _service
from tests.test_workflow_token_budget import _core

SKILL_NAME = "workflow-authoring"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


# --- 1. WF-9: the node-type surface is ONE set, not four ------------------


def test_every_node_type_has_a_strategy_and_is_executable():
    """The guardrail the rest of M7 leans on: a type the validator accepts must
    be a type the engine can run, in the SAME commit — never one milestone
    ahead. This is what makes the engine's old "no strategy" branch dead code."""
    assert NODE_TYPES == frozenset(STRATEGIES) == SUPPORTED_NODE_TYPES


def test_the_guidance_bullets_are_exactly_the_node_types():
    """Set equality, not containment: an undocumented type is as broken as a
    documented one that no longer exists."""
    documented = set(re.findall(r"^- ([a-z_]+):", RUN_GUIDANCE, re.MULTILINE))
    assert documented == set(NODE_TYPES)


def test_the_skill_documents_exactly_the_node_types():
    store = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),))
    skill = store.get(SKILL_NAME)
    assert skill is not None
    for node_type in NODE_TYPES:
        assert f"`{node_type}`" in skill.body


def test_an_impossible_node_type_is_an_engine_fault_not_a_crash(db):
    """Belt and braces for the branch that was removed: a Node hand-built past
    the validator (only a bug can produce one) must still land as a recorded
    fault + a null, never as a dead run thread."""
    spec = WorkflowSpec(
        meta={"name": "ghost", "version": 1},
        inputs={},
        schemas={},
        nodes=(Node("z", "no_such_type", {}),),
    )
    core = _core(db, _ok)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["z"] is None
        assert result.engine_faults == 1
        assert any("z" in fault for fault in result.faults)
    finally:
        core.shutdown()


# --- 2. WF-22: a resume re-uses the spec the run already persisted --------


def test_resume_without_a_spec_reuses_the_persisted_one(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(None, {}, resume_run_id=run_id, token_budget=40)
        assert out["run_id"] == run_id and out["status"] == "started"
        # A resume now also reports what it will replay and re-pay (#44).
        assert set(out) == {"run_id", "status", "cache_preview"}
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_an_explicit_spec_still_wins_over_the_persisted_one(db, tmp_path):
    """Re-sending a spec must keep meaning "run THIS" — the persisted copy is a
    fallback, never an override."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        renamed = {**_TWO_NODE, "meta": {"name": "renamed", "version": 1}}
        svc.start(renamed, {}, resume_run_id=run_id, token_budget=40)
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert [row["name"] for row in svc.list_runs()] == ["renamed"]
    finally:
        svc.shutdown()


def test_resume_of_an_unknown_run_says_to_pass_the_spec(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        out = svc.start(None, {}, resume_run_id="ghost")
        assert "no spec on file" in out["error"] and "spec" in out["error"]
        assert "invalid_spec" not in out  # not the SPEC's fault: there isn't one
    finally:
        svc.shutdown()


def test_a_fresh_run_still_needs_a_spec(db, tmp_path):
    """The didactic refusal, not whatever the validator says about ``None``: a
    fresh run has no persisted spec to fall back on."""
    svc = _service(db, tmp_path, _ok)
    try:
        out = svc.start(None, {})
        assert "needs a 'spec'" in out["error"]
        assert "invalid_spec" not in out
    finally:
        svc.shutdown()


class _RecordingService:
    """The tool's collaborator, reduced to what the tool actually calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start(self, spec, args=None, **kwargs) -> dict:
        self.calls.append((spec, kwargs))
        return {"run_id": "r1", "status": "started"}


def test_the_run_tool_no_longer_demands_a_spec_on_a_resume():
    assert "spec" not in _RUN_SCHEMA["parameters"].get("required", [])
    service = _RecordingService()
    out = json.loads(WorkflowTool(service).run({"resume_run_id": "r1"}))
    assert "error" not in out and out["run_id"] == "r1"
    spec, kwargs = service.calls[0]
    assert spec is None and kwargs["resume_run_id"] == "r1"


def test_the_run_tool_still_refuses_a_spec_less_fresh_run():
    service = _RecordingService()
    out = json.loads(WorkflowTool(service).run({"args": {"x": 1}}))
    assert "error" in out and "spec" in out["error"]
    assert service.calls == []  # refused before the service ever saw it


def test_the_run_tool_forwards_a_non_object_spec_to_the_service():
    """SUP-05 proveniência: a spec EXPLÍCITA do agente chega ao start em
    qualquer shape — o validate_spec (não a porta da tool) rejeita com o erro
    didático "the spec must be a mapping", e a falha de autoria registra a
    candidata. Recusar aqui esconderia o fault de quem aprende com ele."""
    service = _RecordingService()
    out = json.loads(WorkflowTool(service).run({"spec": "meta: name", "resume_run_id": "r1"}))
    assert "error" not in out  # o _RecordingService aceita; quem valida é o serviço
    spec, kwargs = service.calls[0]
    assert spec == "meta: name"
    assert kwargs["agency_authored"] is True  # spec explícita = autoria da agência


# --- 3. WF-23: the closing number is the run's WHOLE cost -----------------


def test_a_finished_run_reports_its_cumulative_spend_without_a_budget(db, tmp_path):
    """No token_budget means no {total, spent, remaining} block at all, so before
    this the only cost figure was the segment's. Nothing about that contract
    changes — the cumulative total simply also gets said."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert "token_budget" not in out
        assert out["tokens_spent_total"] == 2 * LEAF_COST
    finally:
        svc.shutdown()


def test_a_resumed_run_reports_the_whole_run_not_the_last_stretch(db, tmp_path):
    """The dogfood finding: the screen said 2k, the ledger said 30.7k. 'a'
    replays from the cache on the resume, so the SEGMENT only ever sees the one
    leaf 'b' really cost — the run cost two."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        svc.start(None, {}, resume_run_id=run_id, token_budget=40)
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert out["tokens_in"] + out["tokens_out"] == LEAF_COST  # the segment
        assert out["tokens_spent_total"] == 2 * LEAF_COST  # the run
        assert out["token_budget"]["spent"] == 2 * LEAF_COST  # agrees with §7.1
    finally:
        svc.shutdown()


def test_a_run_in_flight_already_reports_what_it_has_spent(db, tmp_path):
    entered, release, responder = _gate()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        assert svc.status(run_id)["tokens_spent_total"] == 0  # nothing landed yet
    finally:
        release.set()
        svc.shutdown()


def test_a_problematic_run_never_touches_the_legacy_insights_file(tmp_path):
    """Legacy automatic learning is OFF: whatever the run cost, a problematic
    outcome writes nothing. The cumulative-total plumbing (WF-23) is still
    accepted — it just has no legacy file to land in."""
    result = RunResult(status="degraded", nodes_total=2, null_count=1, tokens_in=5, tokens_out=3)
    library.record_outcome(tmp_path, {"meta": {"name": "wf"}}, result, tokens_total=999)
    library.record_outcome(tmp_path, {"meta": {"name": "wf"}}, result)
    assert not (tmp_path / "workflows" / "insights.md").exists()


# --- 4. WF-8: judge_panel is gated as ONE shape, before it spends ---------


def _counting(calls: list):
    def responder(prompt: str) -> str:
        calls.append(prompt)
        return '{"score": 9}' if "Score this attempt" in prompt else "R"

    return responder


def _panel_spec(attempts: int, judges: int, synthesize: Any = None):
    node = {
        "id": "J",
        "type": "judge_panel",
        "judges": judges,
        "attempts": [{"prompt": f"a{i}"} for i in range(attempts)],
        # 'synthesize' is a required field; only a dict one really spawns a leaf.
        "synthesize": synthesize or {"prompt": "synthesize ${winner}"},
    }
    return validate_spec({"meta": {"name": "jp", "version": 1}, "nodes": [node]})


def test_an_oversized_panel_is_rejected_before_a_single_attempt_spawns(db):
    """3 attempts x 3 judges + 1 synthesis = 13 leaves against a max_fanout of 8.
    The per-phase gate let the 3 attempts run and be billed before the judges
    tripped the cap; the whole shape is now weighed first."""
    calls: list = []
    core = _core(db, _counting(calls))
    try:
        engine = WorkflowEngine(core, budget=Budget(max_fanout=8))
        result = engine.run(_panel_spec(3, 3), {})
        assert result.outputs["J"] is None
        assert result.cap_trips == 1
        assert calls == []  # the discriminator: nothing was paid for
        assert engine.budget.tokens_spent == 0
    finally:
        core.shutdown()


def test_the_preflight_counts_the_synthesis_leaf_too(db):
    """4 attempts x 1 judge = 8 fits a max_fanout of 8; the synthesis leaf is the
    ninth. Under the per-phase gate this panel ran to the end — forgetting the
    synthesis leaf is precisely how one dies after paying for everything else."""
    calls: list = []
    core = _core(db, _counting(calls))
    try:
        engine = WorkflowEngine(core, budget=Budget(max_fanout=8))
        assert engine.run(_panel_spec(4, 1), {}).cap_trips == 1
        assert calls == []
    finally:
        core.shutdown()


def test_a_panel_with_nothing_to_synthesize_is_not_charged_for_it(db):
    """...and the +1 is conditional: a non-dict 'synthesize' spawns no leaf, so
    the same 4x1 shape fits. A flat +1 would refuse a panel that fits."""
    calls: list = []
    core = _core(db, _counting(calls))
    try:
        engine = WorkflowEngine(core, budget=Budget(max_fanout=8))
        result = engine.run(_panel_spec(4, 1, synthesize="none"), {})
        assert result.status == "complete"
        assert result.cap_trips == 0
        assert result.outputs["J"] == "R"  # the unsynthesised winner
    finally:
        core.shutdown()


def test_a_panel_that_fits_its_shape_still_runs(db):
    calls: list = []
    core = _core(db, _counting(calls))
    try:
        engine = WorkflowEngine(core, budget=Budget(max_fanout=8))
        result = engine.run(_panel_spec(3, 1), {})  # 3 + 3 + 1 = 7
        assert result.status == "complete"
        assert result.cap_trips == 0
        assert len(calls) == 7
    finally:
        core.shutdown()


# --- 5. WF-21: an allowlisted root can be readable without being writable --


def _denied(out: str) -> bool:
    return "error" in json.loads(out)


def _dispatch(working_root: Path, policy: WorkflowPolicy, *, tainted: bool = False):
    return sandbox_dispatch(
        lambda name, args: '{"ok": true}',
        working_root=working_root,
        policy=policy,
        tainted=tainted,
    )


def test_a_read_only_root_reads_but_does_not_write(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkflowPolicy(fs_allow=({"path": str(repo), "mode": "ro"},))
    d = _dispatch(tmp_path / "work", policy)
    assert not _denied(d("read_file", {"path": str(repo / "src.py")}))
    assert _denied(d("write_file", {"path": str(repo / "src.py")}))


def test_the_write_refusal_says_the_root_is_read_only(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkflowPolicy(fs_allow=({"path": str(repo), "mode": "ro"},))
    out = json.loads(_dispatch(tmp_path / "work", policy)("write_file", {"path": str(repo / "a")}))
    assert "read-only" in out["error"].lower()


def test_a_read_write_root_still_writes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkflowPolicy(fs_allow=({"path": str(repo), "mode": "rw"},))
    d = _dispatch(tmp_path / "work", policy)
    assert not _denied(d("write_file", {"path": str(repo / "out.txt")}))


def test_a_bare_string_root_stays_read_write(tmp_path):
    """Back-compat: every policy written before this fix granted both, and
    silently downgrading a production policy to read-only is not a fix."""
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkflowPolicy(fs_allow=(str(repo),))
    assert policy.fs_allow == (FsRoot(repo, writable=True),)
    assert not _denied(
        _dispatch(tmp_path / "work", policy)("write_file", {"path": str(repo / "x")})
    )


def test_the_working_root_is_always_writable(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    policy = WorkflowPolicy(fs_allow=({"path": str(tmp_path / "repo"), "mode": "ro"},))
    assert not _denied(_dispatch(work, policy)("write_file", {"path": str(work / "scratch.txt")}))


def test_a_path_outside_every_root_still_says_out_of_scope(tmp_path):
    policy = WorkflowPolicy(fs_allow=({"path": str(tmp_path / "repo"), "mode": "ro"},))
    out = json.loads(_dispatch(tmp_path / "work", policy)("write_file", {"path": "/etc/passwd"}))
    assert "outside" in out["error"]


def test_taint_still_kills_a_read_only_root(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    policy = WorkflowPolicy(fs_allow=({"path": str(repo), "mode": "ro"},))
    d = _dispatch(tmp_path / "work", policy, tainted=True)
    assert _denied(d("read_file", {"path": str(repo / "src.py")}))


def test_an_fs_root_can_be_handed_in_ready_made(tmp_path):
    """The normalisation is idempotent — a caller holding FsRoots (the wiring
    that will read a richer config one day) gets them back untouched."""
    root = FsRoot(tmp_path / "repo", writable=False)
    assert WorkflowPolicy(fs_allow=(root,)).fs_allow == (root,)


def test_a_non_string_path_is_denied_rather_than_coerced(tmp_path):
    """A leaf sending ``path: 7`` must be refused, not have it stringified into
    something that might resolve."""
    policy = WorkflowPolicy(fs_allow=(str(tmp_path),))
    assert _denied(_dispatch(tmp_path / "work", policy)("read_file", {"path": 7}))


def test_load_policy_reads_both_modes(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "fs_allow": [
                    "/legacy",
                    {"path": "/reads", "mode": "ro"},
                    {"path": "/writes", "mode": "rw"},
                ],
                "egress_allow": ["api.test"],
            }
        )
    )
    policy = load_policy(path)
    assert policy.egress_allow == ("api.test",)
    assert {(str(r.path), r.writable) for r in policy.fs_allow} == {
        ("/legacy", True),
        ("/reads", False),
        ("/writes", True),
    }


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "/data", "mode": "read"},  # not one of ro|rw
        {"path": "", "mode": "ro"},  # an empty path resolves to the CWD
        {"mode": "ro"},  # no path at all
        {"path": 7, "mode": "rw"},
        None,
    ],
)
def test_a_malformed_root_is_dropped_not_guessed(tmp_path, entry):
    """Deny-by-default all the way down: a typo must never widen (or silently
    narrow) what the operator granted — the entry simply does not exist."""
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"fs_allow": [entry]}))
    assert load_policy(path).fs_allow == ()


# --- 6. the skill says what the dogfood learned about spec size -----------


def test_the_skill_warns_that_the_whole_spec_rides_in_one_tool_call():
    store = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),))
    skill = store.get(SKILL_NAME)
    assert skill is not None
    body = skill.body.lower()
    assert "one tool call" in body
    assert "schema_ref" in body


# --- 7. WF-10: an answer a human GAVE is never asked for twice ------------


_TWO_CHECKPOINTS = {
    "meta": {"name": "cp2", "version": 1},
    "nodes": [
        {"id": "ok", "type": "checkpoint", "prompt": "Approve the plan?"},
        {"id": "ok2", "type": "checkpoint", "prompt": "Approve the budget?"},
        {"id": "go", "type": "agent", "prompt": "Proceed given ${ok2}."},
    ],
}


def _answer_first_checkpoint(svc, answer):
    """Launch the two-checkpoint spec, answer 'ok' with `answer`, and return
    (run_id, the status of the run once it has paused on the SECOND one)."""
    run_id = svc.start(_TWO_CHECKPOINTS, {})["run_id"]
    assert svc.status(run_id, wait=True, timeout=10)["checkpoint"]["node_id"] == "ok"
    svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"ok": answer})
    return run_id, svc.status(run_id, wait=True, timeout=10)


def test_an_empty_checkpoint_answer_is_still_an_answer(db, tmp_path):
    """A human who answers "" has ANSWERED — the emptiness gate on cache_store
    exists for a leaf that said nothing (WF-7), not for a person who did. Cached
    like any other completion, the second resume finishes instead of re-opening
    a question the human already closed."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id, paused = _answer_first_checkpoint(svc, "")
        assert paused["checkpoint"]["node_id"] == "ok2"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"ok2": "yes"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"  # not re-paused on 'ok'
        assert done["outputs"]["ok"] == ""
    finally:
        svc.shutdown()


def test_a_null_checkpoint_answer_is_still_an_answer(db, tmp_path):
    """Same rule for an explicit null (what a declared ``default: null`` sends):
    the run may go on to null downstream, but it must never re-ask 'ok'."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id, paused = _answer_first_checkpoint(svc, None)
        assert paused["checkpoint"]["node_id"] == "ok2"
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"ok2": "yes"})
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] != "paused"
        assert done["outputs"]["ok"] is None
    finally:
        svc.shutdown()
