"""Tests for the `workflow` node — one-level nesting (Fase 8, Milestone H)."""

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, reply="R"):
    def factory():
        from tests.test_loop import FakeClient

        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return OrchestrationCore(db, factory)


_CHILD = {"meta": {"name": "child", "version": 1},
          "nodes": [{"id": "leaf", "type": "agent", "prompt": "do ${args.x}"}]}


def _engine(core, **kw):
    return WorkflowEngine(core, budget=Budget(), **kw)


def test_workflow_node_runs_nested_template(db):
    core = _core(db, reply="CHILD_OUT")
    loader = {"child": _CHILD}.get
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "child",
                                       "args": {"x": "hi"}}]})
    try:
        result = _engine(core, loader=loader).run(parent, {})
        # the nested workflow's outputs come back under the `workflow` node
        assert result.outputs["sub"] == {"leaf": "CHILD_OUT"}
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_nested_output_is_referenceable(db):
    core = _core(db, reply="X")
    loader = {"child": _CHILD}.get
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [
                                # args must satisfy the child's ${args.x}: an unset ref
                                # fails that leaf now (never prompts it with "null").
                                {"id": "sub", "type": "workflow", "ref": "child",
                                 "args": {"x": "hi"}},
                                {"id": "after", "type": "agent", "prompt": "got ${sub.leaf}",
                                 "depends_on": ["sub"]},
                            ]})
    try:
        result = _engine(core, loader=loader).run(parent, {})
        assert result.outputs["sub"] == {"leaf": "X"}
        assert result.outputs["after"] == "X"  # downstream saw the nested output
    finally:
        core.shutdown()


def test_unknown_ref_resolves_to_null(db):
    core = _core(db)
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "nope"}]})
    try:
        result = _engine(core, loader={}.get).run(parent, {})
        assert result.outputs["sub"] is None
    finally:
        core.shutdown()


def test_nested_invalid_template_records_named_fault(db):
    # H9 (issue #79): the ref'd template exists but fails validate_spec (here,
    # a 'label' field removed by #73). Every OTHER cause of a null node goes
    # through engine.record_fault; this one used to only logger.warning and
    # return None, leaving the rollup with a null node and zero causal signal.
    core = _core(db)
    invalid_child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [{"id": "leaf", "type": "agent", "prompt": "x", "label": "nope"}],
    }
    loader = {"child": invalid_child}.get
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "child"}]})
    try:
        result = _engine(core, loader=loader).run(parent, {})
        assert result.outputs["sub"] is None
        # the fault names the rejected node's rule code (label_removed) — never
        # the didactic prose (issue.message/.example), which is metadata-unsafe.
        assert any("sub" in f and "child" in f and "label_removed" in f for f in result.faults)
    finally:
        core.shutdown()


def test_depth_cap_blocks_second_level(db):
    # child itself contains a workflow node -> nesting it from the parent would be
    # depth 2 -> the nested run faults that node to null (one level only).
    core = _core(db, reply="R")
    nesting_child = {"meta": {"name": "child", "version": 1},
                     "nodes": [{"id": "deeper", "type": "workflow", "ref": "child"}]}
    loader = {"child": nesting_child}.get
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "child"}]})
    try:
        result = _engine(core, loader=loader).run(parent, {})
        # parent's sub ran the child (depth 1); the child's 'deeper' node hit the
        # depth cap -> engine fault -> null inside the nested outputs.
        assert result.outputs["sub"] == {"deeper": None}
    finally:
        core.shutdown()


def test_nested_failures_surface_in_parent_rollup(db):
    # The discriminator: a nested run whose leaf dies must NOT read as a clean
    # parent (else J would certify a broken composite as a template).
    def factory():
        from tests.test_loop import FakeClient

        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=FakeClient([RuntimeError("leaf died")]))

    core = OrchestrationCore(db, factory)
    loader = {"child": _CHILD}.get
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "child", "args": {}}]})
    try:
        result = _engine(core, loader=loader).run(parent, {})
        # the nested leaf nulled -> folded into the parent: null_rate reflects it
        assert result.null_count >= 1
        assert result.nodes_total >= 2  # parent's sub node + the nested leaf
        assert result.null_rate > 0  # NOT a clean run -> J won't save it as a template
    finally:
        core.shutdown()


def test_nested_fanout_is_bounded_by_shared_budget(db):
    # the nested run draws from the SAME budget: a fan-out over the shared cap is
    # rejected inside the nested workflow (can't escape the parent's limit).
    core = _core(db, reply="R")
    fanout_child = {"meta": {"name": "child", "version": 1},
                    "nodes": [{"id": "fan", "type": "parallel",
                               "branches": [{"type": "agent", "prompt": str(i)} for i in range(5)]}]}
    parent = validate_spec({"meta": {"name": "parent"},
                            "nodes": [{"id": "sub", "type": "workflow", "ref": "child"}]})
    try:
        engine = WorkflowEngine(core, budget=Budget(max_fanout=3), loader={"child": fanout_child}.get)
        result = engine.run(parent, {})
        # the nested fan-out of 5 > shared max_fanout 3 -> rejected -> fan node null
        assert result.outputs["sub"] == {"fan": None}
    finally:
        core.shutdown()
