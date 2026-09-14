"""#127 boundary interleavings with actual Core/Service and temporary SQLite."""

from contextlib import contextmanager
from dataclasses import replace
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.state.runstate import StateWrite
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.service import WorkflowService
from tests.test_workflow_autoresume_readiness import Timers, observe_arming
from tests.test_workflow_pipeline import ScriptedClient
from tests.test_workflow_quota import _SPEC, _quota_responder, TimerFactory
from tests.test_workflow_service_submission import isolated_environment  # noqa: F401


@contextmanager
def service(tmp_path, responder=_quota_responder):
    db = SessionDB(tmp_path / "state.db")
    timers, now = Timers(), [1000.0]
    svc = WorkflowService(
        base_child_factory=lambda: Agent(model="synthetic", provider=get_provider_profile("anthropic"),
                                         client=ScriptedClient(responder)),
        db=db, home=tmp_path, clock=lambda: now[0],
        lease_timer_factory=TimerFactory(), resume_timer_factory=timers,
    )
    observe_arming(svc, timers)
    try:
        yield svc, db, timers, now
    finally:
        svc.shutdown()
        db.close()


def pause(svc, timers):
    rid = svc.start(_SPEC)["run_id"]
    assert svc.status(rid, wait=True, timeout=5)["status"] == "paused"
    timers.wait()
    return rid


@pytest.mark.parametrize("boundary", ["plan", "before_persist", "after_persist", "prepare"])
def test_cancel_between_planning_persistence_and_readiness(tmp_path, monkeypatch, boundary):
    with service(tmp_path) as (svc, db, timers, _):
        entered, release = threading.Event(), threading.Event()
        target, name = ((svc._autoresume, "deadline") if boundary == "plan" else
                        (svc._autoresume, "prepare") if boundary == "prepare" else
                        (svc._store, "save_snapshot"))
        original = getattr(target, name)

        def gate(*args, **kwargs):
            if name == "save_snapshot" and kwargs["mode"] != "finish":
                return original(*args, **kwargs)
            before = boundary == "after_persist"
            result = original(*args, **kwargs) if before else None
            entered.set()
            assert release.wait(5)
            return result if before else original(*args, **kwargs)

        monkeypatch.setattr(target, name, gate)
        try:
            rid = svc.start(_SPEC)["run_id"]
            assert entered.wait(5)
            if boundary == "after_persist":
                assert svc._store.load(rid).resume_at == 1060  # first accepted pause already owns timing
            assert svc.cancel(rid).get("ok")
            release.set()
            svc._runs[rid].future.result(5)
            if boundary == "prepare":
                with timers.condition:
                    assert timers.condition.wait_for(lambda: timers.completed, timeout=5)
            assert svc._store.load(rid).status == "cancelled"
            assert svc._store.load(rid).resume_at is None
            assert not timers.started and svc._autoresume.current(rid) is None
        finally:
            release.set()


def test_failed_authoritative_pause_never_plans_retry(tmp_path, monkeypatch):
    with service(tmp_path) as (svc, db, timers, _):
        original = svc._store.save_snapshot

        def fail_pause(snapshot, **kwargs):
            if kwargs["mode"] == "finish":
                assert snapshot.resume_at == 1060
                return StateWrite("storage_error")
            return original(snapshot, **kwargs)

        monkeypatch.setattr(svc._store, "save_snapshot", fail_pause)
        rid = svc.start(_SPEC)["run_id"]
        svc._runs[rid].future.result(5)
        assert svc._store.load(rid).status == "running"
        assert svc._store.load(rid).resume_at is None
        assert timers.started == [] and svc._autoresume.current(rid) is None


def test_already_completed_future_registers_inline_without_rewriting_snapshot(tmp_path):
    with service(tmp_path) as (svc, db, timers, _):
        rid = pause(svc, timers)
        state = svc._runs[rid]
        svc._autoresume.cancel(rid)
        row = svc._store.load(rid)
        assert svc._store.save_snapshot(replace(row, progress={"done": 20}), fence=row.fence,
                                       mode="snapshot", expected_revision=row.revision).accepted
        before = db.run_state_get(rid)
        assert state.future.done()
        svc._on_paused(state, state.result)
        assert len(timers.started) == 2  # completed Future invokes this on the caller
        assert db.run_state_get(rid) == before


def test_delayed_old_completion_preserves_successors_timer(tmp_path, monkeypatch):
    with service(tmp_path) as (svc, db, timers, _):
        entered, release = threading.Event(), threading.Event()
        arm = svc._arm_resume
        old_done = threading.Event()

        def delay_old(scheduler, plan):
            if plan.token.attempts == 0:
                entered.set()
                assert release.wait(5)
                try:
                    return arm(scheduler, plan)
                finally:
                    old_done.set()
            return arm(scheduler, plan)

        monkeypatch.setattr(svc, "_arm_resume", delay_old)
        try:
            rid = svc.start(_SPEC)["run_id"]
            assert entered.wait(5)
            old = svc._runs[rid]
            assert old.future.done() and not timers.started
            assert svc.resume(rid).get("status") == "started"
            svc._runs[rid].future.result(5)
            new = timers.wait()
            current = svc._autoresume.current(rid)
            assert current.token.fence > old.fence
            release.set()
            assert old_done.wait(5)
            assert svc._autoresume.current(rid) is current
            assert not new.cancelled
        finally:
            release.set()


@pytest.mark.parametrize("entry", ["invalid_spec", "submission_refused", "renewal_refused"])
def test_manual_failure_preserves_only_the_authorized_generation(tmp_path, monkeypatch, entry):
    with service(tmp_path) as (svc, db, timers, _):
        rid = pause(svc, timers)
        old = svc._autoresume.current(rid)
        before = svc._store.load(rid)
        if entry == "invalid_spec":
            assert svc.start({"nodes": []}, resume_run_id=rid).get("error")
            assert svc._store.load(rid) == before
            assert svc._autoresume.current(rid) is old
            return
        if entry == "submission_refused":
            svc._pool.shutdown()
        else:
            def reject_timer(*args):
                raise RuntimeError("synthetic renewal refusal")
            monkeypatch.setattr(svc._store._heartbeat, "_timer_factory", reject_timer)
        with pytest.raises(RuntimeError):
            svc.resume(rid)
        restored = svc._store.load(rid)
        assert restored.fence == before.fence + 1
        assert (restored.status, restored.resume_at, restored.attempts) == (
            before.status, before.resume_at, before.attempts,
        )
        assert svc._autoresume.current(rid) is None
        assert old.timer.cancelled
        old.timer.fire()  # the new fence never migrates to the callback of the old one
        assert svc._store.load(rid) == restored
        assert svc.rearm_pending_resumes() == 1  # only this explicit recovery rebuilds authority
        assert svc._autoresume.current(rid).token.fence == restored.fence


@pytest.mark.parametrize("change", ["cancel", "takeover", "manual", "shutdown"])
def test_timer_claim_still_requires_authoritative_acquisition(tmp_path, monkeypatch, change):
    with service(tmp_path) as (svc, db, timers, _):
        rid = pause(svc, timers)
        old_timer = timers.wait()
        expected = svc._resume_expected
        other = RunStateStore(db, clock=lambda: 1000, timer_factory=TimerFactory())

        def after_claim(token):
            assert svc._autoresume.current(rid) is None
            if change == "cancel":
                assert svc.cancel(rid).get("ok")
            elif change == "takeover":
                assert other.acquire(rid)
                other.release(rid)
            elif change == "manual":
                assert svc.resume(rid).get("status") == "started"
                svc._runs[rid].future.result(5)
                timers.wait(2)
            else:
                svc.shutdown()
            return expected(token)

        monkeypatch.setattr(svc, "_resume_expected", after_claim)
        try:
            old_timer.fire()
            row = svc._store.load(rid)
            assert row.attempts == (1 if change == "manual" else 0)
            if change == "manual":
                assert svc._autoresume.current(rid).token.fence == row.fence
                assert not timers.wait(2).cancelled
            else:
                assert svc._autoresume.current(rid) is None
        finally:
            other.shutdown()
