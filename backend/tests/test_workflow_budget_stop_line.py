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
from lohra.workflow import library
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED, Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService


class CostedClient(ModelClient):
    """A one-turn client whose reported usage is whatever the test asked for.

    ``cost`` is a flat number of input tokens, or a callable over the rendered
    prompt for the tests that need one node to be cheap and the next expensive."""

    def __init__(self, cost) -> None:
        self._cost = cost

    def _prompt(self, kwargs) -> str:
        msgs = kwargs.get("messages") or []
        return " ".join(m.get("content", "") for m in msgs if isinstance(m.get("content"), str))

    def create(self, **kwargs):
        cost = self._cost(self._prompt(kwargs)) if callable(self._cost) else self._cost
        return {
            "content": [{"type": "text", "text": "R"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": cost, "output_tokens": 0},
        }

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, cost, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=CostedClient(cost),
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


# --- the stop line's edges ---------------------------------------------


def test_a_leaf_that_exactly_fits_still_spawns(db):
    """The predicate is ``remaining < estimate``, strictly. A ceiling that pays
    for the next leaf to the token buys it — refusing here would leave the last
    leaf of every exactly-sized budget unspawned and the run paused for money it
    still had."""
    core = _core(db, 500)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=1000))
        result = engine.run(_two_node_spec(), {})
        assert result.status == "complete"
        assert engine.budget.snapshot() == {
            "total": 1000, "spent": 1000, "remaining": 0, "overrun": 0
        }
    finally:
        core.shutdown()


def test_the_first_leaf_of_a_run_is_never_refused_on_a_guess(db):
    """Before this run has priced a leaf of its own, ``est_leaf_cost`` is a
    static constant that knows nothing about these leaves. Refusing on it would
    stop a small-ceiling run before it ever bought the measurement that would
    have told it the truth — so the gate stays ``spent >= total`` until then."""
    core = _core(db, 3)  # a leaf far cheaper than EST_TOKENS_PER_LEAF
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=10))
        result = engine.run(_one_node_spec(), {})
        assert result.status == "complete"
        assert engine.budget.tokens_spent == 3
    finally:
        core.shutdown()


def test_an_overrun_never_degrades_the_verdict(db):
    """``derive_status`` does not read the budget and this issue does not change
    that (decision: an overrun is not a degradation). The advisory is discounted
    by ``unrecovered`` on exactly the terms #45 set — it advises about a node
    that CONCLUDED, so it says nothing about the shape."""
    from lohra.workflow.accounting import derive_status, unrecovered

    core = _core(db, 700)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=100))
        result = engine.run(_one_node_spec(), {})
        assert derive_status(result) == "complete"
        assert unrecovered(result) is False
        assert result.faults == result.advisory_faults  # the ONLY fault is the advice
    finally:
        core.shutdown()


def test_only_the_charge_that_crosses_the_ceiling_says_so():
    """The mechanism, at the level it lives on: the crossing is ONE event, so
    exactly one charge reports it. Computed inside the lock, which is what makes
    it exactly-once under the pipeline's concurrent ``on_done`` workers."""
    budget = Budget(token_budget=1000)
    assert budget.charge_tokens(400, 0) is False  # 400 — inside
    assert budget.charge_tokens(400, 0) is False  # 800 — still inside
    assert budget.charge_tokens(400, 0) is True  # 1200 — the crossing
    assert budget.charge_tokens(400, 0) is False  # 1600 — already over
    assert budget.overrun == 600


def test_a_fanout_that_lands_over_the_ceiling_writes_one_advisory(db):
    """A soft gate cannot stop a barrier's leaves once they are dispatched, so
    several of them land past the ceiling. The FIRST one to cross writes the
    advice; the ones behind it write nothing (one fault per crossing, never one
    per leaf)."""
    spec = validate_spec(
        {
            "meta": {"name": "slf", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "cheap"},
                {
                    "id": "P",
                    "type": "parallel",
                    "depends_on": ["a"],
                    "branches": [{"type": "agent", "prompt": f"costly b{i}"} for i in range(3)],
                },
            ],
        }
    )
    # 'a' teaches the run that a leaf costs 10, so the fan-out looks affordable;
    # the branches then cost 700 each and blow through the ceiling together.
    core = _core(db, lambda prompt: 700 if "costly" in prompt else 10)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=1000))
        result = engine.run(spec, {})
        assert engine.budget.tokens_spent == 2110  # 10 + 3 x 700, all charged
        assert engine.budget.overrun == 1110
        assert sum("token budget overrun" in f for f in result.faults) == 1
    finally:
        core.shutdown()


def test_a_nested_run_reports_its_overrun_through_the_parent(db):
    """The nested engine shares the parent's budget by reference, so the
    crossing is recorded wherever it happens and folds up namespaced — the same
    treatment every other nested advisory gets."""
    inner = {
        "meta": {"name": "inner", "version": 1},
        "nodes": [{"id": "i1", "type": "agent", "prompt": "one"}],
    }
    spec = validate_spec(
        {
            "meta": {"name": "outer", "version": 1},
            "nodes": [{"id": "n", "type": "workflow", "ref": "inner"}],
        }
    )
    core = _core(db, 700)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=100), loader={"inner": inner}.get)
        result = engine.run(spec, {})
        assert [f for f in result.advisory_faults if "token budget overrun" in f] == [
            "sub[inner]: token budget overrun: spent 700 of 100 (leaf i1)"
        ]
    finally:
        core.shutdown()


# --- the pause as the RENEWAL CHECKPOINT (service level) ----------------


def _service(db, home, cost):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=CostedClient(cost),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


_THREE_NODE = {
    "meta": {"name": "stopline", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
        {"id": "c", "type": "agent", "prompt": "last", "depends_on": ["b"]},
    ],
}


def test_a_bump_that_does_not_buy_one_leaf_re_pauses_before_spawning(db, tmp_path):
    """The edge the operator actually hits (issue #71). A run paused at 700 of
    1000 is handed 1200 — more than it spent, so ``refuse_spent_budget`` lets it
    through — but 500 does not pay for a leaf this run has measured at 700. It
    must re-pause BEFORE spawning, saying so, or the human sees a resume that
    "did nothing" and repeats it.

    Two services on purpose: the second one is a fresh process, so the measured
    average has to survive on disk (the cached cells), not in the engine."""
    svc = _service(db, tmp_path, 700)
    try:
        run_id = svc.start(_THREE_NODE, {}, token_budget=1000)["run_id"]
        first = svc.status(run_id, wait=True, timeout=10)
        assert first["status"] == "paused"
        assert first["token_budget"] == {
            "total": 1000, "spent": 700, "remaining": 300, "overrun": 0
        }
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, 700)
    try:
        assert "error" not in svc2.start(resume_run_id=run_id, token_budget=1200)
        again = svc2.status(run_id, wait=True, timeout=10)
        assert again["status"] == "paused"
        assert again["reason"] == TOKEN_BUDGET_EXHAUSTED
        # Nothing was spawned: the bump bought the run no leaf at all, and the
        # fault is what tells the human that instead of leaving them to guess.
        assert again["token_budget"]["spent"] == 700
        assert any(
            "next leaf estimated at 700 tokens (measured average), "
            "only 500 left of 1200" in fault
            for fault in again["faults_total"]
        ), again["faults_total"]
    finally:
        svc2.shutdown()


def test_a_bump_that_does_buy_a_leaf_replays_the_cells_and_carries_on(db, tmp_path):
    """The other half of the same edge: a ceiling that DOES pay for the next
    leaf resumes from the cache and spends only on what is left to do."""
    svc = _service(db, tmp_path, 700)
    try:
        run_id = svc.start(_THREE_NODE, {}, token_budget=1000)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, 700)
    try:
        assert "error" not in svc2.start(resume_run_id=run_id, token_budget=2100)
        out = svc2.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        # 'a' replayed from its cached cell; only 'b' and 'c' were re-spawned.
        assert out["cells_replayed"] == 1
        assert out["token_budget"] == {
            "total": 2100, "spent": 2100, "remaining": 0, "overrun": 0
        }
    finally:
        svc2.shutdown()


def test_a_certified_template_says_how_far_past_the_ceiling_its_run_went(db, tmp_path):
    """An overrun does not degrade the run, so it reaches ``library`` as
    ``complete`` and SHOULD. Certifying it silently would publish a template
    whose only measured run cost 7x what the operator authorized — so the number
    rides into ``meta``, exactly where the artifact divergences ride, and the
    divergence count is NOT inflated by an advisory that is not about an
    artifact."""
    svc = _service(db, tmp_path, 700)
    try:
        run_id = svc.start(
            {"meta": {"name": "overspender", "version": 1},
             "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]},
            {},
            token_budget=100,
        )["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()
    templates = library.list_templates(tmp_path)
    assert [t["name"] for t in templates] == ["overspender"]
    assert templates[0]["budget_overrun"] == 600
    assert templates[0]["artifact_divergences"] == 0


def test_a_resume_keeps_measuring_with_this_runs_own_rate(db, tmp_path):
    """The seed that makes the gate above possible: a resumed run counts the
    leaves earlier stretches priced, not just the tokens they spent. Without the
    count the average falls back to a static constant and the stop line stops
    being about THIS run."""
    from lohra.workflow.spend import seed_charges, seed_spend

    svc = _service(db, tmp_path, 700)
    try:
        run_id = svc.start(_THREE_NODE, {}, token_budget=1000)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc.shutdown()
    assert seed_spend(db, run_id) == (700, 0)
    assert seed_charges(db, run_id) == 1
    assert Budget(token_budget=1200, tokens_in=700, charges=1).est_leaf_cost == 700
    # ...and a run that priced nothing seeds nothing: the constant is right there.
    assert seed_charges(db, "run-that-never-ran") == 0
