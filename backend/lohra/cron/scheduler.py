"""Cron scheduler — run due jobs on each tick (spec §6).

``tick`` is pure given an injected ``run_job`` callback and ``now``: it finds due
jobs, runs each (isolating failures), and marks every attempted job as run so a
broken job can't retry-storm. The ~60s loop + file lock is a thin wrapper around
``tick`` (``run_scheduler_loop``) that the dashboard starts in a background
thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from lohra.cron.schedule import is_due
from lohra.cron.store import CronStore

logger = logging.getLogger(__name__)

RunJob = Callable[[dict], Any]
TICK_SECONDS = 60.0


def tick(store: CronStore, run_job: RunJob, *, now: float) -> list[tuple[str, bool]]:
    """Run every due job once. Returns ``[(job_id, succeeded), ...]``.

    Every per-job step — the due check, the run, and the mark — is inside the
    try, so a malformed job (bad type/value, missing id) is skipped and logged
    rather than aborting the whole tick and re-raising every minute.
    """
    results: list[tuple[str, bool]] = []
    for job in store.list():
        try:
            if not is_due(job, now=now):
                continue
            run_job(job)
            ok = True
        except Exception as exc:  # one bad job must not stop the others
            logger.warning("cron job %r failed: %s", job.get("id"), exc)
            ok = False
        # Mark run even on failure: it fired, and an interval/cron job that never
        # marks would fire again every tick. Guard the id access too.
        job_id = job.get("id")
        if job_id is not None:
            store.mark_run(job_id, when=now)
            results.append((job_id, ok))
    return results


def run_scheduler_loop(  # pragma: no cover - background thread + sleep
    store: CronStore,
    run_job: RunJob,
    *,
    stop: threading.Event,
    interval: float = TICK_SECONDS,
) -> None:
    """Tick until ``stop`` is set. Runs in a daemon thread started by the gateway.

    Known limitation: a fixed-minute cron (``30 9 * * *``) can be missed if no
    tick lands in that wall-clock minute (drift, or a long blocking run). There
    is no catch-up; the minute-floor guard only prevents double-fire.
    """
    while not stop.is_set():
        try:
            tick(store, run_job, now=time.time())
        except Exception as exc:
            logger.warning("cron tick failed: %s", exc)
        stop.wait(interval)
