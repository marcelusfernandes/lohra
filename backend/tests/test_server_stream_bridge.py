"""Request-local cancellation, bounded Unicode delivery and terminal snapshots."""

import asyncio
import dataclasses
import threading

import pytest

from lohra.server.cancellation import RequestCancellation
from lohra.server.service import CompletionInterrupted
from lohra.server.stream_bridge import StreamBridge, StreamLimits


@pytest.mark.parametrize("field,value", [
    ("pieces", 0), ("pieces", True), ("pieces", 1.5),
    ("bytes", 3), ("bytes", False), ("workers", 0), ("workers", 1.5),
    ("drain_seconds", -1), ("drain_seconds", float("nan")),
    ("shutdown_seconds", float("inf")), ("shutdown_seconds", True),
])
def test_stream_limits_reject_impossible_or_unbounded_values(field, value):
    with pytest.raises(ValueError):
        StreamLimits(**{field: value})


@pytest.mark.parametrize("cancel_first", [False, True])
def test_binding_is_sticky_idempotent_and_callback_runs_outside_lock(cancel_first):
    cancellation = RequestCancellation()
    observations, callback_states, readers = [], [], []

    def callback():
        # A different thread must acquire the actual controller lock.
        reader = threading.Thread(target=lambda: observations.append(cancellation.cancelled))
        readers.append(reader)
        reader.start()
        reader.join(1)
        callback_states.append(not reader.is_alive())

    try:
        if cancel_first:
            cancellation.cancel()
        token = cancellation.bind(callback)
        cancellation.cancel()
        cancellation.cancel()
        cancellation.unbind(object())
        cancellation.cancel()
        cancellation.unbind(token)
    finally:
        for reader in readers:
            reader.join(1)
        assert all(not reader.is_alive() for reader in readers)
    # Outside the callback: _invoke deliberately catches callback exceptions.
    assert callback_states == [True]
    assert observations == [True]
    second = cancellation.bind(lambda: observations.append("next binding"))
    cancellation.unbind(token)  # stale identity must not detach the new binding
    cancellation.cancel()
    cancellation.unbind(second)
    assert observations == [True, "next binding"]


def test_detached_and_foreign_requests_are_not_interrupted(caplog):
    first, second = RequestCancellation(), RequestCancellation()
    calls = []
    token = first.bind(lambda: calls.append("detached"))
    first.unbind(token)
    second.bind(lambda: calls.append("second"))
    first.cancel()
    assert not second.cancelled and calls == []

    def failing():
        calls.append("failing")
        raise RuntimeError("synthetic interrupt error")

    first.bind(failing)
    first.cancel()
    assert calls == ["failing"] and "synthetic interrupt error" in caplog.text


def test_unicode_backpressure_preserves_order_with_four_byte_capacity():
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        text = "Aé🙂中" * 5
        errors = []

        def produce():
            try:
                bridge.put(text)
            except BaseException as exc:
                errors.append(exc)
            finally:
                bridge.publish({"content": text}, None)

        producer = threading.Thread(target=produce)
        producer.start()
        pieces = []
        try:
            async for piece in bridge.deltas():
                pieces.append(piece)
                assert len(piece.encode()) <= 4
                count, size = bridge.buffered
                assert count <= 1 and size <= 4
            assert "".join(pieces) == text and not errors
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(producer.join, 2)
            assert not producer.is_alive()

    asyncio.run(asyncio.wait_for(case(), 3))


@pytest.mark.parametrize("terminal_first", [False, True])
def test_cancel_wakes_full_writer_and_terminal_never_needs_queue_space(terminal_first):
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        bridge.put("a")
        waiting = threading.Event()
        original_wait = bridge._condition.wait

        def observe_wait(*args):
            waiting.set()
            return original_wait(*args)

        bridge._condition.wait = observe_wait
        producer = threading.Thread(target=lambda: bridge.put("🙂" * 100))
        producer.start()
        try:
            assert await asyncio.to_thread(waiting.wait, 1)
            if terminal_first:
                bridge.publish({"content": "a"}, None)
                assert bridge.receipt.disposition == "completed"
            bridge.close(cancel=True)
            assert bridge.buffered == (0, 0)
            await asyncio.to_thread(producer.join, 1)
            assert not producer.is_alive()
            bridge.put("late" * 100)
            bridge.close(cancel=True)
            assert bridge.buffered == (0, 0)
            if not terminal_first:
                assert bridge.receipt is None  # cancelled delivery != final receipt
                bridge.publish({"usage": {"prompt_tokens": 11}}, None)
            expected = "completed" if terminal_first else "cancelled"
            assert bridge.disposition == bridge.receipt.disposition == expected
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(producer.join, 1)
            assert not producer.is_alive()

    asyncio.run(case())


def test_receipt_freezes_usage_and_duplicate_publication_cannot_reopen_it():
    async def case():
        bridge = StreamBridge(StreamLimits())
        usage = {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
        bridge.publish(None, CompletionInterrupted(usage, True))
        receipt = bridge.receipt
        usage["prompt_tokens"] = 100
        receipt.usage["completion_tokens"] = 100
        bridge.publish({"content": "late success"}, None)
        bridge.close(cancel=True)
        assert bridge.receipt is receipt and receipt.disposition == "cancelled"
        assert receipt.usage == {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}
        assert receipt.usage_uncertain and "lower bound" in receipt.error_message
        with pytest.raises(dataclasses.FrozenInstanceError):
            receipt.disposition = "completed"

    asyncio.run(case())


def test_cancel_is_sticky_before_a_blocked_producer_is_woken():
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1, bytes=4))
        bridge.put("a")
        waiting, finished = threading.Event(), threading.Event()
        original_wait, original_notify = bridge._condition.wait, bridge._condition.notify_all
        wake_states = []

        def wait(*args):
            waiting.set()
            return original_wait(*args)

        def notify():
            wake_states.append(bridge.cancellation.cancelled)
            original_notify()

        bridge._condition.wait = wait
        bridge._condition.notify_all = notify

        def produce():
            bridge.put("b")
            finished.set()

        worker = threading.Thread(target=produce)
        worker.start()
        try:
            assert await asyncio.to_thread(waiting.wait, 1)
            bridge.close(cancel=True)
            assert await asyncio.to_thread(finished.wait, 1)
            assert wake_states == [True]
        finally:
            bridge.close(cancel=True)
            await asyncio.to_thread(worker.join, 1)
            assert not worker.is_alive()

    asyncio.run(case())


def test_notifications_coalesce_without_a_second_unbounded_queue(monkeypatch):
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=100))
        notifications = []
        notify = bridge._loop.call_soon_threadsafe

        def observe(callback, *args):
            notifications.append(callback)
            return notify(callback, *args)

        monkeypatch.setattr(bridge._loop, "call_soon_threadsafe", observe)
        for _ in range(100):
            bridge.put("x")
        assert bridge._notification_pending
        # The loop has not run a notification yet: publication also coalesces.
        bridge.publish({}, None)
        assert len(notifications) == 1
        pieces = [piece async for piece in bridge.deltas()]
        assert pieces == ["x"] * 100
        bridge.close(cancel=True)

    asyncio.run(case())


def test_full_queue_error_publishes_separately_and_keeps_prior_empty_delta():
    async def case():
        bridge = StreamBridge(StreamLimits(pieces=1))
        bridge.put("")
        assert bridge.buffered == (1, 0)
        bridge.publish(None, RuntimeError("synthetic failure after a delta"))
        assert bridge.receipt.disposition == "failed"
        assert "after a delta" in bridge.receipt.error_message
        assert [piece async for piece in bridge.deltas()] == [""]

    asyncio.run(case())
