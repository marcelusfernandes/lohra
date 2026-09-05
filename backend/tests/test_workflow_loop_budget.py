"""``loop_until_dry.budget`` — a per-node TOKEN CEILING (issue #73 follow-up,
landing after #71's run-level token budget contract).

Distinct from the RUN-level `Budget.token_budget` (#71): this is the author's
own ceiling on ONE node's rounds, checked between rounds (never mid-round,
same soft doctrine as #71) against the sum of `tokens_in + tokens_out` the
node's own leaves have cost so far. Hitting it is informational, not a
failure — the author asked for a bound and the loop respected it — so it is
recorded through `record_advisory_fault` (discounted by `derive_status`) and
the run still seals `complete` (unless the harvest is ALSO empty because
every round genuinely died — see the dead-rounds test below, which mirrors
the run-level `TokenBudgetExhausted` path: nothing harvested is `None`,
never `[]`).

A budget-cut harvest IS CACHED (adversarial-review decision, MEDIUM-4): it is
the same kind of author-declared cap as `max_rounds`, `budget` is already part
of the cell's identity so a raised budget is simply a different cell, and NOT
caching would only punish a resume of the SAME run — re-spending the node's
own budget against the run's token budget on every resume, and turning a
`checkpoint` that interpolates `${loop}` into one that re-asks the human every
time. The "budget reached" advisory itself survives across a resume via
`prior_advisory` (carried unconditionally), so the fact the harvest was cut
short is never lost even though the harvest itself replays.
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


def _counting_responder(reply="always-something"):
    calls = {"n": 0}

    def responder(_prompt):
        calls["n"] += 1
        return reply

    return calls, responder


def _loop_node(*, budget=None, max_rounds=5, stop_after_k_empty=5, schema=None):
    body = {"type": "agent", "prompt": "go"}
    if schema is not None:
        body["schema"] = schema
    node = {
        "id": "loop", "type": "loop_until_dry",
        "body": body,
        "stop_after_k_empty": stop_after_k_empty, "max_rounds": max_rounds,
    }
    if budget is not None:
        node["budget"] = budget
    return node


def _run(core, node, *, extra_nodes=None, **engine_kwargs):
    nodes = [node] + list(extra_nodes or [])
    spec = validate_spec({"meta": {"name": "l"}, "nodes": nodes})
    return WorkflowEngine(core, budget=Budget(), **engine_kwargs).run(spec, {})


def _cached_node_ids(db, run_id):
    rows = db._connection.execute(
        "SELECT node_id FROM workflow_node_cache WHERE run_id = ?", (run_id,)
    ).fetchall()
    return [row["node_id"] for row in rows]


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


def test_a_loop_budget_stop_IS_cached(db):
    """MEDIUM-4 (adversarial review): a budget-shortened harvest DOES replay
    on resume — the same treatment a max_rounds-capped harvest already gets.
    Not caching it would only punish a resume of the SAME run (re-spending
    the node's own budget against the run's token budget every time), and a
    raised budget already gets a different cell (it is part of the cell
    identity), so nothing is lost by caching the shorter one."""
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        cache = NodeCache(db, "run-loop-budget")
        result = _run(core, _loop_node(budget=250, max_rounds=5, stop_after_k_empty=5),
                      run_id="run-loop-budget", cache=cache)
        assert _cached_node_ids(db, "run-loop-budget") == ["loop"]
        assert result.outputs["loop"] == ["always-something"] * 3

        # And it REPLAYS: a second stretch on the same run/spec does not
        # spawn a single extra leaf.
        result2 = _run(core, _loop_node(budget=250, max_rounds=5, stop_after_k_empty=5),
                        run_id="run-loop-budget", cache=cache)
        assert calls["n"] == 3  # unchanged: the second run was a pure replay
        assert result2.outputs["loop"] == ["always-something"] * 3
    finally:
        core.shutdown()


def test_loop_dryness_wins_over_budget_on_the_same_round(db):
    """MEDIUM-2 (adversarial review): a round that is both the K-th empty AND
    crosses the budget is a COMPLETE, legitimately-dry harvest, not a
    budget-cut one — no 'loop budget' fault, and it caches like any other dry
    harvest."""
    calls, responder = _counting_responder(reply="")  # empty every round
    core = _core(db, responder)
    try:
        cache = NodeCache(db, "run-dry-wins")
        result = _run(core, _loop_node(budget=100, max_rounds=5, stop_after_k_empty=1),
                      run_id="run-dry-wins", cache=cache)
        assert calls["n"] == 1  # one empty round is enough to call it dry
        assert result.outputs["loop"] == []  # dry, not cut short
        assert not any("loop budget" in f for f in result.faults)
        assert result.status == "complete"
        assert _cached_node_ids(db, "run-dry-wins") == ["loop"]  # a real dry harvest caches
    finally:
        core.shutdown()


def test_loop_budget_reached_with_nothing_harvested_because_rounds_died(db):
    """MEDIUM-3 (adversarial review): the budget break must mirror the
    run-level TokenBudgetExhausted path 12 lines up — nothing harvested is
    `None`, never `[]`, so a downstream reader cannot mistake "every round
    failed" for "looked and found nothing"."""
    def responder(prompt):
        if prompt == "hi":
            return "fine"
        return "not-json"  # every loop round dies on schema validation

    core = _core(db, responder)
    try:
        # A schema-mismatch round costs 3 calls (initial + 2 validation
        # steers) * 100 tokens = 300/round (measured empirically). budget=700
        # crosses on the 3rd dead round (900 >= 700) but not the 2nd (600).
        cache = NodeCache(db, "run-dead-then-budget")
        result = _run(
            core,
            _loop_node(budget=700, max_rounds=5, stop_after_k_empty=5,
                       schema={"type": "object", "required": ["x"]}),
            extra_nodes=[{"id": "ok", "type": "agent", "prompt": "hi"}],
            run_id="run-dead-then-budget", cache=cache,
        )
        assert result.outputs["ok"] == "fine"
        assert result.outputs["loop"] is None  # NOT [] — every round died
        for i in range(3):
            assert f"loop: round {i} died (not counted as dry)" in result.faults
        expected_fault = "loop: loop budget reached after 3 rounds: 900 of 700 tokens"
        assert expected_fault in result.faults
        assert expected_fault in result.advisory_faults
        assert result.status == "degraded"  # a null is never a clean run
        assert "loop" not in _cached_node_ids(db, "run-dead-then-budget")  # a dead harvest never caches
    finally:
        core.shutdown()


def test_loop_budget_never_preempts_round_zero(db):
    """Round 0 always spawns regardless of how small `budget` is — the soft
    doctrine (#71's run-level gate has the same rule): a leaf already decided
    on is never refused mid-flight, and there is no "flight" before the first
    round starts."""
    calls, responder = _counting_responder()
    core = _core(db, responder)
    try:
        result = _run(core, _loop_node(budget=1, max_rounds=5, stop_after_k_empty=5))
        assert calls["n"] == 1  # round 0 spawned even though budget=1 < one leaf's cost
        assert result.outputs["loop"] == ["always-something"]
        expected_fault = "loop: loop budget reached after 1 round: 100 of 1 tokens"
        assert expected_fault in result.advisory_faults
    finally:
        core.shutdown()


def test_loop_budget_trip_with_empty_but_alive_rounds_is_cached_as_short_list(db):
    """Pins a deliberate, narrower reading of MEDIUM-3 than its literal
    wording ('collected if collected else None'): that phrasing would ALSO
    null a round that is merely empty-but-alive (not yet dry, `intact` still
    True) when the budget trips before `stop_after_k_empty` is reached. This
    codebase instead nulls ONLY when a round genuinely DIED (`intact` False)
    — an empty-but-alive, budget-cut harvest is the SAME kind of short, real
    result a `max_rounds`-capped loop already returns as `[]` and caches
    (MEDIUM-4's own reasoning), so it gets `[]`, not `None`, and is cached
    like any other budget-cut harvest."""
    calls, responder = _counting_responder(reply="")  # empty every round, never dead
    core = _core(db, responder)
    try:
        cache = NodeCache(db, "run-empty-alive-budget")
        # stop_after_k_empty=5 -> round 0's empty output alone is NOT dry yet
        # (empty_streak=1 < 5); budget=100 trips right after it.
        result = _run(core, _loop_node(budget=100, max_rounds=5, stop_after_k_empty=5),
                      run_id="run-empty-alive-budget", cache=cache)
        assert calls["n"] == 1
        assert result.outputs["loop"] == []  # short, real, NOT None
        expected_fault = "loop: loop budget reached after 1 round: 100 of 100 tokens"
        assert expected_fault in result.advisory_faults
        assert result.status == "complete"  # [] is not a null output
        assert _cached_node_ids(db, "run-empty-alive-budget") == ["loop"]
    finally:
        core.shutdown()


def test_loop_cell_hash_changes_with_budget_present_or_absent(db):
    """The loop's cache key must fold `budget` in (only when authored, like
    `max_iterations` for agents) — a different budget can yield a different,
    shorter output, so replaying across the change would be wrong.

    End-to-end, not a direct `cell_hash` call with hand-built args, and with a
    CONTROL (MEDIUM-1, adversarial review): stretch 2 re-runs the identical
    spec first and must be a real cache HIT (no extra spawn) before stretch 3
    changes `budget` and must be a MISS."""
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

        # Stretch 2 (the CONTROL): the IDENTICAL spec, same run/cache -> must
        # be a real cache HIT, not a coincidence of the assertion below.
        result2 = _run(core, _loop_node(budget=1000, max_rounds=1, stop_after_k_empty=5),
                        run_id="run-x", cache=cache)
        assert calls["n"] == 1  # unchanged: replayed, not re-spawned
        assert result2.outputs["loop"] == ["always-something"]

        # Stretch 3: SAME run_id/cache, budget dropped from the spec. If
        # `budget` were not part of the identity, this would replay stretch
        # 1/2's cell (no fresh spawn); it must instead be a cache MISS.
        result3 = _run(core, _loop_node(max_rounds=1, stop_after_k_empty=5),
                        run_id="run-x", cache=cache)
        assert calls["n"] == 2  # a fresh spawn happened, not a replay
        assert result3.outputs["loop"] == ["always-something"]
    finally:
        core.shutdown()
