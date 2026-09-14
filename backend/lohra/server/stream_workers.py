"""App-owned SSE producers: bounded admission and honest draining ownership."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lohra.server.stream_bridge import StreamBridge, StreamLimits

_log = logging.getLogger(__name__)


class StreamUnavailable(RuntimeError):
    pass


@dataclass(eq=False)
class StreamWorker:
    bridge: StreamBridge
    thread: threading.Thread
    launching: bool = True


class StreamWorkers:
    def __init__(self, limits: StreamLimits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._workers: list[StreamWorker] = []
        self._closed = False
        self._cleanup_claimed = False

    def _reap_locked(self) -> None:
        self._workers = [w for w in self._workers if w.launching or w.thread.is_alive()]

    def snapshot(self) -> tuple[StreamWorker, ...]:
        """Retain even a receipt-published worker until its thread really exits."""
        with self._lock:
            self._reap_locked()
            return tuple(self._workers)

    def start(self, service: Any, kwargs: dict) -> StreamWorker:
        bridge = StreamBridge(self.limits)

        def produce() -> None:
            result, error = None, None
            try:
                cancellable = getattr(service, "run_cancellable", None)
                if callable(cancellable):
                    result = cancellable(cancellation=bridge.cancellation,
                                         on_delta=bridge.put, **kwargs)
                else:
                    # Explicit optional protocol: no signature inspection or
                    # TypeError retry, including errors thrown inside providers.
                    result = service.run(on_delta=bridge.put, **kwargs)
            except BaseException as exc:
                error = exc
            finally:
                bridge.publish(result, error)

        worker = StreamWorker(bridge, threading.Thread(
            target=produce, daemon=True, name="lohra-sse",
        ))
        with self._lock:
            self._reap_locked()
            if self._closed:
                raise StreamUnavailable("server is shutting down")
            if len(self._workers) >= self.limits.workers:
                raise StreamUnavailable("stream producer capacity exhausted")
            self._workers.append(worker)
        try:
            worker.thread.start()
        except Exception as exc:
            with self._lock:
                # A wrapper can raise after Thread.start actually succeeded.
                # Keep that accepted thread charged until its physical exit.
                if worker.thread.ident is None:
                    self._workers.remove(worker)
            bridge.close(cancel=True)
            raise StreamUnavailable("stream producer could not start") from exc
        finally:
            with self._lock:
                worker.launching = False
        return worker

    async def drain(self, worker: StreamWorker) -> None:
        await self._wait((worker,), self.limits.drain_seconds)

    @staticmethod
    async def _wait(workers: tuple[StreamWorker, ...], seconds: float) -> None:
        deadline = asyncio.get_running_loop().time() + seconds
        while any(w.launching or w.thread.is_alive() for w in workers):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.01, remaining))

    async def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._reap_locked()
            workers = tuple(self._workers)
        for worker in workers:
            worker.bridge.close(cancel=True)
        await self._wait(workers, self.limits.shutdown_seconds)
        remaining = self.snapshot()
        if remaining:
            _log.warning("server shutdown: %d stream producer(s) still draining", len(remaining))

    def cleanup_if_idle(self, cleanup: Callable[[], None]) -> bool:
        """Claim host cleanup once, only after admission closed and threads died.

        A throwing cleanup remains visible to its caller; the callback is never
        silently retried (it may already have performed part of its close).
        """
        with self._lock:
            self._reap_locked()
            if not self._closed or self._workers:
                return False
            if self._cleanup_claimed:
                return True
            self._cleanup_claimed = True
        cleanup()
        return True


def shutdown_openai_app(app: Any) -> None:
    """CLI fallback with lifespan disabled, after the blocking runner returns.

    Never close the shared SDK underneath a still-owned SSE producer. If the
    drain expires, leave the client to process teardown; no unbounded join or
    additional reaper thread is introduced. The host may call this again later.
    """
    workers = app.state.stream_workers
    asyncio.run(workers.shutdown())
    if not workers.cleanup_if_idle(app.state.cleanup):
        _log.warning("shared provider client left open: stream producers still own it")
        return
