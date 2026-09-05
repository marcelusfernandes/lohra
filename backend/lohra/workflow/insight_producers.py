"""Standalone insight-candidate producers used by ``WorkflowService.start``
(Wave 9, E1 part 2, issue #50).

Kept OUT of ``service.py`` on purpose: that module is already well past this
repo's file-size convention (200-400 lines typical, 800 max), and a new
trigger should mean a new small function here, not more weight on an already
oversized file. Every producer here is self-contained — it takes the
``InsightStore`` handle and the data the caller already computed, with no
dependency on ``WorkflowService`` internals — so a future trigger (the next
gatilho a coordinator decides IS safe to learn from) can land the same way.

Coordinator decision on issue #50 (2026-09-05, last comment): of the six
candidate causal triggers the census enumerated, only lint warnings are wired
here — the others (checkpoint rejection, required/completeness failure,
route_fault correction, aggregation holes) are attribution-indeterminate by
the taxonomy's own fail-closed design and must NOT be wired.
"""

from __future__ import annotations

import logging
from typing import Any

from lohra.workflow.failure_taxonomy import AGENCY_CONFIDENCE_MIN, SIGNAL_SPEC_SHAPE

logger = logging.getLogger(__name__)

# A lint warning is weaker evidence than an outright rejection:
# `_record_spec_candidate` (service.py) uses confidence 1.0 because the spec
# never ran at all. Here the spec DID run — the author's intent shipped, just
# with a shape the schema tolerates but the harness ignores — so the
# confidence sits exactly at the taxonomy's own floor for a learnable AGENCY
# verdict (`AGENCY_CONFIDENCE_MIN`), never inflated to match a hard refusal's
# certainty.
LINT_WARNING_CONFIDENCE = AGENCY_CONFIDENCE_MIN


def _first_sentence(message: str) -> str:
    """The lint message up to (and including) its first '. ' — lint.py's own
    messages never carry prompt text or free-form user content (only the
    rule's fixed didactic prose plus, separately, a ``node_id`` field this
    function never touches), so trimming to one sentence keeps the summary
    metadata-safe by construction, just shorter."""
    head, sep, _rest = message.partition(". ")
    return f"{head}." if sep else message


def record_lint_warning_candidates(insights: Any, warnings: list[dict[str, Any]]) -> None:
    """Record one candidate per DISTINCT lint rule among ``warnings``.

    The caller (``WorkflowService._start_unlocked``) gates this to the same
    ``agency_authored and explicit_spec`` provenance check
    ``_record_spec_candidate`` uses for a rejected spec — a resume replaying
    a persisted (not-currently-authored) spec, or an operator/test call
    without the explicit flag, must call this with nothing, not filter here.

    Two warnings sharing one rule (e.g. ``nested_id_type_ignored`` flagged on
    two different nodes of the same spec) are ONE lesson, not two: dedupe by
    rule happens here, before ``record`` is ever called for it. A repeat of
    the same rule across separate runs is a SECOND dedupe layer, done by
    the store itself (E1's structural fingerprint over
    ``(kind, responsibility, mechanism, signals)``) — this function does not
    need to know about that layer at all.

    Never raises: a broken store must not cost a run that is otherwise
    starting successfully. Each warning's record call is caught and logged
    independently, so one bad row never stops the rest."""
    seen_rules: set[str] = set()
    for warning in warnings:
        rule = str(warning.get("rule") or "")
        if not rule or rule in seen_rules:
            continue
        seen_rules.add(rule)
        message = str(warning.get("message") or "")
        try:
            insights.record(
                kind="candidate",
                status="lint_warning",
                mechanism="validation",
                signals=(SIGNAL_SPEC_SHAPE, f"rule:{rule}"),
                confidence=LINT_WARNING_CONFIDENCE,
                summary=(
                    "authored workflow spec accepted with a lint warning "
                    f"({rule}): {_first_sentence(message)}"
                ),
            )
        except Exception:
            logger.warning(
                "workflow: could not record lint-warning candidate for rule %r",
                rule,
                exc_info=True,
            )
