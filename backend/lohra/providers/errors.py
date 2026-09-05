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
# The provider does not HAVE the model this route named (issue #85, W9-E8). A
# fourth structural kind, and the only one whose remedy the harness may apply
# itself: the credential is fine, the quota is fine, the network is fine — the
# slug is wrong, and the operator's own tier map already names models that are
# not. Deterministic within the route for the reason ``auth_failed`` is (the
# same request goes out on every attempt and gets the same 404), so it buys no
# same-route re-spawn either — see ``workflow/leaf_retry.NO_RESPAWN_KINDS`` and
# ``workflow/model_substitution.py`` for what it buys instead.
MODEL_NOT_FOUND = "model_not_found"

# Error codes that mean "you are out of quota" in the Responses payload (the
# Codex backend reports failures as an event code, with no HTTP status attached).
_QUOTA_CODES = frozenset(
    {"usage_limit_reached", "rate_limit_exceeded", "insufficient_quota", "quota_exceeded"}
)
_TOO_MANY_REQUESTS = 429
_UNAUTHORIZED = 401
_FORBIDDEN = 403
_NOT_FOUND = 404

# ...and the payload code that NAMES the model. The openai SDK lifts
# ``body.error.code`` onto the exception, so this one attribute covers both the
# 404 the API returns and the 400 an OpenRouter-style gateway would answer with.
_MODEL_NOT_FOUND_CODES = frozenset({"model_not_found"})

# The anthropic SDK sets no ``code``; its 404 body is
# ``{"type": "error", "error": {"type": "not_found_error", ...}}``.
_NOT_FOUND_ERROR_TYPE = "not_found_error"

# The (module, class) PAIRS that may carry "no such model". Deliberately pairs
# rather than a name list through ``_is_sdk_error``: the three siblings above
# name classes both SDKs define identically, but this one is asymmetric —
# ``BadRequestError`` is admitted only on the openai side (the gateway shape,
# and only ever by its CODE, never by its status), while an anthropic 400 must
# stay unclassified. Read with the same convention ``_is_sdk_error`` documents:
# both SDKs stamp ``__module__`` to the bare package name.
_NOT_FOUND_TYPES = frozenset(
    {
        ("anthropic", "NotFoundError"),
        ("openai", "NotFoundError"),
        ("openai", "BadRequestError"),
    }
)


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


def _error_type_of(exc: Exception) -> str | None:
    """The provider's own ``error.type`` off a parsed body, or None.

    Defensive at every hop: ``body`` is whatever the SDK managed to parse, and a
    provider that answered HTML has no dict there at all."""
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    kind = error.get("type")
    return kind if isinstance(kind, str) else None


def _is_model_not_found(exc: Exception, status: int | None) -> bool:
    """Did the provider say this MODEL does not exist? (issue #85)

    Structural like its three siblings, and on the same no-import footing as
    ``_is_sdk_error`` (issue #80): the class is matched by
    ``(type(exc).__module__, type(exc).__name__)``, never by importing an
    optional extra. It uses the PAIR set rather than that helper only because
    this kind is the asymmetric one — see ``_NOT_FOUND_TYPES``.

    Both routes require that identity first — fail-closed, because acting on
    this changes the model a run pays for, and some other library's exception
    carrying the same word is not a provider verdict:

    - a payload ``code`` of ``model_not_found``, on any status. The openai SDK
      lifts it onto the exception; it arrives on the API's 404 and would arrive
      on the 400 a gateway answers with, which is why the code is read without
      pinning a status;
    - a 404 whose body names ``not_found_error`` — the anthropic shape, which
      carries no code at all.

    The known residual: anthropic answers 404 ``not_found_error`` for any
    missing resource, not only a model. It is bounded by WHERE this runs — the
    only anthropic call the loop makes is ``messages.create``, whose sole
    addressable resource is the model — and by what the classification buys: one
    substitution, from a list the operator wrote, or nothing.
    """
    cls = type(exc)
    if (cls.__module__, cls.__name__) not in _NOT_FOUND_TYPES:
        return False
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in _MODEL_NOT_FOUND_CODES:
        return True
    return status == _NOT_FOUND and _error_type_of(exc) == _NOT_FOUND_ERROR_TYPE


def classify_provider_error(exc: Exception) -> str | None:
    """``"quota_exhausted"``/``"auth_failed"``/``"model_not_found"``/``"timeout"``,
    else None.

    Quota checks, in order: the SDK class, the HTTP status, the payload error
    code — and it is checked FIRST, because a 429 is about the plan's rate, not
    the key's validity, and the run-level remedy (wait) differs from every other
    kind here. Auth checks the SDK credential/permission classes and HTTP
    401/403. "This model does not exist" is checked next and cannot collide with
    either: its statuses are 404/400 and its evidence is a payload code or an
    ``error.type``, neither of which a 429 or a 401 carries.
    Timeout checks the SDK timeout classes and ``httpx.TimeoutException``
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
    if _is_model_not_found(exc, status):
        return MODEL_NOT_FOUND
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
