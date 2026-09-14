"""#127 durable pause identity and bounded recovery, using reopened SQLite."""

from contextlib import contextmanager
from dataclasses import replace

import pytest

from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.state import SessionDB
from lohra.state.runstate import ResumeToken
from lohra.workflow.autoresume import AutoResumeScheduler
from lohra.workflow.runstate_store import RunStateStore
from lohra.workflow.service import WorkflowService
from tests.test_workflow_quota import _SPEC, TimerFactory
from tests.test_workflow_service_submission import isolated_environment  # noqa: F401


@contextmanager
def recovery(tmp_path, *, deadline=1005, attempts=2, fenced=True, **fields):
    path = tmp_path / "state.db"
    first = SessionDB(path)
    writer = RunStateStore(first, clock=lambda: 1000, timer_factory=TimerFactory())
    if fenced:
        assert writer.acquire("r")
    assert writer.save(run_id="r", status="paused", pause_reason=QUOTA_EXHAUSTED,
                       spec=_SPEC, resume_at=deadline, attempts=attempts, **fields)
    if fenced:
        writer.release("r")
    writer.shutdown()
    first.close()
    db = SessionDB(path)
    timers, now = TimerFactory(), [1000.0]
    service = WorkflowService(
        base_child_factory=lambda: pytest.fail("unexpected provider entry"),
        db=db, home=tmp_path, clock=lambda: now[0], lease_timer_factory=TimerFactory(),
        resume_timer_factory=timers,
    )
    try:
        yield service, db, timers, now
    finally:
        service.shutdown()
        db.close()


@pytest.mark.parametrize("deadline,attempts,expected", [(1005, 0, 5), (995, 2, 0),
                                                     (None, 2, 240), (1005, 5, None)])
@pytest.mark.parametrize("fenced", [False, True])
def test_cold_recovery_preserves_saved_timing_and_deduplicates(
    tmp_path, deadline, attempts, expected, fenced,
):
    with recovery(tmp_path, deadline=deadline, attempts=attempts, fenced=fenced) as (svc, db, timers, now):
        before = db.run_state_get("r")
        assert len(timers.timers) == (0 if expected is None else 1)
        assert svc.rearm_pending_resumes() == 0
        now[0] = 1002
        assert svc.rearm_pending_resumes() == 0
        if expected is not None:
            assert timers.last.delay == expected
            plan = svc._autoresume.current("r")
            assert plan.token.fence == (1 if fenced else None)
            assert plan.token.resume_at == deadline
        assert db.run_state_get("r") == before  # no fabricated legacy timestamp


def test_recovery_skips_foreign_live_lease_then_explicit_scan_arms_once(tmp_path, caplog):
    with recovery(tmp_path, deadline=None) as (svc, db, timers, now):
        svc.set_autoresume(AutoResumeScheduler(svc.resume, timer_factory=timers, clock=lambda: now[0]))
        other = RunStateStore(db, clock=lambda: now[0], timer_factory=TimerFactory())
        assert other.acquire("r")
        try:
            before = len(timers.timers)
            assert svc.rearm_pending_resumes() == 0
            assert len(timers.timers) == before
            assert "ownership busy" in caplog.text
            other.release("r")
            assert svc.rearm_pending_resumes() == 1
            assert timers.last.delay == 240
            now[0] += 10
            assert svc.rearm_pending_resumes() == 0
            assert len(timers.timers) == before + 1
        finally:
            other.release("r")
            other.shutdown()


@pytest.mark.parametrize("mutation", ["progress", "cancel", "fence", "reason", "deadline", "attempts"])
def test_auto_acquire_checks_semantic_token_inside_sql_not_revision(tmp_path, monkeypatch, mutation):
    with recovery(tmp_path) as (svc, db, timers, _):
        prior = svc._store.load("r")
        token = svc._resume_token(prior)
        acquire = db.acquire_run_state
        other_db = SessionDB(tmp_path / "state.db")
        other = RunStateStore(other_db, clock=lambda: 1000, timer_factory=TimerFactory())

        def change_after_read(*args, **kwargs):
            current = other.load("r")
            if mutation == "cancel":
                assert other.cancel_state("r").accepted
            elif mutation == "fence":
                assert other.acquire("r")
                other.release("r")
            else:
                changes = {"progress": {"done": 12}, "audit_segment_id": None}
                changes.update({"reason": {"pause_reason": "user_requested"},
                                "deadline": {"resume_at": 1100},
                                "attempts": {"attempts": 3}}.get(mutation, {}))
                assert other.save_snapshot(replace(current, **changes), fence=current.fence,
                                           mode="snapshot", expected_revision=current.revision).accepted
            return acquire(*args, **kwargs)

        monkeypatch.setattr(db, "acquire_run_state", change_after_read)
        try:
            result = svc._store.acquire_paused("r", prior, resume_token=token)
            assert result.accepted is (mutation == "progress")
            if mutation == "progress":
                assert result.revision > prior.revision
                svc._store.release("r")
            else:
                assert result.kind == "conflict"
                assert svc._store.lease_expiry("r") is None
        finally:
            other.shutdown()
            other_db.close()


def test_lease_appearing_after_recovery_precheck_refuses_at_acquisition(tmp_path, monkeypatch, caplog):
    with recovery(tmp_path) as (svc, db, timers, _):
        other = RunStateStore(db, clock=lambda: 1000, timer_factory=TimerFactory())
        acquire = svc._store.acquire_paused

        def take_lease(run_id, prior, **kwargs):
            # A lease can be held without changing the pause token (legacy
            # lease producer); the existing SQL ownership predicate still wins.
            with db._connection:
                db._connection.execute(
                    "INSERT INTO workflow_run_locks VALUES (?, ?, ?, ?)",
                    (run_id, other.holder, 1000, 1900),
                )
            return acquire(run_id, prior, **kwargs)

        monkeypatch.setattr(svc._store, "acquire_paused", take_lease)
        try:
            timers.last.fire()
            assert "another process" in caplog.text
            assert svc._autoresume.current("r") is None
            assert svc._store.load("r").fence == 1
            assert svc._store.load("r").attempts == 2
        finally:
            db.release_run_lease("r", other.holder, fence=1)
            other.shutdown()


@pytest.mark.parametrize("failure", [False, True])
def test_rearm_count_includes_only_accepted_immediate_starts(tmp_path, failure):
    with recovery(tmp_path) as (svc, db, timers, now):
        calls = []

        class Immediate:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()
                assert calls == []
                if failure:
                    raise RuntimeError("synthetic partial start")

            def cancel(self):
                pass

        svc.set_autoresume(AutoResumeScheduler(svc.resume, timer_factory=Immediate, clock=lambda: now[0]))
        svc._resume_expected = lambda token: calls.append(token)
        assert svc.rearm_pending_resumes() == (0 if failure else 1)
        assert len(calls) == (0 if failure else 1)
        assert svc._autoresume.current("r") is None


def test_token_also_binds_run_id(tmp_path):
    with recovery(tmp_path) as (svc, db, timers, _):
        row = svc._store.load("r")
        token = ResumeToken("other", row.fence, row.status, row.pause_reason,
                            row.resume_at, row.attempts)
        assert svc._store.acquire_paused("r", row, resume_token=token).kind == "conflict"
