"""Keep a live run's lease fresh on a TIMER, not on its output (WF-29).

The run lease says "a process is inside this run right now". Its other renewal
is the node cache write, which only happens when a NODE finishes — so a run
whose current node outlives the TTL (one slow reasoning-heavy leaf, a whole
``verify``/``judge_panel`` panel) used to lapse its own lease while it was still
working. The next process to look then found an ownerless run and started a
SECOND engine on its node cache and working root: exactly the corruption the
lease exists to prevent.

A heartbeat is what tells "slow" from "dead". It ticks on wall-clock time for as
long as the run holds the lease and stops the moment the lease is let go, so no
single node's duration can be the ceiling. Timers are injected (the
``AutoResumeScheduler`` contract), so the whole policy is testable without a
single real sleep.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Ticks per lease lifetime. Three, not one: a tick may be lost (a timer that
# fires late, a write that loses a race) and a live run still never lapses.
HEARTBEAT_TICKS_PER_TTL = 3.0

# (delay, fire) -> something with start()/cancel(), like threading.Timer.
TimerFactory = Callable[[float, Callable[[], None]], Any]
# Renew one run's lease; False when it is no longer ours to renew.
Renew = Callable[[str], bool]
# Told the owner, once, that a run it is inside is no longer its own.
OnLeaseLost = Callable[[str], None]


def _daemon_timer(delay: float, fire: Callable[[], None]) -> threading.Timer:
    """A real timer that never keeps the process alive on its own."""
    timer = threading.Timer(delay, fire)
    timer.daemon = True
    return timer


class LeaseHeartbeat:
    """One repeating tick per run_id: renew, re-arm, stop when the lease goes."""

    def __init__(
        self,
        renew: Renew,
        *,
        interval: float,
        timer_factory: TimerFactory | None = None,
        on_lease_lost: OnLeaseLost | None = None,
    ) -> None:
        self._renew = renew
        self._interval = max(0.1, float(interval))
        self._timer_factory = timer_factory if timer_factory is not None else _daemon_timer
        # The heartbeat is the ONLY thing in the process that finds out a run was
        # taken over while we were still inside it. Stopping the timer is the
        # bookkeeping half; this is the half that tells the owner to stop working.
        self._on_lease_lost = on_lease_lost
        self._timers: dict[str, Any] = {}
        # Runs the heartbeat is SUPPOSED to be beating for. ``_arm`` refuses to
        # install a timer for a run not in here, which is what makes stop()
        # authoritative against an in-flight _tick: the tick claims its timer,
        # renews, and re-arms — if a stop() ran in between, the re-arm finds the
        # run gone and no immortal timer survives to renew a released lease.
        # (WF-30 — found by Lohra itself reviewing this file in a dogfood run.)
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def start(self, run_id: str) -> None:
        """Begin beating for this run (re-arming replaces the pending tick)."""
        with self._lock:
            self._active.add(run_id)
        self._arm(run_id)

    def stop(self, run_id: str) -> None:
        """Stop beating. A tick that outlived its run would renew a lease nobody
        is using — the run would read as alive forever and never be resumable."""
        with self._lock:
            self._active.discard(run_id)
            self._drop(run_id)

    def shutdown(self) -> None:
        """No heartbeat outlives the service that armed it."""
        with self._lock:
            self._active.clear()
            for run_id in list(self._timers):
                self._drop(run_id)

    # --- internals ------------------------------------------------------

    def _arm(self, run_id: str) -> None:
        timer = self._timer_factory(self._interval, lambda: self._tick(run_id))
        with self._lock:
            if run_id not in self._active:
                # A stop()/shutdown() won the race against this (re-)arm.
                timer.cancel()
                return
            self._drop(run_id)
            self._timers[run_id] = timer
        timer.start()

    def _drop(self, run_id: str) -> None:
        """Cancel + forget one timer. Called under ``self._lock``."""
        timer = self._timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    def _tick(self, run_id: str) -> None:
        # Claim the tick under the lock, like AutoResumeScheduler._fire: a stop
        # that raced us already popped it, and renewing a lease this process has
        # let go of would keep a finished run looking alive.
        with self._lock:
            if self._timers.pop(run_id, None) is None:
                return
        try:
            held = self._renew(run_id)
        except Exception:  # a timer thread dying silently would strand the run
            logger.exception("workflow: lease heartbeat for run %s failed", run_id)
            held = True  # one lost write is what the TTL is for — keep beating
        if not held:
            # The lease is somebody else's now (or gone). Beating on would only
            # be noise: this process has no claim left to renew.
            logger.debug("workflow: lease for run %s is no longer ours; heartbeat stops", run_id)
            self._notify_lost(run_id)
            return
        self._arm(run_id)

    def _notify_lost(self, run_id: str) -> None:
        """Tell the owner its run was taken over — OUTSIDE ``self._lock``.

        The callback aborts an engine and tears down a thread pool; holding this
        file's lock across somebody else's teardown is how a lock-ordering
        deadlock gets introduced later (the WF-30 lesson, applied to a new
        caller). Exactly once per loss: ``_tick`` already claimed its timer under
        the lock and returns WITHOUT re-arming, so no second tick for this run
        survives to report the same loss again.

        A raise must never reach the timer thread: it would die silently and the
        run would keep beating on a lease it no longer holds."""
        if self._on_lease_lost is None:
            return
        try:
            self._on_lease_lost(run_id)
        except Exception:
            logger.exception("workflow: lease-lost handler failed for run %s", run_id)
