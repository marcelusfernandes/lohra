"""Real token budget for a workflow run (CC-parity M5) — the promise of budget.py.

The budget had a concurrency width and a leaf-spawn lifetime but no token
dimension at all, so "bounded by construction" stopped at the number of leaves
and said nothing about what they cost. M5 adds the missing axis:

- a SOFT gate, checked before every leaf spawn (Claude Code's contract): work
  already in flight is allowed to finish and is charged — it was already paid
  for. Only the NEXT spawn is refused;
- an overrun PAUSES the run (never a silent cap): one fault naming spent/total,
  the finished cells kept in the resume cache;
- a budget pause does NOT auto-resume (waiting does not refill a budget, unlike
  provider quota), and a resume is RAISE-ONLY: a token_budget at or under what
  the run already spent is refused instead of re-pausing instantly;
- spend is cumulative across a resume: per-cell costs land in the cache and a
  run-level total seeds the next engine.

Every leaf here costs a deterministic 8 tokens (the fake usage is 5 in / 3 out),
so the arithmetic in these tests is exact. No real sleeps: the one in-flight
test releases its gate from the pause latch itself.
"""

import threading
import time

import pytest

from lohra.agent.types import Usage

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.state import SessionDB
from lohra.workflow import budget as budget_module
from lohra.workflow import library, strategies
from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED, Budget, LifetimeExhausted
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from lohra.workflow.spend import seed_spend as _seed_spend
from tests.test_workflow_pipeline import ScriptedClient
from tests.test_workflow_quota import TimerFactory, _rate_limited

LEAF_COST = 8  # one fake turn: 5 input + 3 output tokens


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _ok(_prompt):
    return "R"


# --- 1. Budget: the token axis itself -----------------------------------


def test_no_token_budget_is_unlimited_and_invisible():
    budget = Budget()
    budget.charge_tokens(1_000_000, 1_000_000)
    assert budget.tokens_exhausted is False
    assert budget.snapshot() is None  # nothing to report when nothing was asked


def test_charge_tokens_accumulates_in_and_out():
    budget = Budget(token_budget=100)
    budget.charge_tokens(5, 3)
    budget.charge_tokens(2, 1)
    assert (budget.tokens_in, budget.tokens_out) == (7, 4)
    assert budget.tokens_spent == 11
    assert budget.tokens_remaining == 89
    assert budget.tokens_exhausted is False


def test_the_budget_is_spent_once_the_total_is_reached():
    budget = Budget(token_budget=10)
    budget.charge_tokens(6, 4)
    assert budget.tokens_exhausted is True  # spent >= total, not strictly over


def test_remaining_clamps_at_zero_but_spent_stays_honest():
    # An in-flight leaf may overshoot (the gate is soft). "remaining: -40" would
    # read as a negative allowance; the honest number is what was really spent.
    budget = Budget(token_budget=10)
    budget.charge_tokens(30, 20)
    assert budget.tokens_spent == 50
    assert budget.tokens_remaining == 0
    assert budget.snapshot() == {"total": 10, "spent": 50, "remaining": 0}


def test_a_budget_can_start_already_spent():
    # What a resume needs: the new engine picks up where the paused one stopped.
    budget = Budget(token_budget=100, tokens_in=30, tokens_out=20)
    assert budget.tokens_spent == 50 and budget.tokens_remaining == 50


# --- 2. engine: the soft pre-spawn gate --------------------------------


def _two_node_spec():
    return validate_spec(
        {
            "meta": {"name": "tb", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go"},
                {"id": "b", "type": "agent", "prompt": "then ${a}"},
            ],
        }
    )


def test_a_run_inside_its_budget_finishes_and_reports_its_spend(db):
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=100))
        result = engine.run(_two_node_spec(), {})
        assert result.status == "complete"
        assert engine.budget.tokens_spent == 2 * LEAF_COST
    finally:
        core.shutdown()


def test_the_gate_pauses_the_run_instead_of_capping_it_silently(db):
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=5))
        result = engine.run(_two_node_spec(), {})
        # 'a' fit (nothing was spent yet); 'b' is refused BEFORE its spawn.
        assert result.outputs["a"] == "R"
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert result.retry_after is None  # waiting does not refill a budget
        assert [f for f in result.faults] == [
            f"token budget exhausted: spent {LEAF_COST} of 5 tokens"
        ]
    finally:
        core.shutdown()


def test_the_pause_keeps_the_completed_cell_in_the_cache(db):
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(
            core, budget=Budget(token_budget=5), cache=NodeCache(db, "run-TB")
        )
        engine.run(_two_node_spec(), {})
        # The work already paid for survives for the resume — that is the whole
        # point of pausing instead of failing.
        assert NodeCache(db, "run-TB").total_cost() == (5, 3)
    finally:
        core.shutdown()


def _pipeline_spec(items, stages=1):
    return validate_spec(
        {
            "meta": {"name": "tbp", "version": 1},
            "nodes": [
                {
                    "id": "p",
                    "type": "pipeline",
                    "items": items,
                    "stages": [
                        {"type": "agent", "prompt": f"stage{i} ${{item}}"} for i in range(stages)
                    ],
                }
            ],
        }
    )


def test_the_pipeline_gate_pauses_with_exactly_one_fault(db, monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 5.0)
    core = _core(db, _ok, pool_width=2)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=1))
        result = engine.run(_pipeline_spec(["a", "b", "c", "d"], stages=2), {})
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        # N gated cells must not write N faults — the latch records exactly once.
        assert sum("token budget exhausted" in f for f in result.faults) == 1
    finally:
        core.shutdown()


def test_a_leaf_in_flight_is_never_killed_by_the_gate(db, monkeypatch):
    """SOFT: work already spawned is work already paid for. A quota pause kills
    the in-flight leaves (they would all 429 too); a budget pause must NOT —
    cancelling them burns the tokens and throws away the answer."""
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 5.0)
    release = threading.Event()

    def responder(prompt):
        if "slow" in prompt:
            release.wait(5)  # still in flight when the budget runs out
        return "R"

    core = _core(db, responder, pool_width=4)
    cancels: list[str] = []
    original_cancel = core.cancel
    core.cancel = lambda sub_id: (cancels.append(sub_id), original_cancel(sub_id))[1]

    engine = WorkflowEngine(core, budget=Budget(token_budget=5))
    latched = engine.note_budget_exhausted

    def spy(node_id):
        latched(node_id)
        release.set()  # only now may the slow leaf finish — nobody cancelled it

    engine.note_budget_exhausted = spy
    try:
        # "slow" is dispatched first and blocks, so the fast item's first stage
        # is what overruns the budget; its SECOND stage then trips the gate.
        result = engine.run(_pipeline_spec(["slow", "fast"], stages=2), {})
        assert result.status == "paused"
        assert cancels == [], "a budget pause must not cancel in-flight leaves"
        # Both leaves are charged: the in-flight one finished and counted.
        assert engine.budget.tokens_spent == 2 * LEAF_COST
    finally:
        release.set()
        core.shutdown()


_INNER = {
    "meta": {"name": "inner", "version": 1},
    "nodes": [
        {"id": "i1", "type": "agent", "prompt": "one"},
        {"id": "i2", "type": "agent", "prompt": "two", "depends_on": ["i1"]},
    ],
}


def test_a_nested_workflow_cannot_escape_the_budget(db):
    spec = validate_spec(
        {
            "meta": {"name": "outer", "version": 1},
            "nodes": [
                {"id": "n", "type": "workflow", "ref": "inner"},
                {"id": "after", "type": "agent", "prompt": "later", "depends_on": ["n"]},
            ],
        }
    )
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(
            core, budget=Budget(token_budget=5), loader={"inner": _INNER}.get
        )
        result = engine.run(spec, {})
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert "after" not in result.outputs  # the parent stopped scheduling too
    finally:
        core.shutdown()


def test_a_quota_pause_still_reports_quota(db):
    """Generalising the latch must not relabel the pause it already had."""

    def quota(_prompt):
        raise _rate_limited("30")

    core = _core(db, quota)
    try:
        result = WorkflowEngine(core, budget=Budget(token_budget=10_000)).run(
            _two_node_spec(), {}
        )
        assert result.status == "paused"
        assert result.pause_reason == QUOTA_EXHAUSTED
        assert result.retry_after == 30.0
    finally:
        core.shutdown()


# --- 3. per-cell cost in the cache (M5-a) ------------------------------


def test_a_cached_cell_carries_what_it_cost(db):
    cache = NodeCache(db, "run-C")
    cache.put_complete("h1", "node", {"n": 5}, Usage(input_tokens=11, output_tokens=7))
    assert cache.get("h1") == (True, {"n": 5})
    assert cache.total_cost() == (11, 7)


def test_a_cache_row_written_without_a_cost_still_reads(db):
    # Backward-compat by construction: rows from before M5 have no cost sidecar.
    cache = NodeCache(db, "run-OLD")
    cache.put_complete("h", "n", "v")
    assert cache.get("h") == (True, "v")
    assert cache.total_cost() == (0, 0)


def test_cache_cost_is_run_scoped(db):
    NodeCache(db, "run-1").put_complete("h", "n", "v", Usage(input_tokens=4, output_tokens=2))
    assert NodeCache(db, "run-2").total_cost() == (0, 0)


def test_the_engine_persists_each_cell_cost(db):
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(
            core, budget=Budget(token_budget=100), cache=NodeCache(db, "run-E")
        )
        engine.run(_two_node_spec(), {})
        assert NodeCache(db, "run-E").total_cost() == (10, 6)  # 2 cells x (5, 3)
    finally:
        core.shutdown()


def test_seed_spend_takes_the_larger_of_the_two_honest_counts(db):
    """Both sources UNDERCOUNT in different ways, so the bigger one is the
    better lower bound — never whichever happens to exist.

    The run-level row misses whatever ran after it was last written; the cells
    miss every leaf that died or was never cached. Preferring the row outright
    would zero out a crashed run's real spend, since the row is written (seeded)
    the moment a run starts."""
    assert _seed_spend(db, "run-NONE") == (0, 0)

    # No row yet at all -> the cells are all we have.
    NodeCache(db, "run-S").put_complete("h", "n", "v", Usage(input_tokens=9, output_tokens=6))
    assert _seed_spend(db, "run-S") == (9, 6)

    # A clean pause: the row was rewritten at the end, so it is ahead (it also
    # counts the leaves that died and cached nothing).
    db.run_spend_put("run-S", 500, 40, 25)
    assert _seed_spend(db, "run-S") == (40, 25)

    # A CRASH: the row still holds this stretch's seed while the cells recorded
    # real work. Trusting the stale row would resume as if nothing was spent.
    db.run_spend_put("run-C", 500, 0, 0)
    NodeCache(db, "run-C").put_complete("c", "n", "v", Usage(input_tokens=10, output_tokens=6))
    assert _seed_spend(db, "run-C") == (10, 6)


# --- 4. service: the tool-facing contract ------------------------------


_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _service(db, home, responder, *, timers=None):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=home)
    if timers is not None:
        svc.set_autoresume(
            AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: 1000.0)
        )
    return svc


def test_a_bad_token_budget_is_refused_before_anything_spawns(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        for bad in (0, -1, "lots", 1.5, True):
            out = svc.start(_TWO_NODE, {}, token_budget=bad)
            assert "token_budget" in out["error"]
            assert "e.g." in out["error"]  # didactic: show the fix
    finally:
        svc.shutdown()


def test_status_reports_total_spent_and_remaining(db, tmp_path):
    svc = _service(db, tmp_path, _ok, timers=TimerFactory())
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == TOKEN_BUDGET_EXHAUSTED
        assert out["token_budget"] == {"total": 5, "spent": LEAF_COST, "remaining": 0}
        # Nothing is coming to wake this run, so the reply has to say what does.
        assert "token_budget" in out["hint"] and "resume_run_id" in out["hint"]
    finally:
        svc.shutdown()


def test_the_budget_is_visible_while_the_run_is_still_going(db, tmp_path):
    """Mid-run there is no RunResult yet — and mid-run is exactly when knowing
    what is left changes what the agent does. Read it off the live engine."""
    release = threading.Event()
    svc = _service(db, tmp_path, lambda _p: (release.wait(5), "R")[1])
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=900)["run_id"]
        out = svc.status(run_id)  # no wait: the first leaf is still blocked
        assert out["status"] == "running"
        assert "nodes_total" not in out  # no result to summarise yet...
        assert out["token_budget"] == {"total": 900, "spent": 0, "remaining": 900}
    finally:
        release.set()
        svc.shutdown()


def test_a_run_without_a_token_budget_reports_none(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert "token_budget" not in out
    finally:
        svc.shutdown()


def test_a_budget_pause_arms_no_auto_resume(db, tmp_path):
    """Quota comes back on its own; a budget does not. Auto-resuming here would
    burn the five attempts re-pausing on the first spawn every time."""
    timers = TimerFactory()
    svc = _service(db, tmp_path, _ok, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert timers.timers == []
        assert out["resume_at"] is None
    finally:
        svc.shutdown()


def test_a_budget_paused_run_never_teaches_the_library(db, tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(library, "record_outcome", lambda *a, **k: calls.append(a))
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        # The SHAPE did not fail — the operator's budget ran out.
        assert calls == []
    finally:
        svc.shutdown()


def test_resume_refuses_a_budget_that_is_already_spent(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        for too_low in (5, LEAF_COST):
            out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=too_low)
            assert "error" in out
            assert str(LEAF_COST) in out["error"] and "token_budget" in out["error"]
        # ...and the refusal left the run paused, not clobbered.
        assert svc.status(run_id)["status"] == "paused"
    finally:
        svc.shutdown()


def test_resume_without_a_budget_inherits_the_persisted_one(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        # Inheriting an EXHAUSTED budget would re-pause on the first spawn: the
        # raise-only rule turns that silent loop into one clear instruction.
        out = svc.resume(run_id)
        assert "error" in out and "token_budget" in out["error"]
    finally:
        svc.shutdown()


def test_a_quota_pause_still_inherits_its_budget_and_resumes(db, tmp_path):
    timers = TimerFactory()
    quota = {"on": True}

    def responder(_prompt):
        if quota["on"]:
            raise _rate_limited("30")
        return "R"

    svc = _service(db, tmp_path, responder, timers=timers)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=1000)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["reason"] == QUOTA_EXHAUSTED
        quota["on"] = False
        timers.last.fire()  # inherits token_budget=1000, which is NOT spent
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "complete"
        assert out["token_budget"]["total"] == 1000
    finally:
        svc.shutdown()


def test_spend_is_cumulative_across_a_resume(db, tmp_path):
    svc = _service(db, tmp_path, _ok)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=40)
        assert out["run_id"] == run_id
        final = svc.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
        # 'a' replayed from cache (no new spawn); 'b' cost one more leaf. The
        # resume counts BOTH stretches — a counter reset to 0 here would let a
        # run loop forever under a budget it already blew through.
        assert final["token_budget"] == {
            "total": 40,
            "spent": 2 * LEAF_COST,
            "remaining": 40 - 2 * LEAF_COST,
        }
    finally:
        svc.shutdown()


# --- 5. the tool surface the model actually sees -----------------------


def test_the_run_tool_offers_and_forwards_a_token_budget():
    from lohra.workflow.tools import _RUN_SCHEMA, RUN_GUIDANCE, WorkflowTool

    assert "token_budget" in _RUN_SCHEMA["parameters"]["properties"]
    assert "token_budget" in RUN_GUIDANCE
    # The contract the existing skill tests rest on must survive the new clause.
    assert "resume_run_id" in RUN_GUIDANCE and "paused" in RUN_GUIDANCE

    seen: dict = {}

    class Svc:
        def start(self, spec, args, **kwargs):
            seen.update(kwargs)
            return {"run_id": "r1", "status": "started"}

    WorkflowTool(Svc()).run({"spec": {"meta": {}}, "token_budget": 5000})
    assert seen["token_budget"] == 5000


def test_a_budget_refusal_does_not_read_as_a_broken_spec():
    from lohra.workflow.tools import WorkflowTool

    class Svc:
        def start(self, spec, args, **kwargs):
            return {"error": "already spent 800 tokens"}

    out = WorkflowTool(Svc()).run({"spec": {"meta": {}}, "token_budget": 1})
    # Calling this a spec problem sends the author rewriting a spec that is fine.
    assert "invalid workflow spec" not in out
    assert "already spent 800 tokens" in out


def test_a_broken_spec_still_says_so(db, tmp_path):
    from lohra.workflow.tools import WorkflowTool

    svc = _service(db, tmp_path, _ok)
    try:
        out = WorkflowTool(svc).run({"spec": {"nodes": []}})
        assert "invalid workflow spec" in out
    finally:
        svc.shutdown()


# --- 6. barrier fan-outs: the width gate (§7.1 cost gate) ---------------


@pytest.fixture
def cheap_leaves(monkeypatch):
    """Assume a leaf costs what the fake one really costs, so the arithmetic of
    the affordability gate is exact instead of drowning in the real estimate."""
    monkeypatch.setattr(budget_module, "EST_TOKENS_PER_LEAF", LEAF_COST)


def _parallel_spec(branches):
    return validate_spec(
        {
            "meta": {"name": "tbf", "version": 1},
            "nodes": [
                {
                    "id": "P",
                    "type": "parallel",
                    "branches": [{"type": "agent", "prompt": f"b{i}"} for i in range(branches)],
                }
            ],
        }
    )


def test_a_barrier_fanout_cannot_dispatch_past_the_ceiling(db, cheap_leaves):
    """The per-spawn gate alone bounds NOTHING here: a barrier dispatches its
    whole width before any of it is charged, so every check reads the same stale
    zero. The width gate is what makes the ceiling real."""
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=2 * LEAF_COST))
        result = engine.run(_parallel_spec(10), {})
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert engine.budget.tokens_spent == 0  # refused BEFORE it spent 10 leaves
        assert sum("token budget" in f for f in result.faults) == 1
        assert "10" in result.faults[0]  # didactic: the width it asked for
    finally:
        core.shutdown()


def test_a_fanout_the_budget_can_afford_still_runs(db, cheap_leaves):
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=100))
        result = engine.run(_parallel_spec(2), {})
        assert result.status == "complete"
        assert result.outputs["P"] == ["R", "R"]
        assert engine.budget.tokens_spent == 2 * LEAF_COST
    finally:
        core.shutdown()


def test_the_estimate_gives_way_to_what_the_run_really_spends(db, monkeypatch):
    """A static per-leaf guess would refuse this fan-out. The run has already
    measured its own leaves, and its OWN average is the better number."""
    monkeypatch.setattr(budget_module, "EST_TOKENS_PER_LEAF", 1000)
    spec = validate_spec(
        {
            "meta": {"name": "tbc", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go"},
                {
                    "id": "P",
                    "type": "parallel",
                    "depends_on": ["a"],
                    "branches": [{"type": "agent", "prompt": f"b{i}"} for i in range(3)],
                },
            ],
        }
    )
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=40))
        result = engine.run(spec, {})
        assert result.status == "complete"
        assert engine.budget.tokens_spent == 4 * LEAF_COST
    finally:
        core.shutdown()


def test_a_verify_panel_is_gated_by_the_same_width_check(db, cheap_leaves):
    spec = validate_spec(
        {
            "meta": {"name": "tbv", "version": 1},
            "nodes": [{"id": "V", "type": "verify", "finding": "the claim", "skeptics": 5}],
        }
    )
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=2 * LEAF_COST))
        result = engine.run(spec, {})
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert engine.budget.tokens_spent == 0
    finally:
        core.shutdown()


# --- 7. a gated node keeps the work it already paid for ----------------


def _scoring(_prompt):
    return '{"score": 9}' if "Score this attempt" in _prompt else "R"


def _judge_spec(attempts):
    node = {
        "id": "J",
        "type": "judge_panel",
        "judges": 1,
        "attempts": [{"type": "agent", "prompt": f"a{i}"} for i in range(attempts)],
        "synthesize": {"type": "agent", "prompt": "synthesize ${winner}"},
    }
    return validate_spec({"meta": {"name": "tbj", "version": 1}, "nodes": [node]})


def test_judge_panel_crowns_the_attempts_it_already_scored(db, cheap_leaves):
    """Two attempts spawn and score; the second judge is unaffordable. Nulling
    the node here would throw away a fully scored, already-billed candidate."""
    core = _core(db, _scoring)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=3 * LEAF_COST))
        result = engine.run(_judge_spec(2), {})
        assert result.outputs["J"] == "R"  # the scored winner survives the pause
        assert result.status == "paused"
        assert result.pause_reason == TOKEN_BUDGET_EXHAUSTED
        assert sum("token budget" in f for f in result.faults) == 1
    finally:
        core.shutdown()


def test_judge_panel_returns_the_winner_when_it_cannot_afford_to_synthesize(db, cheap_leaves):
    core = _core(db, _scoring)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=2 * LEAF_COST))
        result = engine.run(_judge_spec(1), {})
        assert result.outputs["J"] == "R"  # unsynthesised, but not thrown away
        assert result.status == "paused"
    finally:
        core.shutdown()


def _loop_spec(node_id="L", depends_on=None):
    node = {
        "id": node_id,
        "type": "loop_until_dry",
        "stop_after_k_empty": 1,
        "max_rounds": 3,
        "body": {"type": "agent", "prompt": "round ${round}"},
    }
    if depends_on:
        node["depends_on"] = depends_on
    return node


def test_loop_until_dry_keeps_the_rounds_it_already_harvested(db, cheap_leaves):
    spec = validate_spec({"meta": {"name": "tbl", "version": 1}, "nodes": [_loop_spec()]})
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=LEAF_COST))
        result = engine.run(spec, {})
        assert result.outputs["L"] == ["R"]  # round 0 was real work, really billed
        assert result.status == "paused"
        assert engine.budget.tokens_spent == LEAF_COST
    finally:
        core.shutdown()


def test_a_loop_gated_before_its_first_round_is_null_not_empty(db, cheap_leaves):
    """[] would claim the loop ran dry and found nothing. It never ran at all."""
    spec = validate_spec(
        {
            "meta": {"name": "tbl0", "version": 1},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "go"},
                _loop_spec(depends_on=["a"]),
            ],
        }
    )
    core = _core(db, _ok)
    try:
        engine = WorkflowEngine(core, budget=Budget(token_budget=LEAF_COST))
        result = engine.run(spec, {})
        assert result.outputs["a"] == "R"
        assert result.outputs["L"] is None
        assert result.status == "paused"
    finally:
        core.shutdown()


# --- 6. the LIFETIME axis: an atomic reserve, not check-then-charge (#14) ---


def test_reserve_is_atomic_across_concurrent_claimers():
    """The CONTRACT of the reserve: N claimers, one grant, nothing over-taken.

    Honest about what it is: a contract test, not the discriminator. The two
    lock acquisitions it replaces sat back-to-back here, so racing them directly
    is luck — ``test_concurrent_spawns_cannot_oversubscribe_the_lifetime`` is the
    one that separates the hypotheses, because it holds the window open where the
    real I/O lives. (Measured: that one yields ['spawned', 'spawned'] against
    check-then-charge and ['refused', 'spawned'] against this.)"""
    budget = Budget(lifetime=1)
    granted: list[bool] = []
    ready = threading.Barrier(8)
    lock = threading.Lock()

    def claim():
        ready.wait(5)  # everybody reads the ledger in the same instant
        ok = budget.reserve(1)
        with lock:
            granted.append(ok)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sum(granted) == 1, "a lifetime of 1 must grant exactly one claim"
    assert budget.lifetime_remaining == 0


def test_a_refund_returns_a_slot_and_never_goes_below_zero():
    budget = Budget(lifetime=2)
    assert budget.reserve(1) is True
    assert budget.lifetime_remaining == 1
    budget.refund(1)
    assert budget.lifetime_remaining == 2
    budget.refund(5)  # a double refund must not MINT lifetime
    assert budget.lifetime_remaining == 2


def test_concurrent_spawns_cannot_oversubscribe_the_lifetime(db):
    """The race where it actually bites: pipeline on_done workers advance items
    concurrently, so the check at ``_advance`` and the charge inside the engine's
    spawn funnel are separated by real, unlocked I/O.

    The barrier makes it deterministic: both threads are INSIDE ``core.spawn``
    (past the check, before the charge) at the same time. Before the atomic
    reserve, both were granted and the run spawned twice its declared lifetime."""
    core = _core(db, _ok, pool_width=4)
    original_spawn = core.spawn

    def slow_spawn(*args, **kwargs):
        # Stand in for what really sits between the check and the charge: a DB
        # write, a GatewaySession, a pool submit. Without it the window is a few
        # instructions wide and the race is luck; with it the pre-fix outcome is
        # reliably "both granted".
        time.sleep(0.05)
        return original_spawn(*args, **kwargs)

    core.spawn = slow_spawn
    engine = WorkflowEngine(core, budget=Budget(lifetime=1))
    at_the_gate = threading.Barrier(2, timeout=5)
    outcomes: list[str] = []
    lock = threading.Lock()

    def claim():
        at_the_gate.wait()  # both threads decide against the SAME ledger
        try:
            engine.spawn_leaf("go")
            with lock:
                outcomes.append("spawned")
        except LifetimeExhausted:
            with lock:
                outcomes.append("refused")

    try:
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert sorted(outcomes) == ["refused", "spawned"], outcomes
        assert engine.budget.lifetime_remaining == 0
    finally:
        core.spawn = original_spawn
        core.shutdown()


def test_a_leaf_that_never_ran_gives_its_lifetime_slot_back(db):
    """A leaf cancelled while still QUEUED consumed no provider call at all.
    Keeping its slot charged spends the run's declared lifetime on work that
    never happened — and, before issue #8, its terminal transition never even
    fired, so no refund hook could have run."""
    gate, started = threading.Event(), threading.Event()

    def responder(_prompt):
        started.set()
        gate.wait(5)
        return "R"

    core = _core(db, responder, pool_width=1)
    try:
        engine = WorkflowEngine(core, budget=Budget(lifetime=4))
        engine.spawn_leaf("occupies the only worker")
        assert started.wait(5)
        queued = engine.spawn_leaf("never starts")
        assert engine.budget.lifetime_remaining == 2  # both claimed a slot
        core.cancel(queued)
        engine.account_leaf(queued)
        assert engine.budget.lifetime_remaining == 3  # the one that never ran is back
        engine.account_leaf(queued)  # exactly once, whatever path reaches it twice
        assert engine.budget.lifetime_remaining == 3
    finally:
        gate.set()
        core.shutdown()


def test_a_leaf_that_ran_and_failed_stays_charged(db):
    """The counterpart, and deliberate: ``token_budget`` defaults to None, so the
    lifetime is the ONLY hard bound a run has by default. Refunding a leaf that
    actually ran would let an always-failing retry shape spawn forever."""
    def boom(_prompt):
        raise RuntimeError("leaf died")

    core = _core(db, boom, pool_width=2)
    try:
        engine = WorkflowEngine(core, budget=Budget(lifetime=4))
        sub_id = engine.spawn_leaf("go")
        core.collect(sub_id, wait=True, timeout=5)
        assert core.collect(sub_id)["status"] == "error"
        engine.account_leaf(sub_id)
        assert engine.budget.lifetime_remaining == 3  # ran: stays charged
    finally:
        core.shutdown()
