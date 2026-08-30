"""Steering budget for workflow runs — per leaf and per run.

Three hard ceilings, checked in this order:

- ``MAX_EXTERNAL_STEERS_PER_LEAF`` — external steers a single leaf may
  receive (refusal reason ``"leaf_limit"``);
- ``MAX_CORRECTIONS_PER_LEAF`` — total (external + internal) steers a
  single leaf may absorb; internal steers are the harness's own
  corrections (refusal reason ``"correction_limit"``);
- ``MAX_EXTERNAL_STEERS_PER_RUN`` — external steers across the whole
  run, all leaves combined. Internal steers never count against the
  run ceiling — a run at its external cap can still issue its own
  corrections (refusal reason ``"run_limit"``).

``reserve_external(sub_id)`` / ``reserve_internal(sub_id)`` return a
frozen :class:`SteeringReservation` that is both the decision (``accepted``,
``reason``) and the receipt (post-decision ``leaf_used``, ``run_used``,
``corrections_used``). ``rollback_external(sub_id)`` releases the one open
external reservation of a leaf: its leaf, corrections and run counters all
fall back — the slot returns to the budget. All state sits behind a lock;
the engine and the pipeline barrier touch this from different threads.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass

__all__ = [
    "MAX_CORRECTIONS_PER_LEAF",
    "MAX_EXTERNAL_STEERS_PER_LEAF",
    "MAX_EXTERNAL_STEERS_PER_RUN",
    "SteeringReservation",
    "SteeringLimits",
]

#: Total (external + internal) steers one leaf may absorb.
MAX_CORRECTIONS_PER_LEAF = 2

#: External steers one leaf may receive from outside the harness.
MAX_EXTERNAL_STEERS_PER_LEAF = 1

#: External steers across the whole run (internal never counts here).
MAX_EXTERNAL_STEERS_PER_RUN = 3


@dataclass(frozen=True)
class SteeringReservation:
    """Immutable decision + receipt for one steering-slot attempt."""

    accepted: bool
    reason: str | None  # None when accepted; else which ceiling refused
    kind: str  # "external" | "internal"
    sub_id: str
    leaf_used: int  # external count for this leaf, after the attempt
    run_used: int  # run-wide external count, after the attempt
    corrections_used: int  # external + internal for this leaf, after


@dataclass
class _LeafCounters:
    external: int = 0
    internal: int = 0
    corrections: int = 0
    open_serial: int | None = None  # the leaf's one open external slot


class SteeringLimits:
    """Thread-safe steering budget with explicit reservation decisions."""

    def __init__(
        self,
        *,
        max_corrections_per_leaf: int = MAX_CORRECTIONS_PER_LEAF,
        max_external_per_leaf: int = MAX_EXTERNAL_STEERS_PER_LEAF,
        max_external_per_run: int = MAX_EXTERNAL_STEERS_PER_RUN,
    ) -> None:
        self.lock = threading.Lock()
        self._max_corrections = max_corrections_per_leaf
        self._max_ext_leaf = max_external_per_leaf
        self._max_ext_run = max_external_per_run
        self._leaves: dict[str, _LeafCounters] = {}
        self._run_external = 0
        self._serials = itertools.count()

    # -- reservation -----------------------------------------------------

    def _leaf(self, sub_id: str) -> _LeafCounters:
        counters = self._leaves.get(sub_id)
        if counters is None:
            counters = _LeafCounters()
            self._leaves[sub_id] = counters
        return counters

    def reserve_external(self, sub_id: str) -> SteeringReservation:
        """Take one external steering slot for ``sub_id``.

        Never raises on exhaustion — the frozen decision carries the
        refusing ceiling in ``reason``; the caller must treat ``accepted
        is False`` as a hard stop, not a soft hint.
        """
        with self.lock:
            leaf = self._leaf(sub_id)
            if leaf.external >= self._max_ext_leaf:
                return self._refused("external", sub_id, leaf, "leaf_limit")
            if leaf.corrections >= self._max_corrections:
                return self._refused("external", sub_id, leaf, "correction_limit")
            if self._run_external >= self._max_ext_run:
                return self._refused("external", sub_id, leaf, "run_limit")
            leaf.external += 1
            leaf.corrections += 1
            self._run_external += 1
            leaf.open_serial = next(self._serials)
            return SteeringReservation(
                accepted=True,
                reason=None,
                kind="external",
                sub_id=sub_id,
                leaf_used=leaf.external,
                run_used=self._run_external,
                corrections_used=leaf.corrections,
            )

    def reserve_internal(self, sub_id: str) -> SteeringReservation:
        """Take one internal (harness-issued) steering slot for ``sub_id``.

        Bounded only by the per-leaf corrections ceiling — internal
        steers never touch the run's external budget.
        """
        with self.lock:
            leaf = self._leaf(sub_id)
            if leaf.corrections >= self._max_corrections:
                return self._refused("internal", sub_id, leaf, "correction_limit")
            leaf.internal += 1
            leaf.corrections += 1
            return SteeringReservation(
                accepted=True,
                reason=None,
                kind="internal",
                sub_id=sub_id,
                leaf_used=leaf.external,
                run_used=self._run_external,
                corrections_used=leaf.corrections,
            )

    def rollback_external(self, sub_id: str) -> bool:
        """Release the open external reservation of ``sub_id``.

        Returns the leaf's leaf/corrections/run counters to where they
        were before it. Idempotent: a leaf with no open external slot
        is a no-op returning ``False``.
        """
        with self.lock:
            leaf = self._leaves.get(sub_id)
            if leaf is None or leaf.open_serial is None:
                return False
            leaf.open_serial = None
            leaf.external = max(0, leaf.external - 1)
            leaf.corrections = max(0, leaf.corrections - 1)
            self._run_external = max(0, self._run_external - 1)
            return True

    # -- internals ---------------------------------------------------------

    def _refused(
        self, kind: str, sub_id: str, leaf: _LeafCounters, reason: str
    ) -> SteeringReservation:
        return SteeringReservation(
            accepted=False,
            reason=reason,
            kind=kind,
            sub_id=sub_id,
            leaf_used=leaf.external,
            run_used=self._run_external,
            corrections_used=leaf.corrections,
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        with self.lock:
            return (
                "SteeringLimits("
                f"run_external={self._run_external}/{self._max_ext_run}, "
                f"leaves={len(self._leaves)})"
            )
