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
from dataclasses import dataclass
from typing import Any, Callable

from lohra.state.runstate import ResumeToken

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


@dataclass(eq=False)
class ResumePlan:
    """Local identity, retained from accepted pause through readiness and claim."""

    run_id: str
    deadline: float
    token: ResumeToken | None
    resume: Callable[[], Any]
    timer: Any = None
    starting: bool = False
    accepted: bool = False
    fired_early: bool = False


class AutoResumeScheduler:
    """One identified plan per run; timer effects never run under its mutex."""

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
        self._plans: dict[str, ResumePlan] = {}
        self._lock = threading.Lock()
        self._closed = False

    @property
    def _timers(self) -> dict[str, Any]:
        with self._lock:
            return {run_id: plan.timer for run_id, plan in self._plans.items()
                    if plan.timer is not None}

    def deadline(self, run_id: str, attempts: int, retry_after: float | None = None) -> float | None:
        """Choose timing while the caller still owns the functional decision."""
        if attempts >= self._max_attempts:
            logger.warning("workflow: run %s stays paused after %d auto-resume attempt(s); "
                           "resume it manually with run_workflow(resume_run_id=...)",
                           run_id, attempts)
            return None
        return self._clock() + resume_delay(attempts, retry_after)

    def prepare(
        self, run_id: str, *, attempts: int, deadline: float | None,
        token: ResumeToken | None = None, resume: Callable[[], Any] | None = None,
    ) -> ResumePlan | None:
        """Install bookkeeping only. Equal durable tokens retain their local deadline."""
        if attempts >= self._max_attempts:
            return None
        chosen = deadline if deadline is not None else self.deadline(run_id, attempts)
        plan = ResumePlan(run_id, chosen, token, resume or (lambda: self._resume(run_id)))
        with self._lock:
            if self._closed:
                return None
            old = self._plans.get(run_id)
            if old is not None and token is not None and old.token is not None:
                if token == old.token:
                    return old
                if (token.fence or 0) <= (old.token.fence or 0):
                    return None  # an old recovery read cannot replace a newer pause
            self._plans[run_id] = plan
        self._cancel_timer(old)
        return plan

    def current(self, run_id: str) -> ResumePlan | None:
        with self._lock:
            return self._plans.get(run_id)

    def arm(self, plan: ResumePlan) -> bool:
        """True only for a newly accepted start, including an immediate firing.

        A start may call us inline, then throw. Record that firing until start
        returns successfully; neither wait in the callback nor grant early work.
        """
        with self._lock:
            if self._plans.get(plan.run_id) is not plan or plan.starting:
                return False
            plan.starting = True
        timer = None
        try:
            timer = self._timer_factory(max(0, plan.deadline - self._clock()),
                                        lambda: self._fire(plan))
            with self._lock:
                current = self._plans.get(plan.run_id) is plan
                if current:
                    plan.timer = timer
            if not current:
                timer.cancel()
                return False
            timer.start()
        except BaseException as error:
            self.cancel(plan.run_id, plan=plan)
            if not isinstance(error, Exception):
                raise  # cleanup the identity without swallowing process interrupts
            logger.exception("workflow: could not arm auto-resume of run %s", plan.run_id)
            return False
        with self._lock:
            current = self._plans.get(plan.run_id) is plan
            if current:
                plan.accepted = True
                fired = plan.fired_early
        if not current:
            self._cancel_timer(plan)
            return False
        if fired:
            self._fire(plan)
        return True

    def schedule(
        self, run_id: str, *, attempts: int, retry_after: float | None = None
    ) -> float | None:
        """Arm the retry; return the wall-clock time it will fire, or None when
        the attempt cap is spent (the run stays paused for a MANUAL resume —
        loudly, never a silent give-up)."""
        deadline = self.deadline(run_id, attempts, retry_after)
        if deadline is None:
            return None
        plan = self.prepare(run_id, attempts=attempts, deadline=deadline)
        return deadline if plan is not None and self.arm(plan) else None

    def cancel(self, run_id: str, *, plan: ResumePlan | None = None) -> None:
        with self._lock:
            old = self._plans.get(run_id)
            if old is None or (plan is not None and old is not plan):
                return
            del self._plans[run_id]
        self._cancel_timer(old)

    def acquired(self, run_id: str, fence: int) -> None:
        """Only a winning acquisition revokes the previous acquisition's intent."""
        plan = self.current(run_id)
        if plan is not None and (plan.token is None or (plan.token.fence or 0) < fence):
            self.cancel(run_id, plan=plan)

    def cancel_fence(self, run_id: str, fence: int | None) -> None:
        plan = self.current(run_id)
        if plan is not None and (plan.token is None or plan.token.fence == fence):
            self.cancel(run_id, plan=plan)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            plans = list(self._plans.values())
            self._plans.clear()
        for plan in plans:
            self._cancel_timer(plan)

    # --- internals ------------------------------------------------------

    @staticmethod
    def _cancel_timer(plan: ResumePlan | None) -> None:
        if plan is not None and plan.timer is not None:
            try:
                plan.timer.cancel()
            except Exception:
                logger.exception("workflow: could not cancel retry timer of run %s", plan.run_id)

    def _fire(self, plan: ResumePlan) -> None:
        # Claim the timer under the lock: a cancel that raced us already popped
        # it, and re-launching a run the caller just stopped is exactly the
        # resurrection WF-19 is about.
        with self._lock:
            if self._plans.get(plan.run_id) is not plan:
                return
            if not plan.accepted:
                plan.fired_early = True
                return
            del self._plans[plan.run_id]
        run_id = plan.run_id
        try:
            outcome = plan.resume()
        except Exception:  # a timer thread dying silently would strand the run
            logger.exception("workflow: auto-resume of run %s failed", run_id)
            return
        # The service refuses a resume it can't do safely (the run turned out to
        # be live, or stopped being paused). Say so — a retry swallowed here
        # would leave the run paused with nothing left to wake it.
        if isinstance(outcome, dict) and outcome.get("error"):
            logger.warning("workflow: auto-resume of run %s refused: %s", run_id, outcome["error"])
