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


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def branch_prompt(template: Any) -> Any:
    """An agent-shaped entry (``{prompt: ...}``) or a bare prompt string."""
    if isinstance(template, dict):
        return template.get("prompt", "")
    return template


def strict_prompt(engine: Any, node_id: str, template: Any, context: dict[str, Any]) -> Any:
    """Resolve one leaf prompt, or record a fault and return None.

    An upstream null must never reach a leaf as the literal "null" — the model
    reads it as content. Callers treat None as "this leaf must not be spawned"."""
    prompt, missing = refs.resolve_strict(template, context)
    if missing is not None:
        engine.record_fault(f"{node_id}: upstream null: {missing}")
        return None
    if prompt is None:
        engine.record_fault(f"{node_id}: prompt resolved to null")
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
