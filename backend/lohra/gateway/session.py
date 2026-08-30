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
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.agent.notices_overlay import (
    DEAD_TURN_TTL_SECONDS,
    build_turn_notice,
    claim_lineage_notices,
    format_notice_overlay,
    lineage_owners,
)
from lohra.gateway.events import event_frame
from lohra.state import SessionDB

Emit = Callable[[dict], None]
# (parent_session_id, agent, compressed_messages) -> child_session_id, or None
# when the fork was skipped (another process holds the compaction lock).
OnCompaction = Callable[[str, Agent, list[dict]], "str | None"]
# Called with 'read' (drain_steers delivered it) or 'discarded'
# (discard_steers dropped it) when a steer reaches its end state.
OnSettle = Callable[[str], None]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Steer:
    """One queued steer — immutable text plus optional settle callback."""

    text: str
    on_settle: OnSettle | None = None


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
        self._inbox: list[_Steer] = []
        self._inbox_lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self._busy.locked()

    def interrupt(self) -> None:
        self.agent.request_interrupt()

    def enqueue_steer(self, text: str, on_settle: OnSettle | None = None) -> None:
        """Queue a steer for the running turn (read before its next iteration).

        ``on_settle`` (optional) fires exactly once when the text reaches its
        end state: ``'read'`` when :meth:`drain_steers` delivers it, or
        ``'discarded'`` when :meth:`discard_steers` drops it. Callbacks run
        outside the inbox lock and are fail-isolated: one that raises is
        logged and never affects other queued items or the delivery itself.
        """
        with self._inbox_lock:
            self._inbox.append(_Steer(text, on_settle))

    def drain_steers(self) -> list[str]:
        """Pop all queued steers (empty list if none); settles each as 'read'."""
        with self._inbox_lock:
            if not self._inbox:
                return []
            entries = self._inbox
            self._inbox = []
        # Snapshot+clear commit before delivery, and callbacks always fire
        # outside the lock so a slow/failing one cannot block steer producers
        # or deadlock the turn draining the inbox.
        self._settle(entries, "read")
        return [entry.text for entry in entries]

    def discard_steers(self) -> None:
        """Drop all queued steers without delivering them; settles 'discarded'.

        Unlike :meth:`drain_steers` this never hands the texts back — they
        are gone. Returns ``None``.
        """
        with self._inbox_lock:
            if not self._inbox:
                return
            entries = self._inbox
            self._inbox = []
        self._settle(entries, "discarded")

    def _settle(self, entries: list[_Steer], outcome: str) -> None:
        """Fire each entry's on_settle exactly once — lock-free, fail-isolated."""
        for entry in entries:
            if entry.on_settle is None:
                continue
            try:
                entry.on_settle(outcome)
            except Exception:
                logger.exception("steer on_settle callback failed (outcome=%s)", outcome)

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
        # SUP-05: notices duráveis do lineage entram como overlay request-facing
        # do turno. A claim é única por sessão (lease) — o token que permite o
        # ack pós-persistência ou o release em falha mora aqui.
        token, claimed = claim_lineage_notices(
            self.db.notices, lineage_owners(self.db, self.session_id)
        )
        overlay = format_notice_overlay(claimed)
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
                request_overlay=overlay,
            )
        except Exception:
            self._release_notices(token)
            raise
        finally:
            # Restore the pristine dispatch so the agent never carries a stale
            # emit closure between turns (and so a fork can reuse it cleanly).
            self.agent.tool_dispatch = self._base_dispatch

        try:
            committed, child_id = self._persist(result, prior, emit)
        except Exception:
            # Persistência quebrada = turno não chegou ao estado canônico: as
            # notices NÃO podem ser ackadas (at-least-once).
            self._release_notices(token)
            raise

        if result["error"] or result["interrupted"]:
            # Turno morto: nada foi persistido (regra preservada) — as notices
            # recebidas voltam a pendente e o fato do turno morto fica durável
            # para o próximo turno/processo (SUP-05).
            self._release_notices(token)
            self._publish_dead_turn_notice(result)
        elif not committed:
            # Turno "completo" mas DESCARTADO (outro processo é dono do child
            # canônico da compactação): não há persistência deste turno, então
            # as notices ficam pendentes para o próximo (at-least-once).
            self._release_notices(token)
        else:
            # Persistência canônica concluída (no child, quando houve fork):
            # só agora as notices podem ser ackadas.
            self._ack_notices(token)

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

    def _release_notices(self, token: str | None) -> None:
        """Devolve as notices do turno para pendente (at-least-once)."""
        if token is None:
            return
        try:
            self.db.notices.release(token)
        except Exception:  # noqa: BLE001 — release quebrado não mata o epílogo
            logger.exception("notice release failed (session=%s)", self.session_id)

    def _ack_notices(self, token: str | None) -> None:
        """Remove as notices entregues — APÓS persistência limpa/canônica."""
        if token is None:
            return
        try:
            self.db.notices.ack(token)
        except Exception:  # noqa: BLE001 — ack quebrado não mata o epílogo
            logger.exception("notice ack failed (session=%s)", self.session_id)

    def _publish_dead_turn_notice(self, result: dict) -> None:
        """Publica o fato operacional do turno morto (owner = esta sessão).

        É notice, não insight: contexto de retry do próximo turno/processo,
        nunca aprendizado atribuído a uma escolha da Lohra (SUP-05).
        """
        kind = result.get("error_kind")
        error = result.get("error")
        if result.get("interrupted") and not error:
            status = "interrupted"
        else:
            status = "interrupted (interrupt requested)" if result.get("interrupted") else "error"
        text = build_turn_notice(status=status, error=error, error_kind=kind)
        try:
            self.db.notices.publish(
                self.session_id, text, ttl_seconds=DEAD_TURN_TTL_SECONDS
            )
        except Exception:  # noqa: BLE001 — notice é best-effort, nunca derruba
            logger.exception("dead-turn notice publish failed (session=%s)", self.session_id)

    def _persist(self, result: dict, prior: list[dict], emit: Emit) -> tuple[bool, str | None]:
        """Persist a completed turn. Returns ``(committed, child_id)``.

        ``committed`` é False quando o turno NÃO chegou ao estado canônico
        (erro, interrupção ou fork perdido para outro processo) — o consumidor
        de notices usa isso para NÃO ackar. ``child_id`` é o filho do fork de
        compactação, quando houve.

        A compacted turn rewrote the history, so it cannot be appended to the
        parent (that would break API alternation on resume). Instead the parent
        is closed and the full compressed transcript is forked into a child
        session — mirroring the `lohra chat` lineage split.
        """
        if result["error"] or result["interrupted"]:
            # a MENSAGEM não persiste (alternância), mas o GASTO existiu
            self._record_session_cost(result)
            return (False, None)  # never persist a dangling user/tool message

        if result["compacted"] and self._on_compaction is not None:
            child_id = self._on_compaction(self.session_id, self.agent, result["messages"])
            if child_id is None:
                # Another process is forking this session; we can't append the
                # rewritten history to the parent without breaking alternation,
                # so drop this turn (the other process owns the canonical child).
                # O GASTO do turno existiu mesmo assim — registra no pai,
                # paridade com o CLI que perde o lock (achado 5, review sol).
                self._record_session_cost(result)
                return (False, None)
            emit(
                event_frame(
                    "session.forked",
                    self.session_id,
                    {"parent_session_id": self.session_id, "child_session_id": child_id},
                )
            )
            # O turno de compactação é o MAIS caro do ciclo (carrega o contexto
            # inteiro) — registra no filho, paridade com o run_chat (achado 1).
            self._record_session_cost(result, session_id=child_id)
            return (True, child_id)

        # Tudo-ou-nada (achado 1 do review SUP-05): um turno meio-persistido
        # quebra a alternância no resume e faria o dead-turn notice mentir.
        self.db.save_messages(self.session_id, result["messages"][len(prior):])
        self._record_session_cost(result)
        return (True, None)

    def _record_session_cost(self, result: dict, session_id: str | None = None) -> None:
        """Acumula o usage do turno na linha da sessão (mesmo contrato do CLI:
        preço do momento, fail-closed, nunca derruba o turno)."""
        from lohra.agent.session_cost import record_turn
        from lohra.memory.paths import lohra_home

        provider = getattr(getattr(self.agent, "provider", None), "name", "") or ""
        record_turn(
            self.db, session_id or self.session_id, result.get("usage_total"),
            provider=provider, model=self.agent.model, home=lohra_home(),
            api_calls=result.get("api_calls") or 1,
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
