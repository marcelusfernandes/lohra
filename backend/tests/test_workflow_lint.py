"""``lint_spec`` (issue #49) — a disconnected multi-node DAG warns, never fails.

``validate_spec`` accepts a spec with N nodes and zero edges between them today
(no cycle, every ref resolves — there just aren't any). The engine still runs
them one at a time, in a queue, with no relation to each other: a silent-loss
of the parallelism the author almost certainly wanted. ``lint_spec`` runs
*after* ``validate_spec`` succeeds and returns warnings, never errors.
"""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.lint import lint_spec
from lohra.workflow.nodes import WorkflowSpec
from lohra.workflow.schema import SpecIssue, ValidationError, validate_spec
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool
from tests.test_loop import FakeClient, _text_response


def _accepted(spec_dict: dict) -> WorkflowSpec:
    parsed = validate_spec(spec_dict)
    assert isinstance(parsed, WorkflowSpec), (
        f"fixture must validate: {parsed.message if isinstance(parsed, ValidationError) else parsed}"
    )
    return parsed


def test_disconnected_multi_node_spec_warns():
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "do a thing"},
                {"id": "b", "type": "agent", "prompt": "do another thing"},
                {"id": "c", "type": "agent", "prompt": "do a third thing"},
            ],
        }
    )
    issues = lint_spec(spec)
    assert len(issues) == 1
    issue = issues[0]
    assert isinstance(issue, SpecIssue)
    assert issue.rule == "disconnected_dag"
    assert "3 nodes" in issue.message
    assert "skill_view('workflow-authoring')" in issue.message


def test_depends_on_edge_silences_the_warning():
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "do a thing"},
                {"id": "b", "type": "agent", "prompt": "do another thing"},
                {"id": "c", "type": "agent", "prompt": "third", "depends_on": ["a"]},
            ],
        }
    )
    assert lint_spec(spec) == ()


def test_ref_only_edge_silences_the_warning():
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "do a thing"},
                {"id": "b", "type": "agent", "prompt": "use ${a}"},
            ],
        }
    )
    assert lint_spec(spec) == ()


def test_single_node_spec_never_warns():
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [{"id": "a", "type": "agent", "prompt": "do a thing"}],
        }
    )
    assert lint_spec(spec) == ()


def test_single_parallel_node_never_warns():
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [
                {"id": "a", "type": "parallel", "branches": ["do x", "do y"]},
            ],
        }
    )
    assert lint_spec(spec) == ()


def test_partial_connection_does_not_warn():
    # Only SOME nodes are disconnected — this is a real, common shape (one
    # setup node feeding two of three leaves, one true standalone side node)
    # and lint_spec is deliberately narrow: it flags only the ALL-disconnected
    # case, never a noisier "some node somewhere has no edge" rule.
    spec = _accepted(
        {
            "meta": {"name": "demo"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "do a thing"},
                {"id": "b", "type": "agent", "prompt": "use ${a}"},
                {"id": "c", "type": "agent", "prompt": "unrelated standalone thing"},
            ],
        }
    )
    assert lint_spec(spec) == ()


# --- issue #82 follow-up (owner decision, 2026-09-05): id/type on an
# embedded shape (branch/attempt/stage/body) warn instead of blocking -------


def test_id_on_a_parallel_branch_warns():
    spec = _accepted(
        {
            "meta": {"name": "fan"},
            "nodes": [
                {"id": "fan", "type": "parallel", "branches": [{"id": "x", "prompt": "go"}]},
            ],
        }
    )
    issues = lint_spec(spec)
    assert len(issues) == 1
    assert issues[0].rule == "nested_id_type_ignored"
    assert issues[0].node_id == "fan"
    assert "positional" in issues[0].message


def test_type_agent_on_a_pipeline_stage_warns():
    spec = _accepted(
        {
            "meta": {"name": "pl"},
            "nodes": [
                {
                    "id": "pl",
                    "type": "pipeline",
                    "items": ["a"],
                    "stages": [{"type": "agent", "prompt": "go ${item}"}],
                },
            ],
        }
    )
    issues = lint_spec(spec)
    assert len(issues) == 1
    assert issues[0].rule == "nested_id_type_ignored"
    assert issues[0].node_id == "pl"


def test_nested_shape_with_only_prompt_never_warns_about_id_or_type():
    spec = _accepted(
        {
            "meta": {"name": "fan"},
            "nodes": [
                {"id": "fan", "type": "parallel", "branches": [{"prompt": "go"}, "also go"]},
            ],
        }
    )
    assert lint_spec(spec) == ()


# --- wire: WorkflowService.start / the tool surface -------------------------


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _child_factory(reply="ok"):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return factory


def _service(db, tmp_path, reply="ok"):
    return WorkflowService(base_child_factory=_child_factory(reply), db=db, home=tmp_path)


def test_service_start_carries_lint_warnings_for_a_disconnected_spec(db, tmp_path):
    svc = _service(db, tmp_path)
    spec = {
        "meta": {"name": "demo"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "also go"},
        ],
    }
    try:
        out = svc.start(spec)
        assert "warnings" in out
        assert out["warnings"][0]["rule"] == "disconnected_dag"
    finally:
        svc.shutdown()


def test_service_start_has_no_warnings_key_for_a_connected_spec(db, tmp_path):
    svc = _service(db, tmp_path)
    spec = {
        "meta": {"name": "demo"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "use ${a}"},
        ],
    }
    try:
        out = svc.start(spec)
        assert "warnings" not in out  # byte-identical default reply
    finally:
        svc.shutdown()


def test_run_workflow_tool_surfaces_the_warning(db, tmp_path):
    svc = _service(db, tmp_path)
    spec = {
        "meta": {"name": "demo"},
        "nodes": [
            {"id": "a", "type": "agent", "prompt": "go"},
            {"id": "b", "type": "agent", "prompt": "also go"},
        ],
    }
    try:
        out = json.loads(WorkflowTool(svc).run({"spec": spec}))
        assert out["ok"] is True
        assert "warnings" in out
        assert out["warnings"][0]["rule"] == "disconnected_dag"
    finally:
        svc.shutdown()
