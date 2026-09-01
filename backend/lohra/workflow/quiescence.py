"""Waiting for a cancelled leaf to go QUIET (issue #8 / issue #42-B).

``OrchestrationCore.cancel`` is cooperative: it sets an interrupt flag the turn
reads at the top of its loop (``loop.py``), and the dispatch of a tool call does
not check it at all. A leaf inside a long ``terminal`` or ``write_file`` keeps
running — and every leaf of a run shares ONE ``working_root``, so the leaf the
engine already gave up on can still be writing exactly where its successor is
about to read.

Both engine cancel sites (the scalar leaf timeout and the pipeline barrier's
expiry) used to cancel and walk away. This module is the bounded wait they take
instead:

- **short and capped** — a cancel that blocked on a provider call would be a
  worse bug than the one it fixes, so the wait is a few seconds at most
  (``LOHRA_CANCEL_QUIESCENCE_S`` for the operator who wants a different one);
- **one cap per call**: the leaves handed to a single ``await_quiescence`` share
  it, so the pipeline barrier cancelling N stragglers pays it once. It is NOT a
  per-RUN budget: the engine collects scalar leaves one at a time (``run_parallel``,
  ``verify``, ``judge_panel``, ``gate`` all loop over their leaves), so a node
  whose leaves ALL blow their deadline pays one cap per timed-out leaf. That is
  the price of a sequential collect, not a defect of the wait — the alternative
  is cancelling the whole node's fan-out on the first straggler;
- **honest either way** — the report says whether the leaf really settled or
  was still alive when the cap expired, and the caller puts that in the FAULT.
  "cancelled" alone told the author nothing about whether the material state
  under the successor was quiet.

It never cancels anything itself and never raises: it only observes.
"""

from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

# Short by construction. Long enough for a leaf between two tool calls to reach
# its interrupt check; far too short to be mistaken for "the leaf finished".
CANCEL_QUIESCENCE_TIMEOUT = 5.0
QUIESCENCE_ENV = "LOHRA_CANCEL_QUIESCENCE_S"

# Said once, in the fault, so an author reading a rollup knows WHY a still-live
# leaf matters rather than just that one existed.
_ALIVE_HINT = "shared working_root may be mutated"


def quiescence_timeout() -> float:
    """The operator's cap, else the built-in one.

    Read at CALL time (never captured at import) so a test — and an operator
    exporting the env var mid-process — gets the value that is true now. Any
    unparseable, negative or non-finite value falls back loudly-in-the-log to
    the default rather than disabling the wait by accident."""
    raw = os.environ.get(QUIESCENCE_ENV)
    if raw is None or not raw.strip():
        return CANCEL_QUIESCENCE_TIMEOUT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value < 0:
        logger.warning(
            "workflow: ignoring %s=%r (not a non-negative number); using %.1fs",
            QUIESCENCE_ENV, raw, CANCEL_QUIESCENCE_TIMEOUT,
        )
        return CANCEL_QUIESCENCE_TIMEOUT
    return value


@dataclass(frozen=True)
class QuiescenceReport:
    """What the wait observed — immutable, so a caller cannot rewrite history."""

    settled: tuple[str, ...] = ()
    still_alive: tuple[str, ...] = ()
    elapsed: float = 0.0
    limit: float = 0.0

    @property
    def clean(self) -> bool:
        """True when nothing was still running when we stopped looking."""
        return not self.still_alive

    def clause(self) -> str:
        """The fault's quiescence clause — empty when there was nothing to wait
        for (a caller that appends it must not add empty parentheses)."""
        if not self.settled and not self.still_alive:
            return ""
        if self.clean:
            return f"settled in {self.elapsed:.1f}s"
        count = len(self.still_alive)
        noun = "leaf" if count == 1 else "leaves"
        return (
            f"{count} {noun} STILL RUNNING after {self.limit:.1f}s quiescence wait "
            f"— {_ALIVE_HINT}"
        )

    def suffix(self) -> str:
        """The whole parenthesis for a fault about ONE cancelled leaf.

        Always opens with the literal ``cancelled`` — the word the lifecycle
        tests and the failure taxonomy read — and only then says how it went."""
        clause = self.clause()
        return f"cancelled; {clause}" if clause else "cancelled"


def await_quiescence(
    core: Any,
    sub_ids: Iterable[str],
    *,
    timeout_s: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> QuiescenceReport:
    """Wait (briefly, once, for all of them) until the cancelled leaves are quiet.

    Uses the core's own per-sub-session wait (``collect(wait=True, timeout=…)``),
    which returns when the sub-session's turn has fully unwound — its completion
    hook included. A sub-session the core does not know, or whose collect blows
    up, counts as settled: there is nothing left to wait for, and a cleanup path
    must never be the thing that kills a run thread.

    The cap covers THIS call. Callers that collect leaf by leaf (the engine's
    scalar path, and every rigor node that loops over its leaves) therefore pay
    one cap per timed-out leaf; hand a LIST in wherever the leaves are already
    known together, as the pipeline barrier does.

    Deliberately NOT used by the quota/pause cancel path, nor by the pipeline's
    stranded-leaf path: both run on ``on_done`` workers, which must never block.
    """
    limit = quiescence_timeout() if timeout_s is None else max(0.0, float(timeout_s))
    start = clock()
    deadline = start + limit
    settled: list[str] = []
    alive: list[str] = []
    for sub_id in sub_ids:
        remaining = max(0.0, deadline - clock())
        try:
            out = core.collect(sub_id, wait=True, timeout=remaining)
        except Exception:  # a broken cleanup is not a broken run
            logger.exception("workflow: quiescence wait failed for leaf %s", sub_id)
            settled.append(sub_id)
            continue
        status = out.get("status") if isinstance(out, dict) else None
        if status == "running":
            alive.append(sub_id)
        else:
            settled.append(sub_id)
    return QuiescenceReport(
        settled=tuple(settled),
        still_alive=tuple(alive),
        elapsed=max(0.0, clock() - start),
        limit=limit,
    )
