"""OrchestrationCore — registry of sub-sessions + inbox + async collection.

A thin layer over GatewaySession. Each ``spawn`` builds an INDEPENDENT child
(isolated Agent via ``child_factory``), persists it with ``parent_session_id``,
and runs its turn on a capped thread pool — returning the ``sub_id`` immediately.
``steer`` injects text into a live sub-session (inbox, drained between
iterations); ``collect`` reads its status/output.

Locked decisions (spec §3): sub-sessions are independent (no shared busy-lock,
no compaction fork — `on_compaction=None`), concurrency is capped, and steer is
between-iterations only.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.state import SessionDB

logger = logging.getLogger(__name__)

ChildFactory = Callable[[], Agent]
DEFAULT_MAX_CONCURRENT = 4
# The in-memory registry is bounded so a long-lived dashboard doesn't leak a
# GatewaySession (+Agent+events) per spawn forever. Generous so normal resume
# flows never trip it; only TERMINAL sub-sessions are evicted (the DB row
# persists, so only in-memory resume/collect of an evicted child is lost).
DEFAULT_MAX_CHILDREN = 200
MAX_CAUSAL_HISTORY = 64
# A sub-session dropped from the queue before the pool ever started it. Distinct
# from "interrupted" (a turn that RAN and was stopped mid-flight) because the two
# cost different things: this one consumed no provider call at all, which is what
# lets an accounting layer tell "never happened" from "happened and was stopped".
CANCELLED = "cancelled"
# The statuses that say "this sub-session is executing NOTHING, and what
# ``collect`` reports about it is a total". Public because "did this leaf's bill
# land?" is a question consumers must ask before writing anything down about it:
# a status outside this set is work still in flight, never a total (the workflow
# engine's accounting, issue #42).
#
# A sub-session leaves this set exactly one way: another turn. Both paths that
# start one -- ``steer``'s submit and every iteration of ``_run``'s loop -- put
# the status back to ``running`` under the core lock, ATOMICALLY with the
# decision to run again (issue #60). So no reader ever sees a terminal status
# over meters that are still moving, and ``_evict_if_needed`` never mistakes a
# working sub-session for a settled one.
TERMINAL_STATUSES = frozenset({"complete", "error", "interrupted", CANCELLED})
_TERMINAL_STATUSES = TERMINAL_STATUSES  # legacy alias, this module's own callers

# Env vars to tune the limits (the CLI --max-parallel flag overrides the first).
ENV_MAX_PARALLEL = "LOHRA_MAX_PARALLEL"
ENV_MAX_SUBSESSIONS = "LOHRA_MAX_SUBSESSIONS"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("ignoring %s=%r: not an integer; using %d", name, raw, default)
        return default
    if value < 1:
        logger.warning("ignoring %s=%r: must be >= 1; using %d", name, raw, default)
        return default
    return value


def resolve_limits(*, max_parallel: int | None = None) -> tuple[int, int]:
    """Resolve (max_concurrent, max_children) from, in order: an explicit
    override (the CLI flag), the env vars, then the defaults. The project rule is
    that concurrency stays configurable but NEVER unbounded."""
    concurrent = (
        max_parallel
        if max_parallel is not None
        else _positive_int_env(ENV_MAX_PARALLEL, DEFAULT_MAX_CONCURRENT)
    )
    children = _positive_int_env(ENV_MAX_SUBSESSIONS, DEFAULT_MAX_CHILDREN)
    return max(1, concurrent), children


@dataclass
class _SubSession:
    sub_id: str
    session: GatewaySession
    parent_id: str | None
    # "running" also covers a turn QUEUED on the pool but not started yet: the
    # work is committed, so nothing about this sub-session is a total (#60).
    status: str = "running"  # running | complete | error | interrupted | cancelled
    output: str = ""
    # WHY the turn failed, when the kind is actionable (e.g. "quota_exhausted"),
    # plus the provider's retry-after hint. The output string alone is prose.
    error_kind: str | None = None
    retry_after: float | None = None
    # True once ANY turn of this sub-session finished through ``_finalize`` --
    # i.e. it reached a provider and its bill is on the books. What tells
    # "never ran" from "ran, then was stopped" when a LATER turn is dropped from
    # the pool queue: only the first may be called ``cancelled``, because that
    # status is what refunds a lifetime slot downstream (issue #60, F1).
    landed: bool = False
    future: Future | None = None
    on_done: "Callable[[str], None] | None" = None
    done_fired: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    # The cache/reasoning meters the transports already normalize (Fatia C).
    # ``tokens_in`` is the UNCACHED prompt in every provider, so the three are
    # disjoint and can be summed. Report-only: the budget still counts in+out.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # WHICH agent spent it — a price needs a (provider, model), and a leaf may
    # run on a different one than its parent (cross-provider delegation).
    provider: str | None = None
    model: str | None = None
    # True once two turns of this sub-session disagreed on the agent. Without it
    # ``(None, None)`` is ambiguous — "never attributed" and "attribution
    # withheld" look identical — and the NEXT turn would re-claim a total that
    # already spans two models.
    attribution_dropped: bool = False
    forced_fallback: bool = False
    # STICKY: one turn of this sub-session had its stream CLOSED mid-flight by
    # an interrupt (issue #42, épico E3), so the meters above are a FLOOR — the
    # provider may have billed everything it generated before the socket died,
    # and usage only ever arrives at the END of a stream. Sticky because the
    # totals accumulate across turns: a later turn reporting its usage cleanly
    # does not make the earlier gap known. Nothing here estimates the
    # difference — the flag travels, the numbers never move.
    usage_uncertain: bool = False
    # Sticky cancel flag. The agent's own interrupt flag is CONSUMED by the turn
    # it interrupts (``clear_interrupt`` at the end of run_conversation), so it
    # cannot tell ``_run`` that this sub-session is dead — this can.
    cancelled: bool = False
    # Gate for ``steer_active``: a steer that requires a LIVE sub-session is
    # accepted only while this is True. Flipped False wherever the turn can no
    # longer drain the inbox (finalize/except) or the sub-session is cancelled.
    accepting_steer: bool = True
    # Opaque workflow-owned identity. Orchestration transports it without
    # importing or interpreting the workflow schema (OBS-02).
    causal_context: Any | None = None
    causal_history: list[Any] = field(default_factory=list)
    causal_history_dropped: int = 0
    audit_tool_names: frozenset[str] = frozenset()


def _tool_names(agent: Agent) -> frozenset[str]:
    definitions = list(agent.tool_definitions)
    if isinstance(agent.forced_tool, dict):
        definitions.append(agent.forced_tool)
    names: set[str] = set()
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        name = definition.get("name")
        function = definition.get("function")
        if not isinstance(name, str) and isinstance(function, dict):
            name = function.get("name")
        if isinstance(name, str):
            names.add(name)
    return frozenset(names)


def _audit_safe_frame(
    frame: dict[str, Any], allowed: frozenset[str], agent: Any = None
) -> dict[str, Any]:
    """Mark only runtime-offered tool names as audit-safe identifiers, and name
    the agent that produced a turn boundary.

    ``model``/``provider`` are read off the LIVE agent at frame time (a
    ``configure`` hook may have swapped either before the turn), never off the
    sub-session's merged cost attribution — that one drops to ``None`` when a
    steered turn disagrees, which is exactly when the question is interesting.
    Both are configuration identity, not content; the sink bounds them."""
    params = frame.get("params") if isinstance(frame, dict) else None
    if not isinstance(params, dict):
        return frame
    kind = params.get("type")
    if kind in {"message.start", "message.complete"}:
        payload = params.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        named = {
            "_audit_model": getattr(agent, "model", None),
            "_audit_provider": getattr(getattr(agent, "provider", None), "name", None),
        }
        return {**frame, "params": {**params, "payload": {**payload, **named}}}
    if kind not in {"tool.start", "tool.complete"}:
        return frame
    payload = params.get("payload")
    if not isinstance(payload, dict):
        return frame
    safe_payload = dict(payload)
    name = safe_payload.get("name")
    if isinstance(name, str) and name in allowed:
        safe_payload["_audit_tool_name_known"] = True
    else:
        safe_payload.pop("name", None)
        safe_payload["_audit_tool_name_known"] = False
    return {**frame, "params": {**params, "payload": safe_payload}}


def _attribute(
    sub: _SubSession, model: str | None, provider: str | None
) -> tuple[str | None, str | None]:
    """(model, provider) for a sub-session whose cost is one running total.

    Unset -> take it; same agent again -> keep it; a different agent -> None for
    both, because one number cannot honestly carry two prices — and once dropped
    it stays dropped (``NodeCost.merge`` tells the two Nones apart by an empty
    ``usage``; here the tokens are already summed in, so the flag does it)."""
    if sub.attribution_dropped:
        return (None, None)
    if sub.model is None and sub.provider is None:
        return (model or sub.model, provider or sub.provider)
    if (model or sub.model, provider or sub.provider) == (sub.model, sub.provider):
        return (sub.model, sub.provider)
    sub.attribution_dropped = True
    return (None, None)


class OrchestrationCore:
    def __init__(
        self,
        db: SessionDB,
        child_factory: ChildFactory,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_children: int = DEFAULT_MAX_CHILDREN,
        event_sink: "Callable[[str, Any, dict[str, Any]], None] | None" = None,
    ) -> None:
        self._db = db
        self._child_factory = child_factory
        self._max = max(1, max_concurrent)
        self._max_children = max(1, max_children)
        # Optional observer for workflow audit. Frames are never retained here:
        # the core only transports opaque causal identity to a fail-isolated sink.
        self._event_sink = event_sink
        self._pool = ThreadPoolExecutor(max_workers=self._max, thread_name_prefix="orch")
        self._children: dict[str, _SubSession] = {}
        self._lock = threading.Lock()
        self._active = 0  # turns currently executing (for queue logging)

    # --- public API -----------------------------------------------------

    def spawn(
        self,
        prompt: str,
        *,
        parent_id: str | None = None,
        on_done: "Callable[[str], None] | None" = None,
        configure: "Callable[[Agent], None] | None" = None,
        causal_context: Any | None = None,
    ) -> str:
        """Create an independent sub-session, start its turn, return its id now.

        ``on_done(sub_id)`` (if given) is invoked exactly once when the
        sub-session reaches a terminal status — the non-blocking completion hook
        the pipeline scheduler chains stages off (spec §4.3). Usually that is the
        pool worker that ran the turn; for a sub-session dropped from the queue
        before it ever started it is instead the thread that cancelled it
        (``cancel``/``shutdown``), because no worker will ever run it. Either
        way it MUST NOT block on another orch task — and, since a caller's own
        thread may now run it, it must not block on anything slow at all.

        ``configure(agent)`` (if given) tweaks the freshly-built child before its
        turn — e.g. set ``forced_tool`` for a tool-less structured-output leaf
        (§5.2). Default None → byte-identical for orchestration/delegate."""
        sub_id = uuid4().hex
        agent = self._child_factory()
        if configure is not None:
            configure(agent)
        # on_compaction=None: a sub-session must NEVER fork-on-compaction into a
        # grandchild mid-run (its child agent also has no context_engine, so
        # compaction can't trigger — belt and suspenders).
        session = GatewaySession(sub_id, agent, self._db, on_compaction=None)
        self._db.create_session(
            sub_id,
            source="orchestration",
            model=agent.model,
            system_prompt=agent.system_prompt().text,
            parent_session_id=parent_id,
        )
        sub = _SubSession(
            sub_id=sub_id, session=session, parent_id=parent_id, on_done=on_done,
            causal_context=causal_context,
            causal_history=[causal_context] if causal_context is not None else [],
            audit_tool_names=_tool_names(agent),
        )
        with self._lock:
            self._evict_if_needed()
            self._children[sub_id] = sub
            if self._active >= self._max:
                logger.info(
                    "orchestration: queued sub-session %s (%d active >= cap %d)",
                    sub_id,
                    self._active,
                    self._max,
                )
            sub.future = self._pool.submit(self._run, sub_id, prompt)
        return sub_id

    def steer(
        self, sub_id: str, text: str, *, causal_context: Any | None = None
    ) -> dict[str, Any]:
        """Inject ``text`` into a sub-session: into the live turn's inbox if a turn
        is running (or about to), else as a fresh turn."""
        sub = self._get(sub_id)
        if sub is None:
            return {"error": f"no sub-session {sub_id!r}"}
        # Decide under the lock so two concurrent steers (the model can emit
        # several tool calls in one turn) can't both start a turn and clobber
        # ``future`` — the loser would leave collect(wait) blocked on a future
        # that finished early. An in-flight (submitted-but-not-done) future
        # counts as running, so the loser routes to the inbox instead.
        with self._lock:
            # A cancelled sub-session stays dead. Read the sticky flag as late as
            # possible — right at the submit decision — because cancel() sets it
            # without this lock (WF-19).
            if sub.cancelled:
                return {"error": f"sub-session {sub_id!r} was cancelled"}
            in_flight = sub.future is not None and not sub.future.done()
            if sub.session.busy or in_flight:
                # Enqueue under the core lock too: a steer that loses the
                # submit race lands in the inbox atomically with the decision.
                sub.session.enqueue_steer(text)
                queue_it = True
            else:
                if causal_context is not None:
                    sub.causal_context = causal_context
                    sub.causal_history.append(causal_context)
                    excess = len(sub.causal_history) - MAX_CAUSAL_HISTORY
                    if excess > 0:
                        del sub.causal_history[:excess]
                        sub.causal_history_dropped += excess
                # Accepting again: this fresh turn will drain the inbox, so
                # steer_active may target this sub-session from now on.
                sub.accepting_steer = True
                # ...and it is RUNNING again, atomically with the submit (issue
                # #60): the previous turn's terminal status -- and its meters --
                # must never be read as a total for work already committed, not
                # even in the window before the pool picks the future up.
                # ``done_fired`` goes with it: the hook of the turn that just
                # ended was CONSUMED when it fired, so this turn is free to arm
                # a fresh one (``watch_done``). AFTER the submit on purpose: a
                # pool that refuses the work (shut down) must leave the terminal
                # status standing rather than a sub-session that says "running"
                # forever and can never be evicted. Nobody observes the order --
                # the worker's own loop-top waits on this very lock.
                sub.future = self._pool.submit(self._run, sub_id, text)
                sub.status = "running"
                sub.done_fired = False
                queue_it = False
        if queue_it:
            return {"ok": True, "queued": True}
        return {"ok": True, "queued": False}

    def steer_active(
        self,
        sub_id: str,
        text: str,
        *,
        causal_context: Any | None = None,
        expected_causal: Any | None = None,
        on_settle: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Inject ``text`` into a sub-session that must ALREADY be active.

        Unlike ``steer``, this NEVER starts a new turn: unknown, cancelled and
        not-accepting sub-sessions are refused. The whole decision -- including
        the ``enqueue_steer`` -- happens under ``self._lock`` so a steer cannot
        slip into a turn that is finalizing: whoever ends the turn flips
        ``accepting_steer`` under the SAME lock, so a steer is either accepted
        into the inbox and later READ, or refused, or accepted and later
        DISCARDED (a cancel drops the inbox). Acceptance therefore promises
        exactly one settle outcome -- never delivery of the text itself.

        ``expected_causal`` closes the snapshot-to-enqueue race: when supplied,
        the current occurrence must still match under this same lock.

        ``on_settle(outcome)`` (if given) fires exactly once with ``'read'``
        (``drain_steers`` delivered it) or ``'discarded'``
        (``discard_steers`` dropped it).
        """
        with self._lock:
            sub = self._children.get(sub_id)
            if sub is None:
                return {"error": f"no sub-session {sub_id!r}"}
            if sub.cancelled:
                return {"error": f"sub-session {sub_id!r} was cancelled"}
            if not sub.accepting_steer:
                return {"error": f"sub-session {sub_id!r} is not active"}
            if expected_causal is not None and sub.causal_context != expected_causal:
                return {"error": f"sub-session {sub_id!r} causal occurrence changed"}
            if causal_context is not None:
                sub.causal_context = causal_context
                sub.causal_history.append(causal_context)
                excess = len(sub.causal_history) - MAX_CAUSAL_HISTORY
                if excess > 0:
                    del sub.causal_history[:excess]
                    sub.causal_history_dropped += excess
            sub.session.enqueue_steer(text, on_settle=on_settle)
        return {"ok": True, "queued": True}

    def collect(
        self, sub_id: str, *, wait: bool = False, timeout: float | None = None
    ) -> dict[str, Any]:
        """Read a sub-session's status/output. ``wait`` blocks for the current turn."""
        sub = self._get(sub_id)
        if sub is None:
            return {"error": f"no sub-session {sub_id!r}"}
        if wait and sub.future is not None:
            try:
                sub.future.result(timeout=timeout)
            except Exception:
                pass  # timeout or turn error — reflected in status/output below
        return {
            "status": sub.status,
            "output": sub.output,
            "tokens_in": sub.tokens_in,
            "tokens_out": sub.tokens_out,
            "cache_read_tokens": sub.cache_read_tokens,
            "cache_write_tokens": sub.cache_write_tokens,
            "reasoning_tokens": sub.reasoning_tokens,
            "provider": sub.provider,
            "model": sub.model,
            "forced_fallback": sub.forced_fallback,
            # The four meters above are a FLOOR when this is True (issue #42):
            # a stream the interrupt closed never delivered its usage.
            "usage_uncertain": sub.usage_uncertain,
            "error_kind": sub.error_kind,
            "retry_after": sub.retry_after,
        }

    def watch_done(self, sub_id: str, callback: "Callable[[str], None]") -> bool:
        """Install ``callback`` as this sub-session's completion hook AFTER the
        fact — for a consumer that spawned without one and only later discovered
        it needs telling when the turn finally lands (the workflow engine's
        timed-out leaf, whose bill does not exist until it settles, issue #42).

        Returns True iff THIS callback was installed and will fire. False means
        "decide now, nothing is going to call you": the sub-session is unknown,
        already terminal — which since issue #60 really does mean idle, because a
        steered sub-session is back to ``running`` before its next turn starts,
        so an install DURING that turn is accepted and fires at the end of it —
        or already owns a hook, and clobbering the pipeline's ``on_done`` would
        strand the item it chains.

        One hook per turn-SERIES: it fires exactly once when the submitted series
        lands (a turn that drains its own inbox into another iteration is still
        one series, hooked once at the end) and is consumed as it fires, leaving
        the seam free for whatever the next steer starts.

        Claimed under the same lock ``_fire_done`` claims the hook with, and the
        status it reads is always set BEFORE that call (``_finalize`` /
        ``_settle_dropped``), so the install either wins and the callback runs,
        or loses to a terminal status the caller can read for itself. The
        callback inherits the whole ``on_done`` contract: exactly once, on
        whatever thread settled the turn, and it MUST NOT block."""
        sub = self._get(sub_id)
        if sub is None:
            return False
        with self._lock:
            if sub.status in TERMINAL_STATUSES or sub.on_done is not None:
                return False
            sub.on_done = callback
            return True

    def causal_snapshot(self, sub_id: str) -> dict[str, Any] | None:
        """The workflow-owned identity of a sub-session, for the workflow only.

        Deliberately NOT part of ``collect()``: the two agent-facing tools splat
        that dict into ``tool_result(**out)`` and json.dumps it, so an opaque
        workflow object there is a hard TypeError — and three always-null keys
        in every ordinary collect even when it is not.

        ``steer`` updates these under the same lock, so they are snapshotted
        together: a concurrent reader never observes a latest context absent
        from its history (or vice versa). The value itself remains opaque.
        """
        sub = self._get(sub_id)
        if sub is None:
            return None
        with self._lock:
            return {
                "causal_context": sub.causal_context,
                "causal_history": tuple(sub.causal_history),
                "causal_history_dropped": sub.causal_history_dropped,
            }

    def list_children(self, parent_id: str) -> list[str]:
        with self._lock:
            return [s.sub_id for s in self._children.values() if s.parent_id == parent_id]

    def cancel(self, sub_id: str) -> dict[str, Any]:
        """Cancel a sub-session: drop it from the queue if not started, else
        cooperatively interrupt the running turn.

        "Cooperatively" now reaches INTO the provider round-trip on a streaming
        turn (issue #42, épico E3): the stream consumer reads the flag between
        events and closes the connection, so a leaf settles in the time of one
        event instead of however long the provider takes to finish generating.
        The bill for that closed stream is unknown — ``collect`` reports
        ``usage_uncertain`` for it. What still runs to the end: a non-streaming
        call, and a tool already in flight."""
        sub = self._get(sub_id)
        if sub is None:
            return {"error": f"no sub-session {sub_id!r}"}
        with self._lock:
            # Both flags under the lock: a steer_active racing the cancel sees
            # either the pre-cancel world (its steer accepted into the inbox) or
            # the refusal. A steer accepted just before the cancel MAY be
            # discarded by the cancelled turn -- never promise it will be read.
            # ``future.cancel`` stays OUTSIDE: ``_settle_dropped`` fires the
            # completion hook, which re-acquires this lock.
            sub.cancelled = True  # before the interrupt: no queued steer may relaunch it
            sub.accepting_steer = False
        if sub.future is not None and sub.future.cancel():
            # The turn will never run, so nothing will ever drain the inbox:
            # settle every accepted steer as 'discarded' BEFORE the completion
            # hook fires (outside the core lock — callbacks must not hold it).
            sub.session.discard_steers()
            # The pool will never run ``_run`` for this one, and ``_run`` is the
            # only other place that fires the completion hook — so fire it HERE
            # or the consumer chained off on_done waits out its own barrier for
            # a turn that is never going to happen (issue #8).
            self._settle_dropped(sub)
            return {"ok": True, "cancelled": "queued"}
        sub.session.interrupt()
        return {"ok": True, "cancelled": "running"}

    def shutdown(self, *, wait: bool = True) -> None:
        """Cancel every sub-session and tear down the pool.

        ``wait=True`` (default, process/run teardown) drains: it returns once the
        turns already running have finished. ``wait=False`` is the CANCEL path —
        it accepts no new work, drops everything still queued, and returns
        immediately; turns already inside a provider call keep draining in the
        background. The interrupt is cooperative, but since issue #42 (épico E3)
        a STREAMING turn honours it between events — it closes the stream rather
        than waiting for the provider to finish generating — so those drain in
        the time it takes to deliver one more event. A NON-streaming call, or a
        tool already in flight, still runs to completion.
        Either way, no sub-session starts new work after this returns.
        """
        self._stop_all()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
        if not wait:
            # ``cancel_futures`` drops the still-queued work items at the POOL
            # level, never touching ``cancel()`` — so fixing that method alone
            # still strands every leaf the cancel path throws away here. Settle
            # them through the same fire-once seam, AFTER the pool stopped
            # accepting work: an on_done that tries to re-spawn then gets a clean
            # refusal instead of a task the shutdown would silently drop next.
            #
            # Re-read ``_children`` rather than reusing the snapshot above: that
            # one was taken before the lock was released, and a ``spawn`` landing
            # in the window between it and the pool teardown submitted a task the
            # pool then dropped — absent from the old snapshot, so nobody marked
            # it terminal and nobody fired its hook (the hang, one race later).
            # A second sweep HERE is provably complete: the pool guards submit and
            # shutdown with one lock, so a later spawn cannot have been queued at
            # all — it is refused outright and its caller sees the refusal.
            for sub in self._stop_all():
                self._settle_dropped(sub)

    def _stop_all(self) -> list[_SubSession]:
        """Mark every sub-session dead and interrupt it; return the snapshot.

        Snapshot under the lock, interrupt outside it: ``_fire_done`` (which the
        callers reach next) takes the same non-reentrant lock."""
        with self._lock:
            children = list(self._children.values())
            for sub in children:
                sub.cancelled = True
                sub.accepting_steer = False
        for sub in children:
            # No turn can drain these inboxes anymore (flags already flipped
            # under the lock) — settle their steers as 'discarded', then
            # interrupt. Both outside the core lock: discard fires the steer
            # settle callbacks, which must never run under ``self._lock``.
            sub.session.discard_steers()
            sub.session.interrupt()
        return children

    # --- internals ------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Drop oldest SETTLED sub-sessions to keep the registry under cap.
        Called under ``self._lock``. A sub-session with work in flight is never
        evicted (so a registry full of live turns may briefly exceed the cap) —
        and "in flight" is TWO conditions, not one: the status says the last turn
        landed, AND the future confirms no worker still owns this sub-session,
        queued or running. Evicting a live leaf makes ``collect`` answer "no
        sub-session" to the very engine that spawned it (issue #60)."""
        if len(self._children) < self._max_children:
            return
        for sub_id, sub in list(self._children.items()):
            if len(self._children) < self._max_children:
                break
            if sub.status in _TERMINAL_STATUSES and (
                sub.future is None or sub.future.done()
            ):
                del self._children[sub_id]
                logger.info(
                    "orchestration: evicted terminal sub-session %s (registry at cap %d)",
                    sub_id,
                    self._max_children,
                )

    def _get(self, sub_id: str) -> _SubSession | None:
        with self._lock:
            return self._children.get(sub_id)

    def _observe(self, sub: _SubSession, frame: dict[str, Any]) -> None:
        if self._event_sink is None:
            return
        try:
            self._event_sink(
                sub.sub_id,
                sub.causal_context,
                _audit_safe_frame(
                    frame, sub.audit_tool_names, getattr(sub.session, "agent", None)
                ),
            )
        except Exception:  # observability must never change leaf semantics
            logger.exception("orchestration event sink failed for %s", sub.sub_id)

    def _run(self, sub_id: str, text: str) -> None:
        sub = self._get(sub_id)
        if sub is None:
            return
        with self._lock:
            self._active += 1
        try:
            current: str | None = text
            while current is not None:
                with self._lock:
                    # Running again from here: idempotent for a first turn,
                    # load-bearing for every turn after it (issue #60).
                    sub.status = "running"
                    sub.done_fired = False
                result = sub.session.submit(
                    current, lambda frame: self._observe(sub, frame)
                )
                if result.get("busy"):
                    # Lost the race to a concurrent turn — hand our text to its
                    # inbox so the live turn picks it up (no lost steer).
                    sub.session.enqueue_steer(current)
                    return
                # Finalize AND decide "still accepting?" atomically under the
                # core lock: a steer_active either lands before this point (and
                # is read or deliberately discarded below) or after the flip
                # (and is refused) -- never into an inbox nobody will look at
                # again (termination race). ``_finalize`` is inside the same
                # hold because the terminal status it writes and the decision to
                # run another turn are ONE step: a reader that polled in between
                # saw "complete" over a turn already committed (issue #60).
                with self._lock:
                    self._finalize(sub, result)
                    if sub.cancelled:
                        # A cancelled sub-session must stay dead: DISCARD the
                        # inbox outright. A steer queued before the cancel may
                        # be thrown away here, and relaunching a turn from it
                        # would resurrect exactly the work the caller stopped
                        # (WF-19).
                        sub.session.discard_steers()
                        current = None
                    else:
                        leftover = sub.session.drain_steers()
                        current = "\n".join(leftover) if leftover else None
                    if current is None:
                        sub.accepting_steer = False
                    else:
                        # Another turn, decided right here: back to running
                        # before any reader can look at this sub-session again.
                        sub.status = "running"
                        sub.done_fired = False
        except Exception as exc:
            # submit() persists to the DB outside run_conversation's error
            # handling, so an unexpected raise here would otherwise leave the
            # sub-session stuck "running" forever (collect swallows the future's
            # error). Mark it failed so collect reports the truth -- and dead
            # for steer_active: no inbox will ever be drained again.
            with self._lock:
                sub.status = "error"
                sub.output = f"{type(exc).__name__}: {exc}"
                sub.accepting_steer = False
            # The turn died mid-flight: its inbox will never be drained —
            # settle accepted steers as 'discarded' (outside the core lock).
            sub.session.discard_steers()
            self._observe(
                sub,
                {
                    "method": "event",
                    "params": {
                        "type": "message.complete",
                        "payload": {"status": "error"},
                    },
                },
            )
        finally:
            with self._lock:
                self._active -= 1
        # Fire the completion hook ONCE, outside the lock (the callback may
        # re-spawn → re-acquire the lock) and never on the busy-handoff return
        # above (the winning _run fires it).
        self._fire_done(sub)

    def _settle_dropped(self, sub: _SubSession) -> None:
        """Give a sub-session the pool dropped its terminal transition.

        Only for a future that was CANCELLED (never started): a running turn ends
        through ``_run`` like any other. The status is set BEFORE the hook fires,
        so a consumer whose on_done immediately reads ``collect()`` — the
        pipeline's ``_stage_done`` does exactly that — sees a terminal state
        rather than the constructor's optimistic "running".

        WHICH terminal status depends on the sub-session's past, not on this
        drop (issue #60, F1). ``CANCELLED`` is a claim about the whole
        sub-session — "it never reached a provider" — and downstream that claim
        REFUNDS a lifetime slot (``engine.account_leaf`` → ``Budget.refund``).
        A steered sub-session whose FIRST turn ran and billed would then mint
        lifetime out of a turn that never happened, which is the overrun the
        budget exists to prevent. So a sub-session that has landed a turn gets
        ``interrupted`` instead: refund-safe (only ``CANCELLED`` refunds),
        honest (something did stop it), and already administrative for
        ``leaf_retry``. Restoring the previous ``complete`` was the rejected
        alternative — it would hide the discarded turn from ``_cancel_inflight``
        and from anyone asking whether this leaf is still to be waited on.

        The write happens UNDER the lock, like every other status transition:
        the whole point of the fix around it is that no reader ever catches a
        status mid-change."""
        if sub.future is None or not sub.future.cancelled():
            return
        with self._lock:
            sub.status = "interrupted" if sub.landed else CANCELLED
        self._fire_done(sub)

    def _fire_done(self, sub: _SubSession) -> None:
        # Claim the hook UNDER the lock, invoke it outside (the same protocol
        # LeaseHeartbeat._tick uses). Three threads can reach this for one
        # sub-session now — its own pool worker, ``cancel()`` and
        # ``shutdown()`` — so the unlocked check-then-set this used to be is a
        # real double-fire window, and "exactly once" is the whole contract.
        # ...and CONSUME it: the hook belongs to the turn-series that just
        # landed, so clearing it here is what lets a steered sub-session arm a
        # fresh one for its next turn (``watch_done``) without ever running this
        # one twice (issue #60).
        with self._lock:
            if sub.on_done is None or sub.done_fired:
                return
            hook, sub.on_done = sub.on_done, None
            sub.done_fired = True
        try:
            hook(sub.sub_id)
        except Exception:
            logger.exception("orchestration: on_done callback failed for %s", sub.sub_id)

    @staticmethod
    def _finalize(sub: _SubSession, result: dict) -> None:
        # ``usage_total`` (every API call of the turn), not ``usage`` (the LAST
        # call): a turn with N tool round-trips really cost N calls, and charging
        # only the terminal one under-reported every sub-session that used tools.
        usage = result.get("usage_total") or result.get("usage")
        if usage is not None:
            sub.tokens_in += getattr(usage, "input_tokens", 0) or 0
            sub.tokens_out += getattr(usage, "output_tokens", 0) or 0
            sub.cache_read_tokens += getattr(usage, "cache_read_tokens", 0) or 0
            sub.cache_write_tokens += getattr(usage, "cache_write_tokens", 0) or 0
            sub.reasoning_tokens += getattr(usage, "reasoning_tokens", 0) or 0
        # Read off the live agent (defensively — a test double may be neither):
        # a configure hook can swap a leaf's model or provider before its turn.
        # The tokens above ACCUMULATE across turns, so the attribution follows
        # ``NodeCost.merge``: first turn sets it, an agreeing turn keeps it, a
        # DISAGREEING turn (a steered sub-session resumed on another model) drops
        # it to None. Pricing a two-model total at the last model's rate is money
        # for the wrong agent; withholding it keeps the tokens and drops the
        # dollars, which is the fail-closed half of this fatia.
        sub.landed = True  # this turn reached a provider: never "never ran"
        agent = getattr(sub.session, "agent", None)
        model = getattr(agent, "model", None)
        provider = getattr(getattr(agent, "provider", None), "name", None)
        sub.model, sub.provider = _attribute(sub, model, provider)
        if result.get("forced_fallback"):
            sub.forced_fallback = True
        if result.get("usage_uncertain"):
            # Never reassigned to False (unlike ``error_kind`` below): a turn
            # whose stream was cut left a hole in THIS sub-session's running
            # total, and a later clean turn does not fill it. Uncertainty is
            # monotonic; the honest report is "part of this bill is unknown".
            sub.usage_uncertain = True
        # Always reassign (never only-on-error): a steered retry that succeeds
        # must not leave a stale quota kind on a now-complete sub-session.
        sub.error_kind = result.get("error_kind")
        sub.retry_after = result.get("retry_after")
        if result.get("error"):
            sub.status = "error"
            sub.output = result["error"]
        elif result.get("interrupted"):
            sub.status = "interrupted"
            sub.output = result.get("final_response") or ""
        else:
            sub.status = "complete"
            sub.output = result.get("final_response") or ""
