"""Triage of the three candidates left open by the dogfood review (Codex).

`docs/history/reviews/2026-08-26-dogfood-codex.md` closed with three unverified
suspicions raised by a 3-branch review of that day's own code. WF-30 (a real
race found the same way) is the precedent that says they are worth chasing, so
each one gets a test that TRIES to reproduce it:

- **(i) `shutdown()` frees leases without asking for a cancel** — a surviving
  engine racing a new owner. **REFUTED**: `shutdown` calls `request_cancel` on
  every live engine before it tears anything down, and `release` is scoped by
  HOLDER at the SQL level, so it cannot take a lease somebody else now owns.
  Both halves are pinned below so the refutation does not rot.
- **(ii) `cancel()` has no liveness guard and can re-label a finished run** —
  **CONFIRMED**, and worse than described: a completed run stays in the live
  registry, so `cancel` took the IN-MEMORY path, flipped the state to
  `cancelled`, wrote it over the terminal line and answered `{"ok": true}`. The
  outcome of a finished run was erased and the caller was told it worked.
  Fixed here (small): a run that already carries a real outcome is refused.
- **(iii) the pause is armed before the final persist/release, so a premature
  auto-resume is refused as "still live" and the timer is spent** —
  **CONFIRMED as a mechanism**, low severity in practice (the floor between a
  pause and its retry is 60s and the epilogue takes milliseconds), and the fix
  is NOT small: `resume_at` has to be on the persisted line, so arming after the
  release means splitting "compute the retry time" from "arm the timer". Left
  as a strict xfail rather than a rushed change.
"""

import threading

import pytest

from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.watch import TERMINAL
from tests.test_workflow_operability import _TWO_NODE, _ok, _service, db  # noqa: F401
from tests.test_workflow_quota import _quota_responder


class _SyncTimer:
    """Fires the instant it is started — the premature retry, deterministically."""

    def __init__(self, delay, fire):
        self.delay = delay
        self._fire = fire

    def start(self):
        self._fire()

    def cancel(self):
        pass


def _finished_run(db, home, responder=_ok):  # noqa: F811
    svc = _service(db, home, responder)
    run_id = svc.start(_TWO_NODE, {})["run_id"]
    assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    return svc, run_id


# --- (i) shutdown / leases: REFUTED, pinned ----------------------------------


def test_shutdown_asks_every_live_run_to_cancel_before_tearing_down(db, tmp_path):  # noqa: F811
    """The original suspicion was that `shutdown` frees the lease while its own
    engine keeps working. It does not: the engine is told to stop first."""
    entered = threading.Event()
    release = threading.Event()

    def responder(prompt):
        entered.set()
        release.wait(5)
        return "R"

    svc = _service(db, tmp_path, responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert entered.wait(5)
        engine = svc._runs[run_id].engine
        assert engine is not None and not engine.cancelled
        release.set()
        svc.shutdown()
        assert engine.cancelled  # asked to stop, not just abandoned
    finally:
        release.set()


def test_a_release_can_never_take_a_lease_another_holder_owns(db, tmp_path):  # noqa: F811
    """The other half of (i): even if a stale owner did release late, the write
    is scoped by holder — it cannot free the lease the new owner is renewing."""
    stale = RunStateStore(db, holder="stale-owner")
    fresh = RunStateStore(db, holder="fresh-owner")
    try:
        stale.save(run_id="r1", name="n", owner=None, status="running", spec={}, args={})
        assert stale.acquire("r1")
        assert stale.release("r1")
        assert fresh.acquire("r1")
        held = fresh.lease_expiry("r1")
        assert held is not None
        assert stale.release("r1") is False  # the DELETE matched no row of ours
        assert fresh.lease_expiry("r1") == held  # ...and the real owner still holds it
    finally:
        stale.shutdown()
        fresh.shutdown()


# --- (ii) cancel over a finished run: CONFIRMED, fixed -----------------------


def test_cancelling_a_finished_run_never_erases_its_outcome(db, tmp_path):  # noqa: F811
    """The bug as found: a completed run is still in the live registry, so the
    in-memory branch ran with no liveness guard at all — it flipped the state,
    persisted `cancelled` over `complete`, and answered ok."""
    svc, run_id = _finished_run(db, tmp_path)
    try:
        assert run_id in svc._runs  # the shape that made the in-memory path reachable
        out = svc.cancel(run_id)
        assert "error" in out
        assert "complete" in out["error"]  # says WHAT it already is
        assert svc._store.load(run_id).status == "complete"
        assert svc.status(run_id)["status"] == "complete"
    finally:
        svc.shutdown()


def test_cancelling_a_finished_run_this_process_only_knows_from_disk(db, tmp_path):  # noqa: F811
    """The same guard on the durable path: a fresh process holds no state, so
    the refusal has to ride on the line itself."""
    first, run_id = _finished_run(db, tmp_path)
    first.shutdown()
    fresh = _service(db, tmp_path, _ok)
    try:
        out = fresh.cancel(run_id)
        assert "error" in out and "complete" in out["error"]
        assert fresh._store.load(run_id).status == "complete"
    finally:
        fresh.shutdown()


def test_a_paused_run_is_still_cancellable(db, tmp_path):  # noqa: F811
    """The guard must refuse a FINISHED run, never a stopped-but-live one:
    cancelling a paused run is exactly how an operator stops its auto-resume."""
    svc = _service(db, tmp_path, _quota_responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert svc.cancel(run_id) == {"ok": True, "run_id": run_id}
        assert svc._store.load(run_id).status == "cancelled"
    finally:
        svc.shutdown()


def test_cancelling_a_cancelled_run_stays_idempotent(db, tmp_path):  # noqa: F811
    """`cancelled` is deliberately NOT in the refusal set: a second cancel says
    the same thing as the first and overwrites nothing anyone will miss."""
    svc = _service(db, tmp_path, _quota_responder)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert svc.cancel(run_id)["ok"] is True
        assert svc.cancel(run_id)["ok"] is True
    finally:
        svc.shutdown()


def test_the_refusal_set_is_the_outcomes_a_run_can_carry():
    """Anti-drift: the statuses that mean "this run already ended with a real
    verdict" are the terminal ones minus `cancelled` (kept idempotent)."""
    from lohra.workflow.service import FINISHED_STATUSES

    assert FINISHED_STATUSES == frozenset(TERMINAL) - {"cancelled"}


# --- (iii) the premature auto-resume: CONFIRMED, fix is not small ------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CONFIRMED (candidate iii). ``_on_paused`` arms the retry from INSIDE the "
        "run thread's try-block, before the finally that shuts the core down, "
        "persists the state and releases the lease. A timer that fires inside "
        "that window calls back into ``start(resume_run_id=…)``, which finds the "
        "run's future still un-done, correctly refuses it as live — and "
        "``AutoResumeScheduler._fire`` has already POPPED the timer, so nothing "
        "re-arms. The run stays paused forever while its durable line still "
        "advertises a ``resume_at`` nobody will honour. Unreachable in practice "
        "(MIN_RESUME_DELAY is 60s and the epilogue takes milliseconds) and only "
        "within one long-lived process (a cold start re-arms off that line). The "
        "fix is NOT small: ``resume_at`` has to be on the persisted line, so "
        "arming after the release means splitting compute-the-time from arm-the-"
        "timer across the epilogue — a change to the pause contract, not a patch."
    ),
)
def test_an_auto_resume_that_fires_too_early_is_not_silently_dropped(db, tmp_path):  # noqa: F811
    svc = _service(db, tmp_path, _quota_responder)
    svc.set_autoresume(
        AutoResumeScheduler(
            svc.resume, timer_factory=lambda delay, fire: _SyncTimer(delay, fire),
            clock=lambda: 1000.0,
        )
    )
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        row = svc._store.load(run_id)
        assert row.resume_at is not None  # the line promises a retry...
        # ...so a retry must still be pending. It is not: the premature fire
        # consumed the only timer this run had.
        assert svc._autoresume._timers.get(run_id) is not None
    finally:
        svc.shutdown()


def test_the_premature_refusal_is_at_least_visible_in_the_line(db, tmp_path, caplog):  # noqa: F811
    """What the run DOES get today, pinned so the xfail above has a baseline:
    a loud refusal in the log and a durable ``resume_at`` a cold start can
    re-arm from — the mitigation that keeps (iii) at low severity."""
    svc = _service(db, tmp_path, _quota_responder)
    svc.set_autoresume(
        AutoResumeScheduler(
            svc.resume, timer_factory=lambda delay, fire: _SyncTimer(delay, fire),
            clock=lambda: 1000.0,
        )
    )
    try:
        with caplog.at_level("WARNING"):
            run_id = svc.start(_TWO_NODE, {})["run_id"]
            assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        assert any("auto-resume of run" in record.message for record in caplog.records)
        row = svc._store.load(run_id)
        assert row.status == "paused" and row.resume_at is not None
    finally:
        svc.shutdown()
