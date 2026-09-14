"""#69: linearized steer acceptance and lock-free settlement; no providers/shell."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from threading import Event

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB


class Client(ModelClient):
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "content": [{"type": "text", "text": str(len(self.calls))}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    def stream(self, *, on_text=None, on_reasoning=None, abort_check=None, **kwargs):
        return self.create(**kwargs)

    def close(self):
        pass


@contextmanager
def ready(*, on_done=None):
    db = SessionDB(":memory:")
    client = Client()
    core = OrchestrationCore(
        db,
        lambda: Agent(model="synthetic", provider=get_provider_profile("anthropic"), client=client),
    )
    try:
        sid = core.spawn("initial", causal_context={"turn": 1}, on_done=on_done)
        yield core, sid, client
    finally:
        core.shutdown()
        db.close()


def test_on_done_cannot_accept_an_orphan():
    entered, release = Event(), Event()
    observed = []

    # A held callback makes the future demonstrably pending after the final loop.
    def callback(sid):
        entered.set()
        assert release.wait(5)
        observed.append(core.steer(sid, "orphan"))

    with ready(on_done=callback) as (core, sid, client):
        try:
            assert entered.wait(5)
            assert not core._children[sid].future.done()
            assert core.collect(sid)["status"] == "complete"
        finally:
            release.set()
        core.collect(sid, wait=True, timeout=5)
        assert "error" in observed[0]
        assert core._children[sid].session.drain_steers() == []
        assert len(client.calls) == 1


def test_real_closed_executor_preserves_every_published_field():
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        sub = core._children[sid]
        # A full retained history must not lose its oldest entry on refusal.
        sub.causal_history = [{"turn": i} for i in range(64)]
        sub.causal_context = sub.causal_history[-1]
        sub.causal_history_dropped = 7
        before = (
            sub.future,
            sub.status,
            sub.done_fired,
            sub.accepting_steer,
            deepcopy(core.causal_snapshot(sid)),
        )
        core._pool.shutdown(wait=True)  # actual executor refusal, not mocked submit
        try:
            answer = core.steer(sid, "refused", causal_context={"turn": 2})
        except RuntimeError as exc:
            answer = {"raised": str(exc)}
        after = (
            sub.future,
            sub.status,
            sub.done_fired,
            sub.accepting_steer,
            deepcopy(core.causal_snapshot(sid)),
        )
        assert "error" in answer, (answer, before, after)
        assert after == before
        assert "error" in core.steer_active(sid, "must remain refused")
        assert sub.session.drain_steers() == []
        assert len(client.calls) == 1


@contextmanager
def epilogue():
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        entered, release = Event(), Event()
        session = core._children[sid].session
        original = session.submit

        def pause_after_submit(text, emit):
            result = original(text, emit)
            # Once: continuation turns should not pause again.
            session.submit = original
            entered.set()
            assert release.wait(5)
            return result

        session.submit = pause_after_submit
        assert core.steer(sid, "second") == {"ok": True, "queued": False}
        try:
            assert entered.wait(5)
            assert session.busy is False
            yield core, sid, client, release
        finally:
            release.set()


@pytest.mark.parametrize("cancelled", [False, True])
def test_epilogue_settles_read_and_discard_outside_both_locks(cancelled):
    with epilogue() as (core, sid, client, release):
        observations = []
        session = core._children[sid].session

        def settled(outcome):
            available = core._lock.acquire(blocking=False)
            if available:
                core._lock.release()
                snapshot = core.causal_snapshot(sid)  # actual reentrant public read
            else:
                snapshot = None
            observations.append((outcome, available, session._inbox_lock.locked(), snapshot))

        assert core.steer_active(sid, "A", on_settle=settled)["ok"]
        assert core.steer_active(sid, "B", on_settle=settled)["ok"]
        if cancelled:
            core.cancel(sid)
        release.set()
        core.collect(sid, wait=True, timeout=5)
        expected = "discarded" if cancelled else "read"
        assert len(observations) == 2
        assert all(row[0:3] == (expected, True, False) for row in observations), observations
        assert session.drain_steers() == []
        assert len(client.calls) == (2 if cancelled else 3)
        if not cancelled:
            assert client.calls[-1]["messages"][-1]["content"] == "A\nB"


def test_reentrant_callback_can_enqueue_after_the_accepted_batch():
    with epilogue() as (core, sid, client, release):
        observed = []

        def settled(outcome):
            available = core._lock.acquire(blocking=False)
            if not available:
                observed.append({"lock_held": True})
                return
            core._lock.release()
            observed.append(core.steer_active(sid, "B"))

        assert core.steer_active(sid, "A", on_settle=settled)["ok"]
        release.set()
        core.collect(sid, wait=True, timeout=5)
        assert observed == [{"ok": True, "queued": True}]
        messages = client.calls[-1]["messages"]
        assert messages[-2]["content"] == "A"
        assert "B" in str(messages[-1]["content"])
        assert core._children[sid].session.drain_steers() == []


@pytest.mark.parametrize("operation", ["cancel", "shutdown"])
def test_cancel_or_shutdown_during_blocked_settlement_prevents_new_turn(operation):
    with epilogue() as (core, sid, client, release):
        callback_entered, callback_release, stopped = Event(), Event(), Event()

        def settled(outcome):
            callback_entered.set()
            assert callback_release.wait(5)

        assert core.steer_active(sid, "A", on_settle=settled)["ok"]
        release.set()
        assert callback_entered.wait(5)

        def stop():
            if operation == "cancel":
                core.cancel(sid)
            else:
                core.shutdown(wait=False)
            stopped.set()

        with ThreadPoolExecutor(max_workers=1) as other:
            stopping = other.submit(stop)
            try:
                assert stopped.wait(1), "Core operation blocked behind settlement callback"
            finally:
                callback_release.set()
            stopping.result(5)
        core.collect(sid, wait=True, timeout=5)
        assert len(client.calls) == 2, "a followup request started after cancellation"
        assert core._children[sid].accepting_steer is False
        assert core._children[sid].session.drain_steers() == []


def test_successful_submit_keeps_worker_behind_publication_lock():
    with ready() as (core, sid, client):
        core.collect(sid, wait=True, timeout=5)
        original_run, original_submit = core._run, core._pool.submit
        worker_entered = Event()
        at_entry, contexts = [], []

        def run(*args):
            at_entry.append(core._lock.locked())
            worker_entered.set()
            return original_run(*args)

        def submit(*args, **kwargs):
            result = original_submit(*args, **kwargs)
            assert worker_entered.wait(5)  # worker scheduled before submit returns
            return result

        core._run, core._pool.submit = run, submit
        core._event_sink = lambda _sid, ctx, _frame: contexts.append(ctx)
        assert core.steer(sid, "new", causal_context={"turn": 2}) == {"ok": True, "queued": False}
        core.collect(sid, wait=True, timeout=5)
        assert at_entry == [True]
        assert contexts and all(ctx == {"turn": 2} for ctx in contexts)
        assert core.causal_snapshot(sid)["causal_history"] == ({"turn": 1}, {"turn": 2})


def test_accepted_batch_delivers_once_in_order_without_reentrant_callback():
    with epilogue() as (core, sid, client, release):
        outcomes = []
        assert core.steer_active(sid, "A", on_settle=lambda x: outcomes.append(("A", x)))["ok"]
        assert core.steer_active(sid, "B", on_settle=lambda x: outcomes.append(("B", x)))["ok"]
        release.set()
        core.collect(sid, wait=True, timeout=5)
        assert outcomes == [("A", "read"), ("B", "read")]
        assert len(client.calls) == 3
        assert client.calls[-1]["messages"][-1]["content"] == "A\nB"
        assert core._children[sid].session.drain_steers() == []


def test_eviction_between_lookup_and_decision_cannot_accept_dead_target():
    from threading import current_thread

    db = SessionDB(":memory:")
    clients = []

    def factory():
        client = Client()
        clients.append(client)
        return Agent(model="synthetic", provider=get_provider_profile("anthropic"), client=client)

    core = OrchestrationCore(db, factory, max_children=1)
    first_hold_ended, release = Event(), Event()
    underlying = core._lock

    class GateLock:
        def __enter__(self):
            underlying.acquire()
            return self

        def __exit__(self, *_):
            underlying.release()
            if current_thread().name.startswith("steerer") and not first_hold_ended.is_set():
                first_hold_ended.set()
                assert release.wait(5)

        def acquire(self, *args, **kwargs):
            return underlying.acquire(*args, **kwargs)

        def release(self):
            return underlying.release()

        def locked(self):
            return underlying.locked()

    core._lock = GateLock()
    try:
        sid = core.spawn("old")
        core.collect(sid, wait=True, timeout=5)
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="steerer") as caller:
            future = caller.submit(core.steer, sid, "too late")
            try:
                assert first_hold_ended.wait(5)
                replacement = core.spawn("replacement")
                core.collect(replacement, wait=True, timeout=5)
            finally:
                release.set()
            outcome = future.result(5)
        core.shutdown()
        assert "error" in outcome or len(clients[0].calls) == 2, (outcome, len(clients[0].calls))
    finally:
        release.set()
        core.shutdown()
        db.close()
