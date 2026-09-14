"""Hermetic real SDK -> ASGI -> Service/Agent fixtures for #133."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import socket
import subprocess

from fastapi.testclient import TestClient
import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.agent.stream_abort import AbortedStream
from lohra.providers import get_provider_profile
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))

    def blocked(*args, **kwargs):
        raise AssertionError("no external network or process in relay contracts")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


KNOWN = {"prompt_tokens": 11, "completion_tokens": 5,
         "prompt_tokens_details": {"cached_tokens": 2},
         "completion_tokens_details": {"reasoning_tokens": 3}}
ZERO = {"prompt_tokens": 0, "completion_tokens": 0}


def chat(reason="stop", content="TEXT", usage=KNOWN, *, tool=False):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [{"id": "tc", "type": "function", "function": {
            "name": "synthetic_tool", "arguments": "{}"}}]
    return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else reason}],
            "usage": deepcopy(usage)}


def factory(script, mode="chat_completions", *, calls=None, before_run=False):
    class Client(ModelClient):
        def __init__(self):
            self.script = deepcopy(script)

        def create(self, **kwargs):
            if calls is not None:
                calls.append(kwargs)
            raw = self.script.pop(0)
            if isinstance(raw, Exception):
                raise raw
            return raw

        def stream(self, *, on_text=None, **kwargs):
            raw = self.create(**kwargs)
            if on_text:
                if isinstance(raw, AbortedStream):
                    on_text("partial before abort")
                elif isinstance(raw, dict):
                    message = (raw.get("choices") or [{}])[0].get("message", {})
                    if message.get("content"):
                        on_text(message["content"])
            return raw

    def build():
        agent = Agent(model="synthetic", provider=replace(get_provider_profile("openai"), api_mode=mode),
                      client=Client(), max_iterations=max(1, len(script)),
                      tool_dispatch=lambda name, args: "{}")
        if before_run:
            agent.request_interrupt()
        return agent
    return build


@contextmanager
def relay(script, mode="chat_completions", *, before_run=False, agent_factory=None, stream_limits=None):
    calls, requests, closes = [], [], []
    app = create_openai_app(CompletionService(agent_factory or factory(script, mode, calls=calls,
                                                    before_run=before_run)), api_key="synthetic",
                            stream_limits=stream_limits)
    try:
        with TestClient(app) as local:
            class Body(httpx2.SyncByteStream):
                def __init__(self, payload):
                    self.payload = payload

                def __iter__(self):
                    for pos in range(0, len(self.payload), 23):
                        yield self.payload[pos:pos + 23]

                def close(self):
                    closes.append(True)

            def handle(request):
                requests.append(json.loads(request.content))
                reply = local.post(request.url.path, content=request.content, headers={
                    "Authorization": "Bearer synthetic", "Content-Type": "application/json"})
                return httpx2.Response(reply.status_code, headers=dict(reply.headers), stream=Body(reply.content))

            http = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
            sdk = openai.OpenAI(api_key="synthetic", base_url="https://relay.invalid/v1",
                                http_client=http, max_retries=0)
            try:
                yield sdk, local, calls, requests, closes
            finally:
                sdk.close()
                http.close()
    finally:
        assert app.state.stream_workers.snapshot() == ()


def request(sdk, endpoint, streamed):
    if endpoint == "chat":
        return sdk.chat.completions.create(model="synthetic", messages=[{"role": "user", "content": "A"}],
            stream=streamed, **({"stream_options": {"include_usage": True}} if streamed else {}))
    return sdk.responses.create(model="synthetic", input="A", stream=streamed)
