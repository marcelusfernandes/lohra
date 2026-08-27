"""Tests for the WorkflowEngine + strategies (Fase 8, Milestone B)."""

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import FakeClient, _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, *, reply="ok", fail=False, max_concurrent=4):
    def factory():
        client = FakeClient([] if fail else [_text_response(reply)])
        if fail:  # a client with no responses raises -> run_conversation -> error
            client = FakeClient([RuntimeError("leaf boom")])
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
        )

    return OrchestrationCore(db, factory, max_concurrent=max_concurrent)


def _engine(core, **budget_kw):
    return WorkflowEngine(core, budget=Budget(**budget_kw))


def test_topological_scheduling_feeds_downstream(db):
    core = _core(db, reply="DATA")
    spec = validate_spec(
        {
            "meta": {"name": "chain"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "start"},
                {"id": "b", "type": "agent", "prompt": "use ${a}", "depends_on": ["a"]},
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert result.outputs["a"] == "DATA"
        assert result.outputs["b"] == "DATA"  # ran after a, with a's output available
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_rollup_aggregates_leaf_tokens(db):
    # _text_response carries usage (5 in / 3 out); two agent nodes -> summed once.
    core = _core(db, reply="R")
    spec = validate_spec({"meta": {"name": "t"}, "nodes": [
        {"id": "a", "type": "agent", "prompt": "x"},
        {"id": "b", "type": "agent", "prompt": "y"}]})
    try:
        result = _engine(core).run(spec, {})
        assert result.tokens_in == 10 and result.tokens_out == 6  # 2 leaves x (5,3)
    finally:
        core.shutdown()


def test_dead_leaf_resolves_to_null(db):
    core = _core(db, fail=True)
    spec = validate_spec({"meta": {"name": "x"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]})
    try:
        result = _engine(core).run(spec, {})
        assert result.outputs["a"] is None
        assert result.null_count == 1
    finally:
        core.shutdown()


def test_parallel_barriers_and_preserves_order(db):
    # each leaf replies with the same canned text; assert width + order shape.
    core = _core(db, reply="R")
    spec = validate_spec(
        {
            "meta": {"name": "fan"},
            "nodes": [
                {
                    "id": "fan",
                    "type": "parallel",
                    "branches": [
                        {"type": "agent", "prompt": "one"},
                        {"type": "agent", "prompt": "two"},
                        {"type": "agent", "prompt": "three"},
                    ],
                }
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert result.outputs["fan"] == ["R", "R", "R"]  # list, input order, all collected
    finally:
        core.shutdown()


def test_fanout_over_budget_is_rejected_and_logged(db):
    core = _core(db, reply="R")
    spec = validate_spec(
        {
            "meta": {"name": "fan"},
            "nodes": [
                {"id": "fan", "type": "parallel",
                 "branches": [{"type": "agent", "prompt": str(i)} for i in range(5)]},
            ],
        }
    )
    try:
        result = _engine(core, max_fanout=3).run(spec, {})
        assert result.outputs["fan"] is None  # rejected
        assert any("fan-out" in f for f in result.faults)  # logged into faults
        assert result.status == "failed"  # the run's only node nulled: nothing survived
    finally:
        core.shutdown()


def test_engine_fault_is_isolated_and_run_continues(db):
    # A strategy that raises on malformed input (verify skeptics not an int) is an
    # engine fault: recorded + nulled, while the rest of the run continues.
    core = _core(db, reply="R")
    spec = validate_spec(
        {
            "meta": {"name": "x"},
            "nodes": [
                {"id": "boom", "type": "verify", "finding": "x", "skeptics": "not-a-number"},
                {"id": "a", "type": "agent", "prompt": "start"},
            ],
        }
    )
    try:
        result = _engine(core).run(spec, {})
        assert result.outputs["a"] == "R"  # the agent node still ran
        assert result.outputs["boom"] is None  # engine fault -> nulled
        assert result.engine_faults >= 1
        assert any("engine fault" in f for f in result.faults)
    finally:
        core.shutdown()
