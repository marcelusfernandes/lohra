"""Ephemeral acquisition usage, independent of functional seal and Core eviction.

The lock order is ledger -> Budget. No Core/result/SQLite lock spans these
operations. Normal accounting still debits immediately; after drain, finalization
applies only missing UUID deltas. It never edits an engine result or a cache.
"""

from dataclasses import dataclass
import logging
from threading import Lock

from lohra.agent.types import Usage
from lohra.workflow.budget import Budget
from lohra.workflow.usage_book import maximum, total

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinancialUsage:
    usage: Usage  # this acquisition's five-meter UUID union
    tokens_in: int  # seeded cumulative Budget totals
    tokens_out: int
    charges: int
    overrun: int
    errors: tuple[str, ...]


class AcquisitionUsageLedger:
    def __init__(self, budget: Budget) -> None:
        self._budget = budget
        self._lock = Lock()
        self._received: dict[str, Usage] = {}
        self._errors: list[str] = []
        self._final: FinancialUsage | None = None

    @property
    def errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._errors)

    def observe(self, sub_id: str, usage: Usage) -> None:
        """Stable fail-isolated Core sink; capture failure is never silent."""
        try:
            self.receive(sub_id, usage)
        except Exception as exc:
            with self._lock:
                self._errors.append(f"terminal usage capture failed: {type(exc).__name__}: {exc}")
            logger.exception("workflow: terminal usage capture failed for %s", sub_id)

    def _check_frozen(self, sub_id: str, usage: Usage) -> bool:
        if self._final is None:
            return False
        previous = self._budget.token_state().applied.get(sub_id, Usage())
        if maximum(previous, usage) != previous:
            detail = f"new terminal usage after financial freeze for {sub_id}"
            self._errors.append(detail)
            logger.error("workflow: %s", detail)
        return True

    def receive(self, sub_id: str, usage: Usage) -> None:
        with self._lock:
            if self._check_frozen(sub_id, usage):
                return
            self._received[sub_id] = maximum(self._received.get(sub_id, Usage()), usage)

    def apply(self, sub_id: str, usage: Usage) -> bool:
        """Accepted normal charge and canonical applied marker, atomically."""
        with self._lock:
            if self._check_frozen(sub_id, usage):
                return False
            return self._budget.apply_execution_usage(sub_id, usage)

    def _freeze(self) -> FinancialUsage:
        state = self._budget.token_state()
        ceiling = self._budget.token_budget
        return FinancialUsage(
            total(state.applied.values()), state.tokens_in, state.tokens_out, state.charges,
            max(0, state.tokens_in + state.tokens_out - ceiling) if ceiling is not None else 0,
            tuple(self._errors),
        )

    def finalize(self) -> FinancialUsage:
        """Idempotent after partial application or an ambiguous caller failure.

        Applied-only UUIDs already belong to the canonical Budget book. Received
        UUIDs are folded cumulatively into that book; a failure halfway through
        retains every committed marker. Repeated/concurrent calls retry only
        missing deltas and publish the same immutable snapshot after success.
        """
        with self._lock:
            if self._final is None:
                for sub_id, usage in self._received.items():
                    self._budget.apply_execution_usage(sub_id, usage)
                self._final = self._freeze()
            return self._final
