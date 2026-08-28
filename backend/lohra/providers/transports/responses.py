"""Transport for the OpenAI **Responses API** (Fase 10, B2).

The ChatGPT/Codex subscription backend speaks ONLY the Responses API (not chat
completions), so subscription mode uses this transport. It mirrors the
chat_completions transport's contract but maps Lohra's internal message schema to
the Responses request (`instructions` + `input` items, flat `function` tools,
`max_output_tokens`) and the Response object back to NormalizedResponse.

Built against the real openai SDK Response shapes (output items: message →
output_text, function_call → call_id/name/arguments, reasoning; usage). Object
access goes through get_field so dict fakes work in tests without a live API.
"""

from __future__ import annotations

import copy
from typing import Any

from lohra.agent.types import NormalizedResponse, ToolCall, Usage
from lohra.providers.transports.base import Transport, get_field

# Response.status → our finish reasons (tool calls override this in normalize).
_STATUS_FINISH = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "stop",
    "cancelled": "stop",
}


def _text_of(content: Any) -> str:
    """A message's content as plain text (str, or joined text parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                parts.append(str(part.get("text", "")))
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    return "" if content is None else str(content)


def _user_content(content: Any) -> Any:
    """A user message's content for the Responses `input`. Multi-part content with
    images becomes Responses parts (input_text/input_image) so vision survives;
    plain text stays a string."""
    if not isinstance(content, list):
        return _text_of(content)
    parts: list[dict] = []
    for part in content:
        if isinstance(part, str):
            parts.append({"type": "input_text", "text": part})
        elif isinstance(part, dict):
            ptype = part.get("type")
            if ptype in ("text", "input_text"):
                parts.append({"type": "input_text", "text": str(part.get("text", ""))})
            elif ptype in ("image_url", "input_image"):
                img = part.get("image_url")
                url = img.get("url") if isinstance(img, dict) else img
                if url:
                    parts.append({"type": "input_image", "image_url": url})
    return parts or _text_of(content)


def _convert_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """Internal messages → (Responses `input` items, lifted system text).

    Assistant tool calls become `function_call` items and tool results become
    `function_call_output` items — the Responses API's round-trip shape."""
    items: list[dict] = []
    system_parts: list[str] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            system_parts.append(_text_of(msg.get("content")))
            continue
        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id"),
                    "output": _text_of(msg.get("content")),
                }
            )
            continue
        if role == "assistant":
            # Replay the turn's reasoning items FIRST (store=false continuity): the
            # backend only accepts prior reasoning that carries encrypted state.
            for ritem in (msg.get("provider_data") or {}).get("reasoning_items") or ():
                if isinstance(ritem.get("encrypted_content"), str):
                    items.append(
                        {
                            "type": "reasoning",
                            "summary": ritem.get("summary") or [],
                            "encrypted_content": ritem["encrypted_content"],
                        }
                    )
            text = _text_of(msg.get("content"))
            if text:
                items.append({"role": "assistant", "content": text})
            for call in msg.get("tool_calls") or ():
                fn = call.get("function", {})
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id"),
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments", "{}"),
                    }
                )
            continue
        # user (or any other) → an input message (parts preserved for vision)
        items.append({"role": role or "user", "content": _user_content(msg.get("content"))})
    return items, "\n\n".join(p for p in system_parts if p)


def _convert_tool(tool: dict) -> dict:
    """OpenAI chat tool ({type, function:{name,description,parameters}}) → the
    Responses FLAT function shape ({type:function, name, description, parameters})."""
    fn = tool.get("function", tool)
    return {
        "type": "function",
        "name": fn.get("name"),
        "description": fn.get("description", ""),
        "parameters": copy.deepcopy(fn.get("parameters", {"type": "object", "properties": {}})),
    }


class ResponsesTransport(Transport):
    """Transport for the OpenAI Responses API (subscription/Codex backend)."""

    api_mode = "responses"

    def build_kwargs(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
        effort: str | None = None,
    ) -> dict:
        items, lifted_system = _convert_messages(messages)
        instructions = "\n\n".join(p for p in (system, lifted_system) if p)
        # The Codex/ChatGPT backend REQUIRES store=false (verified live: it 400s with
        # "Store must be set to false" otherwise — it won't persist responses server-side).
        kwargs: dict = {"model": model, "input": items, "store": False}
        if instructions:
            kwargs["instructions"] = instructions
        if max_tokens is not None:
            kwargs["max_output_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [_convert_tool(t) for t in tools]
        if tool_choice is not None:  # force a specific tool (Responses shape)
            kwargs["tool_choice"] = {"type": "function", "name": tool_choice}
        if effort is not None:  # Responses reasoning effort (Codex/gpt-5 support it)
            kwargs["reasoning"] = {"effort": effort}
        # Ask for the encrypted reasoning state so it can be replayed across turns
        # under store=false (a reasoning model loses continuity otherwise — §opencode).
        kwargs["include"] = ["reasoning.encrypted_content"]
        return kwargs

    def normalize_response(self, raw: Any) -> NormalizedResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        reasoning_parts: list[str] = []
        reasoning_items: list[dict] = []
        for item in get_field(raw, "output", None) or ():
            itype = get_field(item, "type")
            if itype == "message":
                for part in get_field(item, "content", None) or ():
                    ptype = get_field(part, "type")
                    if ptype == "output_text":
                        text_parts.append(get_field(part, "text") or "")
                    elif ptype == "refusal":  # surface a refusal as content, not empty
                        text_parts.append(get_field(part, "refusal") or "")
            elif itype == "function_call":
                tool_calls.append(
                    ToolCall(
                        id=get_field(item, "call_id"),
                        name=get_field(item, "name") or "",
                        arguments=get_field(item, "arguments") or "{}",
                    )
                )
            elif itype in ("reasoning", "thinking"):
                # Real SDK: ResponseReasoningItem.summary[].text. Fall back to a flat
                # field only for dict fakes that don't model the summary list.
                summary = get_field(item, "summary", None)
                if summary:
                    reasoning_parts.extend(get_field(s, "text") or "" for s in summary)
                else:
                    reasoning_parts.append(
                        get_field(item, "thinking") or get_field(item, "text") or ""
                    )
                reasoning_items.append(_capture_reasoning(item, summary))
        status = get_field(raw, "status") or "completed"
        finish = "tool_calls" if tool_calls else _STATUS_FINISH.get(status, "stop")
        # Keep reasoning items with encrypted state for replay (store=false).
        replayable = [r for r in reasoning_items if isinstance(r.get("encrypted_content"), str)]
        return NormalizedResponse(
            content="".join(text_parts) or None,
            finish_reason=finish,
            tool_calls=tuple(tool_calls),
            reasoning="".join(reasoning_parts) or None,
            usage=_usage(get_field(raw, "usage", None)),
            provider_data={"reasoning_items": replayable} if replayable else None,
        )


def _capture_reasoning(item: Any, summary: Any) -> dict:
    """A reasoning output item as a plain dict for replay (type/summary/encrypted)."""
    return {
        "type": "reasoning",
        "summary": [
            {"type": "summary_text", "text": get_field(s, "text") or ""} for s in (summary or ())
        ],
        "encrypted_content": get_field(item, "encrypted_content"),
    }


def _usage(raw: Any) -> Usage | None:
    if raw is None:
        return None
    return Usage(
        input_tokens=get_field(raw, "input_tokens") or 0,
        output_tokens=get_field(raw, "output_tokens") or 0,
        cache_read_tokens=get_field(get_field(raw, "input_tokens_details"), "cached_tokens") or 0,
        reasoning_tokens=get_field(get_field(raw, "output_tokens_details"), "reasoning_tokens")
        or 0,
    )
