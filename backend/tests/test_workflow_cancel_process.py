"""Cancellation arbitration across actual spawned processes, with synthetic leaves."""

import multiprocessing
from dataclasses import asdict
from pathlib import Path

import pytest

from lohra.state import SessionDB
from tests.test_workflow_cancel_transition import SPEC, isolated_environment  # noqa: F401
from tests.test_workflow_operability import _service
from tests.test_workflow_quota import TimerFactory, _rate_limited


def _cancel_in_process(path, run_id, pipe, hold):
    db = SessionDB(Path(path) / "state.db")
    svc = _service(db, Path(path), lambda _: "unexpected", timers=TimerFactory())
    original = svc._store.cancel_state

    def gated_cancel(*args, **kwargs):
        if hold:
            pipe.send("before_decision")
            assert pipe.poll(15)
            assert pipe.recv() == "decide"
        return original(*args, **kwargs)

    svc._store.cancel_state = gated_cancel
    try:
        reply = svc.cancel(run_id)
        pipe.send((reply, asdict(svc._store.load(run_id))))
    finally:
        svc.shutdown()
        db.close()
        pipe.close()


def _receive(pipe):
    assert pipe.poll(15), "canceller process did not report"
    return pipe.recv()


@pytest.mark.parametrize("winner", ["complete", "paused", "cancel_first"])
def test_process_cancel_uses_current_state_and_old_owner_cannot_resurrect(tmp_path, winner):
    db = SessionDB(tmp_path / "state.db")
    calls = []

    def answer(_):
        calls.append(True)
        if len(calls) == 1 or winner == "paused":
            raise _rate_limited("30")
        return "ok"

    svc = _service(db, tmp_path, answer, timers=TimerFactory())
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe()
    process = None
    try:
        rid = svc.start(SPEC, {"input": "kept"}, owner="synthetic-owner", tainted=True)["run_id"]
        svc._runs[rid].future.result(15)
        assert svc._store.load(rid).status == "paused"
        process = ctx.Process(
            target=_cancel_in_process, args=(str(tmp_path), rid, child, winner != "cancel_first")
        )
        process.start()
        child.close()
        first = _receive(parent)
        if winner == "cancel_first":
            reply, cancelled = first
            assert reply.get("ok") and cancelled["status"] == "cancelled"
            # The same old service and a new process-local service must agree.
            assert "error" in svc.resume(rid)
            fresh_db = SessionDB(tmp_path / "state.db")
            fresh = _service(fresh_db, tmp_path, lambda _: "unexpected", timers=TimerFactory())
            try:
                assert "error" in fresh.resume(rid)
                assert asdict(fresh._store.load(rid)) == cancelled
            finally:
                fresh.shutdown()
                fresh_db.close()
            assert len(calls) == 1
        else:
            assert first == "before_decision"
            assert svc.resume(rid).get("status") == "started"
            svc._runs[rid].future.result(15)
            before = asdict(svc._store.load(rid))
            assert (before["status"], before["attempts"]) == (winner, 1)
            parent.send("decide")
            reply, after = _receive(parent)
            if winner == "complete":
                assert "error" in reply and after == before
            else:
                assert reply.get("ok") and after["status"] == "cancelled"
                changed = {"status", "pause_reason", "checkpoint", "route_fault", "resume_at", "updated_at", "revision"}
                assert {k: v for k, v in after.items() if k not in changed} == {
                    k: v for k, v in before.items() if k not in changed
                }
                assert after["pause_reason"] is after["resume_at"] is None
            assert len(calls) == 2
        process.join(15)
        assert process.exitcode == 0
        reopened = SessionDB(tmp_path / "state.db")
        try:
            assert reopened.run_state_get(rid)["status"] == (
                "complete" if winner == "complete" else "cancelled"
            )
        finally:
            reopened.close()
    finally:
        if process is not None and process.is_alive():
            parent.send("decide")
            process.join(15)
            if process.is_alive():
                process.terminate()
                process.join(5)
        child.close()
        parent.close()
        svc.shutdown()
        db.close()
