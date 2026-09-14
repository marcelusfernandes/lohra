"""Terminal structure and the final abort point, without external I/O."""

import json
import socket
import subprocess

import httpx2
import openai
import pytest

from lohra.agent.client import (
    assemble_anthropic_stream,
    assemble_responses_stream,
    assemble_streamed_response,
)
from lohra.agent.stream_abort import is_aborted
from tests.test_stream_abort import AnthropicStream, RecordingStream


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("external network/process forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@pytest.mark.parametrize("reason", [None, "", " \t\n", 7, False])
@pytest.mark.parametrize("kind", ["chat", "anthropic"])
def test_finish_reason_is_nonblank_text(kind, reason):
    if kind == "chat":
        stream = RecordingStream([{"choices": [{"delta": {}, "finish_reason": reason}]}])
        assemble = assemble_streamed_response
    else:
        stream = AnthropicStream([{"type": "message_stop"}], final={"stop_reason": reason})
        assemble = assemble_anthropic_stream
    with pytest.raises(ValueError, match="stream.*terminal"):
        assemble(stream)
    assert stream.closed == 1


@pytest.mark.parametrize("kind", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("last_callback", [False, True], ids=["empty", "last-reasoning"])
def test_abort_at_eof_precedes_terminal_validation(kind, last_callback):
    interrupted = not last_callback
    seen = []

    def reasoning(text):
        nonlocal interrupted
        seen.append(text)
        interrupted = True

    event = {
        "chat": {"choices": [{"delta": {"reasoning_content": "LAST"}}]},
        "anthropic": {"type": "content_block_delta",
                      "delta": {"type": "thinking_delta", "thinking": "LAST"}},
        "responses": {"type": "response.reasoning_summary_text.done", "text": "LAST"},
    }[kind]
    stream_type = AnthropicStream if kind == "anthropic" else RecordingStream
    stream = stream_type([event] if last_callback else [])
    assemble = {"chat": assemble_streamed_response, "anthropic": assemble_anthropic_stream,
                "responses": assemble_responses_stream}[kind]
    result = assemble(stream, on_reasoning=reasoning, abort_check=lambda: interrupted)
    assert is_aborted(result) and stream.closed == 1
    assert seen == (["LAST"] if last_callback else [])


@pytest.mark.parametrize("terminal", [False, True], ids=["done-only", "finish-then-usage"])
def test_real_chat_sdk_requires_finish_and_drains_trailing_usage(terminal):
    closes, requests, seen = [], [], []
    chunk = {"id": "chatcmpl_test", "object": "chat.completion.chunk", "created": 1,
             "model": "synthetic", "choices": [{"index": 0, "delta": {"content": "TEXT"},
                                                  "finish_reason": None}]}
    events = [chunk]
    if terminal:
        events.append({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    events.append({**chunk, "choices": [], "usage": {
        "prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16}})
    payload = ("".join(f"data: {json.dumps(event)}\n\n" for event in events)
               + "data: [DONE]\n\n").encode()

    class Body(httpx2.SyncByteStream):
        def __iter__(self):
            yield payload

        def close(self):
            closes.append(True)

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

    transport = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    sdk = openai.OpenAI(api_key="synthetic", base_url="https://synthetic.invalid",
                        http_client=transport, max_retries=0)
    result, error = None, None
    try:
        stream = sdk.chat.completions.create(model="synthetic", messages=[], stream=True,
                                             stream_options={"include_usage": True})
        try:
            result = assemble_streamed_response(stream, on_text=seen.append)
        except ValueError as exc:
            error = exc
    finally:
        sdk.close()
        transport.close()
    assert len(requests) == 1 and requests[0]["stream_options"] == {"include_usage": True}
    assert closes == [True] and seen == ["TEXT"]
    if terminal:
        assert error is None and result["choices"][0]["message"]["content"] == "TEXT"
        assert (result["usage"].prompt_tokens, result["usage"].completion_tokens) == (11, 5)
    else:
        assert result is None and isinstance(error, ValueError)
        assert "terminal" in str(error)


@pytest.mark.parametrize("terminal", ["completed", "incomplete"])
def test_responses_status_fallback_comes_from_observed_terminal(terminal):
    stream = RecordingStream([{"type": f"response.{terminal}"}])
    result = assemble_responses_stream(stream)
    assert result == {"status": terminal, "output": [], "usage": None}
    assert stream.closed == 1


@pytest.mark.parametrize("kind", ["chat", "anthropic"])
def test_unknown_nonblank_reason_is_preserved_for_native_interpretation(kind):
    reason = "future_vendor_reason"
    if kind == "chat":
        result = assemble_streamed_response([
            {"choices": [{"delta": {}, "finish_reason": reason}]}])
        assert result["choices"][0]["finish_reason"] == reason
    else:
        stream = AnthropicStream([{"type": "message_stop"}], final={"stop_reason": reason})
        assert assemble_anthropic_stream(stream)["stop_reason"] == reason
