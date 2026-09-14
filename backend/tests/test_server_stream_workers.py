"""Physical worker ownership and finite shutdown, with no unjoined test threads."""

import asyncio
import threading

import pytest

from lohra.server.app import create_openai_app
from lohra.server.stream_bridge import StreamBridge, StreamLimits
from lohra.server.stream_workers import StreamUnavailable, StreamWorkers


RESULT = {"model": "synthetic", "content": "ok", "finish_reason": "stop",
          "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class HeldService:
    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.interrupts = 0

    def run_cancellable(self, *, cancellation, on_delta):
        def interrupt():
            self.interrupts += 1

        token = cancellation.bind(interrupt)
        try:
            self.entered.set()
            assert self.release.wait(5), "test must release noncooperative producer"
            on_delta("late")
            return RESULT
        finally:
            cancellation.unbind(token)


async def _release(services, workers):
    for service in services:
        service.release.set()
    for worker in workers:
        await asyncio.to_thread(worker.thread.join, 2)
    assert all(not worker.thread.is_alive() for worker in workers)


def test_active_and_draining_share_admission_until_real_thread_exit():
    async def case():
        pool = StreamWorkers(StreamLimits(workers=2, drain_seconds=0))
        services = [HeldService() for _ in range(3)]
        workers = []
        try:
            workers.extend(pool.start(service, {}) for service in services[:2])
            for service in services[:2]:
                assert await asyncio.to_thread(service.entered.wait, 1)
            workers[0].bridge.close(cancel=True)
            assert len(pool.snapshot()) == 2
            assert workers[0].bridge.receipt is None and workers[0].thread.is_alive()
            with pytest.raises(StreamUnavailable, match="capacity"):
                pool.start(services[2], {})
            assert not services[2].entered.is_set()
            services[0].release.set()
            await asyncio.to_thread(workers[0].thread.join, 1)
            assert not workers[0].thread.is_alive()
            workers.append(pool.start(services[2], {}))
            assert len(pool.snapshot()) == 2
            assert workers[0].bridge.receipt.disposition == "cancelled"
            assert workers[0].bridge.receipt.usage["prompt_tokens"] == 1
        finally:
            await _release(services, workers)
            assert pool.snapshot() == ()

    asyncio.run(case())


def test_published_receipt_does_not_release_a_tail_held_thread(monkeypatch):
    async def case():
        pool = StreamWorkers(StreamLimits(workers=1))
        published, release_tail = threading.Event(), threading.Event()
        original = StreamBridge.publish

        def publish_and_hold(self, result, error):
            original(self, result, error)
            published.set()
            assert release_tail.wait(5)

        monkeypatch.setattr(StreamBridge, "publish", publish_and_hold)
        service = HeldService()
        service.release.set()
        worker = pool.start(service, {})
        try:
            assert await asyncio.to_thread(published.wait, 1)
            assert worker.bridge.receipt.disposition == "completed"
            assert pool.snapshot() == (worker,)
            with pytest.raises(StreamUnavailable, match="capacity"):
                pool.start(service, {})
        finally:
            release_tail.set()
            await _release([service], [worker])
            assert pool.snapshot() == ()

    asyncio.run(case())


def test_lifespan_uses_one_global_deadline_and_retains_uncooperative_workers(caplog):
    async def case():
        app = create_openai_app(None, stream_limits=StreamLimits(
            workers=3, drain_seconds=0, shutdown_seconds=0.1,
        ))
        pool = app.state.stream_workers
        services = [HeldService() for _ in range(3)]
        workers = []
        try:
            async with app.router.lifespan_context(app):
                workers.extend(pool.start(service, {}) for service in services)
                for service in services:
                    assert await asyncio.to_thread(service.entered.wait, 1)
                workers[0].bridge.close(cancel=True)  # already draining at shutdown
                clock = asyncio.get_running_loop().time
                started = clock()
            elapsed = clock() - started
            assert elapsed < 0.25, elapsed  # not three sequential 100 ms waits
            assert len(pool.snapshot()) == 3
            assert all(w.thread.is_alive() and w.bridge.receipt is None for w in workers)
            assert [s.interrupts for s in services] == [1, 1, 1]
            assert "3 stream producer(s) still draining" in caplog.text
            with pytest.raises(StreamUnavailable, match="shutting down"):
                pool.start(services[0], {})
            # Repeated cleanup does not redeliver interruption or forge receipts.
            for worker in workers:
                worker.bridge.close(cancel=True)
            assert [s.interrupts for s in services] == [1, 1, 1]
        finally:
            await _release(services, workers)
            assert pool.snapshot() == ()

    asyncio.run(case())


def test_thread_start_failure_releases_never_started_reservation(monkeypatch):
    async def case():
        pool = StreamWorkers(StreamLimits(workers=1))
        with monkeypatch.context() as patch:
            def fail_start(self):
                raise RuntimeError("synthetic start failure")
            patch.setattr(threading.Thread, "start", fail_start)
            with pytest.raises(StreamUnavailable, match="could not start"):
                pool.start(HeldService(), {})
            assert pool.snapshot() == ()
        service = HeldService()
        service.release.set()
        worker = pool.start(service, {})
        await _release([service], [worker])

    asyncio.run(case())


def test_optional_protocol_does_not_retry_internal_typeerror():
    async def case():
        class Service:
            calls = 0

            def run_cancellable(self, **kwargs):
                self.calls += 1
                raise TypeError("synthetic provider-internal TypeError")

            def run(self, **kwargs):
                raise AssertionError("must not retry through the legacy entry point")

        service = Service()
        worker = StreamWorkers(StreamLimits()).start(service, {})
        try:
            await asyncio.to_thread(worker.thread.join, 1)
            assert not worker.thread.is_alive() and service.calls == 1
            assert worker.bridge.receipt.disposition == "failed"
            assert "provider-internal TypeError" in worker.bridge.receipt.error_message
        finally:
            await asyncio.to_thread(worker.thread.join, 1)
            assert not worker.thread.is_alive()

    asyncio.run(case())


def test_start_that_raises_after_launch_cannot_escape_ownership(monkeypatch):
    async def case():
        pool = StreamWorkers(StreamLimits(workers=1, shutdown_seconds=0))
        service = HeldService()
        launched = []
        original = threading.Thread.start

        def partially_start(thread):
            original(thread)
            launched.append(thread)
            raise RuntimeError("synthetic exception after thread launch")

        try:
            with monkeypatch.context() as patch:
                patch.setattr(threading.Thread, "start", partially_start)
                with pytest.raises(StreamUnavailable):
                    pool.start(service, {})
            assert await asyncio.to_thread(service.entered.wait, 1)
            retained = pool.snapshot()
            assert len(retained) == 1 and retained[0].thread is launched[0]
            assert retained[0].bridge.cancellation.cancelled
            assert service.interrupts == 1
            with pytest.raises(StreamUnavailable, match="capacity"):
                pool.start(service, {})
        finally:
            service.release.set()
            for thread in launched:
                await asyncio.to_thread(thread.join, 2)
            assert launched and all(not thread.is_alive() for thread in launched)
            assert pool.snapshot() == ()

    asyncio.run(case())


def test_cleanup_callback_is_outside_lock_and_a_failure_is_visible_without_retry():
    async def case():
        pool = StreamWorkers(StreamLimits(shutdown_seconds=0))
        calls, unlocked, readers = [], [], []

        def cleanup():
            calls.append("attempt")
            reader = threading.Thread(target=pool.snapshot)
            readers.append(reader)
            reader.start()
            reader.join(1)
            unlocked.append(not reader.is_alive())
            raise RuntimeError("synthetic partially completed close")

        try:
            assert not pool.cleanup_if_idle(cleanup)  # admission still open
            await pool.shutdown()
            with pytest.raises(RuntimeError, match="partially completed"):
                pool.cleanup_if_idle(cleanup)
            assert pool.cleanup_if_idle(cleanup)
        finally:
            for reader in readers:
                reader.join(1)
            assert all(not reader.is_alive() for reader in readers)
        assert calls == ["attempt"] and unlocked == [True]

    asyncio.run(case())
