"""#127: actual Service/Core/SQLite, manual clocks/timers and explicit readiness."""

import threading

import pytest

from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.state import SessionDB
from tests.test_workflow_quota import _SPEC, _rate_limited, _service, TimerFactory
from tests.test_workflow_service_submission import isolated_environment  # noqa: F401


@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    yield database
    database.close()


class ManualTimer:
    def __init__(self, delay, fire, owner):
        self.delay, self.fire, self.owner = delay, fire, owner
        self.cancelled = False

    def start(self):
        with self.owner.condition:
            self.owner.started.append(self)
            self.owner.condition.notify_all()

    def cancel(self):
        self.cancelled = True


class Timers:
    def __init__(self):
        self.condition = threading.Condition()
        self.started = []
        self.completed = None

    def __call__(self, delay, fire):
        return ManualTimer(delay, fire, self)

    def wait(self, count=1):
        with self.condition:
            assert self.condition.wait_for(
                lambda: len(self.started) >= count and
                (self.completed is None or len(self.completed) >= count), timeout=5,
            )
            return self.started[count - 1]


def observe_arming(service, timers):
    """Tests wait for arming effects explicitly, not merely Future.result()."""
    original = service._arm_resume
    timers.completed = []

    def observe(scheduler, plan):
        try:
            return original(scheduler, plan)
        finally:
            with timers.condition:
                timers.completed.append(plan)
                timers.condition.notify_all()

    service._arm_resume = observe


@pytest.mark.parametrize("cancel_first", [False, True])
def test_old_callback_cannot_consume_replacement(cancel_first):
    timers, calls = Timers(), []
    scheduler = AutoResumeScheduler(calls.append, timer_factory=timers, clock=lambda: 1000)
    try:
        scheduler.schedule("r", attempts=0)
        old = timers.wait()
        if cancel_first:
            scheduler.cancel("r")
        scheduler.schedule("r", attempts=1)
        new = timers.wait(2)
        old.fire()
        assert calls == []
        new.fire()
        new.fire()
        assert calls == ["r"]
    finally:
        scheduler.shutdown()


@pytest.mark.parametrize("fenced", [False, True])
def test_deadline_is_durable_but_timer_waits_for_future_done(db, tmp_path, monkeypatch, fenced):
    now, calls = [1000.0], []
    epilogue, release = threading.Event(), threading.Event()
    timers = Timers()

    def responder(_prompt):
        calls.append("leaf")
        if len(calls) == 1:
            raise _rate_limited("30")
        return "recovered"

    service = _service(db, tmp_path, responder, timers=TimerFactory())
    service.set_autoresume(AutoResumeScheduler(
        service.resume, timer_factory=timers, clock=lambda: now[0],
    ))
    observe_arming(service, timers)
    original = service._emit_done

    def hold_done(state):
        original(state)
        if state.attempts == 0:
            epilogue.set()
            assert release.wait(5)

    monkeypatch.setattr(service, "_emit_done", hold_done)
    try:
        run_id = service.start(_SPEC)["run_id"]
        assert epilogue.wait(5)
        first = service._runs[run_id]
        assert service._store.lease_expiry(run_id) is None
        assert not first.future.done()
        row = service._store.load(run_id)
        assert row.status == "paused" and row.resume_at == 1060.0
        assert timers.started == []
        if fenced:
            service._abort_fenced_run(run_id, first.fence)
            assert service._get(run_id) is None  # stale functional view is correctly hidden
        assert service.rearm_pending_resumes() == 0  # a hidden Future still has to drain
        assert timers.started == []
        now[0] = 1200
        release.set()
        if fenced:
            first.future.result(5)
            with timers.condition:
                assert timers.condition.wait_for(lambda: timers.completed, timeout=5)
            assert not timers.started  # the loss callback revoked the old plan
            assert service.rearm_pending_resumes() == 1  # explicit reconstruction after drain
        timer = timers.wait()
        assert first.future.done() and timer.delay == 0
        timer.fire()
        timer.fire()
        assert service.status(run_id, wait=True, timeout=5)["status"] == "complete"
        assert service._store.load(run_id).attempts == 1
        assert calls == ["leaf", "leaf"]
    finally:
        release.set()
        service.shutdown()
