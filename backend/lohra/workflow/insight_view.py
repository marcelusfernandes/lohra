"""Structured evidence end to end (Wave 9 slice E2, issue #51).

``WorkflowService.recent_insights`` used to collapse every stored
candidate/insight row down to its ``summary`` string, discarding the causal
class the row already carries (``mechanism``, ``responsibility``,
``confidence``, ``status`` — computed once, at write time, by
``InsightStore.record``/``classify_failure``). This module is a pure
projection over rows already returned by ``InsightStore.list``: no new
storage, no new write path, nothing recomputed.

Two shapes come out of the same rows:

- :func:`project_insights` — the structured dict per row, for a
  programmatic consumer that wants to filter by ``responsibility`` (e.g.
  surface only ``agency``-caused candidates) instead of re-deriving the
  class from free text.
- :func:`render_insight_line` — the agent-facing text: the original
  ``summary`` prose, byte-for-byte unchanged, with a compact tag appended
  (``[responsibility · mechanism · confidence · status]``) so a tool result
  still reads as prose but the causal class is visible without an agent
  having to parse a structured dict out of it.

``hits`` (added by a parallel slice, E1, to ``workflow_insight_candidates``)
is OPTIONAL on both the input row and the projected dict: included only
when the row already carries it, never defaulted to 0 or omitted-as-if-zero
— the same "absence != zero" doctrine ``workflow/library.py`` already
applies to its own advisory counters (``artifact_divergences``,
``leaf_respawns``).
"""

from __future__ import annotations

from typing import Any

_STRUCTURED_FIELDS = ("summary", "mechanism", "responsibility", "confidence", "status")


def project_insights(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure projection: ``InsightStore.list`` row -> consumer-facing dict.

    Copies only fields already present on the row; adds nothing that was
    not already computed at write time. ``hits`` travels along only when
    the row has it.
    """
    projected = []
    for row in rows:
        item: dict[str, Any] = {field: row[field] for field in _STRUCTURED_FIELDS}
        if "hits" in row:
            item["hits"] = row["hits"]
        projected.append(item)
    return projected


def render_insight_line(insight: dict[str, Any]) -> str:
    """Agent-facing line: unchanged summary prose + a compact class tag.

    The tag format is fixed and always the same four fields, in the same
    order, regardless of whether ``hits`` is present — ``hits`` is for the
    structured consumer (:func:`project_insights`), not this prose line.
    """
    tag = (
        f"[{insight['responsibility']} · {insight['mechanism']} · "
        f"{insight['confidence']:.1f} · {insight['status']}]"
    )
    return f"{insight['summary']} {tag}"
