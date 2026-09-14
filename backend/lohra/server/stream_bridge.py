"""Bounded thread-to-async SSE delivery and immutable producer receipts."""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from lohra.server.cancellation import RequestCancellation
from lohra.server.service import CompletionInterrupted, CompletionResult


@dataclass(frozen=True)
class StreamLimits:
    """Queue payload limits, producer count, and drain deadlines in seconds."""

    pieces: int = 64
    bytes: int = 256 * 1024
    workers: int = 16
    drain_seconds: float = 0.25
    shutdown_seconds: float = 1.0

    def __post_init__(self) -> None:
        for name, minimum in (("pieces", 1), ("bytes", 4), ("workers", 1)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        for name in ("drain_seconds", "shutdown_seconds"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and nonnegative")


@dataclass(frozen=True)
class StreamReceipt:
    """One final producer snapshot. JSON prevents mutable embedder aliases.

    Cancellation can decide delivery earlier, while this receipt is still absent.
    A late result is retained here without reopening cancelled delivery. Its
    ordinary wire mapping can contain the legacy success estimate; ``usage``
    contains only observed usage, never that estimate.
    """

    disposition: str
    result_json: str | None
    error_message: str | None
    usage_json: str | None
    usage_uncertain: bool
    usage_observed: bool

    @property
    def result(self) -> dict[str, Any] | None:
        return json.loads(self.result_json) if self.result_json is not None else None

    @property
    def usage(self) -> dict[str, Any] | None:
        return json.loads(self.usage_json) if self.usage_json is not None else None


class StreamBridge:
    def __init__(self, limits: StreamLimits) -> None:
        self.limits = limits
        self.cancellation = RequestCancellation()
        self._loop = asyncio.get_running_loop()
        self._ready = asyncio.Event()
        self._condition = threading.Condition()
        self._queue: deque[tuple[str, int]] = deque()
        self._bytes = 0
        self._closed = False
        self._notification_pending = False
        self._disposition: str | None = None
        self._receipt: StreamReceipt | None = None

    def _notify_locked(self) -> None:
        if not self._notification_pending:
            self._notification_pending = True
            try:
                self._loop.call_soon_threadsafe(self._notify)
            except RuntimeError:  # the HTTP loop may already have shut down
                self._notification_pending = False

    def _notify(self) -> None:
        with self._condition:
            self._notification_pending = False
            self._ready.set()

    @property
    def receipt(self) -> StreamReceipt | None:
        with self._condition:
            return self._receipt

    @property
    def disposition(self) -> str | None:
        with self._condition:
            return self._disposition

    @property
    def buffered(self) -> tuple[int, int]:
        with self._condition:
            return len(self._queue), self._bytes

    def put(self, text: str) -> None:
        # Every Unicode scalar fits in four UTF-8 bytes. Bound temporary pieces
        # too; this is a queue cap, not a cap on the provider's original string.
        chars = min(4096, self.limits.bytes // 4)
        for offset in range(0, max(1, len(text)), chars):
            piece = text[offset:offset + chars]
            size = len(piece.encode("utf-8"))
            with self._condition:
                while not self._closed and self._receipt is None and (
                    len(self._queue) >= self.limits.pieces
                    or self._bytes + size > self.limits.bytes
                ):
                    self._condition.wait()
                if self._closed or self._receipt is not None:
                    return
                self._queue.append((piece, size))
                self._bytes += size
                self._notify_locked()

    def publish(self, result: dict | None, error: BaseException | None) -> None:
        # Snapshot outside the lock. Invalid embedder output becomes a normal
        # local failure; publication itself must still wake a waiting reader.
        try:
            result_json = json.dumps(result) if result is not None else None
            usage = error.usage if isinstance(error, CompletionInterrupted) else (
                result.get("usage") if result else None
            )
            if isinstance(result, CompletionResult) and not result.usage_observed:
                usage = None
            usage_json = json.dumps(usage) if usage is not None else None
        except (TypeError, ValueError) as exc:
            error, result_json, usage_json = exc, None, None
        with self._condition:
            if self._receipt is not None:
                return
            if self._disposition is None:
                self._disposition = ("cancelled" if isinstance(error, CompletionInterrupted)
                                     else "failed" if error is not None else "completed")
            self._receipt = StreamReceipt(
                self._disposition, result_json, str(error) if error is not None else None,
                usage_json, bool(getattr(error, "usage_uncertain", False)), usage_json is not None,
            )
            self._condition.notify_all()
            self._notify_locked()

    def close(self, *, cancel: bool) -> None:
        if cancel:
            with self._condition:
                if self._disposition is None:
                    self._disposition = "cancelled"
            # A full-queue producer must see sticky cancellation before it can
            # resume and detach its Agent. Neither lock surrounds the callback.
            self.cancellation.cancel()
        with self._condition:
            self._closed = True
            self._queue.clear()
            self._bytes = 0
            self._condition.notify_all()
            self._notify_locked()

    async def deltas(self) -> AsyncIterator[str]:
        while True:
            with self._condition:
                if self._queue:
                    piece, size = self._queue.popleft()
                    self._bytes -= size
                    self._condition.notify_all()
                elif self._closed or self._receipt is not None:
                    return
                else:
                    piece = None
                    self._ready.clear()
            if piece is None:
                await self._ready.wait()
            else:
                yield piece
