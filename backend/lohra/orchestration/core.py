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
_TERMINAL_STATUSES = frozenset({"complete", "error", "interrupted"})

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
    events: list[dict] = field(default_factory=list)
    status: str = "running"  # running | complete | error | interrupted
    output: str = ""
    # WHY the turn failed, when the kind is actionable (e.g. "quota_exhausted"),
    # plus the provider's retry-after hint. The output string alone is prose.
    error_kind: str | None = None
    retry_after: float | None = None
    future: Future | None = None
    on_done: "Callable[[str], None] | None" = None
    done_fired: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    forced_fallback: bool = False
    # Sticky cancel flag. The agent's own interrupt flag is CONSUMED by the turn
    # it interrupts (``clear_interrupt`` at the end of run_conversation), so it
    # cannot tell ``_run`` that this sub-session is dead — this can.
    cancelled: bool = False


class OrchestrationCore:
    def __init__(
        self,
        db: SessionDB,
        child_factory: ChildFactory,
        *,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        max_children: int = DEFAULT_MAX_CHILDREN,
    ) -> None:
        self._db = db
        self._child_factory = child_factory
        self._max = max(1, max_concurrent)
        self._max_children = max(1, max_children)
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
    ) -> str:
        """Create an independent sub-session, start its turn, return its id now.

        ``on_done(sub_id)`` (if given) is invoked exactly once, from the pool
        worker, when the sub-session reaches a terminal status — the non-blocking
        completion hook the pipeline scheduler chains stages off (spec §4.3).
        It runs on an orch worker, so it MUST NOT block on another orch task.

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
        sub = _SubSession(sub_id=sub_id, session=session, parent_id=parent_id, on_done=on_done)
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

    def steer(self, sub_id: str, text: str) -> dict[str, Any]:
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
                queue_it = True
            else:
                sub.future = self._pool.submit(self._run, sub_id, text)
                queue_it = False
        if queue_it:
            sub.session.enqueue_steer(text)
            return {"ok": True, "queued": True}
        return {"ok": True, "queued": False}

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
            "forced_fallback": sub.forced_fallback,
            "error_kind": sub.error_kind,
            "retry_after": sub.retry_after,
        }

    def list_children(self, parent_id: str) -> list[str]:
        with self._lock:
            return [s.sub_id for s in self._children.values() if s.parent_id == parent_id]

    def cancel(self, sub_id: str) -> dict[str, Any]:
        """Cancel a sub-session: drop it from the queue if not started, else
        cooperatively interrupt the running turn."""
        sub = self._get(sub_id)
        if sub is None:
            return {"error": f"no sub-session {sub_id!r}"}
        sub.cancelled = True  # before the interrupt: no queued steer may relaunch it
        if sub.future is not None and sub.future.cancel():
            sub.status = "interrupted"
            return {"ok": True, "cancelled": "queued"}
        sub.session.interrupt()
        return {"ok": True, "cancelled": "running"}

    def shutdown(self, *, wait: bool = True) -> None:
        """Cancel every sub-session and tear down the pool.

        ``wait=True`` (default, process/run teardown) drains: it returns once the
        turns already running have finished. ``wait=False`` is the CANCEL path —
        it accepts no new work, drops everything still queued, and returns
        immediately; turns already inside a provider call keep draining in the
        background (interrupt is cooperative — it never aborts a call in flight).
        Either way, no sub-session starts new work after this returns.
        """
        with self._lock:
            children = list(self._children.values())
        for sub in children:
            sub.cancelled = True
            sub.session.interrupt()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)

    # --- internals ------------------------------------------------------

    def _evict_if_needed(self) -> None:
        """Drop oldest TERMINAL sub-sessions to keep the registry under cap.
        Called under ``self._lock``. Running sub-sessions are never evicted (so a
        registry full of running turns may briefly exceed the cap)."""
        if len(self._children) < self._max_children:
            return
        for sub_id, sub in list(self._children.items()):
            if len(self._children) < self._max_children:
                break
            if sub.status in _TERMINAL_STATUSES:
                del self._children[sub_id]
                logger.info(
                    "orchestration: evicted terminal sub-session %s (registry at cap %d)",
                    sub_id,
                    self._max_children,
                )

    def _get(self, sub_id: str) -> _SubSession | None:
        with self._lock:
            return self._children.get(sub_id)

    def _run(self, sub_id: str, text: str) -> None:
        sub = self._get(sub_id)
        if sub is None:
            return
        with self._lock:
            self._active += 1
        try:
            current: str | None = text
            while current is not None:
                result = sub.session.submit(current, sub.events.append)
                if result.get("busy"):
                    # Lost the race to a concurrent turn — hand our text to its
                    # inbox so the live turn picks it up (no lost steer).
                    sub.session.enqueue_steer(current)
                    return
                self._finalize(sub, result)
                leftover = sub.session.drain_steers()  # steers that landed after the last drain
                # A cancelled sub-session must stay dead: relaunching a whole turn
                # from a steer that was queued before the cancel resurrects exactly
                # the work the caller just stopped (WF-19).
                current = "\n".join(leftover) if leftover and not sub.cancelled else None
        except Exception as exc:
            # submit() persists to the DB outside run_conversation's error
            # handling, so an unexpected raise here would otherwise leave the
            # sub-session stuck "running" forever (collect swallows the future's
            # error). Mark it failed so collect reports the truth.
            sub.status = "error"
            sub.output = f"{type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._active -= 1
        # Fire the completion hook ONCE, outside the lock (the callback may
        # re-spawn → re-acquire the lock) and never on the busy-handoff return
        # above (the winning _run fires it).
        self._fire_done(sub)

    def _fire_done(self, sub: _SubSession) -> None:
        if sub.on_done is None or sub.done_fired:
            return
        sub.done_fired = True
        try:
            sub.on_done(sub.sub_id)
        except Exception:
            logger.exception("orchestration: on_done callback failed for %s", sub.sub_id)

    @staticmethod
    def _finalize(sub: _SubSession, result: dict) -> None:
        usage = result.get("usage")
        if usage is not None:
            sub.tokens_in += getattr(usage, "input_tokens", 0) or 0
            sub.tokens_out += getattr(usage, "output_tokens", 0) or 0
        if result.get("forced_fallback"):
            sub.forced_fallback = True
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
