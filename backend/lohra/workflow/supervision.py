"""Steering supervision: the heavy lift behind ``WorkflowService.steer``.

The service owns the gates that need its private registry (local non-fenced
lookup, liveness, core+engine in hand); this module owns everything a plain
function can hold: instruction validation, causal-identity checks, the
external steering budget, the settlement lifecycle and the injection itself.

``steer_live_run`` receives the live run state (core + engine) and an
``audit`` callback — identity metadata only, never the instruction text —
so the service keeps no steering logic of its own.

The external steering budget is DURABLE (``budget_store``) as well as
in-process: the run ceiling outlives a process handoff (WF-29), so the
per-process :class:`~lohra.workflow.steering.SteeringLimits` counts are
mirrored into the durable store. ``steer_live_run`` reserves in BOTH before
injecting, releases in both when a steer never lands, and reports the
durable count as the authoritative ``run_used``.
"""

from __future__ import annotations

import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Callable

from lohra.workflow.causality import CausalContext
from lohra.workflow.steering import MAX_EXTERNAL_STEERS_PER_RUN

if TYPE_CHECKING:  # pragma: no cover - import cycle exists only for types
    from lohra.workflow.service import RunState

MAX_STEER_CHARS = 4000

# audit(event_type, ctx, sub_id, data?) — identity metadata in, never text.
SteerAudit = Callable[[str, CausalContext, str, "dict[str, Any] | None"], None]

__all__ = ["MAX_STEER_CHARS", "steer_live_run"]


def steer_live_run(
    state: "RunState",
    sub_id: str,
    text: str,
    *,
    segment_id: str,
    attempt: int,
    turn: int,
    audit: SteerAudit,
    budget_store: Any | None = None,
) -> dict[str, Any]:
    """Validate, budget, inject and settle one external steer into a live run.

    The gates the service cannot see (text shape, causal identity, steering
    budget) fail closed with didactic errors. The reservation settles through
    the core's ``on_settle`` callback, which never waits on this thread: an
    outcome that fires before the steer is accepted parks under ``state_lock``
    and is emitted right after; a later one emits directly. Emissions are
    audit events only — identity metadata, never the instruction text.

    ``budget_store`` (optional) is the durable steering-budget store: the run
    ceiling is reserved there too, so the per-process ceiling cannot be
    circumvented by steering through another process. ``run_used`` on the
    receipts and the audit events reports the DURABLE count when a store is
    wired — the cross-process truth.
    """
    if not isinstance(segment_id, str) or not segment_id:
        return {"error": "segment_id must be a non-empty string"}
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        return {"error": "attempt must be a non-negative integer"}
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        return {"error": "turn must be a non-negative integer"}
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
    expected = (segment_id, attempt, turn)
    current = (ctx.segment_id, ctx.attempt, ctx.turn)
    if current != expected:
        return {
            "error": (
                f"sub-session {sub_id!r} occurrence changed: expected "
                f"segment={segment_id!r} attempt={attempt} turn={turn}, current "
                f"segment={ctx.segment_id!r} attempt={ctx.attempt} turn={ctx.turn}"
            ),
            "stale": True,
            "causal_segment_id": ctx.segment_id,
            "causal_attempt": ctx.attempt,
            "causal_turn": ctx.turn,
        }
    if ctx.run_id != state.run_id or ctx.segment_id != engine.segment_id:
        return {
            "error": f"sub-session {sub_id!r} does not belong to run "
            f"{state.run_id!r} segment {engine.segment_id!r}",
            "causal_run_id": ctx.run_id,
            "causal_segment_id": ctx.segment_id,
        }

    # Durable half of the external steering budget (the run ceiling outlives a
    # process handoff, WF-29). Reserved BEFORE the local one; on refusal the
    # per-leaf counters do not exist yet, so the receipt carries the durable
    # run count alone.
    durable_used: int | None = None
    if budget_store is not None:
        durable_ok, durable_used = budget_store.steering_reserve(
            state.run_id, limit=MAX_EXTERNAL_STEERS_PER_RUN
        )
        if not durable_ok:
            audit(
                "steering.exhausted",
                ctx,
                sub_id,
                data={
                    "reason": "run_limit",
                    "run_used": durable_used,
                },
            )
            return {
                "error": "steer refused: external steering budget exhausted",
                "exhausted": True,
                "reason": "run_limit",
                "run_used": durable_used,
            }

    reserve = engine.steering_limits.reserve_external(sub_id)
    if not reserve.accepted:
        if budget_store is not None:
            budget_store.steering_release(state.run_id)
        audit(
            "steering.exhausted",
            ctx,
            sub_id,
            data={
                "reason": reserve.reason,
                "leaf_used": reserve.leaf_used,
                "run_used": durable_used if durable_used is not None else reserve.run_used,
                "corrections_used": reserve.corrections_used,
            },
        )
        return {
            "error": "steer refused: external steering budget exhausted",
            "exhausted": True,
            "reason": reserve.reason,
            "leaf_used": reserve.leaf_used,
            "run_used": durable_used if durable_used is not None else reserve.run_used,
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
        settled = engine.steering_limits.settle_external(sub_id, outcome)
        # A discarded outcome returns the slot to the LOCAL budget; mirror the
        # release into the durable one so a steer that never landed consumes
        # nothing across processes either. No open local slot settled -> the
        # local budget never charged this steer -> the durable one must not.
        if outcome == "discarded" and settled and budget_store is not None:
            budget_store.steering_release(state.run_id)
        with state_lock:
            if not accepted:
                pending.append(outcome)
                return
        # Outside the lock: audit I/O never runs under state_lock.
        emit(outcome)

    out = core.steer_active(sub_id, text, expected_causal=ctx, on_settle=settle)
    if "error" in out:
        rolled_back = engine.steering_limits.rollback_external(sub_id)
        if budget_store is not None and rolled_back:
            budget_store.steering_release(state.run_id)
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
            "run_used": durable_used if durable_used is not None else reserve.run_used,
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
            "run_used": durable_used if durable_used is not None else reserve.run_used,
            "corrections_used": reserve.corrections_used,
        },
    }
