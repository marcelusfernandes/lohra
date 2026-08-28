"""Tests for the chat_completions transport (build_kwargs + normalize_response).

Phase 6 — the OpenAI Chat Completions protocol, also spoken by openrouter,
deepseek, groq, together, and ollama. Converts the internal message schema
(OpenAI superset) into chat-completions kwargs and normalizes raw responses
into the canonical NormalizedResponse.
"""

import json

import pytest

from lohra.agent.types import NormalizedResponse, ToolCall
from lohra.providers.transports import get_transport
from lohra.providers.transports.chat_completions import ChatCompletionsTransport


@pytest.fixture
def transport() -> ChatCompletionsTransport:
    return ChatCompletionsTransport()


# --- registry ---


def test_registry_resolves_chat_completions():
    transport = get_transport("chat_completions")
    assert isinstance(transport, ChatCompletionsTransport)
    assert transport.api_mode == "chat_completions"


# --- build_kwargs ---


def test_build_kwargs_minimal(transport):
    kwargs = transport.build_kwargs(
        model="gpt-4o", messages=[{"role": "user", "content": "olá"}]
    )
    assert kwargs["model"] == "gpt-4o"
    assert kwargs["messages"] == [{"role": "user", "content": "olá"}]
    assert "max_tokens" not in kwargs  # omitted when not provided (provider default)
    assert "temperature" not in kwargs
    assert "tools" not in kwargs


def test_build_kwargs_system_is_a_prepended_message(transport):
    kwargs = transport.build_kwargs(
        model="m", messages=[{"role": "user", "content": "oi"}], system="be brief"
    )
    # OpenAI takes the system prompt as the first message, not a top-level param
    assert kwargs["messages"][0] == {"role": "system", "content": "be brief"}
    assert kwargs["messages"][1] == {"role": "user", "content": "oi"}
    assert "system" not in kwargs


def test_build_kwargs_lifts_system_messages_from_history(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[
            {"role": "system", "content": "from history"},
            {"role": "user", "content": "oi"},
        ],
        system="from caller",
    )
    systems = [m for m in kwargs["messages"] if m["role"] == "system"]
    # caller system first, then the lifted-from-history one, both preserved
    assert [s["content"] for s in systems] == ["from caller", "from history"]


def test_build_kwargs_passes_max_tokens_and_temperature(transport):
    kwargs = transport.build_kwargs(
        model="m",
        messages=[{"role": "user", "content": "x"}],
        max_tokens=256,
        temperature=0.2,
    )
    assert kwargs["max_tokens"] == 256
    assert kwargs["temperature"] == 0.2


def test_build_kwargs_tools_pass_through_openai_shape(transport):
    tool = {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    kwargs = transport.build_kwargs(
        model="m", messages=[{"role": "user", "content": "x"}], tools=[tool]
    )
    assert kwargs["tools"] == [tool]


def test_build_kwargs_passes_image_parts_through(transport):
    content = [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "https://x.test/cat.jpg"}},
    ]
    kwargs = transport.build_kwargs(model="m", messages=[{"role": "user", "content": content}])
    assert kwargs["messages"][0] == {"role": "user", "content": content}


def test_build_kwargs_assistant_tool_calls_and_tool_result(transport):
    messages = [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": "",
            "finish_reason": "tool_calls",
            "reasoning": "internal",  # bookkeeping — must NOT be sent
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                }
            ],
        },
        {"role": "tool", "name": "read_file", "tool_call_id": "call_1", "content": "body"},
    ]
    kwargs = transport.build_kwargs(model="m", messages=messages)
    out = kwargs["messages"]
    assistant = out[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] is None  # empty text + tool_calls -> null
    assert assistant["tool_calls"][0]["id"] == "call_1"
    assert "reasoning" not in assistant and "finish_reason" not in assistant
    tool_msg = out[2]
    # OpenAI tool messages carry only role/tool_call_id/content (no name)
    assert tool_msg == {"role": "tool", "tool_call_id": "call_1", "content": "body"}


def test_build_kwargs_does_not_mutate_input(transport):
    messages = [{"role": "user", "content": "x"}]
    before = json.dumps(messages)
    transport.build_kwargs(model="m", messages=messages, system="s")
    assert json.dumps(messages) == before  # input untouched


def test_build_kwargs_deep_copies_tools(transport):
    tool = {"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}
    kwargs = transport.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], tools=[tool])
    # mutating the produced request must not reach the caller's tool defs
    kwargs["tools"][0]["function"]["parameters"]["type"] = "MUTATED"
    assert tool["function"]["parameters"]["type"] == "object"


def test_build_kwargs_coerces_dict_tool_arguments_to_json(transport):
    # Defensive: if a tool call's arguments slipped through as a dict, send a
    # JSON string (the API requires a string).
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "t", "arguments": {"a": 1}}}
            ],
        }
    ]
    kwargs = transport.build_kwargs(model="m", messages=messages)
    args = kwargs["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert args == '{"a": 1}'


def test_build_kwargs_assistant_text_only_keeps_string_content(transport):
    messages = [{"role": "assistant", "content": "hello", "finish_reason": "stop"}]
    kwargs = transport.build_kwargs(model="m", messages=messages)
    assert kwargs["messages"][0] == {"role": "assistant", "content": "hello"}


# --- normalize_response ---


def _response(message: dict, *, finish_reason="stop", usage=None) -> dict:
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def test_normalize_plain_text(transport):
    raw = _response({"role": "assistant", "content": "hi there"})
    result = transport.normalize_response(raw)
    assert isinstance(result, NormalizedResponse)
    assert result.content == "hi there"
    assert result.finish_reason == "stop"
    assert result.tool_calls == ()


def test_normalize_tool_calls(transport):
    raw = _response(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_9",
                    "type": "function",
                    "function": {"name": "terminal", "arguments": '{"command":"ls"}'},
                }
            ],
        },
        finish_reason="tool_calls",
    )
    result = transport.normalize_response(raw)
    assert result.content is None
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == (
        ToolCall(id="call_9", name="terminal", arguments='{"command":"ls"}'),
    )


def test_normalize_maps_finish_reasons(transport):
    assert transport.normalize_response(_response({"content": "x"}, finish_reason="length")).finish_reason == "length"
    assert transport.normalize_response(_response({"content": "x"}, finish_reason="content_filter")).finish_reason == "content_filter"
    # legacy function_call -> tool_calls
    assert transport.normalize_response(_response({"content": "x"}, finish_reason="function_call")).finish_reason == "tool_calls"
    # unknown -> stop
    assert transport.normalize_response(_response({"content": "x"}, finish_reason="weird")).finish_reason == "stop"


def test_normalize_usage(transport):
    raw = _response(
        {"content": "x"},
        usage={"prompt_tokens": 10, "completion_tokens": 5},
    )
    result = transport.normalize_response(raw)
    assert result.usage is not None
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


def test_normalize_usage_captures_cached_and_reasoning_details(transport):
    # the REAL chat-completions usage shape: nested *_tokens_details objects
    raw = _response(
        {"content": "x"},
        usage={
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 75},
            "completion_tokens_details": {"reasoning_tokens": 9},
        },
    )
    result = transport.normalize_response(raw)
    assert result.usage.cache_read_tokens == 75
    assert result.usage.reasoning_tokens == 9


def test_normalize_usage_missing_details_defaults_to_zero(transport):
    raw = _response(
        {"content": "x"},
        usage={"prompt_tokens": 10, "completion_tokens": 5, "prompt_tokens_details": None},
    )
    result = transport.normalize_response(raw)
    assert result.usage.cache_read_tokens == 0
    assert result.usage.reasoning_tokens == 0


def test_normalize_reasoning_content_when_present(transport):
    # deepseek-reasoner returns reasoning_content alongside content
    raw = _response({"content": "answer", "reasoning_content": "let me think"})
    result = transport.normalize_response(raw)
    assert result.reasoning == "let me think"


def test_normalize_empty_choices_is_safe(transport):
    result = transport.normalize_response({"choices": [], "usage": None})
    assert result.content is None
    assert result.finish_reason == "stop"
    assert result.tool_calls == ()


def test_normalize_reads_attribute_style_objects(transport):
    class Obj:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    message = Obj(role="assistant", content="hi", tool_calls=None)
    choice = Obj(message=message, finish_reason="stop")
    raw = Obj(choices=[choice], usage=Obj(prompt_tokens=3, completion_tokens=4))
    result = transport.normalize_response(raw)
    assert result.content == "hi"
    assert result.usage.input_tokens == 3


def test_normalize_usage_input_is_uncached_disjoint(transport):
    """Fatia C: input_tokens = tokens NAO cacheados, em TODOS os providers.

    A OpenAI reporta ``cached_tokens`` como SUBCONJUNTO de ``prompt_tokens``; a
    fronteira do transport normaliza para a convencao disjunta (a mesma da
    Anthropic), para que somar os medidores nunca conte o cache duas vezes."""
    raw = _response(
        {"content": "x"},
        usage={
            "prompt_tokens": 100, "completion_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 75},
        },
    )
    usage = transport.normalize_response(raw).usage
    assert usage.input_tokens == 25
    assert usage.cache_read_tokens == 75
    # INVARIANTE: os medidores de prompt somam exatamente o total do provider.
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens == 100


def test_normalize_usage_cached_over_prompt_clamps_without_breaking_the_invariant(
    transport,
):
    """Defensivo: um cached_tokens maior que prompt_tokens nunca vira negativo —
    E os medidores continuam somando o total do provider.

    Zerar so o input deixava ``cache_read`` sozinho acima do prompt: os tres
    medidores somavam 80 para um prompt de 50, e o ``gross_usd`` cobrava 30
    tokens que nunca existiram. O cache tambem e capado pelo prompt."""
    raw = _response(
        {"content": "x"},
        usage={
            "prompt_tokens": 50, "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 80},
        },
    )
    usage = transport.normalize_response(raw).usage
    assert usage.input_tokens == 0
    assert usage.cache_read_tokens == 50
    assert usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens == 50
