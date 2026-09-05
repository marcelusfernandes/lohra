"""The token ceiling as a STOP LINE, not a post-mortem (issue #71, Wave 10).

``test_workflow_token_budget.py`` pins the axis itself (charge, clamp, pause,
resume). This file pins the two gaps hypothesis H1 named, and only those:

- **H1(a)** the scalar gate only ever asked ``spent >= total``, so a run whose
  remaining budget cannot pay for one more leaf still spawned it. The ceiling
  became a stop line only AFTER it was crossed.
- **H1(b)** a leaf that crosses the ceiling while in flight is charged and then
  invisible: ``complete``, no fault, no field — the operator could only find it
  by subtracting two numbers in the rollup.

The leaves here report a REAL cost (not the 5/3 of the shared scripted client),
because both gaps are about the size of one leaf against what is left.
"""

from __future__ import annotations

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED, Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec


class CostedClient(ModelClient):
    """A one-turn client whose reported usage is whatever the test asked for."""

    def __init__(self, tokens_in: int, tokens_out: int = 0) -> None:
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out

    def create(self, **_kwargs):
        return {
            "content": [{"type": "text", "text": "R"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": self._tokens_in, "output_tokens": self._tokens_out},
        }

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, tokens_in, tokens_out=0, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=CostedClient(tokens_in, tokens_out),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _one_node_spec():
    return validate_spec(
        {
            "meta": {"name": "sl1", "version": 1},
            "nodes": [{"id": "a", "type": "agent", "prompt": "go"}],
        }
    )


def _two_node_spec():
    return validate_spec(
        {
            "meta": {"name": "sl2", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go"},
                {"id": "b", "type": "agent", "prompt": "then ${a}"},
            ],
        }
    )


# --- Experiment 1 (H1(b)): the in-flight overrun must be VISIBLE ---------


def test_a_leaf_that_crosses_the_ceiling_is_charged_and_marked(db):
    """One node, a ceiling of 100, a leaf that costs 700.

    H1(b) predicts today: ``complete``, ``faults == []``, ``spent == 700`` — the
    run outspent its ceiling by 7x and nothing in the result says so. The gate
    is SOFT by design (the call was already made and already billed), so the
    charge is right; the SILENCE is the defect. What must be true instead: the
    same ``complete`` and the same charge, plus one advisory fault naming the
    crossing and an ``overrun`` in the budget snapshot."""
    core = _core(db, 700)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=100))
        result = engine.run(_one_node_spec(), {})
        # Unchanged by this issue: the leaf finishes, is charged, and an overrun
        # is not a degradation (``derive_status`` never reads the budget).
        assert result.status == "complete"
        assert engine.budget.tokens_spent == 700
        # ...and the part H1(b) says is missing today.
        assert [f for f in result.advisory_faults if "token budget overrun" in f], (
            f"no overrun marker: advisory_faults={result.advisory_faults} "
            f"faults={result.faults}"
        )
        assert engine.budget.snapshot()["overrun"] == 600
    finally:
        core.shutdown()


# --- Experiment 2 (H1(a)): the stop line comes BEFORE the spawn ---------


def test_the_next_leaf_is_refused_when_what_is_left_cannot_pay_for_it(db):
    """Two chained nodes, a ceiling of 3000, a first leaf that costs 2500.

    H1(a) predicts today: ``b`` SPAWNS, because the only question asked is
    ``2500 >= 3000`` (no). The run has already measured that one leaf costs
    2500 and only 500 are left — the estimate the ``Budget`` already computes
    for barrier fan-outs was never consulted here. What must be true instead:
    the run pauses BEFORE spawning ``b``, with a detail that names the estimate,
    so the pause reads as the renewal checkpoint it is."""
    core = _core(db, 2500)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=3000))
        result = engine.run(_two_node_spec(), {})
        assert result.outputs["a"] == "R"  # the leaf that fit still ran
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert engine.budget.tokens_spent == 2500, "'b' must never have spawned"
        assert result.faults == [
            "b: next leaf estimated at 2500 tokens (measured average), "
            "only 500 left of 3000 — token budget exhausted"
        ]
    finally:
        core.shutdown()
