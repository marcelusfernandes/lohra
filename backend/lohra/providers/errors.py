"""Provider-error taxonomy — what KIND of failure a raised exception was.

The loop turns a provider exception into ``str(exc)`` and moves on, so this is
the only place the exception still exists as an object. A 429 is categorically
different from "the leaf crashed": every sibling leaf is about to fail the same
way, so the run must pause rather than null itself node by node.

Classification is STRUCTURAL — an SDK class, an HTTP status, or an error code
from the payload. Never a regex over prose: a tool result quoting "429 rate
limit exceeded" back at us must not pause a healthy run.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

QUOTA_EXHAUSTED = "quota_exhausted"
# The provider went silent for longer than the configured HTTP read timeout
# (issue #48) — categorically different from an ordinary failure: the leaf's
# OWN prompt didn't cause it, so a retry with the same prompt is a reasonable
# next step, unlike most errors. See ``providers/timeouts.py`` for the knob.
TIMEOUT = "timeout"
# The provider refused this route's CREDENTIAL or the permission attached to it
# (issue #43). Categorically different from both siblings above: the client is
# built once per route and cached for the life of the pool, so within one run the
# refusal is deterministic — asking again presents the same key and gets the same
# answer. It is also not a pause: a pause promises the run comes back on its own,
# and nothing about a refused credential fixes itself with time. The remedy is
# the operator's (a key, a scope, an enabled subscription), so this classification
# exists to STOP work, not to schedule more of it.
AUTH_FAILED = "auth_failed"

# Error codes that mean "you are out of quota" in the Responses payload (the
# Codex backend reports failures as an event code, with no HTTP status attached).
_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "rate_limit_exceeded", "insufficient_quota", "quota_exceeded"}
)
_TOO_MANY_REQUESTS = 429
_UNAUTHORIZED = 401
_FORBIDDEN = 403


class ProviderCallFailed(RuntimeError):
    """A provider reported a failed call with a structured error ``code``.

    Raised where the transport has the code in hand (the Responses stream's
    ``response.failed`` event); formatting it into the message alone would throw
    away the only machine-readable signal the classifier can use.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


def _status_of(exc: Exception) -> int | None:
    """The HTTP status an exception carries, under either SDK's attribute name."""
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


# Both SDKs stamp ``__module__`` to the bare package name on every public
# exception class ("anthropic"/"openai", never a submodule path like
# "anthropic._exceptions") — a convenience the SDKs ship so their own
# tracebacks read cleanly. ``_is_sdk_error`` leans on that convention.
_SDK_MODULES = ("anthropic", "openai")


def _is_sdk_error(exc: Exception, *names: str) -> bool:
    """True if ``exc``'s runtime class is one of the SDKs' named exceptions —
    matched STRUCTURALLY, by ``(type(exc).__module__, type(exc).__name__)``,
    never by importing ``anthropic``/``openai`` to ``isinstance``-check against
    the real class (issue #80/H10).

    The old code called ``__import__("anthropic")`` (and ``openai``)
    unconditionally on every classification, before ever looking at ``exc`` —
    paying the SDK's full import cost (~0.3s cold, measured) on the FIRST
    exception a process ever classified, even an ordinary ``RuntimeError`` with
    nothing to do with either SDK. A short barrier timeout (the pipeline's
    ``on_done`` hook) could then race that import and classify the leaf's death
    AFTER the barrier had already given up on it, silently dropping the death
    fault from the run's accounting.

    Matching on ``__module__``/``__name__`` needs neither SDK ever imported:
    if ``exc`` actually came from one, its module already ran (Python cannot
    hand you an instance of a class nobody defined yet), so this loses no case
    a same-process ``isinstance`` would have caught — and it works identically
    for a duck-typed or wrapped exception that only carries the same shape,
    which a live SDK object was never guaranteed to be in the first place.
    """
    cls = type(exc)
    return cls.__module__ in _SDK_MODULES and cls.__name__ in names


def classify_provider_error(exc: Exception) -> str | None:
    """``"quota_exhausted"``/``"auth_failed"``/``"timeout"``, else None.

    Quota checks, in order: the SDK class, the HTTP status, the payload error
    code — and it is checked FIRST, because a 429 is about the plan's rate, not
    the key's validity, and the run-level remedy (wait) differs from every other
    kind here. Auth checks the SDK credential/permission classes and HTTP
    401/403. Timeout checks the SDK timeout classes and ``httpx.TimeoutException``
    (the transport both SDKs run on) — a read timeout never carries an HTTP
    status or a payload code, since no response ever arrived, so its position
    relative to the two status-bearing kinds cannot change an answer. Anything
    unrecognized stays unclassified — an ordinary failure whose leaf dies alone
    (fail-isolation), not a reason to stop the whole run.
    """
    if _is_sdk_error(exc, "RateLimitError"):
        return QUOTA_EXHAUSTED
    status = _status_of(exc)
    if status == _TOO_MANY_REQUESTS:
        return QUOTA_EXHAUSTED
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _QUOTA_CODES:
        return QUOTA_EXHAUSTED
    if _is_sdk_error(exc, "AuthenticationError", "PermissionDeniedError") or status in (
        _UNAUTHORIZED,
        _FORBIDDEN,
    ):
        return AUTH_FAILED
    if isinstance(exc, httpx.TimeoutException) or _is_sdk_error(exc, "APITimeoutError"):
        return TIMEOUT
    return None


def _as_seconds(value: Any) -> float | None:
    """A positive number of seconds, or None. Providers send this as an int, a
    float or a header string; anything else (an HTTP-date, junk) is ignored."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def retry_after_seconds(exc: Exception) -> float | None:
    """How long the provider asked us to wait, if it said so at all.

    Read from an explicit ``retry_after`` attribute first, then the HTTP
    ``retry-after`` header on the attached response. None means "the provider
    gave no hint" — the caller falls back to its own backoff.
    """
    direct = _as_seconds(getattr(exc, "retry_after", None))
    if direct is not None:
        return direct
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    try:
        return _as_seconds(getter("retry-after"))
    except Exception:  # pragma: no cover - a hostile headers mapping
        logger.debug("could not read retry-after header", exc_info=True)
        return None
