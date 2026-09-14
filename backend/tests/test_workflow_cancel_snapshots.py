"""Deferred functional snapshots keep their captured revision and acquisition."""

from collections import Counter
import sqlite3
import threading

import pytest

from lohra.state import SessionDB
from lohra.workflow.runstate_store import RunStateStore
from tests.test_workflow_cancel_transition import SPEC, isolated_environment, paused_service  # noqa: F401
from tests.test_workflow_operability import _service
from tests.test_workflow_quota import TimerFactory, _rate_limited


def test_deferred_snapshot_cannot_undo_cancellation(tmp_path, monkeypatch):
    svc, db, rid, _ = paused_service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    save = svc._store.save_snapshot
    writes = []

    def delayed(snapshot, **conditions):
        entered.set()
        assert release.wait(10)
        return save(snapshot, **conditions)

    monkeypatch.setattr(svc._store, "save_snapshot", delayed)
    thread = threading.Thread(target=lambda: writes.append(svc._persist_state(svc._runs[rid])))
    try:
        thread.start()
        assert entered.wait(10)
        assert svc.cancel(rid).get("ok")
        cancelled = db.run_state_get(rid)
        release.set()
        thread.join(10)
        assert not thread.is_alive() and writes == [False]
        assert db.run_state_get(rid) == cancelled
        assert svc._runs[rid].status == "cancelled" and svc._runs[rid].resume_at is None
    finally:
        release.set()
        thread.join(10)
        svc.shutdown()
        db.close()


def test_commit_return_reordering_does_not_restore_older_pause(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    entered, release = threading.Event(), threading.Event()

    def limited(_):
        raise _rate_limited("30")

    svc = _service(db, tmp_path, limited, timers=TimerFactory())
    save = svc._store.save_snapshot

    def delay_receipt(snapshot, **conditions):
        receipt = save(snapshot, **conditions)
        if conditions["mode"] == "finish":
            assert receipt.kind == "written" and receipt.row["status"] == "paused"
            entered.set()
            assert release.wait(10)
        return receipt

    monkeypatch.setattr(svc._store, "save_snapshot", delay_receipt)
    try:
        rid = svc.start(SPEC, {})["run_id"]
        assert entered.wait(10)
        assert svc.cancel(rid).get("ok")
        cancelled_revision = svc._runs[rid].revision
        release.set()
        svc._runs[rid].future.result(10)
        row = svc._store.load(rid)
        assert row.status == svc._runs[rid].status == "cancelled"
        assert row.resume_at is row.pause_reason is None
        assert row.revision >= cancelled_revision
        assert not svc._autoresume._timers
    finally:
        release.set()
        svc.shutdown()
        db.close()


@pytest.mark.parametrize("failure", ["storage", "takeover", "revision"])
def test_refused_first_write_cleans_resources_and_publishes_nothing(tmp_path, monkeypatch, failure):
    db = SessionDB(tmp_path / "state.db")
    other_db = SessionDB(tmp_path / "state.db")
    owner = RunStateStore(other_db, timer_factory=TimerFactory())
    calls, events, cores, ids = [], [], [], []
    svc = _service(db, tmp_path, lambda _: calls.append(True), timers=TimerFactory())
    svc._events.set_sink(lambda *args: events.append(args))
    write = db.run_state_write

    def reject(run_id, fields, now, **conditions):
        assert conditions["mode"] == "launch"
        state = svc._runs[run_id]
        cores.append(state.core)
        ids.append(run_id)
        if failure == "storage":
            raise sqlite3.OperationalError("synthetic disk failure")
        if failure == "takeover":
            assert db.release_run_lease(run_id, svc._store._holder, fence=state.fence)
            assert owner.acquire(run_id)
            assert owner.save(run_id=run_id, name="winner", status="paused")
        else:
            # The line did not exist when this launch captured revision zero.
            assert svc._store.save(run_id=run_id, name="winner", status="cancelled")
        return write(run_id, fields, now, **conditions)

    monkeypatch.setattr(db, "run_state_write", reject)
    try:
        reply = svc.start(SPEC, {})
        assert "error" in reply and "run_id" not in reply
        assert calls == events == [] and not svc._runs
        assert cores
        with pytest.raises(RuntimeError):
            cores[0]._pool.submit(lambda: None)
        if failure == "takeover":
            assert owner.lease_expiry(ids[0]) is not None  # stale cleanup cannot free winner
        else:
            assert svc._store.lease_expiry(ids[0]) is None
        if failure == "storage":
            assert "persist" in reply["error"] and "ownership" not in reply["error"]
        else:
            assert db.run_state_get(ids[0])["name"] == "winner"
    finally:
        for run_id in ids:
            owner.release(run_id)
        owner.shutdown()
        svc.shutdown()
        other_db.close()
        db.close()


def test_audit_marker_closure_survives_snapshot_but_new_launch_sets_its_segment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    svc, db, rid, _ = paused_service(tmp_path)
    entered, release = threading.Event(), threading.Event()
    save = svc._store.save_snapshot
    try:
        connection = sqlite3.connect(tmp_path / "state.db")
        old = svc._runs[rid]
        assert old.engine.segment_id
        # A snapshot can capture the active marker just before the independent
        # audit writer clears it; the audit UPDATE does not advance revision.
        old.audit_segment_id = old.engine.segment_id
        connection.execute("UPDATE workflow_run_state SET audit_segment_id = ? WHERE run_id = ?",
                         (old.audit_segment_id, rid))
        connection.commit()

        cleared = []

        def cleared_during_snapshot(snapshot, **conditions):
            if conditions["mode"] == "snapshot" and not cleared:
                cleared.append(True)
                connection.execute("UPDATE workflow_run_state SET audit_segment_id = NULL WHERE run_id = ?", (rid,))
                connection.commit()
            return save(snapshot, **conditions)

        monkeypatch.setattr(svc._store, "save_snapshot", cleared_during_snapshot)
        assert svc._persist_state(old)
        assert svc._store.load(rid).audit_segment_id is None

        run = svc._run

        def held_run(*args):
            entered.set()
            assert release.wait(10)
            run(*args)

        monkeypatch.setattr(svc, "_run", held_run)
        assert svc.resume(rid).get("status") == "started"
        assert entered.wait(10)
        new = svc._runs[rid]
        assert new.engine.segment_id != old.engine.segment_id
        assert svc._store.load(rid).audit_segment_id == new.engine.segment_id
        connection.close()
    finally:
        release.set()
        svc.shutdown()
        db.close()


def test_old_release_cannot_stop_same_store_successor_heartbeat(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    now, timers = [10.0], TimerFactory()
    store = RunStateStore(db, clock=lambda: now[0], ttl=9, timer_factory=timers)
    entered, release = threading.Event(), threading.Event()
    stop = store._heartbeat.stop
    results = []

    def held_stop(key):
        if threading.current_thread() is thread:
            entered.set()
            assert release.wait(10)
        stop(key)

    try:
        assert store.acquire("r")
        old_fence = store.fence_of("r")
        monkeypatch.setattr(store._heartbeat, "stop", held_stop)
        thread = threading.Thread(target=lambda: results.append(store.release("r", fence=old_fence)))
        thread.start()
        assert entered.wait(10)
        now[0] = 20.0
        assert store.acquire("r")
        current_fence = store.fence_of("r")
        assert current_fence > old_fence
        current_timer = timers.timers[-1]
        release.set()
        thread.join(10)
        assert not thread.is_alive() and results == [False]
        assert not current_timer.cancelled
        assert "r" in store._renewed
        now[0] = 24.0
        current_timer.fire()
        assert store.lease_expiry("r") == 33.0
        assert timers.timers[-1] is not current_timer and not timers.timers[-1].cancelled
    finally:
        release.set()
        store.shutdown()
        db.close()


def test_old_tick_cannot_renew_successor_or_deliver_its_lease_loss(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    now, timers, lost = [10.0], TimerFactory(), []
    store = RunStateStore(db, clock=lambda: now[0], ttl=9, timer_factory=timers,
                          on_lease_lost=lambda *args: lost.append(args))
    entered, release = threading.Event(), threading.Event()
    renew = db.renew_run_lease
    thread = None

    def held_renew(*args, **kwargs):
        if threading.current_thread() is thread:
            entered.set()
            assert release.wait(10)
        return renew(*args, **kwargs)

    monkeypatch.setattr(db, "renew_run_lease", held_renew)
    try:
        assert store.acquire("r")
        old_timer = timers.last
        thread = threading.Thread(target=old_timer.fire)
        thread.start()
        assert entered.wait(10)
        now[0] = 20.0
        assert store.acquire("r")
        current_timer, fence = timers.last, store.fence_of("r")
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert store.lease_expiry("r") == 29.0 and lost == []
        assert not current_timer.cancelled
        assert db.release_run_lease("r", store.holder, fence=fence)
        current_timer.fire()
        assert lost == [("r", fence)]  # current-generation loss remains observable
    finally:
        release.set()
        if thread is not None:
            thread.join(10)
        store.shutdown()
        db.close()


def test_legacy_line_without_revision_migrates_and_cancel_is_idempotent(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    svc.shutdown()
    db.close()
    with sqlite3.connect(tmp_path / "state.db") as connection:
        connection.execute("ALTER TABLE workflow_run_state DROP COLUMN revision")
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "unexpected", timers=TimerFactory())
    try:
        assert svc._store.load(rid).revision == 0
        assert svc.cancel(rid).get("ok")
        cancelled = db.run_state_get(rid)
        assert cancelled["revision"] == 1 and cancelled["status"] == "cancelled"
        assert svc.cancel(rid).get("ok")
        assert db.run_state_get(rid) == cancelled
    finally:
        svc.shutdown()
        db.close()


def test_late_engine_snapshots_preserve_an_ownerless_abort_fault(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    other = RunStateStore(db, timer_factory=TimerFactory())
    try:
        original_fault = other.load(rid).prior_faults[0]
        assert other.cancel_state(
            rid, extra_faults=["synthetic operator abort", original_fault, original_fault]
        ).accepted
        expected = Counter(other.load(rid).prior_faults)
        state = svc._runs[rid]
        assert not svc._persist_state(state)  # stale revision learns cancellation
        assert svc._persist_state(state)  # a later callback sees that revision
        assert Counter(other.load(rid).prior_faults) == expected
        assert svc._persist_state(state)  # repeated current snapshots do not multiply occurrences
        assert Counter(other.load(rid).prior_faults) == expected
    finally:
        other.shutdown()
        svc.shutdown()
        db.close()


def test_abandoned_launch_does_not_release_same_store_new_acquisition(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "unreachable", timers=TimerFactory())
    save = svc._save_state
    ids = []

    def take_over_then_raise(state, **kwargs):
        assert save(state, **kwargs).kind == "written"
        ids.append(state.run_id)
        assert svc._store.release(state.run_id, fence=state.fence)
        assert svc._store.acquire(state.run_id)
        assert svc._store.save(run_id=state.run_id, name="successor", status="paused")
        raise RuntimeError("synthetic failure after newer acquisition")

    monkeypatch.setattr(svc, "_save_state", take_over_then_raise)
    try:
        with pytest.raises(RuntimeError, match="synthetic failure"):
            svc.start(SPEC, {})
        rid = ids[0]
        assert not svc._runs
        assert svc._store.lease_expiry(rid) is not None
        assert rid in svc._store._renewed
        assert svc._store.load(rid).name == "successor"
    finally:
        for rid in ids:
            svc._store.release(rid)
        svc.shutdown()
        db.close()


def test_cache_callback_after_same_store_takeover_does_not_renew_successor(tmp_path, monkeypatch):
    svc, db, rid, _ = paused_service(tmp_path)
    cache = svc._runs[rid].engine._cache
    entered, release = threading.Event(), threading.Event()
    put = db.cache_put_with_cost
    now = [20.0]
    monkeypatch.setattr(svc._store, "_clock", lambda: now[0])

    def delay_callback(*args, **kwargs):
        accepted = put(*args, **kwargs)
        assert accepted
        entered.set()
        assert release.wait(10)
        return accepted

    monkeypatch.setattr(db, "cache_put_with_cost", delay_callback)
    thread = threading.Thread(target=lambda: cache.put_complete("synthetic-hash", "a", "kept"))
    try:
        thread.start()
        assert entered.wait(10)
        assert svc._store.acquire(rid)
        expiry = svc._store.lease_expiry(rid)
        now[0] = 400.0  # outside the renewal throttle, still within the new TTL
        release.set()
        thread.join(10)
        assert not thread.is_alive()
        assert svc._store.lease_expiry(rid) == expiry
        assert svc._store._renewed[rid] == 20.0
    finally:
        release.set()
        thread.join(10)
        svc._store.release(rid)
        svc.shutdown()
        db.close()
