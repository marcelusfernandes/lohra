"""Leaf-prompt helpers shared by every node strategy.

Extracted from ``strategies.py`` so the gating node types (``gates.py``) can use
the same resolution rules without importing the module that owns the STRATEGIES
table — that direction would be a cycle. Pure helpers: they resolve a template
against the run context and record a fault through the engine, nothing else.
"""

from __future__ import annotations

import json
from typing import Any

from lohra.workflow import refs
from lohra.workflow.nodes import AGGREGATION_ELEMENT, AGGREGATION_RECORDS_DEATHS


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def branch_prompt(template: Any) -> Any:
    """An agent-shaped entry (``{prompt: ...}``) or a bare prompt string."""
    if isinstance(template, dict):
        return template.get("prompt", "")
    return template


def aggregate_types(engine: Any) -> dict[str, str]:
    """The running spec's aggregation nodes, id → type — validated at the
    boundary. A caller holding a stand-in engine (the cache preview, a test
    double) has no such view, and a guard that raised over its absence would
    turn a missing diagnosis into a dead node."""
    types = getattr(engine, "aggregate_types", None)
    return types if isinstance(types, dict) else {}


def aggregate_holes(engine: Any) -> dict[str, frozenset[int]]:
    """What each aggregation RECORDED about its own deaths, id → indices — same
    boundary validation, same reason (see ``aggregate_types``). Absent means
    "nothing was recorded", never "everything died"."""
    holes = getattr(engine, "aggregate_holes", None)
    return holes if isinstance(holes, dict) else {}


def first_aggregate_hole(
    engine: Any, template: Any, context: dict[str, Any]
) -> tuple[str, int, str] | None:
    """The first ``(node_id, index, node_type)`` under a whole-aggregation ref in
    ``template`` that is really a HOLE — or None.

    Two regimes, because two aggregations answer "is this ``None`` a death?"
    differently, and one answer for both would be wrong somewhere (issue #72,
    M1). A ``parallel`` branch is collected with NO schema, so a live branch
    cannot come back as ``None``: there the value IS the evidence. A ``pipeline``
    stage may declare a schema whose ROOT permits null, so its item settles
    ``None`` on an answer the author explicitly allowed — there only the indices
    the scheduler RECORDED as dead count, and a guard that read the value
    instead would tell the author their healthy pipeline had a dead item."""
    aggregates = aggregate_types(engine)
    recorded = aggregate_holes(engine)
    for source, index in refs.aggregate_ref_nulls(template, context, aggregates):
        kind = aggregates[source]
        if kind in AGGREGATION_RECORDS_DEATHS and index not in recorded.get(source, ()):
            continue  # a null the element was ALLOWED to answer, not a hole
        return source, index, kind
    return None


def strict_prompt(engine: Any, node_id: str, template: Any, context: dict[str, Any]) -> Any:
    """Resolve one leaf prompt, or record a fault and return None.

    An upstream null must never reach a leaf as the literal "null" — the model
    reads it as content. Callers treat None as "this leaf must not be spawned".

    Two shapes of the same hole (issue #72). A reference that resolves WHOLLY to
    None is the first; the second is a reference to the output of an aggregation
    node with a dead element in it — the list is not None, so it used to pass,
    and the reduce node read the gap as data (``null`` embedded, ``None`` when
    the whole-ref keeps the list's type). Only the TOP level of an aggregation is
    judged: see ``refs.first_aggregate_hole`` for why."""
    prompt, missing = refs.resolve_strict(template, context)
    if missing is not None:
        engine.record_fault(f"{node_id}: upstream null: {missing}")
        return None
    if prompt is None:
        engine.record_fault(f"{node_id}: prompt resolved to null")
        return None
    if refuse_aggregate_hole(engine, node_id, template, context):
        return None
    return prompt


def refuse_aggregate_hole(
    engine: Any, node_id: str, template: Any, context: dict[str, Any]
) -> bool:
    """Record the fault for the first aggregation hole under ``template`` — True
    means "this leaf must not be spawned".

    Its own function because the hole reaches a leaf by TWO doors: a prompt that
    interpolates the aggregation (``strict_prompt``) and a container field that
    fans out OVER it (``branches``/``attempts`` from a ref, where every entry is
    an inert literal and a dead one is stringified to "null"). One fault text for
    both, or the same defect would read as two different diagnoses."""
    hole = first_aggregate_hole(engine, template, context)
    if hole is None:
        return False
    source, index, kind = hole
    engine.record_fault(
        f"{node_id}: upstream null inside ${{{source}}}[{index}] "
        f"(dead {AGGREGATION_ELEMENT[kind]} of {kind} {source!r})"
    )
    return True


def refuse_aggregate_hole_deep(
    engine: Any, node_id: str, value: Any, context: dict[str, Any]
) -> bool:
    """``refuse_aggregate_hole`` over an AUTHORED structure, walking dicts and
    lists exactly as ``refs.resolve_value`` does.

    The third door (#72, M2): a ``workflow`` node's ``args`` is authored
    structure, not a prompt, and it is resolved with ``resolve_value`` — so
    ``args: {"parts": "${p}"}`` over a holed fan-out used to carry the hole into
    a nested run, where it becomes the CHILD's ``${args.parts}`` and no guard
    downstream can tell it came from a dead branch. ``any`` short-circuits, so
    one fault is recorded, not one per string."""
    if isinstance(value, str):
        return refuse_aggregate_hole(engine, node_id, value, context)
    if isinstance(value, list):
        return any(refuse_aggregate_hole_deep(engine, node_id, item, context) for item in value)
    if isinstance(value, dict):
        return any(
            refuse_aggregate_hole_deep(engine, node_id, item, context) for item in value.values()
        )
    return False


def with_schema_hint(prompt: Any, schema: dict | None) -> str:
    """Nudge a schema'd leaf to emit clean JSON (lenient parsing is the safety
    net; this just cuts wasted retries)."""
    if schema is None:
        return str(prompt)
    return (
        f"{prompt}\n\nRespond with ONLY a JSON object matching this schema — no "
        f"prose, no markdown fences:\n{json.dumps(schema, ensure_ascii=False)}"
    )
