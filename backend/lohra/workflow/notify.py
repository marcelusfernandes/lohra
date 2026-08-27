"""Telling a session that its workflow run stopped for good (M6).

A run outlives the turn that launched it, so the agent that started one is not
sitting there waiting for it: the service fires a callback (wired to the owning
session's steer inbox) when the run reaches a terminal status. The shaping lives
here rather than in the service because it is pure — a status and a token count
in, one line an agent can read out.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# (run_id, status, one-line summary) — fired once when a run stops for good.
OnRunDone = Callable[[str, str, str], None]


def done_summary(*, run_id: str, status: str, name: str, spent: int) -> str:
    """One line, because it lands in an agent's turn: what ran, how it ended,
    what it cost."""
    return (
        f"workflow {name or 'workflow'} ({run_id[:8]}) finished: {status}, "
        f"spent {spent} tokens"
    )


def notify_done(
    callback: OnRunDone | None, *, run_id: str, status: str, name: str, spent: int
) -> None:
    """Fire the completion callback, once, for a run that really stopped.

    Skipped for a cancelled run: the agent asked for that stop, so telling it
    what it just did is noise in a turn it is already steering.

    The callback is somebody else's code running on the run thread — it must
    never be able to take the run down with it, and the run is already over by
    the time it fires, so swallowing the failure loses nothing but noise.
    """
    if callback is None or status == "cancelled":
        return
    summary = done_summary(run_id=run_id, status=status, name=name, spent=spent)
    try:
        callback(run_id, status, summary)
    except Exception:  # a broken sink is not a broken run
        logger.exception("workflow: on_run_done failed for run %s", run_id)
