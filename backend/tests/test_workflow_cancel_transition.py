"""#126 functional transitions. Synthetic engine/clients; real Service/SQLite."""

import os
import sqlite3
import threading

import pytest

from lohra.state import SessionDB
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.runstate_store import RunStateStore
from tests.test_workflow_operability import _service
from tests.test_workflow_quota import TimerFactory, _rate_limited


SPEC = {
    "meta": {"name": "cancel-extra-synthetic"},
    "nodes": [{"id": "a", "type": "agent", "prompt": "synthetic"}],
}


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    keep = {"HOME", "CODEX_HOME", "PATH", "TMPDIR", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
    for name in tuple(os.environ):
        if name not in keep:
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.setenv("LOHRA_AUDIT", "off")
    monkeypatch.chdir(tmp_path)


def test_cancel_winner_survives_engine_exception(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def fail(_engine, _spec, _args):
        entered.set()
        assert release.wait(10)
        raise RuntimeError("synthetic engine failure after cancellation")

    monkeypatch.setattr(WorkflowEngine, "run", fail)
    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, lambda _: "unreachable", timers=TimerFactory())
    try:
        rid = svc.start(SPEC, {})["run_id"]
        assert entered.wait(10)
        assert svc.cancel(rid)["ok"] is True
        assert svc._store.load(rid).status == "cancelled"
        release.set()
        svc._runs[rid].future.result(10)
        assert svc._runs[rid].status == "cancelled"
        assert svc._store.load(rid).status == "cancelled"
    finally:
        release.set()
        svc.shutdown()
        db.close()


def paused_service(tmp_path):
    calls = []

    def answer(_):
        calls.append(True)
        if len(calls) == 1:
            raise _rate_limited("30")
        return "ok"

    db = SessionDB(tmp_path / "state.db")
    svc = _service(db, tmp_path, answer, timers=TimerFactory())
    rid = svc.start(SPEC, {})["run_id"]
    svc._runs[rid].future.result(10)
    assert svc._store.load(rid).status == "paused"
    return svc, db, rid, calls


def test_live_cancel_storage_error_cannot_acknowledge_success(tmp_path, monkeypatch):
    svc, db, rid, _ = paused_service(tmp_path)
    def fail_cancel(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic disk failure")

    monkeypatch.setattr(db, "run_state_cancel", fail_cancel)
    try:
        reply = svc.cancel(rid)
        assert svc._store.load(rid).status == "paused"
        assert "error" in reply and not reply.get("ok")
    finally:
        svc.shutdown()
        db.close()


def test_cancel_transaction_rolls_back_when_storage_fails_after_the_write(tmp_path, monkeypatch):
    from lohra.state import runstate

    svc, db, rid, _ = paused_service(tmp_path)
    before = db.run_state_get(rid)
    put = runstate._put

    def fail_after_write(*args):
        put(*args)
        raise sqlite3.OperationalError("synthetic commit-path failure")

    monkeypatch.setattr(runstate, "_put", fail_after_write)
    try:
        assert "error" in svc.cancel(rid)
        assert db.run_state_get(rid) == before
        assert svc._runs[rid].status == "paused"
    finally:
        svc.shutdown()
        db.close()


def test_functional_cancel_does_not_revoke_original_financial_fence(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    try:
        fence = svc._runs[rid].fence
        assert svc.cancel(rid).get("ok")
        assert svc._store.lease_expiry(rid) is None
        assert db.run_spend_put(rid, None, 7, 4, fence=fence)
        assert svc._store.load(rid).status == "cancelled"
        assert svc._store.acquire(rid)
        assert not db.run_spend_put(rid, None, 999, 999, fence=fence)
        assert db.run_spend_get(rid)["tokens_in"] == 7
        assert svc._store.load(rid).status == "cancelled"
    finally:
        svc.shutdown()
        db.close()


def test_a_stale_abort_remedy_cannot_append_a_fault_after_plain_cancel(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    try:
        prior = svc._store.load(rid)
        assert svc.cancel(rid).get("ok")
        before = db.run_state_get(rid)
        faults = list(svc._runs[rid].prior_faults)
        reply = svc._abort_route_fault(rid, prior, "a")
        assert "error" in reply and "status" not in reply
        assert db.run_state_get(rid) == before
        assert svc._runs[rid].prior_faults == faults
    finally:
        svc.shutdown()
        db.close()


def test_stale_local_cancel_does_not_claim_successor_cancelled(tmp_path):
    svc, db, rid, _ = paused_service(tmp_path)
    other_db = SessionDB(tmp_path / "state.db")
    owner = RunStateStore(other_db, timer_factory=TimerFactory())
    try:
        old_fence = svc._runs[rid].fence
        assert owner.acquire(rid)
        assert owner.fence_of(rid) > old_fence
        assert owner.save(run_id=rid, name="successor", status="running", attempts=1)
        reply = svc.cancel(rid)
        row = owner.load(rid)
        assert (row.name, row.status, row.attempts) == ("successor", "running", 1)
        assert "error" in reply and not reply.get("ok")
    finally:
        owner.release(rid)
        owner.shutdown()
        svc.shutdown()
        other_db.close()
        db.close()


@pytest.mark.parametrize("explicit_replay", [False, True])
def test_cold_resume_acquisition_gap_vs_explicit_replay(tmp_path, monkeypatch, explicit_replay):
    original, db, rid, _ = paused_service(tmp_path)
    next_db = SessionDB(tmp_path / "state.db")
    calls = []
    next_svc = _service(
        next_db, tmp_path, lambda _: (calls.append(True), "ok")[1], timers=TimerFactory()
    )
    method = "acquire" if explicit_replay else "acquire_paused"
    acquire = getattr(next_svc._store, method)

    def cancel_before_acquiring(run_id, *args):
        # A fresh service already validated authoritative paused state. Cancel
        # commits in the remaining gap before actual ownership is acquired.
        assert original.cancel(run_id)["ok"] is True
        assert next_svc._store.load(run_id).status == "cancelled"
        return acquire(run_id, *args)

    monkeypatch.setattr(next_svc._store, method, cancel_before_acquiring)
    try:
        reply = (
            next_svc.start(resume_run_id=rid)
            if explicit_replay
            else next_svc.resume(rid)
        )
        if "error" not in reply:
            next_svc._runs[rid].future.result(10)
        if explicit_replay:
            assert reply.get("status") == "started"
            assert next_svc._store.load(rid).status == "complete"
            assert calls == [True]
        else:
            assert "error" in reply
            assert next_svc._store.load(rid).status == "cancelled"
            assert calls == []
    finally:
        next_svc.shutdown()
        original.shutdown()
        next_db.close()
        db.close()
