"""Telling a session that its workflow run stopped for good (M6).

A run outlives the turn that launched it, so the agent that started one is not
sitting there waiting for it: the service fires a callback (wired to the
owning session's durable notice channel) when the run reaches a terminal
status. The shaping lives here rather than in the service because it is pure —
a status and a token count in, one line an agent can read out.

The OWNER is captured from the ``RunState`` at the moment the fenced terminal
write is accepted and passed WITH the callback (``owner, run_id, status,
summary``). A late ``service.run_owner`` lookup could answer with the
RECOVERING owner — a straggler draining its stretch would then publish its
summary over someone else's run.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

# (owner, run_id, status, one-line summary) — fired once when a run stops for
# good. ``owner`` is captured from the RunState when the fenced terminal write
# was accepted; sinks must not re-derive it from mutable service state.
OnRunDone = Callable[[str | None, str, str, str], None]


def done_summary(*, run_id: str, status: str, name: str, spent: int) -> str:
    """One line, because it lands in an agent's turn: what ran, how it ended,
    what it cost."""
    return f"workflow {name or 'workflow'} ({run_id[:8]}) finished: {status}, spent {spent} tokens"


def notify_done(
    callback: OnRunDone | None,
    *,
    owner: str | None,
    run_id: str,
    status: str,
    name: str,
    spent: int,
) -> None:
    """Fire the completion callback, once, for a run that really stopped.

    Skipped for a cancelled run: the agent asked for that stop, so telling it
    what it just did is noise in a turn it is already steering.

    The callback is somebody else's code running on the run thread — it must
    never be able to take the run down with it, and the run is already over by
    the time it fires, so swallowing the failure loses nothing but noise.

    Sinks receive ``(owner, run_id, status, summary)``. This internal callback
    has one unambiguous shape: treating a sink's own ``TypeError`` as an arity
    mismatch could execute a side-effecting callback twice.
    """
    if callback is None or status == "cancelled":
        return
    summary = done_summary(run_id=run_id, status=status, name=name, spent=spent)
    try:
        callback(owner, run_id, status, summary)
    except Exception:  # a broken sink is not a broken run
        logger.exception("workflow: on_run_done failed for run %s", run_id)
