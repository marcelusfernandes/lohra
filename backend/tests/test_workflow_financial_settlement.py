"""Actual Service/Core/SQLite final commit, failure and acquisition fencing."""

from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from lohra.workflow import library, quiescence
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.events import DONE
from lohra.workflow.financial_settlement import SpendSnapshot
from lohra.workflow.runstate_store import RunStateStore
from lohra.state import SessionDB
from tests.pipeline_deadlines import control_pipeline_deadlines
from tests.test_workflow_pipeline_accounting import _cells, _service, _spec
from tests.test_workflow_post_drain_accounting import VECTOR, meter
from tests.test_workflow_post_drain_accounting import five_meter_reply as five_meter_reply


@pytest.fixture
def late_run(tmp_path, monkeypatch, five_meter_reply):
    started, release, expire, sealed = (Event() for _ in range(4))
    control_pipeline_deadlines(monkeypatch, {"a": expire})
    monkeypatch.setattr(quiescence, "CANCEL_QUIESCENCE_TIMEOUT", 0.02)
    seal = WorkflowEngine._seal

    def observed_seal(engine, result):
        seal(engine, result)
        sealed.set()

    def respond(prompt):
        started.set()
        assert release.wait(5)
        return "LATE"

    monkeypatch.setattr(WorkflowEngine, "_seal", observed_seal)
    records = []
    monkeypatch.setattr(library, "record_outcome", lambda *args, **kwargs: records.append(kwargs))
    db = SessionDB(tmp_path / "state.db")
    service = _service(db, tmp_path, respond)
    try:
        run_id = service.start(_spec(tail=False), token_budget=10)["run_id"]
        state = service._runs[run_id]
        assert started.wait(5)
        expire.set()
        assert sealed.wait(5)
        yield SimpleNamespace(db=db, service=service, run_id=run_id, state=state,
                              release=release, before=deepcopy(state.engine._result), records=records)
    finally:
        expire.set()
        release.set()
        service.shutdown()
        db.close()


@pytest.mark.parametrize("mode", ["clean", "transient", "ambiguous", "write_failure", "capture", "finalize"])
def test_final_write_is_complete_after_drain_and_failure_keeps_cleanup(late_run, monkeypatch, caplog, mode):
    run = late_run
    write = SpendSnapshot.write
    records, events, writes, order, notices = run.records, [], [], [], []
    close, release = run.service._close_audit_segment, run.service._store.release
    monkeypatch.setattr(run.service._events, "emit", lambda *args: events.append(args))
    monkeypatch.setattr(run.service, "_on_run_done", lambda *args: notices.append(args))

    def final_write(snapshot, db, run_id, fence):
        assert run.state.core._pool._shutdown
        assert all(s.future.done() for s in run.state.core._children.values())
        assert run.service._store.load(run_id).status == run.before.status
        assert not run.state.future.done()
        assert run.service._store.lease_expiry(run_id) is not None
        assert "close" not in order and "release" not in order
        writes.append((snapshot, fence))
        if mode == "write_failure" or (mode == "transient" and len(writes) == 1):
            raise OSError("final write unavailable")
        accepted = write(snapshot, db, run_id, fence)
        if mode == "ambiguous" and len(writes) == 1:
            raise OSError("response lost after commit")
        return accepted

    def closed(*args):
        order.append("close")
        return close(*args)

    def released(*args, **kwargs):
        order.append("release")
        return release(*args, **kwargs)

    def fail(*args):
        raise RuntimeError(f"{mode} unavailable")

    monkeypatch.setattr(SpendSnapshot, "write", final_write)
    monkeypatch.setattr(run.service, "_close_audit_segment", closed)
    monkeypatch.setattr(run.service._store, "release", released)
    if mode in {"capture", "finalize"}:
        monkeypatch.setattr(run.state.usage_ledger, "receive" if mode == "capture" else "finalize", fail)
    run.release.set()
    run.state.future.result(timeout=5)
    succeeded = mode in {"clean", "transient", "ambiguous"}
    assert meter(run.db, run.run_id) == (VECTOR if succeeded else (0, 0, 0, 0, 0))
    assert order == ["close", "release"]
    assert run.service._store.lease_expiry(run.run_id) is None
    assert run.state.result == run.before
    assert _cells(run.db) == []
    if len(writes) > 1:
        assert writes[0] == writes[1] and writes[0][0] is writes[1][0]
    assert all(fence == run.state.fence for _, fence in writes)
    done = [args[2] for args in events if args[1] == DONE]
    assert len(done) == 1
    if succeeded:
        assert records[-1]["tokens_total"] == 24 and records[-1]["budget_overrun"] == 14
        assert done[0]["tokens"] == 24
        assert len(notices) == 1 and notices[0][-1].endswith("spent 24 tokens")
        assert run.state.financial_committed is True
    else:
        assert records == [] and notices == [] and run.state.error
        assert done[0]["error"] == run.state.error
    financial = run.service.status(run.run_id)["financial"]
    assert financial["committed"] == (succeeded or mode == "capture")
    assert financial["capture_complete"] == (mode not in {"capture", "finalize"})
    if mode != "clean":
        assert caplog.records


@pytest.mark.parametrize("same_holder", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_final_financial_retry_never_borrows_successor_fence(late_run, monkeypatch, same_holder, ambiguous):
    run = late_run
    write, records, calls = SpendSnapshot.write, run.records, []
    after_expiry = run.service._store.lease_expiry(run.run_id) + 1
    newer = RunStateStore(run.db, holder=run.service._store.holder if same_holder else "new-owner",
                         clock=lambda: after_expiry)
    successor = {}

    def take_over(snapshot, db, run_id, fence):
        calls.append(fence)
        if not successor:
            if ambiguous:
                assert write(snapshot, db, run_id, fence)
            assert newer.acquire(run_id)
            live = newer.fence_of(run_id)
            assert live > fence
            newer.save(run_id=run_id, name="successor", status="running")
            assert db.run_spend_put(run_id, 1000, 100, 200, cache_read=300,
                                    cache_write=400, reasoning=50, fence=live)
            successor.update(row=db.run_spend_get(run_id), state=db.run_state_get(run_id),
                             lease=newer.lease_expiry(run_id))
            if ambiguous:
                raise OSError("commit response lost during takeover")
        return write(snapshot, db, run_id, fence)

    monkeypatch.setattr(SpendSnapshot, "write", take_over)
    run.release.set()
    run.state.future.result(timeout=5)
    assert calls == [run.state.fence] * (2 if ambiguous else 1)
    assert run.db.run_spend_get(run.run_id) == successor["row"]
    assert run.db.run_state_get(run.run_id) == successor["state"]
    assert newer.lease_expiry(run.run_id) == successor["lease"]
    assert records == [] and run.state.financial_committed is False
    assert "fence changed" in run.state.error
    assert run.service.status(run.run_id)["tokens_spent_total"] == 24  # this acquisition only


def test_delayed_publication_keeps_own_frozen_numbers_and_rechecks_authority(late_run, monkeypatch):
    run = late_run
    arrived, proceed, captured, records = Event(), Event(), [], run.records
    publish = run.service._publish_outcome

    def held_publication(state, record):
        captured.append(record)
        arrived.set()
        assert proceed.wait(5)
        return publish(state, record)

    monkeypatch.setattr(run.service, "_publish_outcome", held_publication)
    try:
        run.release.set()
        assert arrived.wait(5)
        assert run.service._store.lease_expiry(run.run_id) is None
        assert captured[0].keywords["tokens_total"] == 24
        assert captured[0].keywords["budget_overrun"] == 14
        newer = RunStateStore(run.db, holder="successor")
        assert newer.acquire(run.run_id)
        newer.save(run_id=run.run_id, name="successor", status="running")
        assert run.db.run_spend_put(run.run_id, 1000, 100, 200, fence=newer.fence_of(run.run_id))
        proceed.set()
        run.state.future.result(timeout=5)
        assert records == []
        assert captured[0].keywords["tokens_total"] == 24
        assert run.service.status(run.run_id)["tokens_spent_total"] == 24
        assert run.db.run_spend_get(run.run_id)["tokens_in"] == 100
        assert newer.lease_expiry(run.run_id) is not None
    finally:
        proceed.set()
