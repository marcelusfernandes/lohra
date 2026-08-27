"""Tests for the Responses-API transport + client (Fase 10, B2).

Unit-tested with dict fakes / a fake SDK — the live path (the real Codex backend,
its model slug + store/stream quirks) needs a user's subscription token to verify.
"""

from lohra.agent.client import ResponsesClient
from lohra.providers.transports import get_transport
from lohra.providers.transports.responses import ResponsesTransport

T = ResponsesTransport()


# --- build_kwargs ---


def test_system_becomes_instructions():
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": "hi"}], system="be terse")
    assert kw["instructions"] == "be terse"
    assert kw["input"] == [{"role": "user", "content": "hi"}]
    assert "messages" not in kw  # Responses uses `input`, not `messages`


def test_lifts_system_messages_into_instructions():
    kw = T.build_kwargs(
        model="m",
        messages=[{"role": "system", "content": "rule"}, {"role": "user", "content": "hi"}],
        system="top",
    )
    assert kw["instructions"] == "top\n\nrule"


def test_tool_calls_and_results_become_responses_items():
    kw = T.build_kwargs(
        model="m",
        messages=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read", "arguments": '{"p":1}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "DATA"},
        ],
    )
    assert kw["input"][0] == {"type": "function_call", "call_id": "c1", "name": "read", "arguments": '{"p":1}'}
    assert kw["input"][1] == {"type": "function_call_output", "call_id": "c1", "output": "DATA"}


def test_tools_use_flat_function_shape():
    kw = T.build_kwargs(
        model="m", messages=[{"role": "user", "content": "x"}],
        tools=[{"type": "function", "function": {"name": "f", "description": "d", "parameters": {"type": "object"}}}],
    )
    assert kw["tools"] == [{"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}]


def test_max_tokens_maps_to_max_output_tokens():
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], max_tokens=99)
    assert kw["max_output_tokens"] == 99 and "max_tokens" not in kw


def test_store_is_false():
    # the Codex backend 400s ("Store must be set to false") without this (verified live)
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}])
    assert kw["store"] is False


def test_forced_tool_choice():
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}], tool_choice="f")
    assert kw["tool_choice"] == {"type": "function", "name": "f"}


def test_registered_under_responses_api_mode():
    assert get_transport("responses") is not None


def test_include_requests_encrypted_reasoning():
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": "x"}])
    assert kw["include"] == ["reasoning.encrypted_content"]


def test_reasoning_with_encrypted_state_is_captured_for_replay():
    raw = {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "th"}],
         "encrypted_content": "ENC"},
        {"type": "message", "content": [{"type": "output_text", "text": "a"}]}]}
    nr = T.normalize_response(raw)
    assert nr.provider_data["reasoning_items"][0]["encrypted_content"] == "ENC"


def test_reasoning_without_encrypted_state_is_not_replayable():
    # only items with encrypted state can be replayed; others are dropped from provider_data
    raw = {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "th"}]}]}
    assert T.normalize_response(raw).provider_data is None


def test_reasoning_items_replayed_first_in_input():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a", "provider_data": {"reasoning_items": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "t"}],
             "encrypted_content": "ENC"}]}},
    ]
    items = T.build_kwargs(model="m", messages=msgs)["input"]
    # reasoning replayed (with encrypted_content) before the assistant text
    assert items[1]["type"] == "reasoning" and items[1]["encrypted_content"] == "ENC"
    assert items[2] == {"role": "assistant", "content": "a"}


# --- normalize_response ---


def test_normalize_message_text():
    raw = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}]}
    nr = T.normalize_response(raw)
    assert nr.content == "hello" and nr.finish_reason == "stop"


def test_normalize_function_call():
    raw = {"status": "completed", "output": [
        {"type": "function_call", "call_id": "x", "name": "read", "arguments": '{"p":1}'}]}
    nr = T.normalize_response(raw)
    assert nr.finish_reason == "tool_calls"
    assert nr.tool_calls[0].id == "x" and nr.tool_calls[0].name == "read"


def test_normalize_incomplete_is_length():
    raw = {"status": "incomplete", "output": []}
    assert T.normalize_response(raw).finish_reason == "length"


def test_normalize_usage():
    raw = {"status": "completed", "output": [], "usage": {"input_tokens": 7, "output_tokens": 3}}
    u = T.normalize_response(raw).usage
    assert u.input_tokens == 7 and u.output_tokens == 3


def test_normalize_reasoning():
    # the REAL SDK shape: ResponseReasoningItem.summary[].text (not a flat field)
    raw = {"status": "completed", "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking..."}]},
        {"type": "message", "content": [{"type": "output_text", "text": "ans"}]}]}
    nr = T.normalize_response(raw)
    assert nr.reasoning == "thinking..." and nr.content == "ans"


def test_normalize_refusal_surfaces_as_content():
    raw = {"status": "completed", "output": [
        {"type": "message", "content": [{"type": "refusal", "refusal": "I can't help with that."}]}]}
    assert T.normalize_response(raw).content == "I can't help with that."


# --- ResponsesClient wiring (fake SDK, no network) ---


def test_assemble_reconstructs_output_under_store_false():
    # store=false: completed.output is EMPTY; reconstruct from output_item.done +
    # fire on_text on deltas (verified live against the Codex backend).
    from lohra.agent.client import assemble_responses_stream

    events = [
        {"type": "response.output_text.delta", "delta": "he"},
        {"type": "response.output_text.delta", "delta": "llo"},
        {"type": "response.output_item.done",
         "item": {"type": "message", "content": [{"type": "output_text", "text": "hello"}]}},
        {"type": "response.completed",
         "response": {"status": "completed", "output": [], "usage": {"input_tokens": 1, "output_tokens": 2}}},
    ]
    seen = []
    out = assemble_responses_stream(events, on_text=seen.append)
    assert seen == ["he", "llo"]
    assert out["status"] == "completed" and out["usage"]["output_tokens"] == 2
    # and it normalizes correctly through the transport
    nr = T.normalize_response(out)
    assert nr.content == "hello"


def test_assemble_raises_on_failed_with_error():
    # a response.failed must surface the provider error, not collapse to empty stop
    import pytest

    from lohra.agent.client import assemble_responses_stream

    events = [{"type": "response.failed", "response": {
        "status": "failed", "error": {"code": "rate_limit", "message": "slow down"}}}]
    with pytest.raises(RuntimeError) as exc:
        assemble_responses_stream(events)
    assert "rate_limit" in str(exc.value) and "slow down" in str(exc.value)


def test_user_image_parts_become_input_image():
    # vision: an image_url part must survive as a Responses input_image (not dropped)
    kw = T.build_kwargs(model="m", messages=[{"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,XYZ"}}]}])
    content = kw["input"][0]["content"]
    assert {"type": "input_text", "text": "what is this?"} in content
    assert {"type": "input_image", "image_url": "data:image/png;base64,XYZ"} in content


def test_assemble_prefers_completed_output_when_present():
    # store=true variant: the terminal event carries the full output -> use it
    from lohra.agent.client import assemble_responses_stream

    events = [{"type": "response.completed", "response": {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "X"}]}]}}]
    out = assemble_responses_stream(events)
    assert out["output"][0]["content"][0]["text"] == "X"


def test_assemble_collects_function_calls():
    from lohra.agent.client import assemble_responses_stream

    events = [
        {"type": "response.output_item.done",
         "item": {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"}},
        {"type": "response.completed", "response": {"status": "completed", "output": []}},
    ]
    nr = T.normalize_response(assemble_responses_stream(events))
    assert nr.finish_reason == "tool_calls" and nr.tool_calls[0].name == "read"


def test_client_calls_responses_create_with_headers(monkeypatch):
    import openai

    captured = {}

    class _Responses:
        def create(self, **kwargs):
            captured["create"] = kwargs
            return [  # create() always streams; return events to assemble
                {"type": "response.output_item.done",
                 "item": {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}},
                {"type": "response.completed", "response": {"status": "completed", "output": []}},
            ]

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.responses = _Responses()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    client = ResponsesClient(
        api_key="tok", base_url="https://x/codex", default_headers={"ChatGPT-Account-ID": "a"}
    )
    result = client.create(model="m", input=[])
    assert captured["init"]["api_key"] == "tok"
    assert captured["init"]["base_url"] == "https://x/codex"
    assert captured["init"]["default_headers"] == {"ChatGPT-Account-ID": "a"}
    # create() must force stream=True (the Codex backend requires it)
    assert captured["create"] == {"model": "m", "input": [], "stream": True}
    assert result["output"][0]["content"][0]["text"] == "hi"  # assembled from the stream
