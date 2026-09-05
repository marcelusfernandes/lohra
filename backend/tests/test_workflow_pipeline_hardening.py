"""Done-path + liveness hardening of the pipeline scheduler (CC-parity, fatia B).

The pipeline settles items from ``on_done`` callbacks that run on orch workers,
where the core only LOGS a raise. These tests pin the invariants that keep a run
honest and alive:
- a crashing done-path settles its item (never strands it until the barrier);
- a barrier timeout is a FAULT in the rollup, not just a log line;
- a straggler landing after the timeout can't mutate/cache/account anything;
- resuming a run that is still live is refused (no shared node cache).
"""

import threading

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import quiescence, strategies
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_loop import FakeClient, _text_response
from tests.test_workflow_pipeline import ScriptedClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture(autouse=True)
def fast_quiescence(monkeypatch):
    """The barrier-timeout tests block their leaves INSIDE the client, where a
    cooperative cancel cannot reach them, so each would otherwise spend the full
    quiescence cap (issue #42-B) waiting for a leaf that cannot obey. The wait
    itself is proved in ``test_workflow_quiescence.py``."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)


def _core(db, responder, *, pool_width=4):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return OrchestrationCore(db, factory, max_concurrent=pool_width)


def _pipeline_spec(stages):
    return validate_spec(
        {
            "meta": {"name": "p"},
            "nodes": [{"id": "p", "type": "pipeline", "items": "${args.items}", "stages": stages}],
        }
    )


_ONE_STAGE = [{"type": "agent", "prompt": "do ${item}"}]


# --- WF-13: an exception in the done-path must settle the item ---


def test_on_done_crash_settles_the_item_instead_of_hanging(db, monkeypatch):
    # The core swallows a raise from on_done (it only logs), so without the guard
    # finish() never runs and the item hangs until the barrier expires.
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 2.0)
    core = _core(db, lambda prompt: "ok")
    engine = WorkflowEngine(core, budget=Budget())

    def boom(sub_id):
        raise RuntimeError("done-path exploded")

    engine.account_leaf = boom  # crash INSIDE the on_done body
    try:
        result = engine.run(_pipeline_spec(_ONE_STAGE), {"items": ["a", "b"]})
        assert result.outputs["p"] == [None, None]
        assert sum("done-path exploded" in f for f in result.faults) == 2
        assert not any("timed out" in f for f in result.faults)  # settled, not stranded
    finally:
        core.shutdown()


# --- barrier timeout is a fault, not just a log line ---


def test_timeout_records_a_fault_and_degrades_the_run(db, monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    try:
        result = WorkflowEngine(core, budget=Budget()).run(_pipeline_spec(_ONE_STAGE), {"items": ["a"]})
        assert result.outputs["p"] == [None]
        assert any("timed out" in f for f in result.faults)
        assert result.status == "degraded"  # a silent timeout would read "complete"
    finally:
        gate.set()
        core.shutdown()


# --- a straggler that lands after the barrier expired is discarded ---


def test_straggler_after_timeout_cannot_mutate_cache_or_account(db, monkeypatch):
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1])
    engine = WorkflowEngine(core, budget=Budget())
    stores: list = []
    accounted: list = []
    engine.cache_store = lambda *a: stores.append(a)
    engine.account_leaf = lambda sub_id: accounted.append(sub_id)
    try:
        result = engine.run(_pipeline_spec(_ONE_STAGE), {"items": ["a"]})
        output = result.outputs["p"]
        assert output == [None]
        gate.set()
        core.shutdown()  # waits for the pool: the straggler's on_done has run by now
        assert output == [None]  # the reported list is a copy — no late mutation
        assert stores == [] and accounted == []
    finally:
        gate.set()
        core.shutdown()


# --- WF-17: resuming a run that is still live must be refused ---


_SPEC = {"meta": {"name": "demo"}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]}


def _blocking_service(db, home, gate):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(lambda prompt: (gate.wait(5), "ok")[1]),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def _fast_service(db, home, reply="DONE"):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


def test_resume_of_a_live_run_is_refused(db, tmp_path):
    gate = threading.Event()
    svc = _blocking_service(db, tmp_path, gate)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        clash = svc.start(_SPEC, {}, resume_run_id=run_id)
        assert "error" in clash and run_id in clash["error"]
        assert "run_id" not in clash
        gate.set()
        final = svc.status(run_id, wait=True, timeout=10)  # the live run is untouched
        assert final["status"] == "complete"
        assert final["outputs"]["a"] == "ok"
    finally:
        gate.set()
        svc.shutdown()


def test_resume_of_a_finished_run_is_allowed(db, tmp_path):
    svc = _fast_service(db, tmp_path)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        again = svc.start(_SPEC, {}, resume_run_id=run_id)
        assert again.get("run_id") == run_id and "error" not in again
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_refused_resume_does_not_leak_a_zombie_slot(db, tmp_path):
    # A refusal must leave the registry exactly as it was: the original run still
    # resumable once it finishes.
    gate = threading.Event()
    svc = _blocking_service(db, tmp_path, gate)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        assert "error" in svc.start(_SPEC, {}, resume_run_id=run_id)
        gate.set()
        svc.status(run_id, wait=True, timeout=10)
        assert svc.start(_SPEC, {}, resume_run_id=run_id).get("run_id") == run_id
    finally:
        gate.set()
        svc.shutdown()


def test_refused_resume_never_starts_a_second_engine(db, tmp_path):
    # The harm the refusal prevents: two engines running the same run_id at once,
    # sharing its node cache. A refusal must spawn NO leaf.
    gate = threading.Event()
    leaves = []
    lock = threading.Lock()

    def responder(prompt):
        with lock:
            leaves.append(prompt)
        gate.wait(5)
        return "ok"

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    svc = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        run_id = svc.start(_SPEC, {})["run_id"]
        while not leaves:  # the first run is live, blocked inside its leaf
            threading.Event().wait(0.01)
        assert "error" in svc.start(_SPEC, {}, resume_run_id=run_id)
        gate.set()
        svc.status(run_id, wait=True, timeout=10)
        assert len(leaves) == 1  # only the original run ever executed
    finally:
        gate.set()
        svc.shutdown()


# --- the lifetime a pipeline runs out of MID-CHAIN is a fault, not a null ---


def test_lifetime_exhausted_mid_pipeline_degrades_the_run_with_a_named_fault(db):
    """``N items x M stages`` can outrun the declared lifetime, and the refusal
    lands INSIDE ``_advance`` — on an on_done worker, not the node thread.

    It used to be a log line and a null item: the run sealed ``complete``, with
    no fault and no cap trip, so the library could certify a TRUNCATED pipeline
    as a reusable template. Every other node type reaches ``FanoutRejected``
    through the engine's node handler and gets a fault plus a cap trip; this
    path has to say the same thing on its own."""
    core = _core(db, lambda prompt: "ok")
    engine = WorkflowEngine(core, budget=Budget(lifetime=1))
    try:
        spec = _pipeline_spec(
            [
                {"type": "agent", "prompt": "one ${item}"},
                {"type": "agent", "prompt": "two"},  # no slot left for this one
            ]
        )
        result = engine.run(spec, {"items": ["a"]})
        assert result.outputs["p"] == [None]
        assert any("lifetime" in fault for fault in result.faults), result.faults
        assert result.cap_trips == 1
        assert result.status == "degraded"  # never a clean "complete"
    finally:
        core.shutdown()


# --- H6: success + timeout + death coexist in ONE pipeline's fault list ---


def test_mixed_faults_in_one_pipeline_keep_every_cause_and_degrade_the_run(db, monkeypatch):
    """3 items x 1 stage: item 0 succeeds, item 1's leaf never answers (barrier
    timeout), item 2's leaf dies outright (provider raises). Coverage gap (#76,
    H6): three tests each pin one cause in isolation; none combines them in the
    same ``_PipelineRun`` to check that the timeout fault doesn't swallow or
    overwrite the death fault, or vice versa.

    ``PIPELINE_TIMEOUT`` is 2.0s here, not the 0.3s the timeout-only test uses:
    item 2's death goes through ``classify_provider_error``, whose FIRST call
    in a process lazily imports the ``anthropic``/``openai`` SDKs (~0.5-1.5s
    cold — measured up to 1.1s wall-clock under ``pytest-cov``'s import
    tracing, near-instant once warm) — a margin the isolated single-cause test
    never needed to budget for. Matches the 2.0s already used by
    ``test_on_done_crash_settles_the_item_instead_of_hanging`` in this file."""
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 2.0)
    gate = threading.Event()

    def responder(prompt):
        tag = prompt.rsplit(" ", 1)[-1]
        if tag == "x":
            return "ok"
        if tag == "y":
            gate.wait(5)
            return "late"
        raise RuntimeError("stage died")  # tag == "z"

    core = _core(db, responder)
    engine = WorkflowEngine(core, budget=Budget())
    try:
        result = engine.run(_pipeline_spec(_ONE_STAGE), {"items": ["x", "y", "z"]})
        assert result.outputs["p"] == ["ok", None, None]
        # Exactly two faults — one per real cause, neither swallowing nor
        # duplicating the other. The death fault is fully deterministic (no
        # timing in it); the timeout fault carries a "settled in X.Xs" clause,
        # so it stays a substring match.
        assert len(result.faults) == 2, result.faults
        assert "p#2#0: leaf error: stage died" in result.faults
        assert any("timed out" in f for f in result.faults), result.faults
        assert result.status == "degraded"
        # The NODE's own output isn't null (it's a 3-element list); only two of
        # its ITEMS are. ``null_count`` counts nulled NODES, not nulled items:
        # ``WorkflowEngine.run`` (engine.py, ``if output is None: result.
        # null_count += 1``) only ever looks at the whole node's output, so a
        # pipeline node whose list contains nulls still reports 0 here — the
        # issue's prediction of ``null_count == 1`` does not hold; this is what
        # the engine actually does, not a bug the test papers over.
        assert result.null_count == 0
        # #72: only the two that really DIED are holes — item 1 (timed out) and
        # item 2 (leaf error), never item 0 (succeeded).
        assert engine.aggregate_holes["p"] == frozenset({1, 2})
    finally:
        gate.set()
        core.shutdown()


# --- the barrier's own cancels must give back what they never spent ---


def test_a_pipeline_timeout_gives_back_the_slots_of_leaves_that_never_ran(db, monkeypatch):
    """The timeout cancels the backlog — and a leaf the pool dropped from its
    QUEUE consumed no provider call, so its lifetime slot bought nothing.

    Its own ``on_done`` cannot give the slot back: ``_expire`` sets ``_expired``
    first, and the hook's straggler guard returns before ``account_leaf`` ever
    runs. So the reservation leaked, and every timed-out run came back with less
    lifetime than it had actually spent."""
    monkeypatch.setattr(strategies, "PIPELINE_TIMEOUT", 0.3)
    gate = threading.Event()
    core = _core(db, lambda prompt: (gate.wait(5), "late")[1], pool_width=1)
    engine = WorkflowEngine(core, budget=Budget(lifetime=4))
    try:
        result = engine.run(_pipeline_spec(_ONE_STAGE), {"items": ["a", "b"]})
        assert result.outputs["p"] == [None, None]
        # Two claimed; the one that ran stays charged, the QUEUED one comes back.
        assert engine.budget.lifetime_remaining == 3
    finally:
        gate.set()
        core.shutdown()
