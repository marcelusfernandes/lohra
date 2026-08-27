"""Auto-resume for runs paused by provider quota (CC-parity WF-1).

A quota pause is the one failure that fixes itself given time, so a paused run
gets a timer that re-launches it as a RESUME (same run_id, same node cache — the
cells that already completed replay instead of re-spawning).

The pause deliberately does NOT block the engine thread: the run returns its
partial result, its future completes, and the retry is scheduled from OUT here.
A run that slept inside the engine would still read as live, and the service's
own liveness guard would then refuse the very resume it was waiting for.

Timers and the clock are injected so the whole policy is testable without a
single real sleep.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Never hammer a rate-limited provider: even a "retry-after: 2" waits this long.
MIN_RESUME_DELAY = 60.0
# A daily quota can take hours to roll over; past this, waiting longer is worse
# than telling the agent to resume by hand.
MAX_RESUME_DELAY = 6 * 60 * 60.0
MAX_RESUME_ATTEMPTS = 5

# Returns the service's start() reply: {run_id,...} or {error: ...}.
Resume = Callable[[str], Any]
TimerFactory = Callable[[float, Callable[[], None]], Any]


def resume_delay(attempts: int, retry_after: float | None = None) -> float:
    """Seconds to wait before retry number ``attempts + 1``.

    The provider's own ``retry-after`` wins when it sent one (it knows when the
    window rolls over); otherwise exponential backoff off the floor. Both are
    clamped into [MIN, MAX] — a provider asking for 2s is still a provider we
    just overran.
    """
    if retry_after is not None and retry_after > 0:
        return min(max(retry_after, MIN_RESUME_DELAY), MAX_RESUME_DELAY)
    return min(MIN_RESUME_DELAY * (2 ** max(0, attempts)), MAX_RESUME_DELAY)


def _daemon_timer(delay: float, fire: Callable[[], None]) -> threading.Timer:
    """A real timer that never keeps the process alive on its own."""
    timer = threading.Timer(delay, fire)
    timer.daemon = True
    return timer


class AutoResumeScheduler:
    """One pending retry per run_id. Cancelling a run cancels its retry."""

    def __init__(
        self,
        resume: Resume,
        *,
        timer_factory: TimerFactory | None = None,
        clock: Callable[[], float] = time.time,
        max_attempts: int = MAX_RESUME_ATTEMPTS,
    ) -> None:
        self._resume = resume
        self._timer_factory = timer_factory if timer_factory is not None else _daemon_timer
        self._clock = clock
        self._max_attempts = max(0, max_attempts)
        self._timers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def schedule(
        self, run_id: str, *, attempts: int, retry_after: float | None = None
    ) -> float | None:
        """Arm the retry; return the wall-clock time it will fire, or None when
        the attempt cap is spent (the run stays paused for a MANUAL resume —
        loudly, never a silent give-up)."""
        if attempts >= self._max_attempts:
            logger.warning(
                "workflow: run %s stays paused after %d auto-resume attempt(s); "
                "resume it manually with run_workflow(resume_run_id=...)",
                run_id,
                attempts,
            )
            return None
        delay = resume_delay(attempts, retry_after)
        timer = self._timer_factory(delay, lambda: self._fire(run_id))
        with self._lock:
            self._drop(run_id)
            self._timers[run_id] = timer
        timer.start()
        logger.info("workflow: run %s paused; auto-resume in %.0fs (attempt %d)",
                    run_id, delay, attempts + 1)
        return self._clock() + delay

    def cancel(self, run_id: str) -> None:
        with self._lock:
            self._drop(run_id)

    def shutdown(self) -> None:
        with self._lock:
            for run_id in list(self._timers):
                self._drop(run_id)

    # --- internals ------------------------------------------------------

    def _drop(self, run_id: str) -> None:
        """Cancel + forget one timer. Called under ``self._lock``."""
        timer = self._timers.pop(run_id, None)
        if timer is not None:
            timer.cancel()

    def _fire(self, run_id: str) -> None:
        # Claim the timer under the lock: a cancel that raced us already popped
        # it, and re-launching a run the caller just stopped is exactly the
        # resurrection WF-19 is about.
        with self._lock:
            if self._timers.pop(run_id, None) is None:
                return
        try:
            outcome = self._resume(run_id)
        except Exception:  # a timer thread dying silently would strand the run
            logger.exception("workflow: auto-resume of run %s failed", run_id)
            return
        # The service refuses a resume it can't do safely (the run turned out to
        # be live, or stopped being paused). Say so — a retry swallowed here
        # would leave the run paused with nothing left to wake it.
        if isinstance(outcome, dict) and outcome.get("error"):
            logger.warning("workflow: auto-resume of run %s refused: %s", run_id, outcome["error"])
