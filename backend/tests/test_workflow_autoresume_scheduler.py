"""Local timer identity, partial starts and reentrant effects for #127."""

import pytest

from lohra.state.runstate import ResumeToken
from lohra.workflow.autoresume import AutoResumeScheduler
from tests.test_workflow_autoresume_readiness import Timers


def token(fence=1, deadline=1060):
    return ResumeToken("r", fence, "paused", "quota_exhausted", deadline, 0)


@pytest.mark.parametrize("failure", [False, True])
def test_inline_start_defers_resume_until_acceptance(failure):
    calls, during, saved = [], [], []

    class Timer:
        def __init__(self, delay, fire):
            self.fire = fire
            saved.append(self)

        def start(self):
            self.fire()
            during.append(list(calls))
            if failure:
                raise RuntimeError("partial start")

        def cancel(self):
            assert not scheduler._lock.locked()

    scheduler = AutoResumeScheduler(calls.append, timer_factory=Timer, clock=lambda: 1000)
    plan = scheduler.prepare("r", attempts=0, deadline=1060)
    assert scheduler.arm(plan) is (not failure)
    assert during == [[]]
    assert calls == ([] if failure else ["r"])
    saved[0].fire()
    assert calls == ([] if failure else ["r"])
    assert scheduler.current("r") is None


@pytest.mark.parametrize("phase", ["factory", "start", "cancel", "resume"])
def test_timer_effects_allow_reentry_without_scheduler_mutex(phase):
    calls, seen = [], []

    def observe(at):
        assert not scheduler._lock.locked()
        scheduler.current("r")  # real reentry: no lock held across effects
        seen.append(at)

    class Timer:
        def __init__(self, delay, fire):
            observe("factory")
            self.fire = fire

        def start(self):
            observe("start")

        def cancel(self):
            observe("cancel")

    def resume(run_id):
        observe("resume")
        calls.append(run_id)

    scheduler = AutoResumeScheduler(resume, timer_factory=Timer)
    scheduler.schedule("r", attempts=0)
    old = scheduler.current("r")
    if phase == "resume":
        old.timer.fire()
        assert calls == ["r"]
    else:
        scheduler.cancel("r")
        old.timer.fire()
        assert calls == []
    assert phase in seen
    scheduler.shutdown()


@pytest.mark.parametrize("boundary", ["factory", "start"])
@pytest.mark.parametrize("change", ["cancel", "replace", "shutdown", "raise"])
def test_invalidation_during_arming_does_not_grant_old_callback(boundary, change):
    timers, calls, old_timers = Timers(), [], []

    def invalidate():
        if change == "cancel":
            scheduler.cancel("r")
        elif change == "shutdown":
            scheduler.shutdown()
        elif change == "replace":
            scheduler._timer_factory = timers
            assert scheduler.schedule("r", attempts=1) == 1120
        else:
            raise RuntimeError("synthetic arming failure")

    class Timer:
        def __init__(self, delay, fire):
            self.fire = fire
            old_timers.append(self)
            fire()  # even a factory can arrange an early callback
            if boundary == "factory":
                invalidate()

        def start(self):
            if boundary == "start":
                invalidate()

        def cancel(self):
            assert not scheduler._lock.locked()

    scheduler = AutoResumeScheduler(calls.append, timer_factory=Timer, clock=lambda: 1000)
    assert scheduler.schedule("r", attempts=0) is None
    old_timers[0].fire()
    assert calls == []
    if change == "replace":
        timers.wait().fire()
        assert calls == ["r"]
    else:
        assert scheduler.current("r") is None
    scheduler.shutdown()


def test_old_completion_and_cancellation_preserve_new_plan():
    timers, calls = Timers(), []
    scheduler = AutoResumeScheduler(calls.append, timer_factory=timers, clock=lambda: 1000)
    old = scheduler.prepare("r", attempts=0, deadline=1060, token=token())
    scheduler.acquired("r", 2)
    new = scheduler.prepare("r", attempts=0, deadline=1100, token=token(2, 1100))
    assert not scheduler.arm(old)
    scheduler.cancel("r", plan=old)
    scheduler.cancel_fence("r", 1)
    scheduler.acquired("r", 1)
    assert scheduler.prepare("r", attempts=0, deadline=1060, token=token()) is None
    assert scheduler.current("r") is new
    assert scheduler.arm(new)
    assert not scheduler.arm(new)
    timers.wait().fire()
    assert calls == ["r"]


def test_shutdown_blocks_delayed_readiness_and_new_admission():
    timers = Timers()
    scheduler = AutoResumeScheduler(lambda _: pytest.fail("resurrected"), timer_factory=timers)
    plan = scheduler.prepare("r", attempts=0, deadline=1060)
    scheduler.shutdown()
    assert not scheduler.arm(plan)
    assert scheduler.schedule("r", attempts=0) is None
    assert timers.started == []


@pytest.mark.parametrize("boundary", ["factory", "start"])
@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_interrupted_arming_revokes_identity_before_propagating(boundary, error_type):
    callbacks, calls = [], []
    error = error_type("synthetic interrupt")

    class Timer:
        def __init__(self, delay, fire):
            callbacks.append(fire)
            fire()
            if boundary == "factory":
                raise error

        def start(self):
            raise error

        def cancel(self):
            assert not scheduler._lock.locked()

    scheduler = AutoResumeScheduler(calls.append, timer_factory=Timer)
    plan = scheduler.prepare("r", attempts=0, deadline=1060)
    with pytest.raises(error_type) as caught:
        scheduler.arm(plan)
    assert caught.value is error
    callbacks[0]()
    assert calls == []
    assert scheduler.current("r") is None
    # Explicit recovery has a clean admission slot; no blind retry is created.
    assert scheduler.prepare("r", attempts=0, deadline=1060) is not None
    scheduler.shutdown()
