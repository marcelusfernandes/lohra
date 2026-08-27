"""anthropic_messages transport — Anthropic Messages API adapter.

Converts the internal message schema (OpenAI superset, see spec §2) into
Anthropic request kwargs and normalizes content-block responses into
NormalizedResponse. Provider quirks handled here, never in the loop:

- ``system`` is a top-level param, not a message role.
- Assistant tool calls are ``tool_use`` content blocks; tool results are
  ``tool_result`` blocks grouped into a single user message.
- ``stop_reason`` maps to the canonical finish_reason set.
- Thinking blocks (with signatures) are preserved verbatim in provider_data —
  the API 400s on replay if they are modified.
"""

from __future__ import annotations

import json
from typing import Any

from lohra.agent.types import NormalizedResponse, ToolCall, Usage, map_finish_reason
from lohra.providers.transports.base import Transport, get_field, parse_tool_arguments

DEFAULT_MAX_TOKENS = 4096

ANTHROPIC_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
    "pause_turn": "pause",
}


def _thinking_blocks_of(message: dict) -> tuple:
    provider_data = message.get("provider_data") or {}
    return tuple(provider_data.get("thinking_blocks") or ())


def _convert_assistant(message: dict) -> dict:
    """Assistant message -> Anthropic format.

    Signed thinking blocks from provider_data are replayed verbatim and FIRST —
    the API 400s if they are missing or out of position. Content already in
    block form passes through; plain text stays a string when nothing forces
    block form.
    """
    thinking_blocks = _thinking_blocks_of(message)
    tool_calls = message.get("tool_calls") or ()
    content = message.get("content")

    if not thinking_blocks and not tool_calls and not isinstance(content, list):
        return {"role": "assistant", "content": content or ""}

    blocks: list[dict] = [dict(block) for block in thinking_blocks]
    if isinstance(content, list):
        blocks.extend(dict(block) for block in content)
    elif content:
        blocks.append({"type": "text", "text": content})
    for call in tool_calls:
        function = call.get("function", {})
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": function.get("name"),
                "input": parse_tool_arguments(function.get("arguments")),
            }
        )
    return {"role": "assistant", "content": blocks}


def _image_source(url: str) -> dict:
    """OpenAI image url (http or data URI) -> Anthropic image source."""
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[len("data:") :].split(";")[0] or "image/png"
        return {"type": "base64", "media_type": media_type, "data": data}
    return {"type": "url", "url": url}


def _convert_user_part(part: dict) -> dict:
    """OpenAI content part -> Anthropic content block (text / image)."""
    part_type = part.get("type")
    if part_type == "image_url":
        url = (part.get("image_url") or {}).get("url", "")
        return {"type": "image", "source": _image_source(url)}
    if part_type == "text":
        return {"type": "text", "text": part.get("text", "")}
    return dict(part)  # already an Anthropic block (e.g. tool_result) — pass through


def _tool_result_block(message: dict) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": message.get("tool_call_id"),
        "content": message.get("content") or "",
    }


def _is_tool_result_message(message: dict) -> bool:
    content = message.get("content")
    return (
        message.get("role") == "user"
        and isinstance(content, list)
        and bool(content)
        and content[0].get("type") == "tool_result"
    )


def _convert_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """Internal history -> (anthropic messages, system text lifted from history)."""
    converted: list[dict] = []
    system_parts: list[str] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            if message.get("content"):
                system_parts.append(message["content"])
        elif role == "tool":
            block = _tool_result_block(message)
            if converted and _is_tool_result_message(converted[-1]):
                previous = converted[-1]
                converted[-1] = {**previous, "content": [*previous["content"], block]}
            else:
                converted.append({"role": "user", "content": [block]})
        elif role == "assistant":
            converted.append(_convert_assistant(message))
        else:
            content = message.get("content")
            if isinstance(content, list):
                blocks = [_convert_user_part(part) for part in content]
                converted.append({"role": role, "content": blocks})
            else:
                converted.append({"role": role, "content": content or ""})
    return converted, "\n\n".join(system_parts)


def _convert_tool(tool: dict) -> dict:
    """OpenAI-style function tool -> Anthropic tool definition."""
    if "input_schema" in tool:
        return dict(tool)
    function = tool.get("function", {})
    return {
        "name": function.get("name"),
        "description": function.get("description", ""),
        "input_schema": function.get("parameters", {"type": "object", "properties": {}}),
    }


def _block_to_plain(block: Any) -> dict:
    """Best-effort plain-dict copy of a content block (for provider_data)."""
    if isinstance(block, dict):
        return dict(block)
    if hasattr(block, "model_dump"):
        return block.model_dump()
    keys = ("type", "thinking", "signature", "data")
    return {key: getattr(block, key) for key in keys if hasattr(block, key)}


def _normalize_usage(raw_usage: Any) -> Usage | None:
    if raw_usage is None:
        return None
    return Usage(
        input_tokens=get_field(raw_usage, "input_tokens", 0) or 0,
        output_tokens=get_field(raw_usage, "output_tokens", 0) or 0,
        cache_read_tokens=get_field(raw_usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=get_field(raw_usage, "cache_creation_input_tokens", 0) or 0,
    )


class AnthropicMessagesTransport(Transport):
    """Transport for the Anthropic Messages API."""

    api_mode = "anthropic_messages"

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
        effort: str | None = None,  # accepted for the ABC; no-op (Anthropic effort
        # shape is model-version-specific — not emitted to avoid a wrong-param 400)
    ) -> dict:
        converted, lifted_system = _convert_messages(messages)
        system_text = "\n\n".join(part for part in (system, lifted_system) if part)
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "messages": converted,
        }
        if system_text:
            kwargs["system"] = system_text
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = [_convert_tool(tool) for tool in tools]
        if tool_choice is not None:  # force a specific tool (anthropic shape)
            kwargs["tool_choice"] = {"type": "tool", "name": tool_choice}
        return kwargs

    def normalize_response(self, raw: Any) -> NormalizedResponse:
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_blocks: list[dict] = []

        for block in get_field(raw, "content", None) or ():
            block_type = get_field(block, "type")
            if block_type == "text":
                text_parts.append(get_field(block, "text") or "")
            elif block_type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=get_field(block, "id"),
                        name=get_field(block, "name") or "",
                        arguments=json.dumps(get_field(block, "input") or {}),
                    )
                )
            elif block_type in ("thinking", "redacted_thinking"):
                thinking_blocks.append(_block_to_plain(block))

        provider_data = {"thinking_blocks": tuple(thinking_blocks)} if thinking_blocks else None
        reasoning = "".join(block.get("thinking") or "" for block in thinking_blocks)
        return NormalizedResponse(
            content="".join(text_parts) or None,
            finish_reason=map_finish_reason(get_field(raw, "stop_reason"), ANTHROPIC_FINISH_REASONS),
            tool_calls=tuple(tool_calls),
            reasoning=reasoning or None,
            usage=_normalize_usage(get_field(raw, "usage")),
            provider_data=provider_data,
        )
