"""Cron job execution — run a due job as a fresh, isolated agent turn (spec §6).

The scheduler calls ``run_job(job)``; ``make_cron_runner`` builds that callback
from an agent factory (fresh Agent per run, no parent state) and reuses
``run_conversation``. A completed run is persisted as its own session (tagged
``source="cron"``) so the user can read what the job did. Tool-less by design —
exposing tools to unattended scheduled runs is a follow-up that needs guards.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation

logger = logging.getLogger(__name__)

AgentFactory = Callable[[], Agent]


def make_cron_runner(agent_factory: AgentFactory, *, db: Any = None) -> Callable[[dict], dict]:
    """Build a ``run_job(job)`` that runs the job's prompt to completion."""

    def run_job(job: dict) -> dict:
        agent = agent_factory()
        result = run_conversation(agent, job["prompt"])
        # run_conversation reports API failures in-band (not by raising), so
        # surface them here instead of silently dropping the run.
        if result["error"]:
            logger.warning("cron job %r run failed: %s", job.get("id"), result["error"])
        elif db is not None and not result["interrupted"]:
            _persist(db, job, agent.model, result)
        return result

    return run_job


def _persist(db: Any, job: dict, model: str, result: dict) -> None:
    from uuid import uuid4

    session_id = uuid4().hex
    db.create_session(
        session_id, source="cron", model=model, title=f"cron: {job.get('name')}"
    )
    for message in result["messages"]:
        db.save_message(session_id, message)
