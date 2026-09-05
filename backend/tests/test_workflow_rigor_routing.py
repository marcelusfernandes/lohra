"""Model routing (`model`/`tier`/`effort`/`provider`) on the FIVE rigor nodes.

Until this slice only an ``agent`` node could be routed: ``verify``,
``judge_panel``, ``loop_until_dry``, ``gate`` and ``completeness_check`` always
ran on whatever model the session itself was on. "Run this whole DAG on
openrouter" was therefore inauthorable — the verify always fell back home — which
is exactly what the dogfood hit.

What these tests pin:

- the four fields are ACCEPTED on the five types (one point of truth: NODE_SPECS)
  and ``tier`` keeps its closed-set refusal for free (schema.py is untouched);
- ONE resolution per NODE, applied to EVERY leaf that node spawns — all the
  skeptics, the attempts AND their judges AND the synthesis, every round of the
  loop, a gate's draft AND its reviewer;
- a provider the harness cannot build nulls that node with a named fault and
  spawns nothing at all (fail-isolation), the same way an ``agent`` node does;
- a node that declares NO routing field keeps its PRE-slice cell hash — the cache
  is persisted, so re-keying it would turn every resume into a silent re-bill;
- the authoring guidance says all of the above (the skill is the only thing an
  authoring agent reads).
"""

import json
from pathlib import Path

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.skills.store import SkillStore, builtin_root
from lohra.state import SessionDB
from lohra.workflow import strategies
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.nodes import WorkflowSpec, gate_attempts
from lohra.workflow.prompts import branch_prompt, strict_prompt
from lohra.workflow.schema import ValidationError, validate_spec
from lohra.workflow.service import SUPPORTED_NODE_TYPES
from lohra.workflow.tiers import Tier, TierMap
from lohra.workflow.tools import RUN_GUIDANCE
from tests.test_loop import _text_response
from tests.test_workflow_m7_features import DEFAULT_MODEL, _core
from tests.test_workflow_pipeline import ScriptedClient

SKILL_NAME = "workflow-authoring"


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture
def skill_body() -> str:
    store = SkillStore(Path("/nonexistent-home"), extra_roots=(), builtin_roots=(builtin_root(),))
    skill = store.get(SKILL_NAME)
    assert skill is not None, "the builtin workflow-authoring skill must ship in the package"
    return skill.body


def _rigor_responder(prompt: str) -> str:
    """Answers whatever shape the node under test forces, so every node type runs
    WHOLE (and therefore lands its cache row)."""
    if '"refuted"' in prompt:
        return json.dumps({"refuted": False, "reason": "it holds"})
    if '"score"' in prompt:
        return json.dumps({"score": 9, "rationale": "good"})
    if '"ok"' in prompt:
        return json.dumps({"ok": True, "feedback": ""})
    if '"complete"' in prompt:
        return json.dumps({"complete": True, "missing": []})
    return "R"


# The five rigor nodes, each shaped so nothing dies under ``_rigor_responder``.
_NODES: dict[str, dict] = {
    "verify": {"type": "verify", "finding": "the sky is blue", "skeptics": 2},
    "judge_panel": {
        "type": "judge_panel",
        "attempts": ["Draft it one way.", "Draft it another way."],
        "judges": 1,
        "synthesize": {"prompt": "Rewrite the winner."},
    },
    "loop_until_dry": {
        "type": "loop_until_dry",
        "body": {"prompt": "Harvest one more."},
        "stop_after_k_empty": 1,
        "max_rounds": 2,
    },
    "gate": {
        "type": "gate",
        "body": {"prompt": "Draft the plan."},
        "validator": "Is the plan complete?",
    },
    "completeness_check": {
        "type": "completeness_check",
        "task": "Ship it.",
        "results": "Half of it.",
    },
}

# Every leaf each node spawns when nothing dies: the whole fan-out the node's
# routing has to cover (2 skeptics; 2 attempts + 2 judges + 1 synthesis; 2 rounds;
# draft + reviewer; 1 critic).
_LEAVES = {"verify": 2, "judge_panel": 5, "loop_until_dry": 2, "gate": 2, "completeness_check": 1}

RIGOR_TYPES = tuple(_NODES)


def _spec(node_type: str, fields: dict | None = None):
    return validate_spec(
        {
            "meta": {"name": "rigor", "version": 1},
            "nodes": [{"id": "n", **_NODES[node_type], **(fields or {})}],
        },
        supported_types=SUPPORTED_NODE_TYPES,
    )


def _run(db, spec, *, seen=None, tiers=None, cache=None):
    core = _core(db, _rigor_responder, seen=seen)
    try:
        return WorkflowEngine(core, budget=Budget(), tiers=tiers, cache=cache).run(spec, {})
    finally:
        core.shutdown()


# --- (a) the fields are authorable, and `tier` stays a closed set ----------


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_every_rigor_node_accepts_the_four_routing_fields(node_type):
    spec = _spec(node_type, {"model": "m", "effort": "high", "provider": "anthropic"})
    assert isinstance(spec, WorkflowSpec)


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_a_tier_outside_the_closed_set_is_refused_on_a_rigor_node(node_type):
    # Free of charge: `_validate_tier` already runs for any node whose fields
    # declare `tier` — declaring the field in NODE_SPECS is the whole change.
    result = _spec(node_type, {"tier": "huge"})
    assert isinstance(result, ValidationError)
    assert "tier" in result.message and "big" in result.message


# --- (b) one resolution per node, every leaf routed ------------------------


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_an_explicit_model_reaches_every_leaf_of_a_rigor_node(db, node_type):
    seen: list[str] = []
    result = _run(db, _spec(node_type, {"model": "explicit-model"}), seen=seen)
    assert result.faults == []
    assert seen == ["explicit-model"] * _LEAVES[node_type]


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_a_tier_routes_every_leaf_of_a_rigor_node(db, node_type):
    seen: list[str] = []
    result = _run(
        db,
        _spec(node_type, {"tier": "big"}),
        seen=seen,
        tiers=TierMap({"big": Tier(model="big-model")}),
    )
    assert result.faults == []
    assert seen == ["big-model"] * _LEAVES[node_type]


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_an_unmapped_tier_warns_once_and_runs_on_the_default(db, node_type):
    """Never silent, never fatal — and the warning is the NODE's, not one per
    leaf: a panel of five would otherwise shout the operator's typo five times."""
    seen: list[str] = []
    result = _run(db, _spec(node_type, {"tier": "small"}), seen=seen)
    assert seen == [DEFAULT_MODEL] * _LEAVES[node_type]
    assert len([f for f in result.faults if "workflow_tiers.json" in f]) == 1


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_an_unmapped_tier_is_still_reported_on_a_cache_hit(db, node_type):
    """The routing is resolved BEFORE the cache lookup, so the warning survives a
    replay: it is about the SPEC the operator has to fix, not about the spawn. A
    resume that says nothing would read as "the tier is mapped now"."""
    seen: list[str] = []
    core = _core(db, _rigor_responder, seen=seen)
    cache = NodeCache(db, "run-unmapped")
    try:
        spec = _spec(node_type, {"tier": "small"})
        runs = [
            WorkflowEngine(core, budget=Budget(), cache=cache).run(spec, {}) for _ in range(2)
        ]
    finally:
        core.shutdown()
    assert seen == [DEFAULT_MODEL] * _LEAVES[node_type]  # the second run really replayed
    for result in runs:
        assert len([f for f in result.faults if "workflow_tiers.json" in f]) == 1


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_an_unavailable_provider_nulls_the_rigor_node_without_spawning(db, node_type):
    """Fail-isolation, identical to an ``agent`` node: no pool here, so any
    cross-provider override is unbuildable. The node drops to null with the fault
    the skill tells the author to look for, and NOTHING is spawned on it."""
    seen: list[str] = []
    result = _run(db, _spec(node_type, {"provider": "ghost"}), seen=seen)
    assert result.outputs["n"] is None
    assert seen == []
    assert any(f.startswith("n: provider unavailable:") for f in result.faults)


def test_a_judge_panel_routes_attempts_judges_and_synthesis_alike(db):
    """The widest node there is: one routing for all THREE groups. Different
    models per group is a documented non-goal, so this is the pin."""
    seen: list[tuple[str, str]] = []
    core = _pair_core(db, _rigor_responder, seen)
    try:
        WorkflowEngine(core, budget=Budget()).run(
            _spec("judge_panel", {"model": "explicit-model"}), {}
        )
    finally:
        core.shutdown()
    groups = {
        "attempts": [m for m, p in seen if "Draft it" in p],
        "judges": [m for m, p in seen if "ATTEMPT:" in p],
        "synthesis": [m for m, p in seen if "WINNER:" in p],
    }
    assert [len(models) for models in groups.values()] == [2, 2, 1]
    for models in groups.values():
        assert set(models) == {"explicit-model"}


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_the_effort_of_a_rigor_node_lands_on_every_leaf(db, node_type):
    """``effort`` is invisible to the wire on this provider, so it is pinned on
    the Agent the core built — otherwise a silently dropped field looks green.
    Every type, not just one: a field that never reaches the leaf is exactly the
    "declared but ignored" footgun the multi-model slice already paid for."""
    core, built = _recording_core(db, _rigor_responder)
    try:
        WorkflowEngine(core, budget=Budget()).run(_spec(node_type, {"effort": "high"}), {})
    finally:
        core.shutdown()
    assert [agent.effort for agent in built] == ["high"] * _LEAVES[node_type]


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_a_buildable_provider_moves_every_leaf_of_a_rigor_node(db, node_type):
    """The half of the provider story the unavailable-path test cannot show: a
    provider the pool CAN resolve really moves the work. Both directions are the
    assertion — every leaf served by the foreign client under the foreign slug,
    and the session's own client serving none of them (an override that merely
    renamed the model while still calling home would pass a one-sided check)."""
    seen: list[tuple[str, str | None]] = []
    core = _tagged_core(db, _rigor_responder, seen)
    away = _TaggedSpy(_rigor_responder, seen, "away", shape=_openai_shaped)
    pool = _OneProviderPool(get_provider_profile("openai"), away)
    try:
        result = WorkflowEngine(core, budget=Budget(), client_pool=pool).run(
            _spec(node_type, {"provider": "openai", "model": "gpt-x"}), {}
        )
    finally:
        core.shutdown()
    assert result.faults == []
    assert seen == [("away", "gpt-x")] * _LEAVES[node_type]


# --- (c) backward compatibility of the persisted cache --------------------


def _legacy_hash(engine, node) -> str:
    """The cell hash EXACTLY as the pre-slice build computed it. A node that
    declares no routing field must still HIT on this key: the cache is persisted
    and run-scoped, so a trailing ``(None, None, None)`` would re-key every cell
    written before the feature existed and re-bill it on the next resume."""
    fields = node.fields
    if node.type == "verify":
        return engine.cell_hash(
            node.id,
            "verify",
            strict_prompt(engine, node.id, fields.get("finding"), {}),
            max(1, int(fields.get("skeptics", 3))),
            fields.get("lenses") or [],
            bool(fields.get("kill_if_majority_refute", True)),
        )
    if node.type == "judge_panel":
        return engine.cell_hash(
            node.id,
            "judge_panel",
            strategies._leaf_prompts(engine, node, "attempts", {}),
            max(1, int(fields.get("judges", 1))),
            fields.get("synthesize"),
        )
    if node.type == "loop_until_dry":
        body = fields.get("body") or {}
        return engine.cell_hash(
            node.id,
            "loop_until_dry",
            strict_prompt(engine, node.id, branch_prompt(body), {"round": 0, "so_far": []}),
            body.get("schema") if isinstance(body, dict) else None,
            max(1, int(fields.get("stop_after_k_empty", 1))),
            max(1, int(fields.get("max_rounds", 3))),
        )
    if node.type == "gate":
        body = fields.get("body") or {}
        return engine.cell_hash(
            node.id,
            "gate",
            strict_prompt(engine, node.id, branch_prompt(body), {}),
            engine.resolve_schema(body) if isinstance(body, dict) else None,
            strict_prompt(engine, node.id, fields.get("validator", ""), {}),
            gate_attempts(fields),
        )
    return engine.cell_hash(
        node.id,
        "completeness_check",
        strict_prompt(engine, node.id, fields.get("task", ""), {}),
        strict_prompt(engine, node.id, fields.get("results", ""), {}),
    )


# The digest each routing-less cell above had BEFORE this slice, captured by
# running the same five specs against a checkout of the pre-slice build (git
# e0f2ff4) and instrumenting the real ``WorkflowEngine.cell_hash`` — not by
# copying what production returns today. ``_legacy_hash`` alone cannot pin this:
# it is a shadow of production, so a change made in both at once leaves the test
# green and silently re-keys every persisted row. A constant cannot drift.
#
# It DOES break if ``_NODES`` above is edited — that is the point. Re-anchor
# deliberately (print the digest from a pre-slice checkout of the new fixture),
# never by pasting whatever the current build produces.
_PRE_SLICE_HASHES = {
    "verify": "13f2f8ccbb83999bb0110c09faca7b2719fc28d8577b2edf5a2857c140b9a1a0",
    "judge_panel": "ec10a1cea9a04391f164ab5ffed7911e94f971675fbde04974d4edfa8ce11e55",
    "loop_until_dry": "b5582d66c1f47baa74cfae5db782851c72abfcda8c3a158876fbe58ded1794c0",
    "gate": "1c6de405506cdca874c06a33b0eeea69cc04a4141544a75c8cc1e6f17f098f33",
    "completeness_check": "f5f41607ddfe8c83c57fdccf5e6f475f8b5701b6eef8fac08701e99c826b531b",
}


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_a_routing_less_rigor_cell_keeps_its_pre_slice_hash(db, node_type):
    run_id = f"run-legacy-{node_type}"
    core = _core(db, _rigor_responder)
    try:
        spec = _spec(node_type)  # declares none of the four routing fields
        engine = WorkflowEngine(core, budget=Budget(), cache=NodeCache(db, run_id))
        assert engine.run(spec, {}).faults == []
        legacy = _legacy_hash(engine, spec.nodes[0])
        assert legacy == _PRE_SLICE_HASHES[node_type]  # the shadow itself has not drifted
        assert db.cache_get(run_id, legacy) is not None  # ...and production still writes that key
    finally:
        core.shutdown()


@pytest.mark.parametrize("node_type", RIGOR_TYPES)
def test_a_rerouted_rigor_node_is_a_different_cell(db, node_type):
    """The other half of the rule: once the node DOES declare routing, changing
    what it resolves to must re-spawn instead of replaying an answer another
    model gave."""
    seen: list[str] = []
    core = _core(db, _rigor_responder, seen=seen)
    cache = NodeCache(db, "run-rerouted")
    try:
        spec = _spec(node_type, {"tier": "big"})
        for slug in ("first-model", "second-model"):
            WorkflowEngine(
                core, budget=Budget(), cache=cache, tiers=TierMap({"big": Tier(model=slug)})
            ).run(spec, {})
        assert seen == ["first-model"] * _LEAVES[node_type] + ["second-model"] * _LEAVES[node_type]
    finally:
        core.shutdown()


# --- (d) the guidance an authoring agent actually reads --------------------

_RIGOR_KNOB_BULLET = "- **Rigor nodes take the same routing knobs**"


def test_the_skill_no_longer_says_the_rigor_nodes_take_no_model_knobs(skill_body):
    assert "they take no model knobs at all" not in skill_body


def _knob_bullet(skill_body: str) -> str:
    start = skill_body.index(_RIGOR_KNOB_BULLET)
    return skill_body[start : skill_body.index("\n- ", start + 1)]


def test_the_skill_names_every_rigor_node_that_takes_the_routing_knobs(skill_body):
    bullet = _knob_bullet(skill_body)
    for node_type in RIGOR_TYPES:
        assert f"`{node_type}`" in bullet


def test_the_skill_names_every_surface_that_does_not_take_the_routing_knobs(skill_body):
    """``parallel`` spawns its branches straight onto the session's model and has
    no routing field of its own — exactly like a pipeline `stages`. Naming only
    `stages` let a reader infer that the OTHER fan-out node routes, which is the
    node with the widest fan-out there is."""
    bullet = _knob_bullet(skill_body)
    for unroutable in ("`parallel`", "`stages`"):
        assert unroutable in bullet


def test_the_skill_says_a_nested_routing_knob_is_ignored_in_silence(skill_body):
    """The footgun this slice CREATED: before it, a `model` on a gate meant an
    `unknown_field` refusal, so the author learned. Now the node accepts it and
    the only remaining wrong place — inside the agent-shaped `body`/`synthesize`
    /`branches`/`stages` dicts — is accepted by the validator and then dropped
    without a fault. Telling the author it is merely 'not supported' is not
    enough: they have to know the run will look green and bill the session model."""
    bullet = _knob_bullet(skill_body)
    assert "silently ignored" in bullet
    for nested in ("`body`", "`synthesize`", "`branches`", "`stages`"):
        assert nested in bullet


def test_the_skill_does_not_promise_an_unmapped_tier_is_free(skill_body):
    """It is not "a warning in the rollup": `_leaf_config` records a FAULT, and
    any fault makes `_derive_status` report `degraded`, which makes `library`
    write a prior instead of certifying the spec as a template. A spec author
    reading the old sentence would leave a typo'd tier in a template candidate."""
    assert "not a failure" not in skill_body
    start = skill_body.index("- **`tier`**")
    bullet = skill_body[start : skill_body.index("\n- ", start + 1)]
    assert "faults" in bullet and "degraded" in bullet


def test_the_skill_does_not_scope_mixed_providers_to_agent_nodes(skill_body):
    """The paragraph that answers "run this whole DAG on openrouter" — the very
    question this slice exists for. Scoped to `agent` nodes it says the opposite
    of what the code now does."""
    start = skill_body.index("Providers can be MIXED")
    paragraph = skill_body[start : skill_body.index("\n\n", start)]
    assert "each `agent` node" not in paragraph
    assert "rigor" in paragraph


def test_run_guidance_scopes_the_routing_knobs_to_the_rigor_nodes_too():
    line = next(line for line in RUN_GUIDANCE.split("\n") if "portable 'tier'" in line)
    for node_type in RIGOR_TYPES:
        assert node_type in line
    assert "every leaf" in line


def test_run_guidance_also_names_the_silence_of_a_nested_routing_knob():
    """The tool description is what an agent reads on EVERY run_workflow call —
    the skill is the deeper reference it may not have opened."""
    assert "used to be silently ignored" in RUN_GUIDANCE
    assert "REFUSED at validation" in RUN_GUIDANCE


# --- test-only cores -------------------------------------------------------


class _PairSpy(ScriptedClient):
    """Records ``(model, prompt)`` per turn as ONE append — a concurrent fan-out
    must not be able to mis-pair a model with another leaf's prompt."""

    def __init__(self, responder, seen):
        super().__init__(responder)
        self._seen = seen

    def create(self, **kwargs):
        self._seen.append((kwargs.get("model"), self._prompt(kwargs)))
        return super().create(**kwargs)


def _pair_core(db, responder, seen):
    def factory():
        return Agent(
            model=DEFAULT_MODEL,
            provider=get_provider_profile("anthropic"),
            client=_PairSpy(responder, seen),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _openai_shaped(text: str) -> dict:
    """One assistant turn as the ``chat_completions`` transport parses it.

    ``_text_response`` is anthropic-shaped: a leaf that really moved to the
    OpenAI profile reads its answer through the other transport, so answering it
    in the home shape would look like an empty leaf, not like a routed one."""
    return {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3},
    }


class _TaggedSpy(ScriptedClient):
    """Records ``(tag, model)`` per turn, so a leaf can be attributed to the
    CLIENT that served it — a cross-provider swap is a client swap, not a slug."""

    def __init__(self, responder, seen, tag, shape=_text_response):
        super().__init__(responder)
        self._seen = seen
        self._tag = tag
        self._shape = shape

    def create(self, **kwargs):
        self._seen.append((self._tag, kwargs.get("model")))
        return self._shape(self._responder(self._prompt(kwargs)))


class _OneProviderPool:
    """The ``ClientPool`` surface a node actually uses: ``get(name)`` ->
    (profile, client). A real pool needs a credential for the target provider;
    what a rigor node depends on is only the pair it hands back."""

    def __init__(self, profile, client):
        self._pair = (profile, client)

    def get(self, name):
        return self._pair


def _tagged_core(db, responder, seen):
    """A core whose leaves call HOME unless something swaps their client."""

    def factory():
        return Agent(
            model=DEFAULT_MODEL,
            provider=get_provider_profile("anthropic"),
            client=_TaggedSpy(responder, seen, "home"),
        )

    return OrchestrationCore(db, factory, max_concurrent=4)


def _recording_core(db, responder):
    """A core that hands back every Agent it built (to inspect its knobs)."""
    built: list[Agent] = []

    def factory():
        agent = Agent(
            model=DEFAULT_MODEL,
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )
        built.append(agent)
        return agent

    return OrchestrationCore(db, factory, max_concurrent=4), built
