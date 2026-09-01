"""Structured JSON envelope for `lohra chat --json` (orchestration-friendly).

Turns one chat turn into a complete, parseable object so an orchestrating agent
(Codex, another Lohra, a script) can consume the result programmatically instead
of scraping human text: input/output, reasoning, tool calls WITH their results,
model, temperature, and token usage.
"""

from __future__ import annotations

from typing import Any

from lohra.agent.types import Usage


def build_envelope(
    prompt: str,
    result: dict,
    *,
    model: str | None,
    temperature: float | None,
    session_id: str,
    provider: str | None = None,
    workflows: list[dict] | None = None,
) -> dict:
    """A complete envelope for this turn. This turn's messages are derived as
    everything after the LAST user message — robust to preflight compaction
    rewriting ``result["messages"]`` (a positional slice would be wrong then).

    ``workflows`` (issue #47) is this turn's dynamic-workflow runs that ended
    ``paused`` or were still alive when the turn's ``WorkflowService`` shut
    down — see ``lohra.workflow.exit_report.collect_turn_workflows``. Omitted
    entirely when empty, so a turn that never touched a workflow gets the
    exact envelope it always did."""
    turn = _this_turn(result.get("messages") or [])
    envelope = {
        "session_id": session_id,
        "model": model,
        "temperature": temperature,
        "input": prompt,
        "output": result.get("final_response"),
        "reasoning": _reasoning(turn),
        "tool_calls": _tool_calls(turn),
        "usage": _usage(result.get("usage")),
        "usage_total": _usage(result.get("usage_total")),
        "cost": _cost(result.get("usage_total"), provider=provider, model=model),
        "stop_reason": _last_finish(turn),
        "completed": bool(result.get("completed")),
        "error": result.get("error"),
        "api_calls": result.get("api_calls"),
    }
    if workflows:
        envelope["workflows"] = workflows
    return envelope


def error_envelope(prompt: str, message: str, *, model: str | None = None, session_id: str = "") -> dict:
    """Same schema as build_envelope, for a failure BEFORE/around the turn — so
    --json mode always writes exactly one parseable object to stdout."""
    return {
        "session_id": session_id,
        "model": model,
        "temperature": None,
        "input": prompt,
        "output": None,
        "reasoning": None,
        "tool_calls": [],
        "usage": None,
        "usage_total": None,
        "cost": None,
        "stop_reason": None,
        "completed": False,
        "error": message,
        "api_calls": 0,
    }


def _this_turn(messages: list[dict]) -> list[dict]:
    """Messages after the last user turn = this turn's assistant + tool messages."""
    last_user = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user = i
    return messages[last_user + 1:]


def _reasoning(messages: list[dict]) -> str | None:
    parts = [m["reasoning"] for m in messages if m.get("role") == "assistant" and m.get("reasoning")]
    return "\n\n".join(parts) or None


def _tool_calls(messages: list[dict]) -> list[dict]:
    """Each tool call paired with its result (matched by tool_call_id)."""
    results = {m.get("tool_call_id"): m.get("content") for m in messages if m.get("role") == "tool"}
    calls: list[dict] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or ():
            fn = call.get("function") or {}
            cid = call.get("id")
            calls.append(
                {
                    "id": cid,
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments"),
                    "result": results.get(cid),
                }
            )
    return calls


def _last_finish(messages: list[dict]) -> str | None:
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("finish_reason"):
            return message["finish_reason"]
    return None


def _cost(usage_total: Any, *, provider: str | None, model: str | None) -> dict | None:
    """Estimated USD cost of the turn from its summed usage, or None when the
    (provider, model) has no list price — never a guess (see lohra.pricing)."""
    if not isinstance(usage_total, Usage) or not provider or not model:
        return None
    from lohra.pricing import estimate_cost

    estimate = estimate_cost(usage_total, provider=provider, model=model)
    return estimate.as_dict() if estimate is not None else None


def _usage(usage: Any) -> dict | None:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage  # already serialized
    if isinstance(usage, Usage):
        out = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        for field in ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
            value = getattr(usage, field)
            if value:
                out[field] = value
        return out
    return None
