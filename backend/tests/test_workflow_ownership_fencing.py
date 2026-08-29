"""Ownership fencing: nothing a STALE owner writes ever lands (issue #12).

The run lease (WF-29) is a correct single-winner: the PRIMARY KEY arbitrates and
the TTL frees a run whose owner died. What it never did was FENCE the writes
made "under ownership" — every one of them was an unconditional
``INSERT OR REPLACE``/``INSERT``, so the process that lost the lease (frozen,
swapped out, or merely stuck in one node longer than the TTL while its heartbeat
learned it lost and only stopped beating) still wrote over the new owner's node
cache, ledgers, durable line and audit ledger, silently.

The scenario is driven here with no sleep and no real freeze: two stores over
ONE file-backed database, a clock that is a list, A acquires, the clock jumps
past the TTL, B acquires, and then A writes — exactly the "A blocks / B takes
over / A wakes" of the issue, deterministically.

Both halves are pinned:

- the FIVE write families that run under ownership (node cache, per-cell cost,
  run-level ledger, durable run line, audit ledger) refuse a stale fence and say
  so in the log — a refusal is never silent and never an exception in a worker;
- an UNFENCED writer (``fence=None``: a pre-fence process, or ``mark_cancelled``
  on a run nobody owns) still writes, so an old database and the ownerless paths
  behave exactly as before.
"""

import logging
import threading

import pytest

from lohra.agent.types import Usage
from lohra.state import SessionDB
from lohra.workflow.cache import NodeCache
from lohra.workflow.audit import AUDIT_SCHEMA_VERSION, AuditTrail
from lohra.workflow.runstate_store import (
    _FENCE_MEMORY,
    EVICTED,
    RECOVERED_FAULT,
    RunStateStore,
)
from tests.test_workflow_durable_state import _TWO_NODE, _counting, _service


@pytest.fixture
def db(tmp_path):
    """File-backed: two "processes" must share the same bytes."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _audit_event(run_id: str) -> dict:
    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "event_type": "node.started",
        "provenance": "synthetic",
        "identity": {"run_id": run_id},
        "data": {},
    }


def _append(db, run_id, *, fence):
    return db.audit_append(
        _audit_event(run_id),
        now=1.0,
        max_events=10,
        max_runs=10,
        retention_seconds=1000.0,
        fence=fence,
    )


def _handover(db, now):
    """A acquires, the TTL lapses, B takes the run. Returns (A, B)."""
    older = RunStateStore(db, holder="A", clock=lambda: now[0], ttl=100.0)
    newer = RunStateStore(db, holder="B", clock=lambda: now[0], ttl=100.0)
    assert older.acquire("r1") is True
    now[0] += 101.0  # the owner never renewed: its lease lapsed
    assert newer.acquire("r1") is True
    return older, newer


# --- 1. the reproduction: a stale owner writes nothing ----------------------


def test_the_stale_owner_writes_nothing_the_new_owner_can_see(db, caplog):
    """A blocks, B takes the run, A wakes up and writes. Every family refuses."""
    now = [0.0]
    older, newer = _handover(db, now)
    stale = older.fence_of("r1")
    live = newer.fence_of("r1")
    assert stale is not None and live is not None and live > stale

    newer.save(run_id="r1", name="B-line", status="running")
    assert db.cache_put("r1", "h1", "a", '"B"', "complete", fence=live) is True
    assert db.cache_cost_put("r1", "h1", 10, 5, fence=live) is True
    assert db.run_spend_put("r1", 500, 10, 5, fence=live) is True
    assert _append(db, "r1", fence=live) == 1

    with caplog.at_level(logging.WARNING, logger="lohra.state.db"):
        older.save(run_id="r1", name="A-line", status="complete")
        assert db.cache_put("r1", "h1", "a", '"A"', "complete", fence=stale) is False
        assert db.cache_put("r1", "h2", "b", '"A"', "complete", fence=stale) is False
        assert db.cache_cost_put("r1", "h1", 999, 999, fence=stale) is False
        assert db.run_spend_put("r1", 1, 999, 999, fence=stale) is False
    assert _append(db, "r1", fence=stale) == 0

    # ...and the new owner's state is exactly what it wrote.
    assert db.run_state_get("r1")["name"] == "B-line"
    assert db.run_state_get("r1")["status"] == "running"
    assert db.cache_get("r1", "h1")["output_json"] == '"B"'
    assert db.cache_get("r1", "h2") is None
    assert db.cache_cost_total("r1") == (10, 5)
    assert db.run_spend_get("r1")["tokens_in"] == 10
    assert len(db.audit_events("r1")) == 1
    # Refused, never silent (requirement 1): the log names the run.
    assert any("r1" in record.getMessage() for record in caplog.records)


def test_a_stale_run_state_write_is_refused_without_raising(db):
    """``RunStateStore.save`` is called from run threads and pool workers: a
    refusal must degrade, never come back as an exception."""
    now = [0.0]
    older, newer = _handover(db, now)
    newer.save(run_id="r1", name="B-line", status="running")
    older.save(run_id="r1", name="A-line", status="failed")  # must not raise
    assert newer.load("r1").name == "B-line"


# --- 2. compat: an unfenced writer still writes ----------------------------


def test_an_unfenced_write_still_lands(db):
    """Requirement 3: a row from before this shipped (and every ownerless path,
    like cancelling a run nobody holds) has no fence to check against."""
    now = [0.0]
    older, newer = _handover(db, now)
    newer.save(run_id="r1", name="B-line", status="running")
    assert db.cache_put("r1", "h9", "n", '"x"', "complete", fence=None) is True
    assert db.cache_cost_put("r1", "h9", 1, 1, fence=None) is True
    assert db.run_spend_put("r1", None, 1, 1, fence=None) is True
    assert _append(db, "r1", fence=None) == 1
    # A run nobody ever leased has no fence row at all: writes just land.
    assert db.cache_put("never-leased", "h", "n", '"x"', "complete", fence=None) is True
    # The cancel path is the OWNERLESS one, and now says so in its own statement:
    # while ANY live lease is on the run — even this store's — cancelling is
    # refused as busy, and the caller takes the cooperative route instead.
    assert newer.mark_cancelled("r1") == "busy"
    assert newer.release("r1") is True
    assert newer.mark_cancelled("r1") == "cancelled"  # the ownerless path (WF-19)
    assert newer.load("r1").status == "cancelled"
    # ...even from the store that LOST the run: cancelling a run nobody holds is
    # administrative, and a fence from the stretch it used to own would drop it.
    newer.save(run_id="r1", name="B-line", status="running", fence=None)
    assert older.mark_cancelled("r1") == "cancelled"
    assert newer.load("r1").status == "cancelled"


def test_the_lease_and_its_fence_are_one_acquisition(db):
    """The fence advances only for the winner: a loser has none to write with."""
    now = [0.0]
    mine = RunStateStore(db, holder="A", clock=lambda: now[0], ttl=100.0)
    theirs = RunStateStore(db, holder="B", clock=lambda: now[0], ttl=100.0)
    assert mine.acquire("r1") is True
    assert theirs.acquire("r1") is False
    # Not None: the run IS fenced, and a loser that read "unfenced" here would
    # write like a pre-#12 caller — the very fail-open the sentinel closes.
    assert theirs.fence_of("r1") is EVICTED
    assert mine.fence_of("r1") == 1


def test_the_same_process_is_fenced_out_of_its_own_earlier_stretch(db):
    """The discriminator that rules out a holder-token: ``holder`` is per STORE,
    so a process that releases and re-acquires presents the same holder — only a
    fence that advances per ACQUISITION rejects a straggler from the stretch
    before (the run was cancelled with ``shutdown(wait=False)``; its workers
    outlive it)."""
    store = RunStateStore(db, holder="A", clock=lambda: 0.0, ttl=100.0)
    assert store.acquire("r1") is True
    first = store.fence_of("r1")
    assert store.release("r1") is True
    assert store.acquire("r1") is True
    second = store.fence_of("r1")
    assert second > first
    assert db.cache_put("r1", "h", "n", '"stale"', "complete", fence=first) is False
    assert db.cache_put("r1", "h", "n", '"live"', "complete", fence=second) is True


def test_a_released_fence_is_kept_not_forgotten(db):
    """Deliberate asymmetry with ``_renewed`` (which IS popped on release): a
    store that forgot its fence would write UNFENCED afterwards — precisely the
    straggler this closes."""
    now = [0.0]
    store = RunStateStore(db, holder="A", clock=lambda: now[0], ttl=100.0)
    assert store.acquire("r1") is True
    assert store.release("r1") is True
    assert store.fence_of("r1") == 1


def test_a_refused_audit_append_is_not_a_sink_failure(db):
    """The audit sink retries a FAILED write (a marker, forever until stop) and
    declares a gap for it. A refusal is neither: the ledger settled it, on a run
    this process no longer owns — so it drains clean and invents no gap."""
    now = [0.0]
    older, newer = _handover(db, now)
    trail = AuditTrail(db, fence_of=older.fence_of)  # the STALE owner's sink
    try:
        assert trail.record(_audit_event("r1")) is True
        assert trail.flush(timeout=2) is True  # not stuck retrying
    finally:
        assert trail.shutdown(timeout=2) is True
    assert db.audit_events("r1") == []  # nothing landed, and no audit.gap either
    assert newer.fence_of("r1") is not None


# --- 3. the same scenario through the real service -------------------------


def test_a_woken_lost_owner_does_not_clobber_the_new_owners_line(db, tmp_path):
    """End to end, on the REAL path: the owner is inside a leaf when its lease
    lapses, a second process recovers the run and finishes it, and only THEN
    does the first one wake up and write its terminal line.

    Before fencing, that late write replaced the recovering process's line —
    the ``recovered after process loss`` fault (the one thing telling a reader
    that work was lost) disappeared from the run's durable record."""
    release = threading.Event()
    now = [7000.0]
    lost = _service(
        db, tmp_path, lambda _p: (release.wait(5), "R")[1], clock=lambda: now[0],
        lease_ttl=100.0,
    )
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {})["run_id"]
        now[0] = 7101.0  # the owner never renewed: the lease lapsed
        assert "error" not in fresh.start(resume_run_id=run_id)
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert calls[0] == 2
    finally:
        release.set()
        lost.shutdown()  # joins the lost run's pool: its late writes are DONE
        fresh.shutdown()

    line = RunStateStore(db, holder="reader", clock=lambda: now[0]).load(run_id)
    assert any(RECOVERED_FAULT in fault for fault in line.prior_faults)


# --- 4. eviction degrades to REFUSAL, never to unfenced (issue #12 follow-up) --


def _inert_timer(_interval, _fire):
    """A heartbeat timer that never arms: the eviction repro acquires a
    thousand runs, and a thousand real ``threading.Timer`` threads is a test
    that measures the OS rather than the fence."""

    class _Inert:
        def start(self) -> None:
            pass

        def cancel(self) -> None:
            pass

    return _Inert()


def _store(db, holder, now):
    return RunStateStore(
        db, holder=holder, clock=lambda: now[0], ttl=100.0, timer_factory=_inert_timer
    )


def _evict_victims_fence(store):
    """Exactly the repro: the owner goes on to run _FENCE_MEMORY other runs, so
    the victim's fence falls out of this store's bounded memory."""
    for index in range(_FENCE_MEMORY):
        assert store.acquire(f"filler-{index}") is True


def test_an_evicted_fence_refuses_the_audit_append(db, caplog):
    """A owns r1, B takes it over, A then acquires _FENCE_MEMORY other runs, and
    only THEN does A's straggling audit event reach the sink.

    Before this, the eviction handed the sink ``None`` — indistinguishable from
    "pre-#12 caller, write unfenced" — so the stale event landed in the new
    owner's numbered stream. Eviction is a memory limit, never a licence."""
    now = [0.0]
    older, newer = _store(db, "A", now), _store(db, "B", now)
    assert older.acquire("r1") is True
    now[0] += 101.0  # the owner never renewed: its lease lapsed
    assert newer.acquire("r1") is True
    _evict_victims_fence(older)
    assert older.fence_of("r1") is EVICTED

    trail = AuditTrail(db, fence_of=older.fence_of)
    with caplog.at_level(logging.WARNING, logger="lohra.workflow.audit"):
        try:
            assert trail.record(_audit_event("r1")) is True
            assert trail.flush(timeout=2) is True
        finally:
            assert trail.shutdown(timeout=2) is True
    assert db.audit_events("r1") == []
    assert any("r1" in record.getMessage() for record in caplog.records)


def test_an_evicted_fence_refuses_the_run_line_write(db):
    """The second fail-open consumer: ``save``'s default fence is whatever this
    store remembers, so an evicted memory must refuse rather than write."""
    now = [0.0]
    older, newer = _store(db, "A", now), _store(db, "B", now)
    assert older.acquire("r1") is True
    now[0] += 101.0
    assert newer.acquire("r1") is True
    newer.save(run_id="r1", name="B-line", status="running")
    _evict_victims_fence(older)
    assert older.save(run_id="r1", name="A-line", status="complete") is False
    assert newer.load("r1").name == "B-line"


def test_a_run_nobody_ever_leased_still_reads_as_unfenced(db):
    """The case the None-compat exists for: a database written before #12 (no
    fence row at all) still writes exactly as it did."""
    reader = RunStateStore(db, holder="R", clock=lambda: 0.0, timer_factory=_inert_timer)
    assert reader.fence_of("never-leased") is None
    assert reader.save(run_id="never-leased", name="old", status="running") is True
    assert reader.load("never-leased").name == "old"


def test_the_refused_audit_append_warning_names_the_run(db, caplog):
    """The contract is "a refusal is never silent AND names the run" — the
    ledger's own refusal used to log neither the run nor the fence, so an
    operator reading the log could not tell WHICH run lost its writes."""
    now = [0.0]
    older, newer = _handover(db, now)
    trail = AuditTrail(db, fence_of=older.fence_of)  # the STALE owner's sink
    with caplog.at_level(logging.WARNING, logger="lohra.workflow.audit"):
        try:
            assert trail.record(_audit_event("r1")) is True
            assert trail.flush(timeout=2) is True
        finally:
            assert trail.shutdown(timeout=2) is True
    refusals = [r.getMessage() for r in caplog.records if "no longer owns it" in r.getMessage()]
    assert refusals and all("r1" in message for message in refusals)
    assert db.audit_events("r1") == []


# --- 5. cancelling is one statement, not a check and then a write ------------


def test_a_cancel_that_races_an_acquisition_is_refused(db):
    """The TOCTOU, driven deterministically: the canceller reads the line, a new
    owner takes the run inside that very window, and only THEN does the cancel
    write. Before this, ``cancelled`` landed unfenced on top of a LIVE owner —
    the run read as stopped while a process was still inside it."""
    now = [0.0]
    canceller, owner = _store(db, "C", now), _store(db, "B", now)
    canceller.save(run_id="r1", name="orphan", status="running", fence=None)

    reader = _store(db, "R", now)
    read_line = canceller.load

    def load_then_lose_the_race(run_id: str):
        row = read_line(run_id)
        assert owner.acquire(run_id) is True  # the window: a new owner arrives
        owner.save(run_id=run_id, name="B-line", status="running")
        return row

    canceller.load = load_then_lose_the_race
    assert canceller.mark_cancelled("r1") == "busy"
    line = reader.load("r1")
    assert line.status == "running" and line.name == "B-line"


def test_cancelling_a_run_nobody_holds_still_works(db):
    """The other half: the ownerless path is exactly what the guard lets through."""
    now = [0.0]
    store = _store(db, "C", now)
    store.save(run_id="r1", name="orphan", status="running", fence=None)
    assert store.mark_cancelled("r1") == "cancelled"
    assert store.load("r1").status == "cancelled"
    assert store.mark_cancelled("nope") == "missing"


def test_the_service_refuses_to_cancel_a_run_another_process_is_inside(db, tmp_path):
    """Through the real service: the durable-only cancel path never writes over
    a live owner — it says the run is busy and names when the lease lapses."""
    now = [0.0]
    svc = _service(db, tmp_path, lambda _p: "ok", clock=lambda: now[0], lease_ttl=100.0)
    try:
        owner = _store(db, "B", now)
        svc._store.save(run_id="r1", name="theirs", status="running", fence=None)
        assert owner.acquire("r1") is True
        out = svc.cancel("r1")
        assert "error" in out and "another process" in out["error"]
        assert owner.load("r1").status == "running"
    finally:
        svc.shutdown()


# --- 6. a cell and its price are one write ---------------------------------


class _TakeoverBetweenCellAndCost:
    """A database that lets a NEW owner take the run in the window between the
    cell write and the cost write — the window that existed while they were two
    commits behind two guards."""

    def __init__(self, db, takeover) -> None:
        self._db = db
        self._takeover = takeover

    def __getattr__(self, name):
        return getattr(self._db, name)

    def cache_cost_put(self, *args, **kwargs):
        self._takeover()  # B acquires the run, right between the two writes
        return self._db.cache_cost_put(*args, **kwargs)


def test_a_cell_and_its_cost_land_together_or_not_at_all(db):
    """A cached cell with no cost row replays for FREE on a resume: the budget
    is charged nothing for work the run really paid for, and a resume loop can
    spend past its ceiling. Cell and price must tell one story."""
    now = [0.0]
    older, newer = _store(db, "A", now), _store(db, "B", now)
    assert older.acquire("r1") is True
    fence = older.fence_of("r1")

    def takeover():
        now[0] += 101.0
        assert newer.acquire("r1") is True

    cache = NodeCache(_TakeoverBetweenCellAndCost(db, takeover), "r1", fence=fence)
    cache.put_complete("h1", "node-a", {"ok": True}, Usage(input_tokens=10, output_tokens=5))

    stored = db.cache_get("r1", "h1") is not None
    priced = db.cache_cost_total("r1") != (0, 0)
    assert stored == priced, "a replayable cell whose cost never landed replays for free"


def test_a_stale_owner_writes_neither_the_cell_nor_its_cost(db):
    """The other side of the same statement: one guard, both rows."""
    now = [0.0]
    older, _newer = _handover(db, now)
    cache = NodeCache(db, "r1", fence=older.fence_of("r1"))
    cache.put_complete("h1", "node-a", {"ok": True}, Usage(input_tokens=10, output_tokens=5))
    assert db.cache_get("r1", "h1") is None
    assert db.cache_cost_total("r1") == (0, 0)


# --- 7. what a run has already spent is read UNDER ownership ---------------


class _LostOwnerLandsItsTally:
    """The lost owner's final ledger write lands exactly as we take the run."""

    def __init__(self, db, run_id, tally) -> None:
        self._db = db
        self._run_id = run_id
        self._tally = tally

    def __getattr__(self, name):
        return getattr(self._db, name)

    def acquire_run_lease(self, *args, **kwargs):
        fence = self._db.acquire_run_lease(*args, **kwargs)
        self._db.run_spend_put(self._run_id, None, *self._tally, fence=None)
        return fence


def test_the_resume_seeds_its_budget_from_the_ledger_it_acquired_over(db, tmp_path):
    """``seed_spend`` used to be read BEFORE the lease was taken, so a resume
    started from a tally that the previous owner was still finishing — and ran
    under a ceiling the run had, in fact, already spent."""
    now = [7000.0]
    seeder = _store(db, "seed", now)
    seeder.save(
        run_id="r1", name="two", status="running", spec=_TWO_NODE, fence=None
    )
    svc = _service(
        db, tmp_path, lambda _p: "R", clock=lambda: now[0], lease_ttl=100.0
    )
    svc._db = _LostOwnerLandsItsTally(db, "r1", (999_999, 1))
    svc._store._db = svc._db
    try:
        out = svc.start(resume_run_id="r1", token_budget=1000)
        assert "error" in out and "already spent" in out["error"]
        # ...and the lease it took to read that ledger is handed straight back.
        assert db.run_lease_expiry("r1", now[0]) is None
    finally:
        svc.shutdown()


# --- 8. the filesystem the fence cannot reach ------------------------------


def test_each_acquisition_gets_its_own_working_root(db, tmp_path, monkeypatch):
    """SQLite fencing stops a stale owner's ROWS; nothing stopped its leaves
    from writing into ``runs/<run_id>/work``, which the recovering owner then
    read as its own scratch. Each acquisition gets its own directory, so a
    straggler dirties only the stretch nobody is reading any more."""
    from lohra.workflow import service as service_module

    roots: list = []
    real = service_module.make_sandboxed_leaf_factory

    def spy(**kwargs):
        roots.append(kwargs["working_root"])
        return real(**kwargs)

    monkeypatch.setattr(service_module, "make_sandboxed_leaf_factory", spy)
    svc = _service(db, tmp_path, lambda _p: "R")
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        (roots[0] / "half-written.txt").write_text("a straggler's leftovers")
        assert "error" not in svc.start(resume_run_id=run_id)
        svc.status(run_id, wait=True, timeout=10)
    finally:
        svc.shutdown()

    assert len(roots) == 2 and roots[0] != roots[1]
    assert all(root.is_dir() and root.parent.name == run_id for root in roots)
    # The new owner is born in a CLEAN directory: nothing of the lost stretch's
    # scratch is in scope for its leaves.
    assert list(roots[1].iterdir()) == []
