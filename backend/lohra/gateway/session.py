"""GatewaySession — drives one agent turn and streams gateway events (spec §2).

This is the protocol heart: prompt.submit -> message.start -> message.delta* /
tool.start+tool.complete* -> message.complete. It is fully synchronous and
sink-agnostic (``emit`` is any callable taking a JSON-RPC frame), so the WS
transport can run it in a thread and forward frames, while tests drive it
directly with a collector.
"""

from __future__ import annotations

import itertools
import json
import threading
from typing import Any, Callable

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.gateway.events import event_frame
from lohra.state import SessionDB

Emit = Callable[[dict], None]
# (parent_session_id, agent, compressed_messages) -> child_session_id, or None
# when the fork was skipped (another process holds the compaction lock).
OnCompaction = Callable[[str, Agent, list[dict]], "str | None"]


class GatewaySession:
    """Bridges a persistent Agent + SessionDB to the gateway event stream."""

    def __init__(
        self,
        session_id: str,
        agent: Agent,
        db: SessionDB,
        *,
        on_compaction: OnCompaction | None = None,
        busy_lock: threading.Lock | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.db = db
        self._base_dispatch = agent.tool_dispatch
        self._on_compaction = on_compaction
        # A forked child reuses its parent's lock so two turns can never run on
        # the shared Agent at once (its system prompt/dispatch are not reentrant).
        self._busy = busy_lock or threading.Lock()
        # Steer inbox (orchestration §6): texts injected into a running turn,
        # drained between iterations. Owned here so both consumers — the core
        # (sub-sessions) and the WS handler (top-level) — steer the same way.
        self._inbox: list[str] = []
        self._inbox_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def interrupt(self) -> None:
        self.agent.request_interrupt()

    def enqueue_steer(self, text: str) -> None:
        """Queue a steer for the running turn (read before its next iteration)."""
        with self._inbox_lock:
            self._inbox.append(text)

    def drain_steers(self) -> list[str]:
        """Pop all queued steers (empty list if none)."""
        with self._inbox_lock:
            if not self._inbox:
                return []
            texts = list(self._inbox)
            self._inbox.clear()
            return texts

    def submit(self, text: str, emit: Emit) -> dict[str, Any]:
        """Run one turn, streaming events to ``emit``. Rejects if already busy.

        The running turn drains this session's steer inbox between iterations. The
        atomic ``acquire`` below is the steer arbiter: a caller that gets
        ``{"busy": True}`` knows a turn is live and routes its text to the inbox
        (via ``enqueue_steer``) instead.
        """
        if not self._busy.acquire(blocking=False):
            emit(event_frame("error", self.session_id, {"message": "session busy"}))
            return {"busy": True}
        try:
            return self._run(text, emit)
        finally:
            self._busy.release()

    def _run(self, text: str, emit: Emit) -> dict[str, Any]:
        prior = self.db.load_messages(self.session_id)
        emit(event_frame("message.start", self.session_id, {}))

        if self._base_dispatch is not None:
            self.agent.tool_dispatch = self._wrap_dispatch(emit)

        def on_text(chunk: str) -> None:
            emit(event_frame("message.delta", self.session_id, {"text": chunk}))

        try:
            result = run_conversation(
                self.agent,
                text,
                conversation_history=prior,
                stream_delta_callback=on_text,
                inbox=self.drain_steers,
            )
        finally:
            # Restore the pristine dispatch so the agent never carries a stale
            # emit closure between turns (and so a fork can reuse it cleanly).
            self.agent.tool_dispatch = self._base_dispatch

        child_id = self._persist(result, prior, emit)

        if result["error"]:
            status = "error"
        elif result["interrupted"]:
            status = "interrupted"
        else:
            status = "complete"
        payload: dict[str, Any] = {
            "text": result["final_response"] or "",
            "status": status,
            "usage": {},
        }
        if result["error"]:
            payload["warning"] = result["error"]
        if child_id is not None:
            payload["child_session_id"] = child_id
        emit(event_frame("message.complete", self.session_id, payload))
        return result

    def _persist(self, result: dict, prior: list[dict], emit: Emit) -> str | None:
        """Persist a completed turn. Returns the child id when a lineage fork ran.

        A compacted turn rewrote the history, so it cannot be appended to the
        parent (that would break API alternation on resume). Instead the parent
        is closed and the full compressed transcript is forked into a child
        session — mirroring the `lohra chat` lineage split.
        """
        if result["error"] or result["interrupted"]:
            return None  # never persist a dangling user/tool message

        if result["compacted"] and self._on_compaction is not None:
            child_id = self._on_compaction(self.session_id, self.agent, result["messages"])
            if child_id is None:
                # Another process is forking this session; we can't append the
                # rewritten history to the parent without breaking alternation,
                # so drop this turn (the other process owns the canonical child).
                return None
            emit(
                event_frame(
                    "session.forked",
                    self.session_id,
                    {"parent_session_id": self.session_id, "child_session_id": child_id},
                )
            )
            return child_id

        for message in result["messages"][len(prior):]:
            self.db.save_message(self.session_id, message)
        self._record_session_cost(result)
        return None

    def _record_session_cost(self, result: dict) -> None:
        """Acumula o usage do turno na linha da sessão (mesmo contrato do CLI:
        preço do momento, fail-closed, nunca derruba o turno)."""
        from lohra.agent.session_cost import record_turn
        from lohra.memory.paths import lohra_home

        provider = getattr(getattr(self.agent, "provider", None), "name", "") or ""
        record_turn(
            self.db, self.session_id, result.get("usage_total"),
            provider=provider, model=self.agent.model, home=lohra_home(),
        )

    def _wrap_dispatch(self, emit: Emit) -> Callable[[str, dict], str]:
        base = self._base_dispatch
        assert base is not None
        sid = self.session_id
        counter = itertools.count(1)

        def wrapped(name: str, args: dict) -> str:
            tool_id = f"tool_{next(counter)}"
            emit(event_frame("tool.start", sid, {"tool_id": tool_id, "name": name, "args_text": json.dumps(args)}))
            result = base(name, args)
            emit(event_frame("tool.complete", sid, {"tool_id": tool_id, "name": name, "args": args, "result": result}))
            return result

        return wrapped
