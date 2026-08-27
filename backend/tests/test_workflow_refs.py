"""Tests for single-pass reference resolution (Fase 8 §2.3 — injection guard)."""

from lohra.workflow.refs import invalid_refs, is_valid_ref, resolve_value


def test_valid_paths():
    assert is_valid_ref("scan")
    assert is_valid_ref("scan.ids")
    assert is_valid_ref("triage.0.fix")
    assert is_valid_ref("args.dump")


def test_expression_like_is_invalid():
    for bad in ["a + b", "scan.ids()", "x ? y : z", "len(x)", "a-b", "'quoted'", "a .b"]:
        assert not is_valid_ref(bad), bad


def test_invalid_refs_collects_expressions():
    bad = invalid_refs("ok ${scan.ids} and bad ${a + b} and ${f()}")
    assert "a + b" in bad and "f()" in bad
    assert "scan.ids" not in bad


def test_whole_value_ref_preserves_type():
    context = {"scan": {"ids": ["b1", "b2"]}}
    assert resolve_value("${scan.ids}", context) == ["b1", "b2"]  # stays a list


def test_embedded_ref_stringifies():
    context = {"args": {"dump": "logs"}}
    assert resolve_value("read ${args.dump} now", context) == "read logs now"


def test_single_pass_does_not_rescan_substituted_value():
    # A leaf output containing ${...}-looking text must pass downstream VERBATIM,
    # never re-resolved (the second-order injection guard).
    context = {"leaf": "${args.secret}", "args": {"secret": "TOPSECRET"}}
    out = resolve_value("downstream: ${leaf}", context)
    assert out == "downstream: ${args.secret}"  # NOT "downstream: TOPSECRET"


def test_missing_path_resolves_to_none_or_empty():
    assert resolve_value("${nope.field}", {}) is None
    assert resolve_value("x ${nope} y", {}) == "x null y"


def test_nested_authored_structure_is_walked():
    context = {"scan": {"ids": ["a"]}}
    authored = {"items": "${scan.ids}", "label": "fixed"}
    assert resolve_value(authored, context) == {"items": ["a"], "label": "fixed"}


def test_field_access_descends_into_a_json_string_output():
    # A node with no schema returns fenced/raw JSON text; ${gen.claims} should
    # still extract the array (robustness for the live gen->verify case).
    context = {"gen": 'Here you go:\n```json\n{"claims": ["x", "y"]}\n```'}
    assert resolve_value("${gen.claims}", context) == ["x", "y"]


def test_whole_value_string_is_not_parsed():
    # ${gen} (no field) returns the raw string verbatim — parsing only on descent.
    raw = 'Here you go:\n```json\n{"claims": ["x"]}\n```'
    assert resolve_value("${gen}", {"gen": raw}) == raw
