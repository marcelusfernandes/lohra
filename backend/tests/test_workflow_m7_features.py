"""M7 fatia B — ergonomia estendida: model tiers + three new node types.

Four features, each pinned by the test that defines it:

- **WF-5 model tiers**: a spec that hard-codes ``model: claude-opus-4-8`` stops
  being portable the moment it becomes a template on another profile. A node may
  now name a ``tier`` (``small``/``medium``/``big``) that the OPERATOR maps to a
  real slug in ``~/.lohra/workflow_tiers.json`` — never the spec, same rule the
  capability policy already follows. Explicit model/effort/provider still win,
  the RESOLVED values are what the resume cache is keyed on, and a tier with no
  mapping WARNS and runs on the default (never silent, never fatal).
- **WF-6 ``gate``**: draft → review → revise, bounded. The body leaf answers, a
  validator leaf answers ``{ok, feedback}``, and a rejection buys a FRESH body
  re-spawn carrying the feedback. Only the approved output is cached.
- **``completeness_check``**: a thin critic node (like ``verify``) that answers
  the fixed ``{complete, missing}`` — the "what is still missing?" pass that used
  to need a hand-rolled ``agent`` + schema every time.
- **WF-10 ``checkpoint``**: the human gate. The engine PAUSES the run (a fourth
  pause reason next to quota / token budget / user), reports what it is waiting
  for, and a resume seeds the answer as the node's output — cached, so a later
  resume never asks the same question twice.

Every leaf costs a deterministic 8 tokens (fake usage 5 in / 3 out). No sleeps.
"""

import json
from pathlib import Path

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import library, strategies
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.nodes import NODE_TYPES, Node
from lohra.workflow.schema import ValidationError, validate_spec
from lohra.workflow.service import SUPPORTED_NODE_TYPES
from lohra.workflow.strategies import STRATEGIES
from lohra.workflow.tiers import MODEL_TIERS, Tier, TierMap, load_tiers
from lohra.workflow.tools import _RUN_SCHEMA, _SPEC_PARAM, RUN_GUIDANCE, WorkflowTool
from tests.test_workflow_operability import _service
from tests.test_workflow_pipeline import ScriptedClient

DEFAULT_MODEL = "claude-opus-4-8"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


class ModelSpyClient(ScriptedClient):
    """A scripted client that also records the model every turn asked for."""

    def __init__(self, responder, seen):
        super().__init__(responder)
        self._seen = seen

    def create(self, **kwargs):
        self._seen.append(kwargs.get("model"))
        return super().create(**kwargs)


def _core(db, responder, *, seen=None, pool_width=4):
    def factory():
        client = ScriptedClient(responder) if seen is None else ModelSpyClient(responder, seen)
        return Agent(
            model=DEFAULT_MODEL, provider=get_provider_profile("anthropic"), client=client
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _ok(_prompt):
    return "R"


def _agent_spec(fields):
    return validate_spec(
        {
            "meta": {"name": "t", "version": 1},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go", **fields}],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )


# --- 1. WF-5: the tier map is operator config -----------------------------


def test_load_tiers_reads_the_operator_map(tmp_path):
    path = tmp_path / "workflow_tiers.json"
    path.write_text(
        json.dumps(
            {
                "big": {"model": "big-model", "effort": "high"},
                "small": {"model": "small-model", "provider": "groq"},
            }
        ),
        encoding="utf-8",
    )
    tiers = load_tiers(path)
    assert tiers.get("big") == Tier(model="big-model", effort="high")
    assert tiers.get("small") == Tier(model="small-model", provider="groq")
    assert tiers.get("medium") is None


def test_a_bare_string_is_the_model_shorthand(tmp_path):
    path = tmp_path / "workflow_tiers.json"
    path.write_text(json.dumps({"medium": "mid-model"}), encoding="utf-8")
    assert load_tiers(path).get("medium") == Tier(model="mid-model")


def test_a_missing_or_broken_tier_file_is_simply_empty(tmp_path):
    assert load_tiers(tmp_path / "nope.json").get("big") is None
    broken = tmp_path / "workflow_tiers.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_tiers(broken).get("big") is None


def test_an_unknown_tier_name_in_the_file_is_dropped(tmp_path):
    """The tier set is CLOSED — an operator typo must not create a fourth tier
    no spec can name."""
    path = tmp_path / "workflow_tiers.json"
    path.write_text(json.dumps({"huge": {"model": "x"}, "big": {"model": "y"}}), encoding="utf-8")
    tiers = load_tiers(path)
    assert tiers.get("huge") is None
    assert tiers.get("big") == Tier(model="y")
    assert set(tiers.tiers) <= set(MODEL_TIERS)


# --- 2. WF-5: resolution at spawn -----------------------------------------


class _FakeEngine:
    def __init__(self, tiers):
        self.tiers = tiers


def test_a_tier_resolves_model_effort_and_provider():
    engine = _FakeEngine(TierMap({"big": Tier("big-model", "openai", "high")}))
    node = Node("a", "agent", {"prompt": "go", "tier": "big"})
    assert strategies._leaf_config(engine, node) == ("big-model", "high", "openai", None)


def test_explicit_fields_beat_the_tier():
    engine = _FakeEngine(TierMap({"big": Tier("big-model", "openai", "high")}))
    node = Node("a", "agent", {"prompt": "go", "tier": "big", "model": "mine", "effort": "low"})
    model, effort, provider, warning = strategies._leaf_config(engine, node)
    assert (model, effort, provider, warning) == ("mine", "low", "openai", None)


def test_an_unmapped_tier_warns_and_falls_back_to_the_default():
    engine = _FakeEngine(TierMap())
    node = Node("a", "agent", {"prompt": "go", "tier": "small"})
    model, effort, provider, warning = strategies._leaf_config(engine, node)
    assert (model, effort, provider) == (None, None, None)
    assert "workflow_tiers.json" in warning and "small" in warning


def test_a_tier_reaches_the_leaf_as_a_real_model(db):
    seen = []
    core = _core(db, _ok, seen=seen)
    try:
        spec = _agent_spec({"tier": "big"})
        engine = WorkflowEngine(
            core, budget=Budget(), tiers=TierMap({"big": Tier(model="big-model")})
        )
        result = engine.run(spec, {})
        assert result.outputs["a"] == "R"
        assert seen == ["big-model"]
    finally:
        core.shutdown()


def test_an_explicit_model_still_wins_at_the_leaf(db):
    seen = []
    core = _core(db, _ok, seen=seen)
    try:
        spec = _agent_spec({"tier": "big", "model": "explicit-model"})
        engine = WorkflowEngine(
            core, budget=Budget(), tiers=TierMap({"big": Tier(model="big-model")})
        )
        engine.run(spec, {})
        assert seen == ["explicit-model"]
    finally:
        core.shutdown()


def test_an_unmapped_tier_runs_the_node_on_the_default_model(db):
    """Never silent, never fatal: the node RUNS (on whatever the run's default
    is) and the rollup carries a fault naming the operator file to fix."""
    seen = []
    core = _core(db, _ok, seen=seen)
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_agent_spec({"tier": "small"}), {})
        assert seen == [DEFAULT_MODEL]
        assert result.outputs["a"] == "R"
        assert result.status == "degraded"  # a fault is a fault; the node still ran
        assert any("workflow_tiers.json" in fault for fault in result.faults)
    finally:
        core.shutdown()


def test_the_resolved_tier_is_part_of_the_cell_identity(db):
    """A resume whose tier now maps somewhere else must NOT replay the answer the
    old model gave — the cell was produced under a different configuration."""
    seen = []
    core = _core(db, _ok, seen=seen)
    spec = _agent_spec({"tier": "big"})
    try:
        for slug in ("big-model", "other-model"):
            WorkflowEngine(
                core,
                budget=Budget(),
                cache=NodeCache(db, "run-1"),
                tiers=TierMap({"big": Tier(model=slug)}),
            ).run(spec, {})
        assert seen == ["big-model", "other-model"]  # re-spawned, not replayed
    finally:
        core.shutdown()


def test_an_unchanged_tier_still_replays_from_the_cache(db):
    """The control for the test above: same resolution, same cell, no spawn."""
    seen = []
    core = _core(db, _ok, seen=seen)
    spec = _agent_spec({"tier": "big"})
    try:
        for _ in range(2):
            WorkflowEngine(
                core,
                budget=Budget(),
                cache=NodeCache(db, "run-1"),
                tiers=TierMap({"big": Tier(model="big-model")}),
            ).run(spec, {})
        assert seen == ["big-model"]
    finally:
        core.shutdown()


def test_a_tier_outside_the_closed_set_is_a_didactic_error():
    result = _agent_spec({"tier": "huge"})
    assert isinstance(result, ValidationError)
    assert "tier" in result.message and "big" in result.message


def test_a_template_authored_with_a_tier_keeps_the_tier(tmp_path):
    """Portability is the whole point: the library must save what was authored,
    never a resolved slug that only exists on this machine."""
    spec = {
        "meta": {"name": "portable"},
        "nodes": [{"id": "a", "type": "agent", "prompt": "go", "tier": "big"}],
    }
    from lohra.workflow.engine import RunResult

    library.record_outcome(tmp_path, spec, RunResult(nodes_total=1, status="complete"))
    saved = library.get_template(tmp_path, "portable")
    assert saved["nodes"][0]["tier"] == "big"
    assert "model" not in saved["nodes"][0]


# --- 3. WF-6: the `gate` node ---------------------------------------------


_GATE_SPEC = {
    "meta": {"name": "g", "version": 1},
    "nodes": [
        {
            "id": "g",
            "type": "gate",
            "body": {"prompt": "Draft the plan."},
            "validator": "Is the plan complete?",
        }
    ],
}


def _gate_responder(script):
    """A responder that answers the VALIDATOR from ``script`` (one verdict per
    review) and the body with the drafts it is fed."""
    verdicts = iter(script)

    def responder(prompt):
        if "CANDIDATE:" in prompt:
            return json.dumps(next(verdicts, {"ok": False, "feedback": "no"}))
        return "revised" if "REVISION" in prompt else "draft"

    return responder


def test_a_gate_returns_the_body_output_once_the_validator_approves(db):
    core = _core(db, _gate_responder([{"ok": True, "feedback": ""}]))
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] == "draft"
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_a_rejected_body_is_respawned_with_the_feedback(db):
    seen_prompts = []

    def responder(prompt):
        seen_prompts.append(prompt)
        if "CANDIDATE:" in prompt:
            ok = "revised" in prompt
            return json.dumps({"ok": ok, "feedback": "name every file"})
        return "revised" if "name every file" in prompt else "draft"

    core = _core(db, responder)
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] == "revised"
        # the second body leaf really carried the reviewer's words
        bodies = [p for p in seen_prompts if "CANDIDATE:" not in p]
        assert len(bodies) == 2 and "name every file" in bodies[1]
    finally:
        core.shutdown()


def test_a_gate_that_never_passes_nulls_with_a_fault(db):
    core = _core(db, _gate_responder([{"ok": False, "feedback": "no"}] * 2))
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] is None
        assert any("validator rejected after 2 attempt(s)" in f for f in result.faults)
    finally:
        core.shutdown()


def test_a_validator_that_cannot_answer_never_approves(db):
    """Fail-closed, exactly like ``verify``'s all-dead skeptics: an unusable
    verdict is a rejection, never a pass."""

    def responder(prompt):
        return "I have opinions but no JSON" if "CANDIDATE:" in prompt else "draft"

    core = _core(db, responder)
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] is None
    finally:
        core.shutdown()


def test_a_gate_charges_every_leaf_it_spawns(db):
    core = _core(db, _gate_responder([{"ok": False, "feedback": "no"}] * 2))
    budget = Budget(lifetime=20)
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        WorkflowEngine(core, budget=budget).run(spec, {})
        assert budget.lifetime_remaining == 20 - 4  # 2 attempts x (body + validator)
    finally:
        core.shutdown()


def test_only_the_approved_output_is_cached(db):
    """A resume replays the answer that PASSED — never the draft that was
    rejected, and never a second round of review."""
    calls = []

    def responder(prompt):
        calls.append(prompt)
        if "CANDIDATE:" in prompt:
            return json.dumps({"ok": "revised" in prompt, "feedback": "sharpen it"})
        return "revised" if "sharpen it" in prompt else "draft"

    core = _core(db, responder)
    spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
    try:
        first = WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, "r")).run(spec, {})
        spawned = len(calls)
        second = WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, "r")).run(spec, {})
        assert first.outputs["g"] == second.outputs["g"] == "revised"
        assert len(calls) == spawned  # replayed: nothing spawned the second time
    finally:
        core.shutdown()


def test_a_dead_draft_is_a_failed_attempt_not_a_pass(db):
    """The reviewer must never be asked to bless a leaf that died — and a run of
    dead drafts must exhaust the attempts, not fall through as an approval."""
    reviewed = []

    def responder(prompt):
        if "CANDIDATE:" in prompt:
            reviewed.append(prompt)
            return json.dumps({"ok": True})
        raise RuntimeError("draft died")

    core = _core(db, responder)
    try:
        spec = validate_spec(_GATE_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] is None
        assert reviewed == []  # nothing was ever put in front of the reviewer
    finally:
        core.shutdown()


def test_an_upstream_null_never_reaches_a_gate(db):
    calls, responder = _counting()
    core = _core(db, responder)
    spec = validate_spec(
        {
            "meta": {"name": "g", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go", "schema": {"type": "object"}},
                {
                    "id": "g",
                    "type": "gate",
                    "body": {"prompt": "Improve ${a.draft}"},
                    "validator": "good?",
                },
            ],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["g"] is None
        assert not any("Improve" in call for call in calls)  # the gate never drafted
        assert any("upstream null" in fault for fault in result.faults)
    finally:
        core.shutdown()


def test_gate_attempts_are_capped_at_the_author_level():
    spec = validate_spec(
        {
            "meta": {"name": "g"},
            "nodes": [
                {
                    "id": "g",
                    "type": "gate",
                    "body": {"prompt": "x"},
                    "validator": "ok?",
                    "attempts": 9,
                }
            ],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )
    assert isinstance(spec, ValidationError)
    assert "attempts" in spec.message


def test_a_gate_needs_a_body_prompt_and_a_validator():
    spec = validate_spec(
        {"meta": {"name": "g"}, "nodes": [{"id": "g", "type": "gate", "body": {}, "validator": 3}]},
        supported_types=SUPPORTED_NODE_TYPES,
    )
    assert isinstance(spec, ValidationError)
    assert "body" in spec.message and "validator" in spec.message


# --- 4. `completeness_check` ----------------------------------------------


_COMPLETENESS_SPEC = {
    "meta": {"name": "c", "version": 1},
    "nodes": [
        {
            "id": "c",
            "type": "completeness_check",
            "task": "List every config file.",
            "results": "${args.found}",
        }
    ],
}


def test_completeness_check_answers_the_fixed_schema(db):
    core = _core(db, lambda _p: json.dumps({"complete": False, "missing": ["pyproject.toml"]}))
    try:
        spec = validate_spec(_COMPLETENESS_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"found": ["setup.cfg"]})
        assert result.outputs["c"] == {"complete": False, "missing": ["pyproject.toml"]}
    finally:
        core.shutdown()


def test_completeness_check_shows_the_leaf_both_the_task_and_the_results(db):
    seen = []

    def responder(prompt):
        seen.append(prompt)
        return json.dumps({"complete": True, "missing": []})

    core = _core(db, responder)
    try:
        spec = validate_spec(_COMPLETENESS_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        WorkflowEngine(core, budget=Budget()).run(spec, {"found": ["setup.cfg"]})
        assert "List every config file." in seen[0] and "setup.cfg" in seen[0]
    finally:
        core.shutdown()


def test_a_completeness_check_that_cannot_answer_nulls(db):
    core = _core(db, lambda _p: "everything looks fine to me")
    try:
        spec = validate_spec(_COMPLETENESS_SPEC, supported_types=SUPPORTED_NODE_TYPES)
        result = WorkflowEngine(core, budget=Budget()).run(spec, {"found": []})
        assert result.outputs["c"] is None
    finally:
        core.shutdown()


def test_an_upstream_null_never_reaches_a_completeness_check(db):
    core = _core(db, lambda _p: json.dumps({"complete": True, "missing": []}))
    spec = validate_spec(
        {
            "meta": {"name": "c", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go", "schema": {"type": "object"}},
                {"id": "c", "type": "completeness_check", "task": "t", "results": "${a.items}"},
            ],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["c"] is None
        assert any("upstream null" in f for f in result.faults)
    finally:
        core.shutdown()


# --- 5. WF-10: the `checkpoint` node --------------------------------------


_CHECKPOINT_SPEC = {
    "meta": {"name": "cp", "version": 1},
    "nodes": [
        {"id": "ok", "type": "checkpoint", "prompt": "Approve the plan?"},
        {"id": "go", "type": "agent", "prompt": "Proceed given ${ok}."},
    ],
}
_CHECKPOINT_WITH_DEFAULT = {
    "meta": {"name": "cpd", "version": 1},
    "nodes": [
        {"id": "ok", "type": "checkpoint", "prompt": "Approve?", "default": "yes"},
        {"id": "go", "type": "agent", "prompt": "Proceed given ${ok}."},
    ],
}


def _counting():
    calls = []

    def responder(prompt):
        calls.append(prompt)
        return "R"

    return calls, responder


def test_a_checkpoint_pauses_the_run_and_says_what_it_wants(db, tmp_path):
    calls, responder = _counting()
    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == CHECKPOINT
        assert out["checkpoint"] == {"node_id": "ok", "prompt": "Approve the plan?"}
        assert "checkpoint_answers" in out["hint"]
        assert calls == []  # a checkpoint NEVER spawns a leaf
    finally:
        svc.shutdown()


def test_a_checkpoint_pause_arms_no_auto_resume(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["resume_at"] is None
    finally:
        svc.shutdown()


def test_a_checkpoint_paused_run_teaches_the_library_nothing(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert not (tmp_path / "workflows").exists()
    finally:
        svc.shutdown()


def test_resuming_with_the_answer_finishes_the_run(db, tmp_path):
    seen = []

    def responder(prompt):
        seen.append(prompt)
        return "R"

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"ok": "ship it"})
        assert out == {"run_id": run_id, "status": "started"}
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"] == {"ok": "ship it", "go": "R"}
        assert "ship it" in seen[0]  # the answer really flowed downstream
    finally:
        svc.shutdown()


def test_a_second_resume_never_asks_the_same_checkpoint_again(db, tmp_path):
    """The answer is cached as a completion, so re-running the run replays it."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        svc.status(run_id, wait=True, timeout=10)
        svc.start(None, {}, resume_run_id=run_id, checkpoint_answers={"ok": "ship it"})
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        out = svc.start(None, {}, resume_run_id=run_id)  # no answers at all
        assert "error" not in out
        assert svc.status(run_id, wait=True, timeout=10)["outputs"]["ok"] == "ship it"
    finally:
        svc.shutdown()


def test_resuming_without_an_answer_is_a_didactic_error(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(None, {}, resume_run_id=run_id)
        assert "ok" in out["error"] and "Approve the plan?" in out["error"]
        assert "checkpoint_answers" in out["error"]
        assert "invalid_spec" not in out  # nothing is wrong with the spec
    finally:
        svc.shutdown()


def test_a_declared_default_answers_the_checkpoint_on_resume(db, tmp_path):
    """The gate still STOPS the first time — a default is what lets an unattended
    resume carry on rather than stalling forever."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_WITH_DEFAULT, {})["run_id"]
        paused = svc.status(run_id, wait=True, timeout=10)
        assert paused["status"] == "paused"
        assert paused["checkpoint"]["default"] == "yes"
        svc.start(None, {}, resume_run_id=run_id)
        done = svc.status(run_id, wait=True, timeout=10)
        assert done["status"] == "complete"
        assert done["outputs"]["ok"] == "yes"
    finally:
        svc.shutdown()


def test_an_explicit_spec_is_not_held_up_by_an_old_checkpoint(db, tmp_path):
    """Re-sending a spec means "run THIS" — the previous run's pending question
    is moot, and a spec with no checkpoint must not inherit its refusal."""
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_CHECKPOINT_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        plain = {
            "meta": {"name": "plain", "version": 1},
            "nodes": [{"id": "go", "type": "agent", "prompt": "just go"}],
        }
        assert "error" not in svc.start(plain, {}, resume_run_id=run_id)
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_a_checkpoint_prompt_is_resolved_before_it_is_asked(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    spec = {
        "meta": {"name": "cpr", "version": 1},
        "nodes": [{"id": "ok", "type": "checkpoint", "prompt": "Ship ${args.what}?"}],
    }
    try:
        run_id = svc.start(spec, {"what": "the release"})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["checkpoint"]["prompt"] == "Ship the release?"
    finally:
        svc.shutdown()


# --- 6. the authoring surface grew by three -------------------------------


def test_an_unresolvable_checkpoint_question_nulls_instead_of_pausing(db, tmp_path):
    """Pausing to ask "Approve null?" would strand the run on a question nobody
    can answer. A prompt that cannot be resolved fails the node like any other."""
    svc = _service(db, tmp_path, _ok)
    spec = {
        "meta": {"name": "cpn", "version": 1},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go", "schema": {"type": "object"}},
            {"id": "ok", "type": "checkpoint", "prompt": "Approve ${a.plan}?"},
        ],
    }
    try:
        run_id = svc.start(spec, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] != "paused"
        assert out["outputs"]["ok"] is None
    finally:
        svc.shutdown()


def test_the_three_new_types_are_first_class(db):
    for node_type in ("gate", "completeness_check", "checkpoint"):
        assert node_type in NODE_TYPES
        assert node_type in STRATEGIES
        assert node_type in SUPPORTED_NODE_TYPES


def test_the_spec_param_lists_every_node_type():
    """The tool's own parameter description is what a model reads before the
    guidance — a type missing here is a type it will never author."""
    for node_type in NODE_TYPES:
        assert node_type in _SPEC_PARAM["description"]


def test_the_guidance_mentions_tiers_and_checkpoint_answers():
    assert "tier" in RUN_GUIDANCE
    assert "checkpoint_answers" in RUN_GUIDANCE


def test_run_workflow_takes_checkpoint_answers():
    properties = _RUN_SCHEMA["parameters"]["properties"]
    assert properties["checkpoint_answers"]["type"] == "object"


class _StubService:
    def __init__(self):
        self.seen = None

    def start(self, spec, args, **kwargs):
        self.seen = kwargs
        return {"run_id": "r", "status": "started"}


def test_the_tool_forwards_checkpoint_answers():
    service = _StubService()
    WorkflowTool(service).run({"resume_run_id": "r", "checkpoint_answers": {"ok": "yes"}})
    assert service.seen["checkpoint_answers"] == {"ok": "yes"}


def test_the_tool_refuses_checkpoint_answers_that_are_not_a_mapping():
    out = WorkflowTool(_StubService()).run({"resume_run_id": "r", "checkpoint_answers": ["yes"]})
    assert "checkpoint_answers" in out and "error" in out


def test_the_builtin_skill_carries_a_gate_example():
    from lohra.skills.store import SkillStore, builtin_root

    skill = SkillStore(Path("/nonexistent-home"), builtin_roots=(builtin_root(),)).get(
        "workflow-authoring"
    )
    assert '"type": "gate"' in skill.body or '"type":"gate"' in skill.body
