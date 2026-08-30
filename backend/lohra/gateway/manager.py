"""SessionManager — owns the live GatewaySessions and the shared SessionDB.

The agent factory is injected so the WS app wires the real Anthropic client +
tools while tests pass a fake. It receives the session id: tools bound into the
agent's dispatch (run_workflow's owner, the orchestration parent) need to know
which session they belong to, and the agent is built before the session exists.
One Agent (and thus one frozen system prompt, Invariante #1) lives per session
for the manager's lifetime; persisted sessions are revived lazily on first
access.
"""

from __future__ import annotations

import threading
from typing import Callable
from uuid import uuid4

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.state import SessionDB
from lohra.state.compression_lock import compression_lock

AgentFactory = Callable[[str], Agent]


class SessionManager:
    def __init__(self, db: SessionDB, agent_factory: AgentFactory) -> None:
        self._db = db
        self._agent_factory = agent_factory
        self._sessions: dict[str, GatewaySession] = {}
        self._lock = threading.RLock()

    @property
    def db(self) -> SessionDB:
        return self._db

    def create_session(
        self,
        *,
        session_id: str | None = None,
        title: str | None = None,
        cwd: str | None = None,
    ) -> GatewaySession:
        sid = session_id or uuid4().hex
        with self._lock:
            existing = self._sessions.get(sid)
            if existing is not None:
                return existing
            agent = self._agent_factory(sid)
            if self._db.get_session(sid) is None:
                self._db.create_session(
                    sid,
                    source="gateway",
                    model=agent.model,
                    system_prompt=agent.system_prompt().text,
                    cwd=cwd,
                    title=title,
                )
            session = GatewaySession(sid, agent, self._db, on_compaction=self.fork_for_compaction)
            self._sessions[sid] = session
            return session

    def get(self, session_id: str) -> GatewaySession | None:
        """Return a live session, reviving a persisted one on first access."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
            row = self._db.get_session(session_id)
            if row is None:
                return None
            if row.get("end_reason") == "compression":
                # Forked away by a lineage split — its turns now live on a child.
                # The client is told the child id via the session.forked event;
                # refuse to revive the dead parent so no turn writes to it.
                return None
            session = GatewaySession(
                session_id,
                self._agent_factory(session_id),
                self._db,
                on_compaction=self.fork_for_compaction,
            )
            self._sessions[session_id] = session
            return session

    def fork_for_compaction(self, parent_id: str, agent: Agent, messages: list[dict]) -> str | None:
        """Lineage split after compaction: close the parent, persist the full
        compressed transcript into a new child session, and register it live.

        Guarded by the cross-process compaction lock so two processes can't fork
        the same session into divergent children; returns ``None`` (fork skipped)
        when another process holds the lock. The child reuses the parent's Agent —
        its frozen system prompt is still valid (Invariante #1); only the
        transcript changed. Mirrors the `lohra chat` lineage split.
        """
        # Acquire the cross-process lock OUTSIDE self._lock so a slow/contended
        # peer can never stall the whole SessionManager behind this wait.
        with compression_lock(self._db, parent_id) as acquired:
            if not acquired:
                return None  # another process is forking this session — back off
            with self._lock:
                parent_row = self._db.get_session(parent_id) or {}
                self._db.end_session(parent_id, "compression")
                child_id = uuid4().hex
                self._db.create_session(
                    child_id,
                    source="gateway",
                    model=agent.model,
                    system_prompt=agent.system_prompt().text,
                    parent_session_id=parent_id,
                    cwd=parent_row.get("cwd"),
                )
                # Tudo-ou-nada (achado 1 do review SUP-05): um child com
                # transcript parcial é uma linhagem quebrada por construção.
                self._db.save_messages(child_id, messages)
                # Evict the now-ended parent and hand the child the parent's busy
                # lock, so the shared Agent is never driven by two turns at once.
                # The child gets a fresh (empty) steer inbox — any steers pending
                # on the forked-away parent are dropped (a no-op until top-level
                # sessions are steerable in milestone B).
                parent_session = self._sessions.pop(parent_id, None)
                busy_lock = parent_session._busy if parent_session is not None else None
                self._sessions[child_id] = GatewaySession(
                    child_id,
                    agent,
                    self._db,
                    on_compaction=self.fork_for_compaction,
                    busy_lock=busy_lock,
                )
                return child_id

    def list_sessions(self) -> list[dict]:
        return self._db.list_sessions()

    def history(self, session_id: str) -> list[dict]:
        return self._db.load_messages(session_id)
