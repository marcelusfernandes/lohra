"""#138 repair: an acquisition owns cleanup until renewal setup succeeds."""

from contextlib import closing
from dataclasses import replace
import sqlite3
from threading import Event, Thread

import pytest

from lohra.state import SessionDB
from lohra.workflow.runstate_store import RunStateStore
from tests.test_workflow_service_submission import (
    InertTimer, SPEC, SyntheticAbort, isolated_environment, service,  # noqa: F401
)


def prepare(r, entry):
    if entry == "fresh":
        return None, lambda: r.svc.start(SPEC)
    rid = r.svc.start(SPEC, owner="prior-owner")["run_id"]
    assert r.svc.status(rid, wait=True, timeout=10)["status"] == "complete"
    if entry == "paused":
        prior = r.svc._store.load(rid)
        assert r.svc._store.save_snapshot(
            replace(prior, status="paused", pause_reason="user_pause"), fence=prior.fence,
            mode="launch", expected_revision=prior.revision,
        ).accepted
        return rid, lambda: r.svc.resume(rid)
    return rid, lambda: r.svc.start(resume_run_id=rid)


@pytest.mark.parametrize("entry", ["fresh", "replay", "paused"])
@pytest.mark.parametrize("phase", ["factory", "start"])
@pytest.mark.parametrize("error_type", [RuntimeError, SyntheticAbort])
def test_failed_renewal_setup_releases_exact_lease_and_allows_immediate_retry(
    tmp_path, monkeypatch, entry, phase, error_type,
):
    with service(tmp_path, monkeypatch) as r:
        rid, launch = prepare(r, entry)
        prior = r.db.run_state_get(rid) if rid else None
        spend = r.db.run_spend_get(rid) if rid else None
        effects = (len(r.calls), len(r.tools), len(r.events), len(r.notices), len(r.executed))
        acquired, timers, observations = [], [], []
        remember = r.svc._store._remember_acquisition
        error = error_type("synthetic renewal setup failure")

        def remember_which(run_id, fence, now):
            acquired.append((run_id, fence))
            return remember(run_id, fence, now)

        def observe():
            observations.append((r.svc._lifecycle_lock.locked(), r.svc._lock.locked(),
                                 r.svc._store._lock.locked(), r.svc._store._heartbeat._lock.locked()))

        class FailingTimer(InertTimer):
            def start(self):
                observe()
                raise error

            def cancel(self):
                observe()

        def factory(delay, callback):
            observe()
            if phase == "factory":
                raise error
            timer = FailingTimer(delay, callback)
            timers.append(timer)
            return timer

        with monkeypatch.context() as patch:
            patch.setattr(r.svc._store, "_remember_acquisition", remember_which)
            patch.setattr(r.svc._store._heartbeat, "_timer_factory", factory)
            with pytest.raises(error_type) as caught:
                launch()
            assert caught.value is error
        rid, fence = acquired[0]
        assert r.svc._store.lease_expiry(rid) is None
        assert r.svc._store.fence_of(rid) == fence  # never rewind/forget ownership
        assert rid not in r.svc._store._renewed
        assert (rid, fence) not in r.svc._store._heartbeat._active
        assert (rid, fence) not in r.svc._store._heartbeat._timers
        for timer in timers:
            timer.callback()  # partial timer.start may leave this callable alive
        assert observations and all(item == (False, False, False, False) for item in observations)
        assert effects == (len(r.calls), len(r.tools), len(r.events), len(r.notices), len(r.executed))
        assert r.db.run_spend_get(rid) == spend
        after = r.db.run_state_get(rid)
        if prior:
            assert {k: v for k, v in after.items() if k != "fence"} == {
                k: v for k, v in prior.items() if k != "fence"
            }
        else:
            assert after is None and r.svc._get(rid) is None
        retry = launch()
        assert retry["status"] == "started"
        assert r.svc.status(retry["run_id"], wait=True, timeout=10)["status"] == "complete"
        assert r.svc._store.lease_expiry(retry["run_id"]) is None
        assert r.closes == []


@pytest.mark.parametrize("entry", ["fresh", "replay", "paused"])
def test_successful_renewal_setup_keeps_normal_acceptance(tmp_path, monkeypatch, entry):
    with service(tmp_path, monkeypatch) as r:
        _, launch = prepare(r, entry)
        reply = launch()
        assert reply["status"] == "started"
        assert r.svc.status(reply["run_id"], wait=True, timeout=10)["status"] == "complete"
        assert len(r.calls) == 2 and len(r.tools) == 1
        assert r.svc._store.lease_expiry(reply["run_id"]) is None


@pytest.mark.parametrize("same_store", [False, True])
def test_successor_survives_a_delayed_renewal_setup_failure(tmp_path, same_store):
    now = [1000.0]
    entered, release = Event(), Event()
    timers, losses, errors = [], [], []
    original = SyntheticAbort("synthetic late renewal setup failure")

    class Timer(InertTimer):
        def start(self):
            if self is timers[0]:
                entered.set()
                assert release.wait(10)
                raise original

    def factory(delay, callback):
        timer = Timer(delay, callback)
        timers.append(timer)
        return timer

    with closing(SessionDB(tmp_path / "state.db")) as db, \
         closing(SessionDB(tmp_path / "state.db")) as other_db:
        old = RunStateStore(db, clock=lambda: now[0], ttl=9, timer_factory=factory,
                            on_lease_lost=lambda *args: losses.append(args))
        new = old if same_store else RunStateStore(
            other_db, clock=lambda: now[0], ttl=9, timer_factory=InertTimer,
        )

        def acquire():
            try:
                old.acquire_result("r")
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=acquire)
        thread.start()
        try:
            assert entered.wait(5)
            now[0] = 1010.0
            successor = new.acquire_result("r")
            assert successor.accepted and successor.fence == 2
            held = new.lease_expiry("r")
            release.set()
            thread.join(10)
            assert not thread.is_alive() and errors == [original]
            assert new.lease_expiry("r") == held
            assert new.fence_of("r") == 2 and new._renewed["r"] == now[0]
            assert ("r", 2) in new._heartbeat._active
            assert ("r", 1) not in old._heartbeat._active
            timers[0].callback()  # even a cancelled timer may already have begun
            assert new.lease_expiry("r") == held and losses == []
            assert ("r", 2) in new._heartbeat._timers
            now[0] = 1013.0
            new._heartbeat._timers[("r", 2)].callback()
            assert new.lease_expiry("r") == 1022.0  # current renewal still works
        finally:
            release.set()
            thread.join(10)
            new.release("r", fence=2)
            old.shutdown()
            if new is not old:
                new.shutdown()


@pytest.mark.parametrize("cleanup_failure", ["cancel", "sqlite"])
def test_renewal_cleanup_failure_preserves_original_cause_and_revokes_callbacks(
    tmp_path, monkeypatch, cleanup_failure,
):
    original = SyntheticAbort("synthetic original timer start failure")
    observations, timers, renewals = [], [], []
    with closing(SessionDB(tmp_path / "state.db")) as db:
        store = RunStateStore(db, clock=lambda: 1000.0, timer_factory=InertTimer)
        release = db.release_run_lease
        renew = db.renew_run_lease

        class Timer(InertTimer):
            def start(self):
                raise original

            def cancel(self):
                observations.append((store._lock.locked(), store._heartbeat._lock.locked()))
                if cleanup_failure == "cancel":
                    raise RuntimeError("synthetic timer cancel failure")

        def factory(delay, callback):
            timer = Timer(delay, callback)
            timers.append(timer)
            return timer

        def release_observed(*args, **kwargs):
            observations.append((store._lock.locked(), store._heartbeat._lock.locked()))
            if cleanup_failure == "sqlite":
                raise sqlite3.OperationalError("synthetic lease delete unavailable")
            return release(*args, **kwargs)

        def renewed(*args, **kwargs):
            renewals.append((args, kwargs))
            return renew(*args, **kwargs)

        monkeypatch.setattr(store._heartbeat, "_timer_factory", factory)
        monkeypatch.setattr(db, "release_run_lease", release_observed)
        monkeypatch.setattr(db, "renew_run_lease", renewed)
        try:
            with pytest.raises(SyntheticAbort) as caught:
                store.acquire_result("r")
            assert caught.value is original
            assert store._heartbeat._active == set() and store._heartbeat._timers == {}
            assert store._renewed == {}
            assert observations == [(False, False), (False, False)]
            timers[0].callback()
            assert renewals == []
            assert store.lease_expiry("r") == (1900.0 if cleanup_failure == "sqlite" else None)
        finally:
            release("r", store.holder, fence=1)
            store.shutdown()


def test_partial_timer_start_cannot_renew_or_report_loss_after_failed_setup_cleanup(tmp_path, monkeypatch):
    entered, finish_tick = Event(), Event()
    ticks, renewals, losses = [], [], []
    original = RuntimeError("synthetic timer started a callback before failing")
    with closing(SessionDB(tmp_path / "state.db")) as db:
        store = RunStateStore(db, clock=lambda: 1000.0, timer_factory=InertTimer,
                              on_lease_lost=lambda *args: losses.append(args))
        renew, sql_renew = store._heartbeat._renew, db.renew_run_lease

        def held_renew(key):
            entered.set()
            assert finish_tick.wait(10)
            return renew(key)

        def sql_observer(*args, **kwargs):
            renewals.append(True)
            return sql_renew(*args, **kwargs)

        class PartialTimer(InertTimer):
            def start(self):
                thread = Thread(target=self.callback)
                ticks.append(thread)
                thread.start()
                assert entered.wait(5)
                raise original

        monkeypatch.setattr(store._heartbeat, "_timer_factory", PartialTimer)
        monkeypatch.setattr(store._heartbeat, "_renew", held_renew)
        monkeypatch.setattr(db, "renew_run_lease", sql_observer)
        try:
            with pytest.raises(RuntimeError) as caught:
                store.acquire_result("r")
            assert caught.value is original
            # Explicitly clean the leaked baseline lease too, so this oracle
            # isolates the already-claimed callback after the same real cleanup.
            store.release("r", fence=1)
            finish_tick.set()
            for thread in ticks:
                thread.join(10)
                assert not thread.is_alive()
            assert renewals == losses == []
            assert store._renewed == {} and store._heartbeat._active == set()
            assert store.lease_expiry("r") is None
        finally:
            finish_tick.set()
            for thread in ticks:
                thread.join(10)
            store.release("r", fence=1)
            store.shutdown()


@pytest.mark.parametrize("action", ["stop", "shutdown", "replace", "stopped_during_factory"])
def test_heartbeat_timer_cancellation_runs_outside_the_mutex(action):
    from lohra.workflow.lease_heartbeat import LeaseHeartbeat

    observations = []
    heartbeat = None

    class Timer(InertTimer):
        def cancel(self):
            held = heartbeat._lock.locked()
            observations.append(held)
            if not held:
                heartbeat.stop("unrelated")  # harmless reentry, never strand a RED worker

    def factory(delay, callback):
        if action == "stopped_during_factory":
            heartbeat.stop("r")
        return Timer(delay, callback)

    heartbeat = LeaseHeartbeat(lambda key: True, interval=1, timer_factory=factory)
    try:
        heartbeat.start("r")
        if action == "stop":
            heartbeat.stop("r")
        elif action == "shutdown":
            heartbeat.shutdown()
        elif action == "replace":
            heartbeat.start("r")
        assert observations == [False]
    finally:
        heartbeat.shutdown()
