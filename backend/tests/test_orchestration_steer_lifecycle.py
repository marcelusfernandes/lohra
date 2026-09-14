"""Steer settlement composes with reentrancy, teardown and busy handoff."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from tests.test_orchestration_core import _gated_core
from tests.test_orchestration_steer_races import epilogue, ready
from lohra.state import SessionDB


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
@pytest.mark.parametrize("discarded", [False, True])
def test_settle_callback_can_stop_its_own_series(operation, discarded):
    with epilogue() as (core, sid, client, release):
        observed, fired = [], []
        assert core.watch_done(sid, fired.append)
        before = len(client.calls)

        def callback(outcome):
            free = core._lock.acquire(blocking=False)
            if not free:
                observed.append((outcome, "locked"))
                return
            core._lock.release()
            result = core.cancel(sid) if operation == "cancel" else core.shutdown(wait=False)
            observed.append((outcome, result))

        assert core.steer_active(sid, "A", on_settle=callback)["ok"]
        if discarded:
            core.cancel(sid)
        release.set()
        result = core.collect(sid, wait=True, timeout=5)
        assert len(observed) == 1 and observed[0][1] != "locked"
        assert observed[0][0] == ("discarded" if discarded else "read")
        assert len(client.calls) == before
        assert result["tokens_in"] == before and result["tokens_out"] == before
        assert result["status"] != "running" and core._children[sid].landed
        assert not core._children[sid].accepting_steer
        assert core._children[sid].session.drain_steers() == []
        assert fired == [sid]


def test_blocked_callback_does_not_hold_another_subsession_hostage():
    with epilogue() as (core, sid, client, release):
        entered, done = Event(), Event()

        def callback(_):
            entered.set()
            done.wait(5)

        assert core.steer_active(sid, "A", on_settle=callback)["ok"]
        release.set()
        assert entered.wait(5)
        with ThreadPoolExecutor(max_workers=1) as other:

            def independent():
                child = core.spawn("independent")
                return core.collect(child, wait=True, timeout=5)

            future = other.submit(independent)
            try:
                assert future.result(2)["status"] == "complete"
            finally:
                done.set()
        assert core.collect(sid, wait=True, timeout=5)["status"] == "complete"


@pytest.mark.parametrize("cancelled", [False, True])
def test_busy_handoff_keeps_winners_hook_and_never_restarts_cancelled_work(cancelled):
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        entered, release = Event(), Event()
        original = client.create

        def blocked(**kwargs):
            client.create = original
            entered.set()
            assert release.wait(5)
            return original(**kwargs)

        client.create = blocked
        try:
            assert core.steer(sid, "winner")["ok"]
            assert entered.wait(5)
            fired = []
            assert core.watch_done(sid, fired.append)
            # Exercise the defensive busy seam with a second worker; it loses
            # GatewaySession's real busy lock and hands its text to the winner.
            with ThreadPoolExecutor(max_workers=1) as other:
                other.submit(core._run, sid, "handoff").result(5)
            assert fired == []
            if cancelled:
                core.cancel(sid)
        finally:
            release.set()
        core.collect(sid, wait=True, timeout=5)
        assert fired == [sid]
        assert len(client.calls) == (2 if cancelled else 3)
        if not cancelled:
            assert client.calls[-1]["messages"][-1]["content"] == "handoff"
        assert core._children[sid].session.drain_steers() == []


def test_submit_error_discards_once_outside_locks_before_done():
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        entered, release = Event(), Event()
        session = core._children[sid].session

        def broken(*_):
            entered.set()
            assert release.wait(5)
            raise RuntimeError("synthetic persistence failure")

        session.submit = broken
        outcomes = []

        def callback(outcome):
            outcomes.append((outcome, core._lock.locked(), session._inbox_lock.locked()))

        try:
            assert core.steer(sid, "raises")["ok"]
            assert entered.wait(5)
            assert core.steer_active(sid, "discard", on_settle=callback)["ok"]
            assert core.watch_done(sid, lambda _: outcomes.append("done"))
        finally:
            release.set()
        result = core.collect(sid, wait=True, timeout=5)
        assert result["status"] == "error"
        assert outcomes == [("discarded", False, False), "done"]
        assert len(client.calls) == 1 and core._children[sid].landed
        assert "error" in core.steer_active(sid, "late")


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
def test_queued_teardown_settles_outside_locks_then_fires_once(operation):
    db = SessionDB(":memory:")
    gate, started = Event(), Event()
    core = _gated_core(db, gate, started, max_concurrent=1)
    observed = []
    try:
        core.spawn("occupies worker")
        assert started.wait(5)
        sid = core.spawn("queued", on_done=lambda _: observed.append("done"))
        session = core._children[sid].session

        def callback(outcome):
            observed.append((outcome, core._lock.locked(), session._inbox_lock.locked()))

        assert core.steer_active(sid, "pending", on_settle=callback)["ok"]
        if operation == "cancel":
            core.cancel(sid)
        else:
            core.shutdown(wait=False)
        assert core.collect(sid)["status"] == "cancelled"
        assert observed == [("discarded", False, False), "done"]
        core.cancel(sid)
        assert observed == [("discarded", False, False), "done"]
    finally:
        gate.set()
        core.shutdown()
        db.close()
