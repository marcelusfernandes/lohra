"""Schema-forced leaf output (spec §5.1) — validate + steer-retry.

A node with a ``schema``/``schema_ref`` must produce JSON matching it. The leaf
keeps its full toolset (no tool stripping); the ENGINE validates the answer and,
on mismatch, steers a correction back into the same sub-session and re-awaits
(bounded). Persistent failure → the node resolves to ``None`` (fail-isolation).
"""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator

from lohra.workflow.jsonio import UNPARSEABLE, loads_lenient

MAX_VALIDATION_RETRIES = 2


def is_empty_output(value: Any) -> bool:
    """A leaf that came back ``complete`` having said nothing (WF-7).

    ``""`` is not an answer, but it is indistinguishable from one downstream: it
    passes every schema-less path, never counts as a null, and would be cached as
    a completion. Treat it as a RECOVERABLE failure instead. ``None`` is NOT
    empty here — that is a dead leaf, already reported with its own cause.
    """
    return isinstance(value, str) and not value.strip()


def parse_and_validate(output: Any, schema: dict) -> tuple[bool, Any, str]:
    """Return (ok, parsed_value, error_message). Tolerantly extracts JSON from a
    string (fenced or prose-wrapped) before validating."""
    value = output
    if isinstance(output, str):
        value = loads_lenient(output)
        if value is UNPARSEABLE:
            return False, None, "the answer is not valid JSON"
    try:
        errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    except Exception as exc:  # a malformed schema shouldn't crash the run
        return False, None, f"schema error: {exc}"
    if errors:
        detail = "; ".join(f"{list(e.path) or '<root>'}: {e.message}" for e in errors[:3])
        return False, None, detail
    return True, value, ""


STRUCTURED_OUTPUT_TOOL = "StructuredOutput"


def synthetic_structured_tool(schema: dict) -> dict:
    """An OpenAI-style tool whose params ARE the node schema — forcing it makes
    the tool arguments the typed answer (spec §5.2). Rides in tools=, never the
    system prompt."""
    return {
        "type": "function",
        "function": {
            "name": STRUCTURED_OUTPUT_TOOL,
            "description": "Return your final answer as this structured object.",
            "parameters": schema,
        },
    }


def extract_structured_call(tool_calls: Any, schema: dict) -> tuple[bool, Any, str]:
    """Find a StructuredOutput call in a response's tool_calls and validate its
    arguments. (False, None, reason) if absent — the provider ignored the forced
    tool_choice → caller falls back to the §5.1 text path (spec §5.3)."""
    for call in tool_calls or []:
        if getattr(call, "name", None) == STRUCTURED_OUTPUT_TOOL:
            return parse_and_validate(call.arguments, schema)
    return (False, None, "no StructuredOutput tool call (provider ignored tool_choice)")


def correction_prompt(schema: dict, error: str) -> str:
    """A steer message telling the leaf to fix its output to match the schema."""
    return (
        "Your previous answer did not match the required JSON schema.\n"
        f"Validation error: {error}\n"
        "Respond with ONLY a JSON object that matches this schema exactly:\n"
        f"{json.dumps(schema, ensure_ascii=False)}"
    )
