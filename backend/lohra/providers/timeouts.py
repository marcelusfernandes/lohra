"""Operator-configurable HTTP read timeout for provider SDK clients.

Both the ``anthropic`` and ``openai`` SDKs default to
``httpx.Timeout(connect=5.0, read=600, write=600, pool=600)`` with no env var
or client-side knob to raise it. A leaf that goes silent for longer than 600s
(no bytes, not "too much output" — see ``docs/specs`` and issue #48) gets an
opaque ``httpx.ReadTimeout``/``APITimeoutError`` with no way for the operator
to widen the window without patching the SDK call site.

``resolve_provider_timeout`` is the ONLY place that reads
``LOHRA_PROVIDER_READ_TIMEOUT``; ``agent/client.py``'s three constructors call
it and only add ``timeout=`` to the SDK kwargs when it returns non-None.

Trade-off this exists to name: the anthropic SDK only applies its own
``_calculate_nonstreaming_timeout`` safety guard (which raises instead of
silently truncating a non-streaming call whose ``max_tokens`` implies more
than 10 minutes of generation) when ``client.timeout == DEFAULT_TIMEOUT``.
That is a VALUE comparison (``httpx.Timeout`` defines ``__eq__``), not an
identity check — so the guard would, in principle, survive a custom
``httpx.Timeout`` that happens to numerically equal
``Timeout(connect=5.0, read=600, write=600, pool=600)``. In practice any read
value other than exactly 600 (the whole point of this module) disarms it, and
this module never asserts otherwise: this is why the unset case stays
byte-identical (no ``timeout=`` kwarg at all) rather than "pass the SDK's own
default value explicitly" — the latter would still be correct today but ties
correctness to an equality check the SDK could change to identity later.
Setting the env var to anything other than 600 is an informed trade of that
guard for a wider read window.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping

import httpx

logger = logging.getLogger(__name__)

ENV_VAR = "LOHRA_PROVIDER_READ_TIMEOUT"

# Mirrors both SDKs' built-in default (see module docstring) — used as the
# fallback when phrasing a timeout fault, never passed to a constructor.
DEFAULT_READ_TIMEOUT_SECONDS = 600.0

_CONNECT_TIMEOUT_SECONDS = 5.0


def resolve_provider_timeout(env: Mapping[str, str] | None = None) -> httpx.Timeout | None:
    """The ``httpx.Timeout`` to pass to a provider SDK constructor, or None.

    None means "pass nothing" — the caller must NOT pass ``timeout=`` at all,
    which is what keeps construction byte-identical to today when the operator
    never sets ``LOHRA_PROVIDER_READ_TIMEOUT`` (see module docstring for why
    that matters beyond cosmetics).

    An unset var returns None silently (the common case). A set-but-invalid
    var (not a number, zero, negative) logs a warning and ALSO returns None —
    fail soft to the default rather than crash client construction over a
    typo'd env var.
    """
    source = env if env is not None else os.environ
    raw = source.get(ENV_VAR)
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not a number; ignoring, default read timeout (%gs) stays in effect",
            ENV_VAR,
            raw,
            DEFAULT_READ_TIMEOUT_SECONDS,
        )
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        logger.warning(
            "%s=%r must be a finite number > 0; ignoring, default read timeout "
            "(%gs) stays in effect",
            ENV_VAR,
            raw,
            DEFAULT_READ_TIMEOUT_SECONDS,
        )
        return None
    return httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS, read=seconds, write=seconds, pool=seconds
    )


def effective_read_timeout_seconds(env: Mapping[str, str] | None = None) -> float:
    """The read timeout actually in effect, for PHRASING a fault message only.

    Never used to construct a client (``resolve_provider_timeout`` is the sole
    source of truth there) — this just mirrors its fallback so a timeout fault
    can name the real number instead of a hardcoded guess.
    """
    timeout = resolve_provider_timeout(env)
    return timeout.read if timeout is not None else DEFAULT_READ_TIMEOUT_SECONDS
