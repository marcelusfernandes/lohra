"""Tests for ModelClient construction.

The real round-trip (`create`) needs network and is covered by the Phase 1
E2E; here we pin the offline contract: construction wires the SDK client, and
``close`` is always safe to call.
"""

import pytest

from lohra.agent.client import (
    AnthropicClient,
    ModelClient,
    OpenAIClient,
    assemble_streamed_response,
    build_client,
    resolve_api_key,
)
from lohra.providers import get_provider_profile
from lohra.providers.base import ProviderProfile

pytest.importorskip("anthropic")  # only the SDK-backed paths need the extra
pytest.importorskip("openai")


def test_anthropic_client_constructs_offline():
    # Constructing the SDK client does not perform any network I/O.
    client = AnthropicClient(api_key="sk-test", base_url="https://example.test")
    assert isinstance(client, ModelClient)
    assert client._client is not None
    client.close()  # must not raise


def test_model_client_default_close_is_noop():
    class Bare(ModelClient):
        def create(self, **kwargs):
            return None

    Bare().close()  # default no-op must not raise


# --- OpenAIClient + factory ---


def test_openai_client_constructs_offline_with_base_url():
    client = OpenAIClient(api_key="sk-test", base_url="https://openrouter.ai/api/v1")
    assert isinstance(client, ModelClient)
    assert str(client._client.base_url).startswith("https://openrouter.ai/api/v1")
    client.close()


def test_openai_client_create_delegates_to_chat_completions():
    client = OpenAIClient(api_key="x")
    captured = {}

    class _Completions:
        def create(self, **kw):
            captured.update(kw)
            return {"ok": True}

    class _Chat:
        completions = _Completions()

    client._client = type("C", (), {"chat": _Chat()})()
    assert client.create(model="gpt-4o", messages=[]) == {"ok": True}
    assert captured["model"] == "gpt-4o"


def test_openai_client_generate_image_returns_b64_list():
    client = OpenAIClient(api_key="x")
    captured = {}

    class _Item:
        def __init__(self, b64):
            self.b64_json = b64

    class _Images:
        def generate(self, **kw):
            captured.update(kw)
            return type("R", (), {"data": [_Item("AAA="), _Item("BBB=")]})()

    client._client = type("C", (), {"images": _Images()})()
    out = client.generate_image(prompt="a fox", model="gpt-image-1", size="1024x1024", n=2)
    assert out == ["AAA=", "BBB="]
    assert captured == {"prompt": "a fox", "model": "gpt-image-1", "size": "1024x1024", "n": 2}


def test_openai_client_generate_image_omits_size_when_absent():
    client = OpenAIClient(api_key="x")
    captured = {}

    class _Images:
        def generate(self, **kw):
            captured.update(kw)
            return type("R", (), {"data": []})()

    client._client = type("C", (), {"images": _Images()})()
    client.generate_image(prompt="x", model="gpt-image-1")
    assert "size" not in captured


def test_base_client_generate_image_raises():
    class Bare(ModelClient):
        def create(self, **kwargs):
            return None

    with pytest.raises(RuntimeError):
        Bare().generate_image(prompt="x", model="m")


def test_resolve_api_key_picks_first_set_env_var():
    profile = ProviderProfile(name="p", env_vars=("FIRST_KEY", "SECOND_KEY"))
    assert resolve_api_key(profile, {"SECOND_KEY": "v2"}) == "v2"
    assert resolve_api_key(profile, {"FIRST_KEY": "v1", "SECOND_KEY": "v2"}) == "v1"
    assert resolve_api_key(profile, {}) is None


def test_build_client_dispatches_on_api_mode():
    anthropic = build_client(get_provider_profile("anthropic"), env={"ANTHROPIC_API_KEY": "k"})
    assert isinstance(anthropic, AnthropicClient)
    openai = build_client(get_provider_profile("openai"), env={"OPENAI_API_KEY": "k"})
    assert isinstance(openai, OpenAIClient)


def test_build_client_ollama_keyless_works_without_env():
    client = build_client(get_provider_profile("ollama"), env={})
    assert isinstance(client, OpenAIClient)
    assert str(client._client.base_url).startswith("http://localhost:11434/v1")
    assert client._client.api_key  # placeholder
    client.close()


def test_build_client_gemini_uses_openai_compat_endpoint():
    client = build_client(get_provider_profile("gemini"), env={"GEMINI_API_KEY": "g-key"})
    assert isinstance(client, OpenAIClient)
    assert "generativelanguage.googleapis.com" in str(client._client.base_url)
    assert client._client.api_key == "g-key"
    client.close()


def test_build_client_unknown_api_mode_raises():
    with pytest.raises(ValueError, match="api_mode"):
        build_client(ProviderProfile(name="x", api_mode="responses"))


def test_build_client_keyless_provider_gets_placeholder():
    # a keyless local endpoint (e.g. ollama) still needs a non-empty key for the
    # OpenAI SDK, but the user shouldn't have to set one
    profile = ProviderProfile(
        name="local", api_mode="chat_completions", base_url="http://x/v1", requires_api_key=False
    )
    client = build_client(profile, env={})
    assert client._client.api_key  # a placeholder, not empty
    client.close()


def test_build_client_keyed_provider_passes_real_key():
    profile = ProviderProfile(
        name="p", api_mode="chat_completions", env_vars=("P_KEY",), base_url="http://x/v1"
    )
    client = build_client(profile, env={"P_KEY": "sk-real"})
    assert client._client.api_key == "sk-real"
    client.close()


# --- streaming accumulator (assemble_streamed_response) ---


def _chunk(*, content=None, tool_calls=None, finish_reason=None, reasoning=None):
    delta = {"content": content, "tool_calls": tool_calls, "reasoning_content": reasoning}
    return {"choices": [{"delta": delta, "finish_reason": finish_reason}]}


def _tc(*, index=None, id=None, name=None, arguments=None):
    return {"index": index, "id": id, "function": {"name": name, "arguments": arguments}}


def test_stream_accumulates_text_and_fires_callback():
    seen = []
    raw = assemble_streamed_response(
        [_chunk(content="he"), _chunk(content="llo"), _chunk(finish_reason="stop")],
        on_text=seen.append,
    )
    assert seen == ["he", "llo"]
    message = raw["choices"][0]["message"]
    assert message["content"] == "hello"
    assert "tool_calls" not in message
    assert raw["choices"][0]["finish_reason"] == "stop"


def test_stream_assembles_tool_call_arguments_across_deltas():
    raw = assemble_streamed_response(
        [
            _chunk(tool_calls=[_tc(index=0, id="c1", name="terminal", arguments='{"cmd":')]),
            _chunk(tool_calls=[_tc(index=0, arguments='"ls"}')]),
            _chunk(finish_reason="tool_calls"),
        ]
    )
    calls = raw["choices"][0]["message"]["tool_calls"]
    assert calls == [
        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": '{"cmd":"ls"}'}}
    ]


def test_stream_keeps_two_tool_calls_separate_by_index():
    raw = assemble_streamed_response(
        [
            _chunk(tool_calls=[_tc(index=0, id="a", name="x", arguments="{}")]),
            _chunk(tool_calls=[_tc(index=1, id="b", name="y", arguments="{}")]),
        ]
    )
    calls = raw["choices"][0]["message"]["tool_calls"]
    assert [c["id"] for c in calls] == ["a", "b"]


def test_stream_falls_back_to_id_when_index_missing():
    # some compat servers omit index; a complete tool_call still survives
    raw = assemble_streamed_response(
        [_chunk(tool_calls=[_tc(id="c9", name="t", arguments="{}")])]
    )
    calls = raw["choices"][0]["message"]["tool_calls"]
    assert calls[0]["id"] == "c9"


def test_stream_appends_args_to_recent_slot_when_no_index_or_id():
    # an arg-only delta with neither index nor id appends to the most recent slot
    raw = assemble_streamed_response(
        [
            _chunk(tool_calls=[_tc(index=0, id="c1", name="t", arguments='{"a":')]),
            _chunk(tool_calls=[_tc(arguments='1}')]),
        ]
    )
    calls = raw["choices"][0]["message"]["tool_calls"]
    assert calls[0]["function"]["arguments"] == '{"a":1}'


def test_stream_fires_reasoning_callback():
    seen = []
    assemble_streamed_response([_chunk(reasoning="thinking")], on_reasoning=seen.append)
    assert seen == ["thinking"]


def test_stream_drops_incomplete_tool_call():
    # a slot that never got an id (or name) must be dropped, not emitted as null
    raw = assemble_streamed_response(
        [_chunk(tool_calls=[_tc(index=0, name="t", arguments='{"a":1}')])]
    )
    assert "tool_calls" not in raw["choices"][0]["message"]


def test_stream_empty_yields_null_content():
    raw = assemble_streamed_response([_chunk(), {"choices": []}])
    assert raw["choices"][0]["message"]["content"] is None


def test_stream_reads_attribute_style_sdk_chunks():
    # the real SDK yields attribute-style objects, not dicts
    class Node:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    delta = Node(content="hi", tool_calls=None, reasoning_content=None)
    chunk = Node(choices=[Node(delta=delta, finish_reason="stop")])
    raw = assemble_streamed_response([chunk])
    assert raw["choices"][0]["message"]["content"] == "hi"


# --- streamed usage capture (the 0-tokens-per-leaf bug, found live) ----------


def test_assembler_captures_the_final_usage_chunk():
    # With stream_options.include_usage the LAST chunk has empty choices and the
    # usage — the assembler used to skip it entirely ("usage": None forever).
    chunks = [
        {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2}},
    ]
    from lohra.agent.client import assemble_streamed_response

    result = assemble_streamed_response(iter(chunks))
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 2}
    assert result["choices"][0]["message"]["content"] == "hi"


def test_assembler_without_a_usage_chunk_keeps_none():
    from lohra.agent.client import assemble_streamed_response

    chunks = [{"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}]
    assert assemble_streamed_response(iter(chunks))["usage"] is None


def test_openai_stream_requests_usage_and_falls_back_when_refused():
    # Providers that reject stream_options must not break: retry without it
    # (usage stays None, exactly today's behavior).
    from lohra.agent.client import OpenAIClient

    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if "stream_options" in kwargs:
                raise TypeError("unexpected keyword argument 'stream_options'")
            return iter([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])

    client = OpenAIClient.__new__(OpenAIClient)
    client._client = type(
        "C", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()}
    )()
    result = client.stream(model="m", messages=[])
    assert result["choices"][0]["message"]["content"] == "ok"
    assert "stream_options" in calls[0] and "stream_options" not in calls[1]


def test_openai_stream_passes_include_usage_when_accepted():
    from lohra.agent.client import OpenAIClient

    seen = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)
            return iter(
                [
                    {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
                    {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1}},
                ]
            )

    client = OpenAIClient.__new__(OpenAIClient)
    client._client = type(
        "C", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()}
    )()
    result = client.stream(model="m", messages=[])
    assert seen["stream_options"] == {"include_usage": True}
    assert result["usage"] == {"prompt_tokens": 3, "completion_tokens": 1}


def test_openai_stream_does_not_retry_unrelated_errors():
    # Sol's finding: a blanket fallback would re-send the request on timeout/
    # 429/5xx — double generation, double billing. Only a stream_options
    # rejection may retry.
    import pytest

    from lohra.agent.client import OpenAIClient

    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            raise TimeoutError("read timed out")

    client = OpenAIClient.__new__(OpenAIClient)
    client._client = type(
        "C", (), {"chat": type("Ch", (), {"completions": FakeCompletions()})()}
    )()
    with pytest.raises(TimeoutError):
        client.stream(model="m", messages=[])
    assert len(calls) == 1  # never retried
