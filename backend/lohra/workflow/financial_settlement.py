"""Freeze this acquisition's financial row after drain, then replace it fenced.

Memory finalization, capture completeness and a successful durable commit are
different facts. A retry replaces the exact same complete row; it never repeats
an additive debit or adopts another acquisition's fence.
"""

from dataclasses import dataclass
import logging

from lohra.agent.types import Usage
from lohra.workflow.usage_ledger import AcquisitionUsageLedger, FinancialUsage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SpendSnapshot:
    token_budget: int | None
    usage: Usage  # all five cumulative meters, including the prior stretch
    overrun: int

    @property
    def tokens_spent(self) -> int:
        return self.usage.input_tokens + self.usage.output_tokens

    def write(self, db, run_id: str, fence: int | None) -> bool:
        return db.run_spend_put(
            run_id, self.token_budget, self.usage.input_tokens, self.usage.output_tokens,
            cache_read=self.usage.cache_read_tokens, cache_write=self.usage.cache_write_tokens,
            reasoning=self.usage.reasoning_tokens, fence=fence,
        )


@dataclass(frozen=True)
class Settlement:
    snapshot: SpendSnapshot | None
    committed: bool
    error: str | None = None


def capture(final: FinancialUsage, prior: Usage, ceiling: int | None, overrun: int) -> SpendSnapshot:
    return SpendSnapshot(
        ceiling,
        Usage(final.tokens_in, final.tokens_out,
              prior.cache_read_tokens + final.usage.cache_read_tokens,
              prior.cache_write_tokens + final.usage.cache_write_tokens,
              prior.reasoning_tokens + final.usage.reasoning_tokens),
        max(overrun, final.overrun),
    )


def settle_finances(
    db, run_id: str, ledger: AcquisitionUsageLedger, *, prior: Usage,
    ceiling: int | None, overrun: int, fence: int | None,
) -> Settlement:
    snapshot = None
    for _ in range(2):
        try:
            final = ledger.finalize()
            snapshot = capture(final, prior, ceiling, overrun)
            break
        except Exception:
            logger.exception("workflow: financial finalization failed for %s", run_id)
    if snapshot is None:
        return Settlement(None, False, "financial finalization failed; only the prior floor is durable")
    capture_error = "; ".join(ledger.errors) or None
    for _ in range(2):
        try:
            if not snapshot.write(db, run_id, fence):
                detail = "final financial write refused: acquisition fence changed"
                logger.error("workflow: %s for %s", detail, run_id)
                return Settlement(snapshot, False, detail)
            return Settlement(snapshot, True, capture_error)
        except Exception:
            logger.exception("workflow: final financial write failed for %s", run_id)
    return Settlement(snapshot, False, "final financial commit not confirmed; inspect durable spend before replay")
