"""The authoring surface: what a spec author actually READS (Fase 8, fatia C).

Two promises are tested here because both are invisible to the rest of the
suite: a validation error must carry its corrected example (schema.py's own
docstring promises it), and the run_workflow description must describe every
node type the engine can actually execute — it is the only proactive
documentation the authoring model ever sees.
"""

from lohra.workflow.nodes import WorkflowSpec
from lohra.workflow.schema import SpecIssue, ValidationError, validate_spec
from lohra.workflow.strategies import STRATEGIES
from lohra.workflow.tools import EXAMPLE_SPEC, RUN_GUIDANCE


def test_message_includes_the_corrected_example():
    error = ValidationError(
        (SpecIssue("meta", "meta.name is required", field="meta.name",
                   example="meta:\n  name: triage-bugs"),)
    )
    assert "meta.name is required" in error.message
    assert "name: triage-bugs" in error.message


def test_real_validation_error_carries_its_example():
    result = validate_spec({"meta": {}, "nodes": [{"id": "a", "type": "agent", "prompt": "x"}]})
    assert isinstance(result, ValidationError)
    assert "triage-bugs" in result.message  # the example the validator authored


def test_issue_without_example_renders_on_one_clean_line():
    error = ValidationError((SpecIssue("nodes", "spec needs a non-empty 'nodes' list",
                                       field="nodes"),))
    assert error.message.splitlines() == ["[nodes] .nodes: spec needs a non-empty 'nodes' list"]


def test_guidance_documents_every_executable_node_type():
    for node_type in STRATEGIES:
        assert f"- {node_type}:" in RUN_GUIDANCE, f"{node_type} undocumented in RUN_GUIDANCE"


def test_embedded_example_spec_is_valid_and_shown():
    # The example must pass the SAME gate WorkflowService.start applies, and the
    # rendered text must be the object we validated (no drift between the two).
    result = validate_spec(EXAMPLE_SPEC, supported_types=frozenset(STRATEGIES))
    assert isinstance(result, WorkflowSpec)
    assert "${args." in RUN_GUIDANCE and "${scan." in RUN_GUIDANCE
    assert '"schema_ref":"FINDING"' in RUN_GUIDANCE


def test_guidance_stays_short_enough_to_ship_on_every_call():
    assert len(RUN_GUIDANCE.splitlines()) <= 45
