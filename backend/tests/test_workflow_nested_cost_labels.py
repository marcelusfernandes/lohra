"""#90 review: display-label collisions must never merge or erase owners' bills."""

import pytest

from lohra.agent.types import Usage
from lohra.state import SessionDB
from lohra.workflow.accounting import NodeCost, RunResult
from lohra.workflow.budget import Budget
from lohra.workflow.costs import node_cost_entries
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_workflow_token_budget import _core


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _agent(node_id, model="root-model"):
    return {"id": node_id, "type": "agent", "prompt": "work", "model": model}


def _spec(nodes):
    return validate_spec({"meta": {"name": "cost-labels"}, "nodes": nodes})


def _child(node_id="do", model="child-model"):
    return {"meta": {"name": "child"}, "nodes": [_agent(node_id, model)]}


def _run(db, nodes, children):
    # depends_on deliberately fixes the order; refs do not constrain these ids.
    ordered = [
        {**node, **({"depends_on": [nodes[i - 1]["id"]]} if i else {})}
        for i, node in enumerate(nodes)
    ]
    core = _core(db, lambda prompt: "done")
    try:
        return WorkflowEngine(core, budget=Budget(), loader=children.get).run(_spec(ordered))
    finally:
        core.shutdown()


def _assert_bill(result, count):
    assert result.status == "complete"
    assert result.tokens_in + result.tokens_out == count * 8
    assert len(result.node_costs) == count
    assert sum(c.usage.input_tokens + c.usage.output_tokens for c in result.node_costs.values()) == count * 8


@pytest.mark.parametrize("root_first", [True, False])
def test_authored_root_id_cannot_erase_or_absorb_nested_costs(db, root_first):
    root = _agent("sub[a]:do")
    call = {"id": "a", "type": "workflow", "ref": "child"}
    result = _run(db, [root, call] if root_first else [call, root], {"child": _child()})
    _assert_bill(result, 2)
    assert result.node_costs[root["id"]].model == "root-model"
    child_key, cost = next((key, value) for key, value in result.node_costs.items() if value.template)
    assert child_key != root["id"] and cost.model == "child-model"
    assert cost.node_path == ("a", "do")
    entry = next(row for row in node_cost_entries(result.node_costs) if row.get("template"))
    assert entry["node_path"] == ["a", "do"]
    assert entry["node_id"] == child_key


@pytest.mark.parametrize("reverse", [True, False])
def test_two_nested_paths_with_the_same_display_label_keep_two_bills(db, reverse):
    calls = [
        {"id": "a", "type": "workflow", "ref": "first"},
        {"id": "a]:b", "type": "workflow", "ref": "second"},
    ]
    children = {"first": _child("b]:do", "first-model"), "second": _child("do", "second-model")}
    result = _run(db, list(reversed(calls)) if reverse else calls, children)
    _assert_bill(result, 2)
    assert {cost.node_path for cost in result.node_costs.values()} == {("a", "b]:do"), ("a]:b", "do")}
    assert {cost.model for cost in result.node_costs.values()} == {"first-model", "second-model"}


def test_an_authored_id_can_imitate_the_fallback_without_claiming_its_bill(db):
    root = _agent("sub[a]:do")
    call = {"id": "a", "type": "workflow", "ref": "child"}
    first = _run(db, [root, call], {"child": _child()})
    _assert_bill(first, 2)
    fallback = next(key for key, cost in first.node_costs.items() if cost.template)
    # The impostor comes LAST: reserving only rows already accounted would miss it.
    result = _run(db, [call, root, _agent(fallback, "impostor-model")], {"child": _child()})
    _assert_bill(result, 3)
    assert result.node_costs[root["id"]].model == "root-model"
    assert result.node_costs[fallback].model == "impostor-model"
    assert next(cost for cost in result.node_costs.values() if cost.template).model == "child-model"


def test_repeated_fold_reuses_the_owner_key_and_keeps_mixed_route_unknown(db):
    core = _core(db, lambda prompt: "done")
    engine = WorkflowEngine(core, budget=Budget())
    try:
        result = engine.run(_spec([_agent("sub[a]:do")]))
        for model in ("first-model", "second-model"):
            nested = RunResult(tokens_in=5, tokens_out=3, node_costs={
                "do": NodeCost(Usage(input_tokens=5, output_tokens=3), "anthropic", model),
            })
            engine.fold_nested(nested, "child", "a")
            if model == "first-model":
                first_keys = set(result.node_costs)
        assert result.tokens_in + result.tokens_out == 24
        assert len(result.node_costs) == 2 and set(result.node_costs) == first_keys
        assert sum(cost.usage.input_tokens + cost.usage.output_tokens for cost in result.node_costs.values()) == 24
        assert result.node_costs["sub[a]:do"].model == "root-model"
        cost = next(value for value in result.node_costs.values() if value.template)
        assert cost.node_path == ("a", "do")
        assert cost.model is None and cost.provider is None
        assert cost.usage == Usage(input_tokens=10, output_tokens=6)
    finally:
        core.shutdown()


def test_ordinary_labels_remain_unchanged(db):
    result = _run(db, [_agent("root"), {"id": "a", "type": "workflow", "ref": "child"}], {"child": _child()})
    _assert_bill(result, 2)
    assert set(result.node_costs) == {"root", "sub[a]:do"}
    assert result.node_costs["sub[a]:do"].node_path == ("a", "do")


def test_a_new_run_releases_old_label_reservations(db):
    core = _core(db, lambda prompt: "done")
    engine = WorkflowEngine(core, budget=Budget(), loader=lambda ref: _child())
    call = {"id": "a", "type": "workflow", "ref": "child"}
    try:
        first = engine.run(_spec([_agent("sub[a]:do"), call]))
        _assert_bill(first, 2)
        second = engine.run(_spec([call]))
        _assert_bill(second, 1)
        assert set(second.node_costs) == {"sub[a]:do"}
    finally:
        core.shutdown()
