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
from lohra.workflow.nodes import AGGREGATION_ELEMENT


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
    aggregates = aggregate_types(engine)
    hole = refs.first_aggregate_hole(template, context, aggregates)
    if hole is not None:
        source, index = hole
        kind = aggregates[source]
        engine.record_fault(
            f"{node_id}: upstream null inside ${{{source}}}[{index}] "
            f"(dead {AGGREGATION_ELEMENT[kind]} of {kind} {source!r})"
        )
        return None
    return prompt


def with_schema_hint(prompt: Any, schema: dict | None) -> str:
    """Nudge a schema'd leaf to emit clean JSON (lenient parsing is the safety
    net; this just cuts wasted retries)."""
    if schema is None:
        return str(prompt)
    return (
        f"{prompt}\n\nRespond with ONLY a JSON object matching this schema — no "
        f"prose, no markdown fences:\n{json.dumps(schema, ensure_ascii=False)}"
    )
