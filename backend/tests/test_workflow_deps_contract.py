"""``depends_on`` contract (issue #2).

The hypothesis: a dependency declared with an unknown id, or a shape the
author did not mean (a string instead of a list, a non-string item), used to
be ACCEPTED by ``validate_spec`` and then silently DROPPED building the DAG
(``graph.dependencies`` / ``schema._detect_cycles`` both filter with
``d in node_ids`` and ``isinstance(d, str)``, which drops instead of
complaining) — so the edge the author asked for just vanished and the run
order changed with no signal at all.

Each case below is checked against the REAL validator, and — for the shapes
that still validate — against the REAL graph functions the engine schedules
on, so a "looks fine" verdict here is never just an assumption about what
``graph.py`` does with it.
"""

from lohra.workflow.graph import dependencies, topological_order
from lohra.workflow.nodes import WorkflowSpec
from lohra.workflow.schema import ValidationError, validate_spec

_BASE = {
    "meta": {"name": "x"},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "go"},
    ],
}


def _spec_with(b_extra: dict) -> dict:
    return {
        "meta": {"name": "x"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "go", **b_extra},
        ],
    }


# --- (a) unknown id — CONFIRMED: was silently accepted+dropped -------------


def test_unknown_dependency_id_is_rejected():
    out = validate_spec(_spec_with({"depends_on": ["ghost"]}))
    assert isinstance(out, ValidationError)
    assert any(i.rule == "depends_on_target" and i.node_id == "b" for i in out.issues)
    assert "ghost" in out.message


# --- (b) string instead of a list — CONFIRMED: silently ignored ------------


def test_string_depends_on_is_rejected_not_silently_ignored():
    # Before the fix: isinstance(explicit, list) was False everywhere the
    # value is read, so this dependency vanished entirely — proven below by
    # showing 'b' used to schedule BEFORE 'a' despite declaring depends_on: "a".
    out = validate_spec(_spec_with({"depends_on": "a"}))
    assert isinstance(out, ValidationError)
    assert any(i.rule == "depends_on_type" and i.node_id == "b" for i in out.issues)


def test_string_depends_on_would_have_reordered_the_run_undetected():
    # Pin the CONSEQUENCE that made (b) worth rejecting: a plain string, if it
    # reached the graph functions unrejected, silently drops the edge and 'b'
    # is free to schedule before 'a' — the exact silent-reorder issue #2 named.
    from lohra.workflow.nodes import Node

    ids = {"a", "b"}
    node_b = Node(id="b", type="agent", fields={"prompt": "go", "depends_on": "a"})
    assert dependencies(node_b, ids) == set()  # the string is silently dropped here


# --- (c) non-string item — CONFIRMED: silently dropped ---------------------


def test_nonstring_depends_on_item_is_rejected():
    out = validate_spec(_spec_with({"depends_on": [1]}))
    assert isinstance(out, ValidationError)
    assert any(i.rule == "depends_on_type" and i.node_id == "b" for i in out.issues)


def test_mixed_valid_and_nonstring_items_rejects_on_the_bad_one_only():
    out = validate_spec(_spec_with({"depends_on": ["a", 1]}))
    assert isinstance(out, ValidationError)
    rules = [i.rule for i in out.issues]
    assert "depends_on_type" in rules
    assert "depends_on_target" not in rules  # "a" itself is a real, known id


def test_null_depends_on_is_rejected_with_a_plain_english_message():
    # A 7th shape beyond the brief's six, found while implementing: an
    # explicit `depends_on: null` (empty YAML value) used to fall through
    # `or []` in _detect_cycles/graph.dependencies as "no deps" — now
    # rejected like every other bad shape, with a message that doesn't leak
    # "NoneType" at the author.
    out = validate_spec(_spec_with({"depends_on": None}))
    assert isinstance(out, ValidationError)
    issue = next(i for i in out.issues if i.rule == "depends_on_type")
    assert "NoneType" not in issue.message
    assert "omit the field" in issue.message


# --- (d) self-dependency — REFUTADA: already rejected (cycle detector) -----


def test_self_dependency_is_already_rejected_by_the_cycle_detector():
    out = validate_spec(
        {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go", "depends_on": ["a"]},
                {"id": "b", "type": "agent", "prompt": "go"},
            ],
        }
    )
    assert isinstance(out, ValidationError)
    assert any(i.rule == "cycle" for i in out.issues)


# --- (e) duplicates — REFUTADA: harmless, correctly de-duplicated ----------


def test_duplicate_dependency_ids_validate_and_deduplicate_correctly():
    spec = validate_spec(_spec_with({"depends_on": ["a", "a"]}))
    assert isinstance(spec, WorkflowSpec)
    node_b = spec.node("b")
    ids = {n.id for n in spec.nodes}
    assert dependencies(node_b, ids) == {"a"}  # one edge, not two, not zero
    assert [n.id for n in topological_order(spec)] == ["a", "b"]


# --- (f) depends_on redundant with a ${ref} to the same node — REFUTADA ----


def test_depends_on_redundant_with_a_ref_to_the_same_node_is_fine():
    spec = validate_spec(_spec_with({"prompt": "use ${a}", "depends_on": ["a"]}))
    assert isinstance(spec, WorkflowSpec)
    node_b = spec.node("b")
    ids = {n.id for n in spec.nodes}
    assert dependencies(node_b, ids) == {"a"}
    assert [n.id for n in topological_order(spec)] == ["a", "b"]


# --- didactic shape: every rejection carries a corrected example -----------


def test_depends_on_rejections_carry_a_corrected_example():
    for bad in ("a", [1], ["ghost"]):
        out = validate_spec(_spec_with({"depends_on": bad}))
        assert isinstance(out, ValidationError), bad
        issue = next(i for i in out.issues if i.field == "depends_on")
        assert issue.example, f"no example for depends_on={bad!r}"
