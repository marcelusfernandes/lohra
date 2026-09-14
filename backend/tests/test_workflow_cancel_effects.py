"""#126 publication and callback identity across functional transitions."""

import threading
import sqlite3

import pytest

from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.events import DONE, NODE
from lohra.state.runstate import StateWrite
from tests.test_workflow_cancel_transition import SPEC, isolated_environment, paused_service  # noqa: F401
from tests.test_workflow_operability import _service
from tests.test_workflow_quota import TimerFactory
from tests.test_workflow_quota import _rate_limited


@pytest.mark.parametrize("cancel_before_result", [False, True])
def test_publication_must_match_the_cancel_decision(tmp_path, monkeypatch, cancel_before_result):
    entered, finish = threading.Event(), threading.Event()
    checked, cancel_now = threading.Event(), threading.Event()
    record_ready, settle_now = threading.Event(), threading.Event()
    published, notifications, events, replies, errors = [], [], [], [], []

    def answer(_):
        entered.set()
        assert finish.wait(10)
        return "ok"

    db = SessionDB(tmp_path / "state.db")
    svc = _service(
        db, tmp_path, answer, timers=TimerFactory(),
        on_run_done=lambda *args: notifications.append(args),
    )
    svc._events.set_sink(lambda rid, kind, data: events.append((kind, data)))
    monkeypatch.setattr(
        library, "record_outcome",
        lambda home, spec, result, **kwargs: published.append(result.status),
    )
    original_spend = svc._persist_spend

    def hold_epilogue(state):
        if state.result is not None:
            record_ready.set()  # record closure was already captured, if any.
            assert settle_now.wait(10)
        return original_spend(state)

    monkeypatch.setattr(svc, "_persist_spend", hold_epilogue)
    original_cancel = svc._store.cancel_state

    def hold_cancel(run_id, **conditions):
        checked.set()
        assert cancel_now.wait(10)
        return original_cancel(run_id, **conditions)

    monkeypatch.setattr(svc._store, "cancel_state", hold_cancel)

    def cancel(rid):
        try:
            replies.append(svc.cancel(rid))
        except BaseException as exc:
            errors.append(exc)

    thread = None
    try:
        rid = svc.start(SPEC, {}, owner="synthetic-owner")["run_id"]
        assert entered.wait(10)
        thread = threading.Thread(target=cancel, args=(rid,))
        thread.start()
        assert checked.wait(10)
        if not cancel_before_result:
            finish.set()
            assert record_ready.wait(10)
        cancel_now.set()
        thread.join(10)
        assert not thread.is_alive() and not errors
        if cancel_before_result:
            finish.set()
            assert record_ready.wait(10)
        settle_now.set()
        svc._runs[rid].future.result(10)
        status = svc._store.load(rid).status
        assert events[-1][0] == DONE and events[-1][1]["status"] == status
        if replies[0].get("ok"):
            assert status == "cancelled"
            assert notifications == []
            assert published == [], (status, published)
        else:
            assert status == "complete"
            assert published == ["complete"]
            assert len(notifications) == 1 and notifications[0][2] == "complete"
    finally:
        cancel_now.set()
        finish.set()
        settle_now.set()
        if thread is not None:
            thread.join(10)
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("old_generation", [True, False])
def test_event_callback_carries_its_own_acquisition(tmp_path, monkeypatch, old_generation):
    svc, db, rid, _ = paused_service(tmp_path)
    try:
        old = svc._runs[rid]
        assert svc.resume(rid).get("status") == "started"
        new = svc._runs[rid]
        new.future.result(10)
        assert old.fence < new.fence
        writes, events = [], []
        persist = svc._persist_state

        def observed(state):
            writes.append(state.fence)
            return persist(state)

        monkeypatch.setattr(svc, "_persist_state", observed)
        svc._events.set_sink(lambda *args: events.append(args))
        engine = old.engine if old_generation else new.engine
        engine._emit(NODE, {"node_id": "synthetic-late-node", "state": "complete"})
        if old_generation:
            assert writes == [], (old.fence, new.fence, writes)
            assert events == []
        else:
            assert writes == [new.fence] and len(events) == 1
    finally:
        svc.shutdown()
        db.close()


def test_lease_loss_callback_cannot_abort_a_new_local_acquisition(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    try:
        old = svc._runs[rid]
        assert svc.resume(rid).get("status") == "started"
        current = svc._runs[rid]
        current.future.result(10)
        svc._abort_fenced_run(rid, old.fence)
        assert not current.fenced and not current.engine.cancelled
        svc._abort_fenced_run(rid, current.fence)
        assert current.fenced and current.engine.cancelled
    finally:
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("refusal", ["storage_error", "conflict"])
def test_refused_functional_finish_cannot_publish_from_later_snapshot(tmp_path, monkeypatch, refusal):
    db = SessionDB(tmp_path / "state.db")
    notifications, records, snapshots = [], [], []
    svc = _service(db, tmp_path, lambda _: "ok", timers=TimerFactory(),
                   on_run_done=lambda *args: notifications.append(args))
    write = db.run_state_write

    def refuse_finish(run_id, fields, now, **conditions):
        if conditions["mode"] == "finish":
            if refusal == "storage_error":
                raise sqlite3.OperationalError("synthetic failure")
            return StateWrite("conflict", db.run_state_get(run_id), conditions["fence"])
        receipt = write(run_id, fields, now, **conditions)
        if conditions["mode"] == "snapshot" and receipt.kind == "written":
            snapshots.append(fields["status"])
        return receipt

    monkeypatch.setattr(db, "run_state_write", refuse_finish)
    monkeypatch.setattr(library, "record_outcome", lambda *a, **kw: records.append(kw))
    try:
        rid = svc.start(SPEC, {}, owner="synthetic-owner")["run_id"]
        svc._runs[rid].future.result(10)
        assert snapshots and snapshots[-1] == "running"  # real later write accepted
        assert svc._store.load(rid).status == "running"
        assert notifications == records == []
        assert svc._store.lease_expiry(rid) is None
        assert svc._runs[rid].error
        # Storage has recovered: an explicit replay can now recover the stopped
        # acquisition in this same service, instead of being stuck "still live".
        monkeypatch.setattr(db, "run_state_write", write)
        assert svc.start(resume_run_id=rid).get("status") == "started"
        svc._runs[rid].future.result(10)
        assert svc._store.load(rid).status == "complete"

    finally:
        svc.shutdown()
        db.close()


def test_cancel_during_prepare_cannot_restore_paused_payload(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    timers = TimerFactory()
    db = SessionDB(tmp_path / "state.db")

    def limited(_):
        raise _rate_limited("30")

    svc = _service(db, tmp_path, limited, timers=timers)
    prepare = svc._autoresume.prepare
    observations = []

    def held_prepare(run_id, **kwargs):
        state = svc._runs[run_id]
        observations.append((svc._lock.locked(), state.state_lock.locked()))
        entered.set()
        assert release.wait(10)
        return prepare(run_id, **kwargs)

    monkeypatch.setattr(svc._autoresume, "prepare", held_prepare)
    try:
        rid = svc.start(SPEC, {})["run_id"]
        assert entered.wait(10)
        assert svc.cancel(rid).get("ok")
        revision = svc._runs[rid].revision
        release.set()
        svc._runs[rid].future.result(10)
        row = svc._store.load(rid)
        assert row.status == svc._runs[rid].status == "cancelled"
        assert row.resume_at is row.checkpoint is row.route_fault is row.pause_reason is None
        assert svc._runs[rid].resume_at is None and row.revision >= revision
        assert observations == [(False, False)]
        assert not svc._autoresume._timers
        assert "error" in svc.resume(rid)
    finally:
        release.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("cancel_first", [False, True])
@pytest.mark.parametrize("status", ["complete", "degraded", "failed"])
def test_functional_winner_is_the_only_outcome_published(tmp_path, monkeypatch, status, cancel_first):
    from lohra.workflow.engine import RunResult, WorkflowEngine

    entered, release = threading.Event(), threading.Event()
    finished, drain = threading.Event(), threading.Event()
    records, notifications, events = [], [], []
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "unreachable", timers=TimerFactory(),
                   on_run_done=lambda *args: notifications.append(args))

    def run(*args):
        entered.set()
        assert release.wait(10)
        return RunResult(status=status, outputs={"a": "kept"})

    def held_epilogue(state):
        finished.set()
        assert drain.wait(10)

    monkeypatch.setattr(WorkflowEngine, "run", run)
    monkeypatch.setattr(svc, "_close_audit_segment", lambda state, engine: held_epilogue(state))
    monkeypatch.setattr(library, "record_outcome", lambda *args, **kw: records.append(args[2]))
    svc._events.set_sink(lambda rid, kind, data: events.append((kind, data)))
    try:
        rid = svc.start(SPEC, {}, owner="synthetic-owner")["run_id"]
        assert entered.wait(10)
        if cancel_first:
            assert svc.cancel(rid).get("ok")
        release.set()
        assert finished.wait(10)
        if not cancel_first:
            assert "error" in svc.cancel(rid)
        expected = "cancelled" if cancel_first else status
        assert svc._store.load(rid).status == expected
        assert records == notifications == []  # decision is not publication
        drain.set()
        svc._runs[rid].future.result(10)
        assert svc._store.load(rid).status == svc._runs[rid].status == expected
        assert events[-1][1]["status"] == expected
        assert len(records) == len(notifications) == (0 if cancel_first else 1)
        if records:
            assert records[0].status == status and records[0].outputs == {"a": "kept"}
    finally:
        release.set()
        drain.set()
        svc.shutdown()
        db.close()


def test_other_connection_sees_decision_before_drain_without_early_publication(tmp_path, monkeypatch):
    from lohra.workflow.engine import RunResult, WorkflowEngine
    from lohra.workflow.watch import watch_run

    monkeypatch.setenv("LOHRA_AUDIT", "on")
    entered, drain = threading.Event(), threading.Event()
    records, notifications = [], []
    db = SessionDB(tmp_path / "state.db")
    other_db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "unreachable", timers=TimerFactory(),
                   on_run_done=lambda *args: notifications.append(args))
    other = _service(other_db, tmp_path, lambda _: "unreachable", timers=TimerFactory())
    persist = svc._persist_spend

    def run(engine, *args):
        engine.budget.charge_tokens(5, 3)  # synthetic uncached usage, no provider
        return RunResult(status="complete", tokens_in=5, tokens_out=3)

    def held_spend(state):
        if state.result is not None:
            entered.set()
            assert drain.wait(10)
        return persist(state)

    monkeypatch.setattr(WorkflowEngine, "run", run)
    monkeypatch.setattr(svc, "_persist_spend", held_spend)
    monkeypatch.setattr(library, "record_outcome", lambda *args, **kw: records.append(args[2]))
    try:
        rid = svc.start(SPEC, {}, owner="synthetic-owner")["run_id"]
        assert entered.wait(10)
        row = other._store.load(rid)
        assert row.status == "complete" and row.audit_segment_id is not None
        assert other._store.lease_expiry(rid) is not None
        assert "error" in other.start(resume_run_id=rid)
        observed = other.status(rid)
        assert observed["status"] == "complete" and observed["tokens_spent_total"] == 0
        lines, warnings, waits = [], [], []
        assert watch_run(other._store, other_db, rid, write=lines.append,
                         warn=warnings.append, sleep=waits.append) == 0
        assert lines and not warnings and not waits
        assert records == notifications == []
        drain.set()
        svc._runs[rid].future.result(10)
        assert other.status(rid)["tokens_spent_total"] == 8
        assert other._store.load(rid).audit_segment_id is None
        assert other._store.lease_expiry(rid) is None
        assert len(records) == len(notifications) == 1
    finally:
        drain.set()
        svc.shutdown()
        other.shutdown()
        other_db.close()
        db.close()
