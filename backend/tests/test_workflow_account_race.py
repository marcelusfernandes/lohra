"""Contabilizar um leaf que ainda NÃO assentou (issue #42, residual da 0.0.21).

``engine.account_leaf`` lê o leaf com ``collect(wait=False)`` — uma vez — e
deduplica por ``sub_id``. Quando essa leitura pegava o leaf ainda ``running``,
ela gravava 0 tokens e ``usage_uncertain=False`` como FATO, e o dedup barrava
para sempre a leitura correta que viria depois. O caminho real que chega lá é
``_timed_out``: um leaf que ignora o cancel (tool em voo / chamada não
streaming) atravessa a espera de quiescência e volta ``running``.

O conserto tem duas metades, e os dois ramos estão pinados aqui:

- uma leitura não-terminal não soma nada e **não entra no dedup**: o leaf fica
  pendente e uma segunda chance (o hook ``on_done`` armado tarde no core, que
  nunca bloqueia) contabiliza o custo REAL quando ele assenta;
- o que ainda estiver voando no ``_seal`` vira ``usage_uncertain_leaves`` + um
  fault que diz por quê — nunca 0-como-fato — e o rollup FECHA ali: um hook que
  dispare depois não reabre a conta que já foi persistida.
"""

import inspect
import threading
import time

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import quiescence
from lohra.workflow.accounting import UNSETTLED_AT_SEAL
from lohra.workflow.budget import Budget
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_workflow_pipeline import ScriptedClient


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


def _engine(core, **kwargs):
    return WorkflowEngine(core, budget=Budget(lifetime=8), **kwargs)


def _slow_spec():
    """One node whose leaf blows its own deadline, then one that answers."""
    return validate_spec(
        {
            "meta": {"name": "race"},
            "nodes": [
                {"id": "a", "type": "agent", "prompt": "slow work", "timeout": 0.2},
                {"id": "b", "type": "agent", "prompt": "fast work", "depends_on": ["a"]},
            ],
        }
    )


def _until(predicate, limit=5.0):
    """Poll a condition instead of sleeping and hoping."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def _faults(result):
    return "\n".join(result.faults)


# --- 1. a non-terminal read writes NOTHING, and keeps its second chance -------


def test_a_leaf_still_running_is_not_accounted_and_not_deduped(db, monkeypatch):
    """The bug, at the engine's own API: the timeout path reads the leaf while it
    is still inside the provider call. Before the fix that read was final — 0
    tokens, ``usage_uncertain`` False, and the sub_id burned in ``_accounted``.
    Now nothing is written and the leaf stays PENDING, so the real bill can still
    land."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)
    gate = threading.Event()
    core = _core(db, lambda _p: (gate.wait(10), "late")[1])
    try:
        engine = _engine(core)
        sub_id = engine.spawn_leaf("blocked in the provider call")
        assert engine.collect_with_schema(sub_id, None, timeout=0.2) is None

        assert sub_id not in engine._accounted  # never a fact about a live leaf
        assert engine._costs.get(sub_id) is None
        assert engine._result.tokens_in == 0
        assert engine._result.usage_uncertain_leaves == 0  # nothing claimed yet
        assert sub_id in engine._pending_account

        gate.set()  # the leaf lands -> the late hook accounts it for REAL
        assert _until(lambda: sub_id in engine._accounted)
        assert engine._costs[sub_id].input_tokens > 0
        assert engine._result.tokens_in == engine._costs[sub_id].input_tokens
        assert engine._result.tokens_in > 0
        assert engine.budget.tokens_spent > 0  # the BUDGET was charged too
    finally:
        gate.set()
        core.shutdown()


def test_a_leaf_that_settles_in_the_window_is_charged_right_there(db, monkeypatch):
    """The other side of the second chance: if the leaf goes terminal between the
    read and the arming, nothing was installed to call us back — so the deferral
    re-reads once and charges it on the spot rather than waiting for the seal."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)
    core = _core(db, lambda _p: "R")
    try:
        engine = _engine(core)
        sub_id = engine.spawn_leaf("quick")
        assert core.collect(sub_id, wait=True, timeout=5).get("status") == "complete"

        # Force the deferral path over a leaf that is ALREADY terminal.
        engine._defer_account(sub_id)
        assert sub_id in engine._accounted
        assert engine._result.tokens_in > 0
    finally:
        core.shutdown()


def test_accounting_a_terminal_leaf_twice_still_charges_once(db):
    """The exactly-once contract the refund rides on is untouched (issue #14 —
    ``test_a_leaf_that_never_ran_gives_its_lifetime_slot_back`` is its other
    pin)."""
    core = _core(db, lambda _p: "R")
    try:
        engine = _engine(core)
        sub_id = engine.spawn_leaf("go")
        core.collect(sub_id, wait=True, timeout=5)
        engine.account_leaf(sub_id)
        once = engine._result.tokens_in
        engine.account_leaf(sub_id)
        assert engine._result.tokens_in == once
        assert once > 0
    finally:
        core.shutdown()


# --- 2. what is still flying at the seal has a HOUSE -------------------------


def test_a_leaf_still_flying_at_the_seal_is_uncertain_with_a_fault(db, monkeypatch):
    """The run really ends with a leaf inside a provider call: the honest entry is
    "one leaf whose bill is unknown" plus a fault naming it — never a 0 the
    rollup would report as the price of that node."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)
    gate = threading.Event()
    core = _core(
        db, lambda p: (gate.wait(10), "late")[1] if "slow" in p else "fast"
    )
    try:
        result = _engine(core).run(_slow_spec(), {})
        assert result.outputs["a"] is None
        assert result.outputs["b"] == "fast"  # the run moved on
        assert result.usage_uncertain_leaves == 1
        assert f"a: {UNSETTLED_AT_SEAL}" in _faults(result)
        assert result.tokens_in > 0  # node b's real bill, not the zombie's
        assert result.status == "degraded"
    finally:
        gate.set()
        core.shutdown()


def test_the_sealed_rollup_is_never_reopened_by_a_late_hook(db, monkeypatch):
    """The seal is where the rollup is persisted. A leaf that lands one instant
    later must not add usage the saved rollup no longer contains — nor contradict
    the "usage unknown" fault already written about it."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)
    gate = threading.Event()
    core = _core(
        db, lambda p: (gate.wait(10), "late")[1] if "slow" in p else "fast"
    )
    try:
        engine = _engine(core)
        result = engine.run(_slow_spec(), {})
        sealed_tokens = result.tokens_in
        sealed_spend = engine.budget.tokens_spent
        assert result.usage_uncertain_leaves == 1

        gate.set()  # the zombie lands AFTER the seal
        straggler = engine.spawned[0]
        assert _until(
            lambda: core.collect(straggler).get("status") in ("complete", "interrupted")
        )
        # The hook really RAN (``_fire_done`` claims it under the core's lock
        # before invoking it) — this asserts "fired and refused", not "probably
        # never fired". The short sleep only lets its body finish.
        assert _until(lambda: core._children[straggler].done_fired)
        time.sleep(0.1)

        assert result.tokens_in == sealed_tokens
        assert engine.budget.tokens_spent == sealed_spend
        assert result.usage_uncertain_leaves == 1  # counted once, at the seal
        assert _faults(result).count(UNSETTLED_AT_SEAL) == 1
    finally:
        gate.set()
        core.shutdown()


# --- 3. the second chance may never block ------------------------------------


def test_no_accounting_path_ever_blocks_on_a_collect(db):
    """``account_leaf`` runs on the core's own ``on_done`` workers: a blocking
    collect there parks a worker the pipeline needs to advance every other item.
    The rule is structural, so it is asserted over the source of the whole
    accounting path rather than timed."""
    engine_src = "".join(
        inspect.getsource(fn)
        for fn in (
            WorkflowEngine.account_leaf,
            WorkflowEngine._defer_account,
            WorkflowEngine._settle_pending,
        )
    )
    assert "wait=True" not in engine_src
    assert "wait=False" in engine_src


def test_the_late_hook_runs_on_the_pool_and_returns(db, monkeypatch):
    """And the behavioural half: the late hook really fires from the worker that
    finished the turn, and that worker goes on to take more work."""
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.2)
    gate = threading.Event()
    core = _core(db, lambda _p: (gate.wait(10), "late")[1], pool_width=1)
    try:
        engine = _engine(core)
        sub_id = engine.spawn_leaf("blocked")
        assert engine.collect_with_schema(sub_id, None, timeout=0.2) is None
        gate.set()
        assert _until(lambda: sub_id in engine._accounted)
        # The single worker is free again: a new leaf on the same pool answers.
        second = engine.spawn_leaf("next")
        assert core.collect(second, wait=True, timeout=5).get("status") == "complete"
    finally:
        gate.set()
        core.shutdown()


# --- 4. the core seam: watch_done ---------------------------------------------


def test_watch_done_installs_on_a_live_sub_session(db):
    gate = threading.Event()
    core = _core(db, lambda _p: (gate.wait(10), "late")[1])
    fired: list[str] = []
    try:
        sub_id = core.spawn("blocked")
        assert _until(lambda: core.collect(sub_id).get("status") == "running")
        assert core.watch_done(sub_id, fired.append) is True
        gate.set()
        assert _until(lambda: fired == [sub_id])
    finally:
        gate.set()
        core.shutdown()


def test_watch_done_refuses_a_terminal_sub_and_an_unknown_one(db):
    core = _core(db, lambda _p: "R")
    try:
        sub_id = core.spawn("go")
        core.collect(sub_id, wait=True, timeout=5)
        # Terminal: nothing will ever fire again, so the caller must decide NOW.
        assert core.watch_done(sub_id, lambda _s: None) is False
        assert core.watch_done("ghost", lambda _s: None) is False
    finally:
        core.shutdown()


def test_watch_done_never_clobbers_an_existing_hook(db):
    """The pipeline chains its stages off ``on_done``; stealing that hook would
    strand an item forever."""
    gate = threading.Event()
    core = _core(db, lambda _p: (gate.wait(10), "late")[1])
    first: list[str] = []
    try:
        sub_id = core.spawn("blocked", on_done=first.append)
        assert core.watch_done(sub_id, lambda _s: first.append("stolen")) is False
        gate.set()
        assert _until(lambda: first == [sub_id])
    finally:
        gate.set()
        core.shutdown()
