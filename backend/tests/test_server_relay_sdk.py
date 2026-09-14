"""#133 coordinator preparation: real SDK over controlled ASGI, no production edits."""

from contextlib import contextmanager
from dataclasses import asdict
import json
import socket
import subprocess

from fastapi.testclient import TestClient
import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient, ResponsesClient
from lohra.agent.loop import run_conversation
from lohra.providers import get_provider_profile
from dataclasses import replace
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    def blocked(*args, **kwargs):
        raise AssertionError("no external network/process in relay preparation")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@contextmanager
def relay(reason, text):
    calls = []
    requests = []
    closes = []

    class Upstream(ModelClient):
        def create(self, **kwargs):
            calls.append(True)
            return {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 11,
                    "completion_tokens": 5,
                    "total_tokens": 16,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            }

        def stream(self, *, on_text=None, **kwargs):
            if on_text and text:
                on_text(text)
            return self.create(**kwargs)

    service = CompletionService(
        lambda: Agent(
            model="synthetic",
            provider=get_provider_profile("openai"),
            client=Upstream(),
            max_iterations=1,
        )
    )
    app = create_openai_app(service, api_key="synthetic")
    try:
        with TestClient(app) as local:

            class Body(httpx2.SyncByteStream):
                def __init__(self, data):
                    self.data = data

                def __iter__(self):
                    for pos in range(0, len(self.data), 23):
                        yield self.data[pos : pos + 23]

                def close(self):
                    closes.append(True)

            def handle(request):
                requests.append(json.loads(request.content))
                reply = local.post(
                    request.url.path,
                    content=request.content,
                    headers={
                        "Authorization": "Bearer synthetic",
                        "Content-Type": "application/json",
                    },
                )
                return httpx2.Response(
                    reply.status_code,
                    headers=dict(reply.headers),
                    stream=Body(reply.content),
                )

            http = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
            sdk = openai.OpenAI(
                api_key="synthetic",
                base_url="https://relay.invalid/v1",
                http_client=http,
                max_retries=0,
            )
            try:
                yield sdk, calls, requests, closes
            finally:
                sdk.close()
                http.close()
    finally:
        assert app.state.stream_workers.snapshot() == ()


@pytest.mark.parametrize("streamed", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize(
    "endpoint,reason,text",
    [
        ("chat", "stop", "OK"),
        ("responses", "stop", "OK"),
        ("chat", "length", None),
        ("chat", "content_filter", "refused"),
        ("responses", "length", "PARTIAL"),
        ("responses", "length", None),
    ],
    ids=[
        "chat-stop",
        "responses-stop",
        "chat-null-length",
        "chat-filter",
        "responses-length",
        "responses-null-length",
    ],
)
def test_actual_sdk_preserves_relay_terminal(endpoint, reason, text, streamed):
    with relay(reason, text) as (sdk, calls, requests, closes):
        if endpoint == "chat":
            kwargs = {
                "model": "synthetic",
                "messages": [{"role": "user", "content": "A"}],
                "stream": streamed,
            }
            if streamed:
                kwargs["stream_options"] = {"include_usage": True}
            raw = sdk.chat.completions.create(**kwargs)
            if streamed:
                chunks = list(raw)
                finish = [
                    c.finish_reason
                    for e in chunks
                    for c in e.choices
                    if c.finish_reason
                ]
                usage = next(e.usage for e in reversed(chunks) if e.usage is not None)
                output = "".join(
                    c.delta.content or "" for e in chunks for c in e.choices
                )
                observed = {
                    "finish": finish,
                    "output": output,
                    "usage": usage.model_dump(),
                }
            else:
                observed = {
                    "finish": [raw.choices[0].finish_reason],
                    "output": raw.choices[0].message.content,
                    "usage": raw.usage.model_dump(),
                }
            print(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "reason": reason,
                        "stream": streamed,
                        "observed": observed,
                    }
                )
            )
            assert calls == [True] and len(requests) == 1 and closes == [True]
            assert (
                observed["usage"]["prompt_tokens"] == 11
                and observed["usage"]["completion_tokens"] == 5
            )
            assert observed["usage"]["prompt_tokens_details"]["cached_tokens"] == 2
            assert (
                observed["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
            )
            assert observed["finish"] == [reason]
        else:
            raw = sdk.responses.create(model="synthetic", input="A", stream=streamed)
            if streamed:
                events = list(raw)
                terminals = [
                    e
                    for e in events
                    if e.type
                    in ("response.completed", "response.incomplete", "response.failed")
                ]
                assert len(terminals) == 1
                raw = terminals[0].response
                event = terminals[0].type
            else:
                event = None
            observed = {
                "status": raw.status,
                "incomplete_details": raw.incomplete_details.model_dump()
                if raw.incomplete_details
                else None,
                "event": event,
                "usage": raw.usage.model_dump() if raw.usage else None,
            }
            print(
                json.dumps(
                    {
                        "endpoint": endpoint,
                        "reason": reason,
                        "stream": streamed,
                        "observed": observed,
                    }
                )
            )
            assert calls == [True] and len(requests) == 1 and closes == [True]
            assert (
                observed["usage"]["input_tokens"] == 11
                and observed["usage"]["output_tokens"] == 5
            )
            assert observed["usage"]["input_tokens_details"]["cached_tokens"] == 2
            assert observed["usage"]["output_tokens_details"]["reasoning_tokens"] == 3
            expected = "incomplete" if reason == "length" else "completed"
            assert observed["status"] == expected
            if streamed:
                assert event == "response." + expected
            if reason == "length":
                assert observed["incomplete_details"] is not None


@pytest.mark.parametrize("reason", ["stop", "length"])
def test_relay_stream_consumed_by_real_lohra_preserves_truncation(reason):
    with relay(reason, "TEXT") as (sdk, calls, requests, closes):
        client = ResponsesClient.__new__(ResponsesClient)
        client._client = sdk
        client._credential_headers = None
        profile = replace(get_provider_profile("openai"), api_mode="responses")
        agent = Agent(
            model="synthetic", provider=profile, client=client, max_iterations=1
        )
        prompt = agent.system_prompt()
        result = run_conversation(agent, "A", stream_delta_callback=lambda text: None)
        print(json.dumps({"reason": reason, "result": result}, default=asdict))
        assert agent.system_prompt() is prompt
        assert calls == [True] and len(requests) == 1 and closes == [True]
        assert result["final_response"] == "TEXT" and not result["error"]
        usage = result["usage_total"]
        assert (
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.reasoning_tokens,
        ) == (9, 5, 2, 3)
        assert result["partial"] is (reason == "length")
        assert result["messages"][-1]["finish_reason"] == reason
