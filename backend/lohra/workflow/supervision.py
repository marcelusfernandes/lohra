"""Steering supervision: the heavy lift behind ``WorkflowService.steer``.

The service owns the gates that need its private registry (local non-fenced
lookup, liveness, core+engine in hand); this module owns everything a plain
function can hold: instruction validation, causal-identity checks, the
external steering budget, the settlement lifecycle and the injection itself.

``steer_live_run`` receives the live run state (core + engine) and an
``audit`` callback — identity metadata only, never the instruction text —
so the service keeps no steering logic of its own.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Callable

from lohra.workflow.causality import CausalContext

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only for types
    from lohra.workflow.service import RunState

MAX_STEER_CHARS = 4000

# audit(event_type, ctx, sub_id, data?) — identity metadata in, never text.
SteerAudit = Callable[[str, CausalContext, str, "dict[str, Any] | None"], None]

__all__ = ["MAX_STEER_CHARS", "steer_live_run"]


def steer_live_run(state: "RunState", sub_id: str, text: str, audit: SteerAudit) -> dict[str, Any]:
    """Validate, budget, inject and settle one external steer into a live run.

    The gates the service cannot see (text shape, causal identity, steering
    budget) fail closed with didactic errors. The reservation settles through
    the core's ``on_settle`` callback, which never waits on this thread: an
    outcome that fires before the steer is accepted parks under ``state_lock``
    and is emitted right after; a later one emits directly. Emissions are
    audit events only — identity metadata, never the instruction text.
    """
    if not isinstance(text, str) or not text.strip():
        return {"error": "steer instruction must be a non-empty string"}
    if len(text) > MAX_STEER_CHARS:
        return {"error": f"steer instruction too long ({len(text)} chars; max {MAX_STEER_CHARS})"}

    core = state.core
    engine = state.engine

    snapshot = core.causal_snapshot(sub_id)
    ctx = snapshot.get("causal_context") if snapshot else None
    if not isinstance(ctx, CausalContext):
        return {"error": f"sub-session {sub_id!r} has no causal identity in run {state.run_id!r}"}
    if ctx.run_id != state.run_id or ctx.segment_id != engine.segment_id:
        return {
            "error": f"sub-session {sub_id!r} does not belong to run "
            f"{state.run_id!r} segment {engine.segment_id!r}",
            "causal_run_id": ctx.run_id,
            "causal_segment_id": ctx.segment_id,
        }

    reserve = engine.steering_limits.reserve_external(sub_id)
    if not reserve.accepted:
        audit(
            "steering.exhausted",
            ctx,
            sub_id,
            data={
                "leaf_used": reserve.leaf_used,
                "run_used": reserve.run_used,
                "corrections_used": reserve.corrections_used,
            },
        )
        return {
            "error": "steer refused: external steering budget exhausted",
            "exhausted": True,
            "reason": reserve.reason,
            "leaf_used": reserve.leaf_used,
            "run_used": reserve.run_used,
            "corrections_used": reserve.corrections_used,
        }

    # Settlement lifecycle for the open reservation. The core may report the
    # outcome from ITS thread, before or after this thread regains control, so
    # every read/write of ``accepted``/``pending`` happens under ``state_lock``
    # and the callback never waits on this one.
    state_lock = threading.Lock()
    accepted = False
    pending: list[str] = []

    def emit(outcome: str) -> None:
        audit(f"steering.{outcome}", ctx, sub_id)

    def settle(outcome: str) -> None:
        engine.steering_limits.settle_external(sub_id, outcome)
        with state_lock:
            if not accepted:
                pending.append(outcome)
                return
        # Outside the lock: audit I/O never runs under state_lock.
        emit(outcome)

    out = core.steer_active(sub_id, text, on_settle=settle)
    if "error" in out:
        engine.steering_limits.rollback_external(sub_id)
        audit("steering.rejected", ctx, sub_id)
        return {
            "error": f"steer rejected by orchestration: {out.get('error')}",
            "rolled_back": True,
        }

    audit(
        "steering.accepted",
        ctx,
        sub_id,
        data={
            "leaf_used": reserve.leaf_used,
            "run_used": reserve.run_used,
            "corrections_used": reserve.corrections_used,
        },
    )
    with state_lock:
        accepted = True
        parked = list(pending)
        pending.clear()
    for outcome in parked:
        emit(outcome)

    return {
        "ok": True,
        "queued": out.get("queued", False),
        "identity": asdict(ctx),
        "receipts": {
            "kind": reserve.kind,
            "leaf_used": reserve.leaf_used,
            "run_used": reserve.run_used,
            "corrections_used": reserve.corrections_used,
        },
    }
