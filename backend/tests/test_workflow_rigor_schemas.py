"""#87: named output schemas in synthesis/loop bodies, including resume identity."""

from copy import deepcopy

import pytest

from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache, content_hash
from lohra.workflow.cache_preview import preview_resume
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.nodes import WorkflowSpec
from lohra.workflow.schema import ValidationError, validate_spec
from tests.test_workflow_rigor import _core

SHAPES = [("judge_panel", "synthesize"), ("loop_until_dry", "body")]
RESULT = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _raw(kind, shape, fields, *, definition=RESULT):
    node = {"id": "work", "type": kind, shape: {"prompt": "Produce JSON", **fields}}
    if kind == "judge_panel":
        node.update(attempts=["Candidate"], judges=1)
    else:
        node.update(max_rounds=1, stop_after_k_empty=1)
    return {"meta": {"name": "rigor-schema", "version": 1},
            "schemas": {"RESULT": deepcopy(definition)}, "nodes": [node]}


def _validated(raw):
    spec = validate_spec(raw)
    assert isinstance(spec, WorkflowSpec), spec
    return spec


def _run(db, spec, replies, calls, *, cache=None):
    def responder(prompt):
        calls.append(prompt)
        if "Score this attempt" in prompt:
            return '{"score": 9}'
        if prompt == "Candidate":
            return "candidate output"
        return next(replies)

    core = _core(db, responder)
    try:
        return WorkflowEngine(core, budget=Budget(), cache=cache).run(spec, {})
    finally:
        core.shutdown()


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("key", ["schema", "schema_ref"])
def test_named_schema_validates_before_any_leaf(kind, shape, key):
    # The RED is the public validator, not a hand-built WorkflowSpec that bypasses it.
    _validated(_raw(kind, shape, {key: "RESULT"}))


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("key", ["schema", "schema_ref"])
@pytest.mark.parametrize("value", ["MISSING", None, True, 7, []])
def test_bad_schema_reference_is_didactic(kind, shape, key, value):
    result = validate_spec(_raw(kind, shape, {key: value}))
    assert isinstance(result, ValidationError)
    issue = next(i for i in result.issues if i.field == f"{shape}.{key}")
    assert issue.rule == ("schema_type" if key == "schema" else "schema_ref")
    assert issue.node_id == "work"
    assert "schema" in issue.message and issue.example


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
def test_schema_and_schema_ref_are_mutually_exclusive(kind, shape):
    result = validate_spec(_raw(kind, shape, {"schema": RESULT, "schema_ref": "RESULT"}))
    assert isinstance(result, ValidationError)
    assert any(i.rule == "schema_xor" and i.field == f"{shape}.schema_ref"
               for i in result.issues)


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
def test_schema_ref_cannot_be_an_inline_object(kind, shape):
    result = validate_spec(_raw(kind, shape, {"schema_ref": RESULT}))
    assert isinstance(result, ValidationError)
    assert any(i.rule == "schema_ref" and i.field == f"{shape}.schema_ref"
               for i in result.issues)


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("key", ["schema", "schema_ref"])
def test_builtin_and_empty_named_schemas_resolve(kind, shape, key):
    raw = _raw(kind, shape, {key: "artifact_manifest"})
    raw.pop("schemas")
    _validated(raw)
    _validated(_raw(kind, shape, {key: "RESULT"}, definition={}))


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("key", ["schema", "schema_ref"])
def test_named_schema_parses_and_corrects_output(db, kind, shape, key):
    spec = _validated(_raw(kind, shape, {key: "RESULT"}))
    calls = []
    result = _run(db, spec, iter(['{"answer": 7}', '{"answer": "ok"}']), calls)
    expected = {"answer": "ok"}
    assert result.outputs["work"] == ([expected] if kind == "loop_until_dry" else expected)
    assert result.validation_retries == 1  # the wrong typed answer was not accepted as prose
    assert result.faults == []


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("key", ["schema", "schema_ref"])
def test_named_schema_cache_replays_and_definition_change_invalidates(db, kind, shape, key):
    raw = _raw(kind, shape, {key: "RESULT"})
    untouched = deepcopy(raw)
    spec = _validated(raw)
    cache = NodeCache(db, "schema-run")
    calls = []
    first = _run(db, spec, iter(['{"answer": "first"}']), calls, cache=cache)
    assert first.status == "complete", first.faults
    assert first.validation_retries == 0
    first_calls = len(calls)
    assert first_calls == (3 if kind == "judge_panel" else 1)
    assert preview_resume(db, "schema-run", spec, {})["replay"] == 1
    replay = _run(db, spec, iter([]), calls, cache=cache)
    assert replay.outputs == first.outputs and len(calls) == first_calls
    assert raw == untouched  # hash normalization cannot rewrite the authored spec

    changed = deepcopy(raw)
    changed["schemas"]["RESULT"]["properties"]["answer"] = {"type": "integer"}
    adapted = _validated(changed)
    preview = preview_resume(db, "schema-run", adapted, {})
    assert preview["replay"] == 0 and preview["invalidate"] == 1
    fresh = _run(db, adapted, iter(['{"answer": 2}']), calls, cache=cache)
    expected = {"answer": 2}
    assert fresh.outputs["work"] == ([expected] if kind == "loop_until_dry" else expected)
    assert len(calls) == 2 * first_calls
    assert preview_resume(db, "schema-run", adapted, {})["replay"] == 1


@pytest.mark.parametrize(("kind", "shape"), SHAPES)
@pytest.mark.parametrize("fields", [{}, {"schema": RESULT}])
def test_inline_and_absent_schema_replay_legacy_hash(db, kind, shape, fields):
    raw = _raw(kind, shape, fields)
    spec = _validated(raw)
    entry = raw["nodes"][0][shape]
    # Frozen pre-#87 formulas: do not ask the current strategy what its key is.
    if kind == "judge_panel":
        legacy = content_hash("rigor-schema", 1, "work", kind, ["Candidate"], 1, entry)
    else:
        legacy = content_hash("rigor-schema", 1, "work", kind, "Produce JSON",
                              entry.get("schema"), 1, 1)
    cache = NodeCache(db, "legacy-run")
    saved = [{"answer": "cached"}] if kind == "loop_until_dry" else {"answer": "cached"}
    cache.put_complete(legacy, "work", saved)
    calls = []
    result = _run(db, spec, iter([]), calls, cache=cache)
    assert result.outputs["work"] == saved and calls == []
    assert preview_resume(db, "legacy-run", spec, {})["replay"] == 1
