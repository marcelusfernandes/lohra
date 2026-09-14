"""Prepare a new child and mark a rejected attempt without a Core lock.

Preparation metadata is retained for inspection, never an accepted running child.
No client is closed here: factories may share one, and nothing was submitted to it.
"""

from __future__ import annotations

import logging
from typing import Callable

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.state import SessionDB

logger = logging.getLogger(__name__)


def prepare_child(
    db: SessionDB, factory: Callable[[], Agent], sub_id: str,
    parent_id: str | None, configure: Callable[[Agent], None] | None,
) -> tuple[Agent, GatewaySession]:
    agent = factory()
    if configure is not None:
        configure(agent)
    # A sub-session must never fork-on-compaction into a grandchild. Its child
    # agent also has no context_engine, so compaction cannot trigger either.
    session = GatewaySession(sub_id, agent, db, on_compaction=None)
    db.create_session(
        sub_id, source="orchestration", model=agent.model,
        system_prompt=agent.system_prompt().text, parent_session_id=parent_id,
    )
    return agent, session


def reject_preparation(db: SessionDB, sub_id: str) -> None:
    """Only the newly prepared row; a failed cleanup never conceals the refusal."""
    try:
        db.end_session(sub_id, "spawn_rejected")
    except Exception:
        logger.warning("orchestration: could not mark rejected preparation %s", sub_id,
                       exc_info=True)
