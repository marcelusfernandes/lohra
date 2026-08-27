"""Tests for the OpenAI-compatible FastAPI app (TestClient, fake service)."""

import json

from fastapi.testclient import TestClient

from lohra.server.app import create_openai_app
from lohra.server.format import CompletionError, UpstreamError


class FakeService:
    def __init__(self, *, content="hi", deltas=None, error=None):
        self.content = content
        self.deltas = deltas or []
        self.error = error
        self.calls = []

    def run(self, *, model, messages, temperature=None, max_tokens=None, on_delta=None):
        self.calls.append({"model": model, "messages": messages})
        if self.error is not None:
            raise self.error
        if on_delta:
            for d in self.deltas:
                on_delta(d)
        return {
            "model": model,
            "content": self.content,
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }


def _client(service, *, api_key=None, models=("claude-opus-4-8",)):
    return TestClient(create_openai_app(service, api_key=api_key, models=models))


def _body(stream=False):
    return {"model": "claude-opus-4-8", "messages": [{"role": "user", "content": "hi"}], "stream": stream}


# --- non-streaming ---


def test_chat_completion_returns_openai_shape():
    client = _client(FakeService(content="hello"))
    resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "chat.completion"
    assert data["choices"][0]["message"]["content"] == "hello"
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["id"].startswith("chatcmpl-")
    assert data["model"] == "claude-opus-4-8"


def test_service_receives_model_and_messages():
    service = FakeService()
    _client(service).post("/v1/chat/completions", json=_body())
    assert service.calls[0]["model"] == "claude-opus-4-8"
    assert service.calls[0]["messages"][-1]["content"] == "hi"


# --- streaming ---


def test_streaming_emits_sse_chunks_and_done():
    client = _client(FakeService(content="hello", deltas=["he", "llo"]))
    resp = client.post("/v1/chat/completions", json=_body(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = [ln for ln in resp.text.split("\n\n") if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    # collect the content deltas
    chunks = [json.loads(ln[len("data: ") :]) for ln in lines if not ln.endswith("[DONE]")]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "hello"
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_streaming_emits_error_event_then_done():
    client = _client(FakeService(error=UpstreamError("boom")))
    resp = client.post("/v1/chat/completions", json=_body(stream=True))
    # the stream has already started (200); the error is delivered as an SSE event
    assert resp.status_code == 200
    assert "boom" in resp.text
    assert resp.text.strip().endswith("[DONE]")


# --- errors ---


def test_streaming_bad_request_returns_400_not_a_200_stream():
    # validation happens before the stream starts, so a malformed streaming
    # request still gets a real 400 (not a 200 with an error event)
    client = _client(FakeService())
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "assistant", "content": "x"}], "stream": True},
    )
    assert resp.status_code == 400
    assert "user message" in resp.json()["error"]["message"]


def test_bad_request_returns_400():
    client = _client(FakeService(error=CompletionError("the last message must be a user message")))
    resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 400
    assert "user message" in resp.json()["error"]["message"]


def test_upstream_error_returns_502():
    client = _client(FakeService(error=UpstreamError("provider 500")))
    resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 502
    assert resp.json()["error"]["message"] == "provider 500"


def test_invalid_body_returns_422():
    # missing 'messages' -> FastAPI validation error
    resp = _client(FakeService()).post("/v1/chat/completions", json={"model": "m"})
    assert resp.status_code == 422


# --- auth ---


def test_requires_bearer_token_when_configured():
    client = _client(FakeService(), api_key="secret")
    assert client.post("/v1/chat/completions", json=_body()).status_code == 401
    ok = client.post(
        "/v1/chat/completions", json=_body(), headers={"Authorization": "Bearer secret"}
    )
    assert ok.status_code == 200


def test_wrong_token_is_rejected():
    client = _client(FakeService(), api_key="secret")
    resp = client.post(
        "/v1/chat/completions", json=_body(), headers={"Authorization": "Bearer nope"}
    )
    assert resp.status_code == 401


def test_open_when_no_key_configured():
    assert _client(FakeService()).post("/v1/chat/completions", json=_body()).status_code == 200


# --- models + health ---


def test_models_endpoint_lists_configured_models():
    client = _client(FakeService(), models=("claude-opus-4-8", "gpt-4o"))
    data = client.get("/v1/models").json()
    assert data["object"] == "list"
    assert [m["id"] for m in data["data"]] == ["claude-opus-4-8", "gpt-4o"]


def test_models_endpoint_respects_auth():
    client = _client(FakeService(), api_key="secret")
    assert client.get("/v1/models").status_code == 401
    assert client.get("/v1/models", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_health_is_open_and_ok():
    client = _client(FakeService(), api_key="secret")
    resp = client.get("/health")
    assert resp.status_code == 200  # health needs no auth
    assert resp.json()["ok"] is True


# --- /v1/responses ---


def _responses_body(stream=False):
    return {"model": "claude-opus-4-8", "input": "hi", "stream": stream}


def test_responses_returns_response_object():
    client = _client(FakeService(content="hello"))
    resp = client.post("/v1/responses", json=_responses_body())
    assert resp.status_code == 200
    data = resp.json()
    assert data["object"] == "response"
    assert data["status"] == "completed"
    assert data["output_text"] == "hello"
    assert data["output"][0]["content"][0]["text"] == "hello"
    assert data["id"].startswith("resp_")
    assert data["usage"]["input_tokens"] == 1  # remapped from prompt_tokens


def test_responses_instructions_become_a_system_message():
    service = FakeService()
    client = _client(service)
    client.post("/v1/responses", json={"model": "m", "input": "hi", "instructions": "be terse"})
    msgs = service.calls[0]["messages"]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[-1]["content"] == "hi"


def test_responses_streaming_emits_typed_events():
    client = _client(FakeService(content="hello", deltas=["he", "llo"]))
    resp = client.post("/v1/responses", json=_responses_body(stream=True))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    text = resp.text
    assert "event: response.created" in text
    assert "event: response.output_text.delta" in text
    assert "event: response.completed" in text
    # the deltas concatenate to the streamed text
    deltas = [
        json.loads(block.split("data: ", 1)[1])["delta"]
        for block in text.split("\n\n")
        if "response.output_text.delta" in block
    ]
    assert "".join(deltas) == "hello"


def test_responses_streaming_failure_emits_response_failed():
    client = _client(FakeService(error=UpstreamError("boom")))
    resp = client.post("/v1/responses", json=_responses_body(stream=True))
    assert resp.status_code == 200  # stream already started
    assert "event: response.failed" in resp.text
    assert "boom" in resp.text


def test_responses_bad_input_returns_400():
    client = _client(FakeService())
    resp = client.post("/v1/responses", json={"model": "m", "input": ""})
    assert resp.status_code == 400


def test_responses_completion_error_returns_400():
    client = _client(FakeService(error=CompletionError("nope")))
    resp = client.post("/v1/responses", json=_responses_body())
    assert resp.status_code == 400


def test_responses_upstream_error_returns_502():
    client = _client(FakeService(error=UpstreamError("provider down")))
    resp = client.post("/v1/responses", json=_responses_body())
    assert resp.status_code == 502


def test_responses_respects_auth():
    client = _client(FakeService(), api_key="secret")
    assert client.post("/v1/responses", json=_responses_body()).status_code == 401
    ok = client.post(
        "/v1/responses", json=_responses_body(), headers={"Authorization": "Bearer secret"}
    )
    assert ok.status_code == 200
