"""Tests for the anthropic_messages transport (build_kwargs + normalize_response).

Phase 1 — see docs/specs/01-agent-core.md §2. The transport converts the
internal message schema (OpenAI superset) into Anthropic Messages kwargs, and
normalizes raw Anthropic responses into the canonical NormalizedResponse.
"""

import json

import pytest

from lohra.agent.types import NormalizedResponse, ToolCall
from lohra.providers.transports import get_transport
from lohra.providers.transports.anthropic_messages import (
    DEFAULT_MAX_TOKENS,
    AnthropicMessagesTransport,
)


@pytest.fixture
def transport() -> AnthropicMessagesTransport:
    return AnthropicMessagesTransport()


# --- registry ---


def test_registry_resolves_anthropic_messages():
    transport = get_transport("anthropic_messages")
    assert isinstance(transport, AnthropicMessagesTransport)
    assert transport.api_mode == "anthropic_messages"


def test_registry_unknown_mode_returns_none():
    assert get_transport("nope") is None


# --- build_kwargs ---


def test_build_kwargs_minimal(transport):
    kwargs = transport.build_kwargs(
        model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "olá"}],
    )
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["messages"] == [{"role": "user", "content": "olá"}]
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS
    assert "temperature" not in kwargs
    assert "system" not in kwargs
    assert "tools" not in kwargs


def test_build_kwargs_converts_image_parts(transport):
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "image_url", "image_url": {"url": "https://x.test/cat.jpg"}},
            ],
        }
    ]
    blocks = transport.build_kwargs(model="m", messages=messages)["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this?"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
    }
    assert blocks[2] == {"type": "image", "source": {"type": "url", "url": "https://x.test/cat.jpg"}}


def test_build_kwargs_system_is_top_level_param(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "user", "content": "oi"}],
        system="be brief",
    )
    assert kwargs["system"] == "be brief"


def test_build_kwargs_lifts_system_messages_from_history(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {"role": "system", "content": "from history"},
            {"role": "user", "content": "oi"},
        ],
        system="from caller",
    )
    assert "from caller" in kwargs["system"]
    assert "from history" in kwargs["system"]
    assert all(m["role"] != "system" for m in kwargs["messages"])


def test_build_kwargs_temperature_and_max_tokens_overrides(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "user", "content": "oi"}],
        temperature=0.2,
        max_tokens=1234,
    )
    assert kwargs["temperature"] == 0.2
    assert kwargs["max_tokens"] == 1234


def test_build_kwargs_does_not_mutate_input(transport):
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "oi"},
        {
            "role": "assistant",
            "content": "vou ler",
            "tool_calls": [
                {
                    "id": "tc_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                }
            ],
        },
        {"role": "tool", "name": "read_file", "tool_call_id": "tc_1", "content": "data"},
    ]
    snapshot = json.loads(json.dumps(messages))
    transport.build_kwargs(model="m", messages=messages)
    assert messages == snapshot


def test_build_kwargs_assistant_tool_calls_become_tool_use_blocks(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {"role": "user", "content": "leia a.txt"},
            {
                "role": "assistant",
                "content": "vou ler",
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.txt"}'},
                    }
                ],
            },
        ],
    )
    assistant = kwargs["messages"][1]
    assert assistant["role"] == "assistant"
    text_blocks = [b for b in assistant["content"] if b["type"] == "text"]
    tool_blocks = [b for b in assistant["content"] if b["type"] == "tool_use"]
    assert text_blocks == [{"type": "text", "text": "vou ler"}]
    assert tool_blocks == [
        {"type": "tool_use", "id": "tc_1", "name": "read_file", "input": {"path": "a.txt"}}
    ]


def test_build_kwargs_malformed_tool_arguments_fall_back_to_empty_input(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{not json"},
                    }
                ],
            },
        ],
    )
    tool_block = kwargs["messages"][0]["content"][0]
    assert tool_block["input"] == {}


def test_build_kwargs_groups_consecutive_tool_results_into_one_user_message(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {"role": "tool", "name": "a", "tool_call_id": "tc_1", "content": "r1"},
            {"role": "tool", "name": "b", "tool_call_id": "tc_2", "content": "r2"},
        ],
    )
    assert len(kwargs["messages"]) == 1
    grouped = kwargs["messages"][0]
    assert grouped["role"] == "user"
    assert grouped["content"] == [
        {"type": "tool_result", "tool_use_id": "tc_1", "content": "r1"},
        {"type": "tool_result", "tool_use_id": "tc_2", "content": "r2"},
    ]


def test_build_kwargs_converts_openai_style_tools(transport):
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "user", "content": "oi"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "description": "Read a file", "parameters": schema},
            }
        ],
    )
    assert kwargs["tools"] == [
        {"name": "read_file", "description": "Read a file", "input_schema": schema}
    ]


def test_build_kwargs_replays_thinking_blocks_before_other_content(transport):
    """Anthropic 400s if signed thinking blocks are not replayed verbatim."""
    thinking = {"type": "thinking", "thinking": "hmm", "signature": "sig123"}
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {
                "role": "assistant",
                "content": "vou ler",
                "provider_data": {"thinking_blocks": (thinking,)},
                "tool_calls": [
                    {
                        "id": "tc_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
        ],
    )
    blocks = kwargs["messages"][0]["content"]
    assert blocks[0] == thinking
    assert blocks[0] is not thinking
    assert [b["type"] for b in blocks] == ["thinking", "text", "tool_use"]


def test_build_kwargs_replays_thinking_blocks_without_tool_calls(transport):
    thinking = {"type": "thinking", "thinking": "hmm", "signature": "sig123"}
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {
                "role": "assistant",
                "content": "resposta",
                "provider_data": {"thinking_blocks": (thinking,)},
            },
        ],
    )
    blocks = kwargs["messages"][0]["content"]
    assert blocks == [thinking, {"type": "text", "text": "resposta"}]


def test_build_kwargs_passes_block_list_content_through(transport):
    """Content already in block form (e.g. multimodal) must survive as-is."""
    block_content = [{"type": "text", "text": "já em blocos"}]
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "assistant", "content": block_content}],
    )
    assert kwargs["messages"][0]["content"] == block_content
    assert kwargs["messages"][0]["content"] is not block_content


def test_build_kwargs_plain_assistant_message_stays_string(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "assistant", "content": "texto simples"}],
    )
    assert kwargs["messages"][0] == {"role": "assistant", "content": "texto simples"}


def test_build_kwargs_passes_through_anthropic_style_tools(transport):
    tool = {"name": "read_file", "description": "Read", "input_schema": {"type": "object"}}
    kwargs = transport.build_kwargs(
        model="m", messages=[{"role": "user", "content": "oi"}], tools=[tool]
    )
    assert kwargs["tools"] == [tool]
    assert kwargs["tools"][0] is not tool


# --- normalize_response ---


def _raw_response(content, stop_reason="end_turn", usage=None):
    return {"content": content, "stop_reason": stop_reason, "usage": usage}


def test_normalize_text_response(transport):
    raw = _raw_response(
        [{"type": "text", "text": "olá!"}],
        usage={"input_tokens": 10, "output_tokens": 5},
    )
    resp = transport.normalize_response(raw)
    assert isinstance(resp, NormalizedResponse)
    assert resp.content == "olá!"
    assert resp.finish_reason == "stop"
    assert resp.tool_calls == ()
    assert resp.usage.input_tokens == 10
    assert resp.usage.output_tokens == 5


def test_normalize_concatenates_multiple_text_blocks(transport):
    raw = _raw_response([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    assert transport.normalize_response(raw).content == "ab"


def test_normalize_maps_max_tokens_to_length(transport):
    raw = _raw_response([{"type": "text", "text": "trunc"}], stop_reason="max_tokens")
    assert transport.normalize_response(raw).finish_reason == "length"


def test_normalize_maps_refusal_to_content_filter(transport):
    raw = _raw_response([], stop_reason="refusal")
    assert transport.normalize_response(raw).finish_reason == "content_filter"


def test_normalize_maps_pause_turn_to_pause(transport):
    """pause_turn means 'resend to continue' — must not look like a final answer."""
    raw = _raw_response([{"type": "text", "text": "parcial"}], stop_reason="pause_turn")
    assert transport.normalize_response(raw).finish_reason == "pause"


def test_normalize_maps_context_window_exceeded_to_length(transport):
    raw = _raw_response([], stop_reason="model_context_window_exceeded")
    assert transport.normalize_response(raw).finish_reason == "length"


def test_normalize_unknown_stop_reason_defaults_to_stop(transport):
    raw = _raw_response([{"type": "text", "text": "x"}], stop_reason="weird_new_reason")
    assert transport.normalize_response(raw).finish_reason == "stop"


def test_normalize_tool_use_response(transport):
    raw = _raw_response(
        [
            {"type": "text", "text": "vou ler"},
            {"type": "tool_use", "id": "tc_1", "name": "read_file", "input": {"path": "a.txt"}},
        ],
        stop_reason="tool_use",
    )
    resp = transport.normalize_response(raw)
    assert resp.finish_reason == "tool_calls"
    assert resp.content == "vou ler"
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert isinstance(call, ToolCall)
    assert call.id == "tc_1"
    assert call.name == "read_file"
    assert json.loads(call.arguments) == {"path": "a.txt"}


def test_normalize_thinking_blocks_preserved_in_provider_data(transport):
    thinking = {"type": "thinking", "thinking": "hmm...", "signature": "sig123"}
    raw = _raw_response([thinking, {"type": "text", "text": "resposta"}])
    resp = transport.normalize_response(raw)
    assert resp.reasoning == "hmm..."
    assert resp.content == "resposta"
    assert resp.provider_data["thinking_blocks"] == (thinking,)


def test_normalize_cache_token_accounting(transport):
    raw = _raw_response(
        [{"type": "text", "text": "x"}],
        usage={
            "input_tokens": 100,
            "output_tokens": 7,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 40,
        },
    )
    usage = transport.normalize_response(raw).usage
    assert usage.cache_read_tokens == 60
    assert usage.cache_write_tokens == 40


def test_normalize_missing_usage_yields_none(transport):
    raw = _raw_response([{"type": "text", "text": "x"}], usage=None)
    assert transport.normalize_response(raw).usage is None


def test_normalize_accepts_attribute_style_objects(transport):
    """The real Anthropic SDK returns objects with attributes, not dicts."""

    class Block:
        type = "text"
        text = "via sdk"

    class UsageObj:
        input_tokens = 3
        output_tokens = 2

    class Response:
        content = [Block()]
        stop_reason = "end_turn"
        usage = UsageObj()

    resp = transport.normalize_response(Response())
    assert resp.content == "via sdk"
    assert resp.finish_reason == "stop"
    assert resp.usage.input_tokens == 3


def test_normalize_attribute_style_thinking_block_copied_to_plain_dict(transport):
    """SDK thinking blocks must survive as plain dicts in provider_data."""

    class ThinkingBlock:
        type = "thinking"
        thinking = "pondering"
        signature = "sig456"

    class Response:
        content = [ThinkingBlock()]
        stop_reason = "end_turn"
        usage = None

    resp = transport.normalize_response(Response())
    assert resp.reasoning == "pondering"
    assert resp.provider_data["thinking_blocks"] == (
        {"type": "thinking", "thinking": "pondering", "signature": "sig456"},
    )
