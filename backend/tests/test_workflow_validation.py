"""Tests for schema-forced output: validate + steer-retry (Fase 8, Milestone C)."""


import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
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
