"""chat_completions transport — OpenAI Chat Completions API adapter.

The same protocol is spoken by openrouter, deepseek, groq, together, and ollama,
so this one transport (paired with a per-profile base_url/api_key) unlocks all of
them. Provider quirks handled here, never in the loop:

- ``system`` is the FIRST message (role "system"), not a top-level param.
- The internal schema already stores assistant ``tool_calls`` and tool
  definitions in OpenAI shape, so conversion is mostly pass-through; bookkeeping
  fields (finish_reason, reasoning, provider_data) are stripped before sending.
- ``finish_reason`` maps to the canonical set; legacy ``function_call`` -> tool_calls.
- ``reasoning_content`` (deepseek-reasoner) is surfaced as ``reasoning``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from lohra.agent.types import NormalizedResponse, ToolCall, Usage, map_finish_reason
from lohra.providers.transports.base import Transport, get_field

CHAT_FINISH_REASONS = {
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "function_call": "tool_calls",  # legacy single-function calling
    "content_filter": "content_filter",
}


def _clean_tool_call(call: dict) -> dict:
    """Keep only the OpenAI-required fields of an assistant tool call."""
    function = call.get("function", {})
    arguments = function.get("arguments")
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments or {})
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": function.get("name"), "arguments": arguments},
    }


def _convert_assistant(message: dict) -> dict:
    """Assistant message -> chat-completions shape (role/content/tool_calls only)."""
    content = message.get("content")
    tool_calls = message.get("tool_calls") or ()
    if tool_calls:
        return {
            "role": "assistant",
            "content": content or None,  # null when only tool calls, no text
            "tool_calls": [_clean_tool_call(call) for call in tool_calls],
        }
    return {"role": "assistant", "content": content or ""}


def _convert_messages(messages: list[dict]) -> list[dict]:
    """Internal history -> chat-completions messages (system stays inline)."""
    out: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            out.append(_convert_assistant(message))
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": message.get("content") or "",
                }
            )
        elif role == "system":
            out.append({"role": "system", "content": message.get("content") or ""})
        else:
            content = message.get("content")
            # A list is already OpenAI content parts (text + image_url) — pass
            # it through so image input reaches the provider.
            out.append(
                {
                    "role": "user",
                    "content": content if isinstance(content, list) else (content or ""),
                }
            )
    return out


def _normalize_usage(raw_usage: Any) -> Usage | None:
    """Normalize to the DISJOINT convention: ``input_tokens`` is what was NOT
    served from cache, in every provider.

    OpenAI-compat reports ``cached_tokens`` as a SLICE of ``prompt_tokens``;
    Anthropic reports its cache meters OUTSIDE ``input_tokens``. One convention
    at the boundary (the Anthropic one) is what lets any downstream code sum the
    meters — for a gross cost, a budget, a rollup — without double-counting the
    cache in half the providers. Invariant, per transport:
    ``input_tokens + cache_read_tokens + cache_write_tokens`` == the provider's
    own prompt total."""
    if raw_usage is None:
        return None
    prompt = get_field(raw_usage, "prompt_tokens", 0) or 0
    cached = get_field(get_field(raw_usage, "prompt_tokens_details"), "cached_tokens") or 0
    if not cached:
        # Moonshot/Kimi reporta o cache no TOPO do usage (`cached_tokens`),
        # não em prompt_tokens_details — sem isto, 100% do prompt cacheado
        # deles contaria (e cobraria) como input não-cacheado.
        cached = get_field(raw_usage, "cached_tokens", 0) or 0
    # Clamp the CACHE, not just the difference: a bogus cached > prompt must not
    # bill a negative input NOR leave the meters summing to more prompt than the
    # provider reported (which the gross cost would then charge for).
    cached = min(cached, prompt)
    return Usage(
        input_tokens=prompt - cached,
        output_tokens=get_field(raw_usage, "completion_tokens", 0) or 0,
        cache_read_tokens=cached,
        reasoning_tokens=get_field(
            get_field(raw_usage, "completion_tokens_details"), "reasoning_tokens"
        )
        or 0,
    )


def _normalize_tool_calls(raw_calls: Any) -> tuple[ToolCall, ...]:
    calls: list[ToolCall] = []
    for call in raw_calls or ():
        function = get_field(call, "function")
        calls.append(
            ToolCall(
                id=get_field(call, "id"),
                name=get_field(function, "name") or "",
                arguments=get_field(function, "arguments") or "{}",
            )
        )
    return tuple(calls)


class ChatCompletionsTransport(Transport):
    """Transport for the OpenAI Chat Completions API and its compatibles."""

    api_mode = "chat_completions"

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
        converted: list[dict] = []
        if system:
            converted.append({"role": "system", "content": system})
        converted.extend(_convert_messages(messages))
        kwargs: dict = {"model": model, "messages": converted}
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if temperature is not None:
            kwargs["temperature"] = temperature
        if effort is not None:  # OpenAI reasoning models accept reasoning_effort
            kwargs["reasoning_effort"] = effort
        if tools:
            # Deep copy: never alias the registry's tool defs (nested function/
            # parameters dicts) into the request — the transport must not mutate
            # or share its inputs.
            kwargs["tools"] = [copy.deepcopy(tool) for tool in tools]
        if tool_choice is not None:  # force a specific tool (OpenAI shape)
            kwargs["tool_choice"] = {"type": "function", "function": {"name": tool_choice}}
        return kwargs

    def normalize_response(self, raw: Any) -> NormalizedResponse:
        choices = get_field(raw, "choices") or ()
        if not choices:
            return NormalizedResponse(content=None, finish_reason="stop")
        choice = choices[0]
        message = get_field(choice, "message")
        return NormalizedResponse(
            content=get_field(message, "content"),
            finish_reason=map_finish_reason(
                get_field(choice, "finish_reason"), CHAT_FINISH_REASONS
            ),
            tool_calls=_normalize_tool_calls(get_field(message, "tool_calls")),
            reasoning=get_field(message, "reasoning_content") or None,
            usage=_normalize_usage(get_field(raw, "usage")),
        )
