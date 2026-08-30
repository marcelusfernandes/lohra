"""Tests for schema-forced output: validate + steer-retry (Fase 8, Milestone C)."""


import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.steering import SteeringLimits
from lohra.workflow.validation import correction_prompt, parse_and_validate
from tests.test_loop import FakeClient, _text_response

_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


# --- parse_and_validate (unit) ---


def test_parse_and_validate_accepts_matching():
    ok, parsed, _ = parse_and_validate('{"n": 5}', _SCHEMA)
    assert ok and parsed == {"n": 5}


def test_parse_and_validate_rejects_non_json():
    ok, _, err = parse_and_validate("not json", _SCHEMA)
    assert not ok and "JSON" in err


def test_parse_and_validate_rejects_schema_mismatch():
    ok, _, err = parse_and_validate('{"n": "text"}', _SCHEMA)
    assert not ok and err


def test_correction_prompt_includes_schema_and_error():
    msg = correction_prompt(_SCHEMA, "n: not an integer")
    assert "schema" in msg.lower() and "n: not an integer" in msg


def test_parse_extracts_json_from_markdown_fence():
    # the live bug: model wraps JSON in ```json ... ``` + prose
    fenced = 'Here are the claims:\n```json\n{"n": 9}\n```\nHope that helps!'
    ok, parsed, _ = parse_and_validate(fenced, _SCHEMA)
    assert ok and parsed == {"n": 9}


def test_parse_extracts_json_from_surrounding_prose():
    prosey = 'Sure! {"n": 3} — let me know if you need more.'
    ok, parsed, _ = parse_and_validate(prosey, _SCHEMA)
    assert ok and parsed == {"n": 3}


def test_parse_extracts_array_from_fence():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    ok, parsed, _ = parse_and_validate('```\n{"x": "hi"}\n```', schema)
    assert ok and parsed == {"x": "hi"}


# --- engine validate + steer-retry (integration) ---


def _core_with_replies(db, replies):
    """One leaf whose client returns `replies` across turns (turn 1, then steers)."""

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(r) for r in replies]),
        )

    return OrchestrationCore(db, factory)


def _run(core, node_extra=None):
    node = {"id": "a", "type": "agent", "prompt": "produce n", "schema": _SCHEMA}
    node.update(node_extra or {})
    spec = validate_spec({"meta": {"name": "v"}, "nodes": [node]})
    return WorkflowEngine(core, budget=Budget()).run(spec, {})


def test_matching_output_passes_through_typed(db):
    core = _core_with_replies(db, ['{"n": 7}'])
    try:
        result = _run(core)
        assert result.outputs["a"] == {"n": 7}  # typed object, not the raw string
    finally:
        core.shutdown()


def test_mismatch_is_steered_then_succeeds(db):
    core = _core_with_replies(db, ['{"n": "bad"}', '{"n": 42}'])  # turn1 bad, after steer good
    try:
        result = _run(core)
        assert result.outputs["a"] == {"n": 42}
        assert result.validation_retries == 1
    finally:
        core.shutdown()


def test_persistent_mismatch_exhausts_retries_to_null(db):
    core = _core_with_replies(db, ['bad'] * 5)  # never valid
    try:
        result = _run(core)
        assert result.outputs["a"] is None
        assert result.null_count == 1
    finally:
        core.shutdown()


def test_schema_ref_resolves_from_spec_schemas(db):
    core = _core_with_replies(db, ['{"n": 3}'])
    spec = validate_spec(
        {
            "meta": {"name": "v"},
            "schemas": {"COUNT": _SCHEMA},
            "nodes": [{"id": "a", "type": "agent", "prompt": "n", "schema_ref": "COUNT"}],
        }
    )
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["a"] == {"n": 3}
    finally:
        core.shutdown()


def test_fenced_json_leaf_validates_through_engine(db):
    # The live bug: gen wrapped its JSON in ```json fences``` -> used to null out
    # and the downstream verify got finding=null. Now it extracts + validates.
    core = _core_with_replies(db, ['Here you go:\n```json\n{"n": 11}\n```'])
    try:
        result = _run(core)
        assert result.outputs["a"] == {"n": 11}  # not None
        assert result.null_count == 0
    finally:
        core.shutdown()


def test_no_schema_passes_text_through(db):
    core = _core_with_replies(db, ["just text"])
    spec = validate_spec({"meta": {"name": "v"}, "nodes": [{"id": "a", "type": "agent", "prompt": "x"}]})
    try:
        result = WorkflowEngine(core, budget=Budget()).run(spec, {})
        assert result.outputs["a"] == "just text"
    finally:
        core.shutdown()




# --- integration: internal correction fixes gated by SteeringLimits ---
#
# The engine's schema-retry loop steers the leaf with a correction_prompt on
# every failed validation attempt. Each fix is an internal steer, so it must
# reserve from SteeringLimits BEFORE steering: accepted -> steer as before;
# refused -> a stable fault naming the current node, 'steering correction
# limit exhausted' and the refusing reason, then return None WITHOUT
# steering (fail-closed; never a silent drop).
#
# Determinism: the scripted leaf answers INVALID on every turn, so every
# correction is provably wasted; the "prior external reservation" case is
# pure accounting (reserve_external on the leaf's sub_id, taken inside the
# spawn wrapper the moment the engine creates it) — no external steer text is
# ever injected, so no race can mask a refusal. A surviving sibling node keeps
# the run-level verdict honest ('degraded', one null of two nodes, not the
# 'failed' a single-nulled run would be).


REFUSAL_TEXT = "steering correction limit exhausted"


class _CountingCore(OrchestrationCore):
    """Counts REAL steer calls (any source) without changing behavior."""

    def __init__(self, db, factory):
        super().__init__(db, factory)
        self.steer_total = 0
        self.steer_texts: list[str] = []

    def steer(self, sub_id, text, *args, **kwargs):
        self.steer_total += 1
        self.steer_texts.append(str(text))
        return super().steer(sub_id, text, *args, **kwargs)


class _ReservingCore(_CountingCore):
    """Takes ONE external reservation on the FIRST leaf the engine spawns —
    the moment it is created, i.e. strictly before any internal correction
    can be reserved for it (the leaf is still on its first turn)."""

    def __init__(self, db, factory, limits):
        super().__init__(db, factory)
        self._limits = limits
        self.reserved: str | None = None

    def spawn(self, prompt, **kwargs):
        sub_id = super().spawn(prompt, **kwargs)
        if self.reserved is None:
            self.reserved = sub_id
            assert self._limits.reserve_external(sub_id).accepted is True
        return sub_id


def _never_valid_core(db, turns=2, limits=None):
    """A leaf that answers bad JSON on every turn (the correction never
    lands). `turns` = 1 first turn + one per steered correction."""

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response('{"n": "bad"}')] * turns),
        )

    cls = _ReservingCore if limits is not None else _CountingCore
    core = cls(db, factory) if limits is None else cls(db, factory, limits)
    return core


def _spec_with_surviving_sibling(name="sg"):
    return validate_spec(
        {
            "meta": {"name": name},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "produce n", "schema": _SCHEMA},
                {"id": "b", "type": "agent", "prompt": "hi"},
            ],
        }
    )


def _assert_refusal_fault(result, limits, engine):
    assert engine.steering_limits is limits  # exposed by property
    refusals = [f for f in result.faults if REFUSAL_TEXT in f]
    assert refusals, result.faults
    assert refusals[0].startswith("a:")  # names the current node
    assert "correction_limit" in refusals[0]  # the refusing reason


def test_engine_defaults_steering_limits_and_keeps_steering(db):
    # Fresh engine -> a default SteeringLimits; the default budget of 2
    # corrections/leaf still lets both internal corrections through, so
    # behavior is unchanged from the ungated loop.
    core = _never_valid_core(db, turns=3)  # turn 1 + two corrections
    try:
        engine = WorkflowEngine(core, budget=Budget())
        assert isinstance(engine.steering_limits, SteeringLimits)
        result = engine.run(_spec_with_surviving_sibling(), {})
        assert result.outputs["a"] is None
        assert result.null_count == 1
        assert result.status == "degraded"  # sibling b survived
        assert result.validation_retries == 2
        assert core.steer_total == 2  # both corrections steered
        assert not [f for f in result.faults if REFUSAL_TEXT in f]
    finally:
        core.shutdown()


def test_custom_limit_one_proves_single_internal_steer_then_refusal(db):
    # max_corrections_per_leaf=1, no external steer: the FIRST internal
    # correction is accepted and steered, the SECOND is refused -> None.
    core = _never_valid_core(db)
    try:
        limits = SteeringLimits(max_corrections_per_leaf=1)
        engine = WorkflowEngine(core, budget=Budget(), steering_limits=limits)
        result = engine.run(_spec_with_surviving_sibling("sg1"), {})
        assert result.outputs["a"] is None
        assert result.null_count == 1
        assert result.status == "degraded"
        assert result.validation_retries == 1  # exactly ONE retry happened
        assert core.steer_total == 1  # exactly ONE internal steer
        _assert_refusal_fault(result, limits, engine)
    finally:
        core.shutdown()


def test_prior_external_reservation_leaves_only_one_internal(db):
    # Default limits (2 corrections/leaf): ONE external slot already taken on
    # the leaf (accounting only) leaves exactly one internal correction; the
    # second is refused. External and internal draw from the same pool.
    limits = SteeringLimits()
    core = _never_valid_core(db, limits=limits)
    try:
        engine = WorkflowEngine(core, budget=Budget(), steering_limits=limits)
        result = engine.run(_spec_with_surviving_sibling("sg2"), {})
        assert core.reserved is not None  # the reservation hit a real leaf
        assert result.outputs["a"] is None
        assert result.status == "degraded"
        assert result.validation_retries == 1  # the one internal slot left
        assert core.steer_total == 1  # that one internal steer ran
        _assert_refusal_fault(result, limits, engine)
    finally:
        core.shutdown()
