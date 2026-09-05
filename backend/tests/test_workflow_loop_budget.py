"""``loop_until_dry.budget`` — a per-node TOKEN CEILING (issue #73 follow-up,
landing after #71's run-level token budget contract).

Distinct from the RUN-level `Budget.token_budget` (#71): this is the author's
own ceiling on ONE node's rounds, checked between rounds (never mid-round,
same soft doctrine as #71) against the sum of `tokens_in + tokens_out` the
node's own leaves have cost so far. Hitting it is informational, not a
failure — the author asked for a bound and the loop respected it — so it is
recorded through `record_advisory_fault` (discounted by `derive_status`) and
the run still seals `complete`.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec


class _FixedCostClient(ModelClient):
    """Replies based on prompt text; every leaf costs exactly 60 in + 40 out
    (100 tokens total) — deterministic so the arithmetic in these tests is
    exact."""

    def __init__(self, responder):
        self._responder = responder

    def _prompt(self, kwargs):
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        return {
            "content": [{"type": "text", "text": self._responder(self._prompt(kwargs))}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 60, "output_tokens": 40},
        }

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=_FixedCostClient(responder),
        )

    return OrchestrationCore(db, factory)


def _counting_responder():
    calls = {"n": 0}

    def responder(_prompt):
        calls["n"] += 1
        return "always-something"

    return calls, responder


def _loop_node(*, budget=None, max_rounds=5, stop_after_k_empty=5):
    node = {
        "id": "loop", "type": "loop_until_dry",
        "body": {"type": "agent", "prompt": "go"},
        "stop_after_k_empty": stop_after_k_empty, "max_rounds": max_rounds,
    }
    if budget is not None:
        node["budget"] = budget
    return node


def _run(core, node, **engine_kwargs):
    spec = validate_spec({"meta": {"name": "l"}, "nodes": [node]})
    return WorkflowEngine(core, budget=Budget(), **engine_kwargs).run(spec, {})


def test_loop_budget_stops_the_loop_before_it_would_overrun(db):
    # never empty -> only the budget can stop it before max_rounds=5
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        result = _run(core, _loop_node(budget=250, max_rounds=5, stop_after_k_empty=5))
        # 100/round: after round 0 -> 100, round 1 -> 200, round 2 -> 300 (>= 250):
        # 3 rounds spawn, the 4th does not.
        assert calls["n"] == 3
        assert result.outputs["loop"] == ["always-something"] * 3
        expected_fault = "loop: loop budget reached after 3 rounds: 300 of 250 tokens"
        assert expected_fault in result.faults
        assert expected_fault in result.advisory_faults
        # advisory faults are discounted by derive_status -> still "complete"
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_loop_without_budget_is_unaffected_runs_to_max_rounds(db):
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        result = _run(core, _loop_node(max_rounds=3, stop_after_k_empty=5))  # no budget
        assert calls["n"] == 3  # capped by max_rounds exactly like before #73
        assert result.outputs["loop"] == ["always-something"] * 3
        assert not any("loop budget" in f for f in result.faults)
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_loop_budget_not_reached_lets_max_rounds_govern(db):
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        # budget generous enough (1000) that max_rounds=3 governs instead
        result = _run(core, _loop_node(budget=1000, max_rounds=3, stop_after_k_empty=5))
        assert calls["n"] == 3
        assert result.outputs["loop"] == ["always-something"] * 3
        assert not any("loop budget" in f for f in result.faults)
        assert result.status == "complete"
    finally:
        core.shutdown()


def test_a_loop_budget_stop_is_not_cached_as_a_dry_harvest(db):
    """A budget-shortened harvest must not replay on resume as though the loop
    ran dry — the next stretch (or a bigger budget) has to be free to collect
    more."""
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        cache = NodeCache(db, "run-loop-budget")
        _run(core, _loop_node(budget=250, max_rounds=5, stop_after_k_empty=5),
             run_id="run-loop-budget", cache=cache)
        rows = db._connection.execute(
            "SELECT node_id FROM workflow_node_cache WHERE run_id = ?", ("run-loop-budget",)
        ).fetchall()
        assert [row["node_id"] for row in rows] == []  # nothing cached: it was short
    finally:
        core.shutdown()


def test_loop_cell_hash_changes_with_budget_present_or_absent(db):
    """The loop's cache key must fold `budget` in (only when authored, like
    `max_iterations` for agents) — a different budget can yield a different,
    shorter output, so replaying across the change would be wrong.

    End-to-end, not a direct `cell_hash` call with hand-built args: the point
    is that CHANGING the authored spec between two stretches of the SAME run
    must miss the cache and re-spawn, not replay a cell identified without the
    field that changed."""
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        cache = NodeCache(db, "run-x")
        # Stretch 1: budget authored but never reached (1000 > 1 round's 100
        # tokens) -> a real, complete harvest (intact) -> gets cached.
        result1 = _run(core, _loop_node(budget=1000, max_rounds=1, stop_after_k_empty=5),
                       run_id="run-x", cache=cache)
        assert calls["n"] == 1
        assert result1.outputs["loop"] == ["always-something"]

        # Stretch 2: SAME run_id/cache, budget dropped from the spec. If
        # `budget` were not part of the identity, this would replay stretch
        # 1's cell (no fresh spawn); it must instead be a cache MISS.
        result2 = _run(core, _loop_node(max_rounds=1, stop_after_k_empty=5),
                       run_id="run-x", cache=cache)
        assert calls["n"] == 2  # a fresh spawn happened, not a replay
        assert result2.outputs["loop"] == ["always-something"]
    finally:
        core.shutdown()
