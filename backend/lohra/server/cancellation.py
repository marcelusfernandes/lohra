"""Request-local cooperative interruption; never owns a provider client."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

_log = logging.getLogger(__name__)


class RequestCancellation:
    """Sticky cancellation with one delivery per binding, outside the lock.

    Bindings are request-local: a fresh Agent belongs to each service invocation.
    A callback already selected can race detach, but can only reach that Agent.
    Callbacks must be short cooperative signals, not provider close/join calls.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._binding: tuple[object, Callable[[], None]] | None = None
        self._delivered = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def bind(self, callback: Callable[[], None]) -> object:
        token = object()
        with self._lock:
            if self._binding is not None:
                raise RuntimeError("request already has an interrupt binding")
            self._binding = (token, callback)
            self._delivered = self._cancelled
            deliver = self._cancelled
        if deliver:
            self._invoke(callback)
        return token

    def unbind(self, token: object) -> None:
        with self._lock:
            if self._binding is not None and self._binding[0] is token:
                self._binding = None

    def cancel(self) -> None:
        callback = None
        with self._lock:
            self._cancelled = True
            if self._binding is not None and not self._delivered:
                self._delivered = True
                callback = self._binding[1]
        if callback is not None:
            self._invoke(callback)

    @staticmethod
    def _invoke(callback: Callable[[], None]) -> None:
        try:
            callback()
        except Exception:
            # A bad embedder signal must not prevent buffer/HTTP cleanup.
            _log.exception("request interrupt callback failed")
