"""Operator-tunable loop limits (flag > env > default).

The iteration cap used to be a constant nobody outside the code could reach, so
a turn that needed one more round than the built-in leash allowed simply died.
This resolves the cap the SAME way the orchestration limits resolve theirs: an
explicit override (the CLI flag) beats the env var, which beats the caller's
default; anything unusable falls back to the default with a warning rather than
failing the run.

No upper cap here on purpose. This is the OPERATOR's knob on their own machine
(the ``--max-parallel`` precedent), not something a model can author — the
model-authored surfaces (workflow nodes, ``delegate_task``, ``spawn_session``)
are the ones bounded by ``MAX_NODE_MAX_ITERATIONS``.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

ENV_MAX_ITERATIONS = "LOHRA_MAX_ITERATIONS"

# The ceiling on a MODEL-AUTHORED cap (a workflow node or stage, a delegate_task
# / spawn_session argument). Generous — a leaf doing real tool work needs room —
# but fixed: what the model writes is a leash, never a blank cheque. The operator
# knob above has no such ceiling; the difference is who is asking.
MAX_AUTHORED_MAX_ITERATIONS = 128


def positive_int_env(name: str, default: int) -> int:
    """An env var read as a positive int, or ``default`` (saying why it fell back)."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring %s=%r: not an integer; using %d", name, raw, default)
        return default
    if value < 1:
        logger.warning("ignoring %s=%r: must be >= 1; using %d", name, raw, default)
        return default
    return value


def resolve_max_iterations(*, override: int | None = None, default: int) -> int:
    """The turn's iteration cap: the flag, else the env var, else ``default``.

    ``default`` is the caller's existing constant, so with neither knob set the
    behaviour is byte-identical to before this knob existed."""
    value = override if override is not None else positive_int_env(ENV_MAX_ITERATIONS, default)
    return max(1, value)


def coerce_authored_max_iterations(value: object) -> tuple[int | None, str | None]:
    """Read a model-supplied ``max_iterations`` argument: ``(value, error)``.

    ``(None, None)`` when it was not asked for. A bad value is REFUSED with the
    range spelled out, never clamped: silently running under a leash the caller
    did not ask for is the exact footgun the workflow validator exists to catch.
    """
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        return None, (
            "'max_iterations' must be a whole number between 1 and "
            f"{MAX_AUTHORED_MAX_ITERATIONS}"
        )
    if not 1 <= value <= MAX_AUTHORED_MAX_ITERATIONS:
        return None, (
            f"'max_iterations' must be between 1 and {MAX_AUTHORED_MAX_ITERATIONS} "
            f"(got {value})"
        )
    return value, None
