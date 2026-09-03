"""What this turn's dynamic-workflow runs looked like at turn's end (issue #47).

``lohra chat --json`` is one-shot: the durable notice a paused run leaves for
its owner (``notify.py`` → ``equip.py``) is addressed to a NEXT turn that never
comes. That is the headless half of the bug — the other half is
``lohra workflow watch`` spinning forever on a run paused by budget, fixed in
``watch.py``. This module closes the CLI's half: it reads what this turn's
``WorkflowService`` is about to lose BEFORE ``cli.py`` calls ``shutdown()``
(which cancels every run still alive), so the ``--json`` envelope can say so
instead of going silent.

Reported only when there is something to say — a run left ``paused``, or a run
was still alive and is about to be cancelled by the turn ending. A turn whose
runs all reached an ordinary terminal status on their own reports nothing here,
which is what keeps the envelope's ``workflows`` field byte-identical-by-absence
for every turn that came before this one.

``pause_reason`` rides the SAME rollup ``workflow_status`` reads
(``WorkflowService.status`` → ``pause_fields``) — never a second
representation of why a run stopped.

Never raises: this reads run STATE, called from ``cli.py``'s ``finally``, right
before ``shutdown()`` and everything after it (``orchestration_core.shutdown()``,
``client_pool.close()``, ``db.close()``, signal-handler restoration...). A status
read that throws here must not skip all of that — so every per-run read is its
own failure domain, reported as an honest ``read_error`` entry rather than
losing the report or the turn.
"""

from __future__ import annotations

import logging
from typing import Any

from lohra.workflow.watch import TERMINAL

logger = logging.getLogger(__name__)


def collect_turn_workflows(service: Any) -> list[dict]:
    """One entry per run THIS service launched or resumed this turn, for the
    two cases a one-shot turn would otherwise report nothing about:

    - ``status: "paused"`` — the run stopped on its own; ``pause_reason`` says
      whether anything is coming back to it on its own (quota — but only while
      THIS process stays up to fire the timer; a one-shot turn's own exit
      cancels it right after, same as any other live run) or a human decision
      is the only remedy (budget, a checkpoint, an explicit pause, or a
      dead route). No
      ``resume_at``: any promise it makes dies with this process before it
      could fire, so carrying it here would read as a promise that isn't one;
    - the run was still ``running`` (or any other non-terminal, non-paused
      status) when read — reported as OBSERVED (``status`` unchanged) plus
      ``cancelled_on_exit: true``, never guessed forward as already
      ``"cancelled"``: the ``shutdown()`` call right after this one is what
      actually stops it, and by the time it runs the true outcome could in
      principle already be a clean finish that slipped in first. The flag
      alone is never a false fact — it says what is ABOUT to happen, not what
      already did.

    Runs this service never touched (another session's, read only through
    ``workflow_status``/``workflow_list``) are not this turn's to report on,
    so this reads ``service.own_run_ids()`` rather than the merged
    ``list_runs()`` view.

    Every per-run read is its own failure domain: a run whose status lookup
    raises is reported with ``read_error`` rather than aborting the whole
    collection (and, one layer up, the caller's cleanup)."""
    entries: list[dict] = []
    try:
        run_ids = list(service.own_run_ids())
    except Exception as exc:  # noqa: BLE001 - must never take the finally down
        logger.warning("workflow: could not list this turn's own runs: %s", exc)
        return entries
    for run_id in run_ids:
        try:
            info = service.status(run_id)
        except Exception as exc:  # noqa: BLE001 - see module docstring
            logger.warning("workflow: could not read run %s for the exit report: %s", run_id, exc)
            entries.append({"run_id": run_id, "status": "unknown", "read_error": str(exc)})
            continue
        if "error" in info:
            continue  # gone between the listing and this read — nothing to say
        status = info.get("status")
        if status == "paused":
            entry: dict[str, Any] = {"run_id": run_id, "status": status}
            reason = info.get("reason")
            if reason is not None:
                entry["pause_reason"] = reason
            entries.append(entry)
        elif status not in TERMINAL:
            entries.append({"run_id": run_id, "status": status, "cancelled_on_exit": True})
    return entries
