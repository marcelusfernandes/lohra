"""A real executor can queue a callable and still raise from submit (#69)."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Thread

import pytest

from tests.test_orchestration_steer_races import ready


@pytest.mark.parametrize("retry_before_drain", [False, True])
def test_partially_submitted_steer_never_executes(monkeypatch, retry_before_drain):
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        sub = core._children[sid]
        # Begin with a real executor whose ONLY worker has never finished a
        # task. No idle-worker timing/semaphore assumptions are needed: steer
        # must try to create worker two after putting its work on the queue.
        core._pool.shutdown(wait=True)
        core._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="orch")
        entered, release = Event(), Event()

        def block():
            entered.set()
            assert release.wait(5)

        blocked = core._pool.submit(block)
        assert entered.wait(5)
        sub.causal_history = [{"turn": i} for i in range(64)]
        sub.causal_context = sub.causal_history[-1]
        sub.causal_history_dropped = 7

        def snapshot():
            return (
                sub.future, sub.status, sub.done_fired, sub.on_done,
                sub.accepting_steer, deepcopy(core.causal_snapshot(sid)),
                core.collect(sid),
            )

        before = snapshot()
        failed_starts, observed = [], []
        core._event_sink = lambda _sid, ctx, _frame: observed.append(ctx)
        original_start = Thread.start

        def fail_second_worker(thread, *args, **kwargs):
            if thread.name.startswith("orch"):
                failed_starts.append(thread.name)
                raise RuntimeError("synthetic thread creation failure")
            return original_start(thread, *args, **kwargs)

        try:
            with monkeypatch.context() as patch:
                patch.setattr(Thread, "start", fail_second_worker)
                answer = core.steer(sid, "must not execute", causal_context={"turn": 64})
            assert failed_starts and "error" in answer
            assert snapshot() == before
            assert len(client.calls) == 1
            if retry_before_drain:
                # A distinct accepted submission cannot grant the earlier
                # rejected one permission merely by reopening accepting_steer.
                assert core.steer(sid, "accepted retry", causal_context={"turn": 65}) == {
                    "ok": True, "queued": False,
                }
                assert core.collect(sid, wait=True, timeout=5)["status"] == "complete"
        finally:
            release.set()
            blocked.result(5)
            # The public shutdown drains even the callable whose submit raised.
            # No private queue inspection/removal and no negative timing wait.
            core._pool.shutdown(wait=True)

        assert len(client.calls) == (2 if retry_before_drain else 1)
        assert core._active == 0 and sub.session.drain_steers() == []
        if retry_before_drain:
            assert observed and all(ctx == {"turn": 65} for ctx in observed)
            assert client.calls[-1]["messages"][-1]["content"] == "accepted retry"
            assert core.collect(sid)["tokens_in"] == 2
            assert sub.causal_history_dropped == 8
        else:
            assert snapshot() == before and observed == []
