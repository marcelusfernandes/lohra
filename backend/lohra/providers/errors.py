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

logger = logging.getLogger(__name__)

QUOTA_EXHAUSTED = "quota_exhausted"

# Error codes that mean "you are out of quota" in the Responses payload (the
# Codex backend reports failures as an event code, with no HTTP status attached).
_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "rate_limit_exceeded", "insufficient_quota", "quota_exceeded"}
)
_TOO_MANY_REQUESTS = 429


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


def _sdk_rate_limit_types() -> tuple[type, ...]:
    """The installed SDKs' rate-limit classes. Imported lazily and defensively:
    both SDKs are optional extras, exactly as in ``agent/client.py``."""
    types: list[type] = []
    for module_name in ("anthropic", "openai"):
        try:  # pragma: no cover - import guard for an optional extra
            module = __import__(module_name)
        except Exception:
            continue
        error = getattr(module, "RateLimitError", None)
        if isinstance(error, type):
            types.append(error)
    return tuple(types)


def classify_provider_error(exc: Exception) -> str | None:
    """``"quota_exhausted"`` for a rate-limit/quota failure, else None.

    Checks, in order: the SDK class, the HTTP status, the payload error code.
    Anything unrecognized stays unclassified — an ordinary failure whose leaf
    dies alone (fail-isolation), not a reason to stop the whole run.
    """
    if isinstance(exc, _sdk_rate_limit_types()):
        return QUOTA_EXHAUSTED
    if _status_of(exc) == _TOO_MANY_REQUESTS:
        return QUOTA_EXHAUSTED
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _QUOTA_CODES:
        return QUOTA_EXHAUSTED
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
