"""Tests for the workflow spec model + validator (Fase 8, Milestone A)."""

from lohra.workflow.nodes import WorkflowSpec
from lohra.workflow.schema import ValidationError, validate_spec


def _valid_spec() -> dict:
    return {
        "meta": {"name": "triage", "description": "find + verify bugs", "version": 1},
        "inputs": {"type": "object", "properties": {"dump": {"type": "string"}}},
        "schemas": {"VERDICT": {"type": "object", "properties": {"ok": {"type": "boolean"}}}},
        "nodes": [
            {"id": "scan", "type": "agent", "prompt": "list bug ids from ${args.dump}",
             "schema": {"type": "object", "properties": {"ids": {"type": "array"}}}},
            {"id": "triage", "type": "pipeline", "items": "${scan.ids}",
             "stages": [{"type": "agent", "prompt": "refute ${item}", "schema_ref": "VERDICT"}]},
            {"id": "report", "type": "agent", "depends_on": ["triage"],
             "prompt": "synthesize ${triage}"},
        ],
    }


def test_valid_spec_parses():
    result = validate_spec(_valid_spec())
    assert isinstance(result, WorkflowSpec)
    assert result.name == "triage"
    assert {n.id for n in result.nodes} == {"scan", "triage", "report"}


def test_unknown_node_type_rejected():
    spec = _valid_spec()
    spec["nodes"].append({"id": "weird", "type": "summon_daemon", "prompt": "x"})
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "node_type" for i in result.issues)


def test_expression_like_reference_rejected():
    spec = _valid_spec()
    spec["nodes"][0]["prompt"] = "compute ${a + b} and ${scan.ids()}"
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "ref_expression" for i in result.issues)


def test_unknown_reference_target_rejected():
    spec = _valid_spec()
    spec["nodes"][0]["prompt"] = "use ${ghost.field}"
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "ref_target" for i in result.issues)


def test_cycle_rejected():
    spec = {
        "meta": {"name": "loop"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "needs ${b}"},
            {"id": "b", "type": "agent", "prompt": "needs ${a}"},
        ],
    }
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "cycle" for i in result.issues)


def test_schema_and_schema_ref_both_rejected():
    spec = _valid_spec()
    spec["nodes"][0]["schema_ref"] = "VERDICT"  # already has inline schema
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "schema_xor" for i in result.issues)


def test_unresolved_schema_ref_rejected():
    spec = _valid_spec()
    spec["nodes"][2]["schema_ref"] = "NOPE"  # report node references a missing schema
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "schema_ref" for i in result.issues)


def test_static_overcap_fanout_rejected():
    spec = _valid_spec()
    spec["nodes"].append(
        {"id": "fan", "type": "parallel", "branches": [{"type": "agent", "prompt": str(i)}
                                                        for i in range(100)]}
    )
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "fanout_cap" for i in result.issues)


def test_missing_required_field_rejected():
    spec = {"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent"}]}  # no prompt
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "missing_field" and i.field == "prompt" for i in result.issues)


def test_errors_are_didactic():
    result = validate_spec({"meta": {"name": "x"},
                            "nodes": [{"id": "a", "type": "agent", "prompt": "${ghost}"}]})
    assert isinstance(result, ValidationError)
    issue = next(i for i in result.issues if i.rule == "ref_target")
    assert issue.node_id == "a"
    assert issue.example  # a corrected example is attached


def test_schema_as_string_naming_a_known_schema_is_tolerated():
    # the live schema/schema_ref mix-up: schema: "NAME" where NAME is in schemas:
    spec = _valid_spec()
    spec["nodes"][0] = {"id": "scan", "type": "agent", "prompt": "x", "schema": "VERDICT"}
    result = validate_spec(spec)
    assert isinstance(result, WorkflowSpec)  # tolerated (resolves at runtime)


def test_schema_as_unresolvable_string_is_rejected():
    spec = _valid_spec()
    spec["nodes"][0] = {"id": "scan", "type": "agent", "prompt": "x", "schema": "NOPE"}
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    assert any(i.rule == "schema_type" for i in result.issues)


def test_unsupported_type_rejected_when_restricted():
    # The supported_types mechanism: a valid type not in the engine's strategy set
    # is rejected didactically at author time (used by the service).
    spec = {"meta": {"name": "x"},
            "nodes": [{"id": "a", "type": "verify", "finding": "x", "skeptics": 3}]}
    result = validate_spec(spec, supported_types=frozenset({"agent"}))
    assert isinstance(result, ValidationError)
    assert any(i.rule == "unsupported_type" for i in result.issues)


def test_validate_never_raises_on_garbage():
    for garbage in [None, 42, "string", [], {"nodes": "not a list"}]:
        result = validate_spec(garbage)
        assert isinstance(result, ValidationError)  # returned, not raised


# --- issue #15: `min_success_ratio` was removed, not merely unknown ---------


def test_min_success_ratio_on_pipeline_is_rejected_with_its_own_rule():
    spec = _valid_spec()
    spec["nodes"][1]["min_success_ratio"] = 0.8  # the "triage" pipeline node
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    issue = next(i for i in result.issues if i.field == "min_success_ratio")
    assert issue.rule == "min_success_ratio_removed"
    assert issue.rule != "unknown_field"
    assert not any(i.rule == "unknown_field" and i.field == "min_success_ratio"
                   for i in result.issues)
    assert issue.node_id == "triage"


def test_min_success_ratio_on_parallel_is_rejected_with_its_own_rule():
    spec = {
        "meta": {"name": "fan"},
        "nodes": [
            {
                "id": "p",
                "type": "parallel",
                "min_success_ratio": 0.5,
                "branches": [{"id": "x", "type": "agent", "prompt": "go"}],
            },
        ],
    }
    result = validate_spec(spec)
    assert isinstance(result, ValidationError)
    issue = next(i for i in result.issues if i.field == "min_success_ratio")
    assert issue.rule == "min_success_ratio_removed"


def test_min_success_ratio_removed_message_names_the_substitute():
    spec = _valid_spec()
    spec["nodes"][1]["min_success_ratio"] = 0.8
    result = validate_spec(spec)
    issue = next(i for i in result.issues if i.rule == "min_success_ratio_removed")
    assert "gate" in issue.message
    assert "completeness_check" in issue.message
    assert "required" in issue.message
    assert issue.example  # a corrected example is attached
    # the rendered text (what the agent actually reads) carries the rule name,
    # so it can be grepped/matched even without the structured SpecIssue.
    assert "min_success_ratio_removed" in result.message
