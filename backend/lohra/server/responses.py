"""OpenAI Responses API shapes (pure, no network).

``/v1/responses`` is OpenAI's newer surface: the request carries ``input``
(string or items) + ``instructions``, and the response is a ``response`` object
with an ``output`` array of message items. It runs on the same agent turn as
chat completions — only the request parsing and the wire format differ.

The objects and events here are shaped to validate against the official
``openai`` SDK's pydantic models (Response, ResponseTextDeltaEvent, ...): every
SDK-required field is present, streaming carries ``sequence_number`` and the
output-item/content-part ``added`` events the SDK's stream consumer indexes
into, and the event sequence is created -> output_item.added ->
content_part.added -> output_text.delta* -> completed.
"""

from __future__ import annotations

import json
from typing import Any

from lohra.server.format import CompletionError

_TEXT_PART_TYPES = ("input_text", "output_text", "text")


def _content_text(content: Any) -> str:
    """A content value (string or list of parts) -> plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in _TEXT_PART_TYPES
        ]
        return "".join(parts)
    return ""


def parse_responses_input(input_value: Any, instructions: str | None) -> list[dict]:
    """Responses ``input`` + ``instructions`` -> internal messages list."""
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})

    if isinstance(input_value, str):
        if not input_value:
            raise CompletionError("'input' must not be empty")
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        if not input_value:
            raise CompletionError("'input' must not be empty")
        for item in input_value:
            if not isinstance(item, dict) or "role" not in item:
                raise CompletionError("each input item needs a 'role' and 'content'")
            messages.append({"role": item["role"], "content": _content_text(item.get("content"))})
    else:
        raise CompletionError("'input' must be a string or a list of items")
    return messages


def _message_item(response_id: str, content: str, status: str, parts: list[dict] | None) -> dict:
    return {
        "type": "message",
        "id": f"msg_{response_id}",
        "status": status,
        "role": "assistant",
        "content": [dict(part, **({"annotations": []} if part["type"] == "output_text" else {}))
                    for part in parts] if parts else [{"type": "output_text", "text": content, "annotations": []}],
    }


def _detail(usage: dict, group: str, name: str) -> int:
    """One nested usage detail, or 0. Total by construction: an older caller
    (or the estimate path) simply has no details, and 0 is the truth there."""
    details = usage.get(group)
    value = details.get(name) if isinstance(details, dict) else None
    return int(value or 0)


def build_response_object(
    *,
    response_id: str,
    model: str,
    content: str | None,
    status: str,
    usage: dict[str, Any] | None,  # None: interrupted with no observed usage
    created: int,
    error: dict | None = None,
    incomplete_details: dict | None = None,
    output_parts: list[dict] | None = None,
    lohra_usage: dict | None = None,
    native_outcome: dict | None = None,
) -> dict[str, Any]:
    """Build the OpenAI ``response`` object (validates against the SDK Response)."""
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": [_message_item(response_id, content or "", "incomplete" if status == "incomplete" else
                                 "completed", output_parts)] if content or output_parts or status == "completed" else [],
        "output_text": ("".join(p.get("text", "") for p in output_parts) if output_parts else content or ""),
        "error": error,
        "incomplete_details": incomplete_details,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": None if usage is None else {
            "input_tokens": usage["prompt_tokens"],
            # The REAL meters (Fatia C), not the zeros this used to hardcode. The
            # service already emits them in the OpenAI shape — where the details
            # are a breakdown of ``prompt_tokens`` — so they pass straight
            # through. cache_write_tokens virou obrigatório no SDK openai 3.5 — o
            # SDK antigo (1.x) aceita o extra; o teste valida contra o SDK REAL,
            # então este dict acompanha a superfície viva da Responses API.
            "input_tokens_details": {
                "cached_tokens": _detail(usage, "prompt_tokens_details", "cached_tokens"),
                "cache_write_tokens": _detail(
                    usage, "prompt_tokens_details", "cache_write_tokens"
                ),
            },
            "output_tokens": usage["completion_tokens"],
            "output_tokens_details": {
                "reasoning_tokens": _detail(
                    usage, "completion_tokens_details", "reasoning_tokens"
                )
            },
            "total_tokens": usage["total_tokens"],
        },
        **({"lohra_usage": lohra_usage} if lohra_usage is not None else {}),
        # Anthropic's final reason does not change a native text part's type.
        **({"lohra_native_outcome": {"api_mode": "anthropic_messages", "reason": "refusal"}}
           if native_outcome and native_outcome.get("api_mode") == "anthropic_messages"
           and native_outcome.get("reason") == "refusal" else {}),
    }


def response_state(result: dict) -> tuple[str, dict | None]:
    """Translate only known native evidence; an unsupported cause stays absent."""
    native = result.get("native_outcome") or {}
    reason = None
    if native.get("api_mode") == "responses" and native.get("status") == "incomplete":
        observed = native.get("incomplete_reason")
        if observed in ("max_output_tokens", "max_messages", "content_filter", "steered"):
            reason = observed
    elif result["finish_reason"] == "length":
        if native.get("reason") in ("length", "max_tokens") or not native:
            reason = "max_output_tokens"
    elif result["finish_reason"] == "content_filter":
        if native.get("api_mode") == "anthropic_messages" and native.get("reason") == "refusal":
            return "completed", None
        reason = "content_filter"
    else:
        return "completed", None
    return "incomplete", {"reason": reason} if reason is not None else None


def responses_sse(event_type: str, payload: dict[str, Any]) -> str:
    """Frame a typed Responses SSE event (``event:`` + ``data:`` lines)."""
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def build_response_created_event(
    *, response_id: str, model: str, created: int, sequence_number: int
) -> str:
    response = {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "model": model,
        "output": [],
        "error": None,
        "incomplete_details": None,
        "instructions": None,
        "metadata": {},
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    return responses_sse(
        "response.created",
        {"type": "response.created", "sequence_number": sequence_number, "response": response},
    )


def build_output_item_added_event(*, response_id: str, sequence_number: int) -> str:
    """The empty assistant message item — the SDK's stream snapshot indexes into it."""
    return responses_sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "sequence_number": sequence_number,
            "output_index": 0,
            "item": {
                "type": "message",
                "id": f"msg_{response_id}",
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            },
        },
    )


def build_content_part_added_event(*, response_id: str, sequence_number: int) -> str:
    return responses_sse(
        "response.content_part.added",
        {
            "type": "response.content_part.added",
            "sequence_number": sequence_number,
            "item_id": f"msg_{response_id}",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
    )


def build_text_delta_event(*, response_id: str, delta: str, sequence_number: int) -> str:
    return responses_sse(
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "sequence_number": sequence_number,
            "item_id": f"msg_{response_id}",
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
            "logprobs": [],
        },
    )


def build_response_completed_event(response: dict[str, Any], *, sequence_number: int) -> str:
    return responses_sse(
        "response.completed",
        {"type": "response.completed", "sequence_number": sequence_number, "response": response},
    )


def build_response_failed_event(response: dict[str, Any], *, sequence_number: int) -> str:
    return responses_sse(
        "response.failed",
        {"type": "response.failed", "sequence_number": sequence_number, "response": response},
    )


def build_response_terminal_event(response: dict[str, Any], *, sequence_number: int) -> str:
    event_type = "response." + response["status"]
    return responses_sse(event_type, {"type": event_type, "sequence_number": sequence_number,
                                     "response": response})
