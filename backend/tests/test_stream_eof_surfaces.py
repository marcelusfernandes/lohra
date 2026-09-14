"""Real SDK/loop consumers: an uncertified turn is never persisted or published."""

from contextlib import closing, contextmanager
from dataclasses import replace
import json
import socket
import subprocess

import anthropic
from fastapi.testclient import TestClient
import httpx2
import openai
import pytest

from lohra import cli
from lohra.agent.agent import Agent
from lohra.agent.client import AnthropicClient, OpenAIClient, ResponsesClient
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.providers.errors import ProviderCallFailed
from lohra.server.app import create_openai_app
from lohra.server.service import CompletionService
from lohra.server.stream_workers import StreamWorkers
from lohra.state import SessionDB
from tests.subscription_fakes import own_login
from tests.test_stream_eof_create_abort import _anthropic_prefix, _body, _sse


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    def blocked(*args, **kwargs):
        raise AssertionError("external network/process forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


def _events(kind, *, tool=False, terminal=False):
    if kind == "anthropic":
        events = _anthropic_prefix()
        if tool:
            events += [
                {"type": "content_block_stop", "index": 0},
                {"type": "content_block_start", "index": 1, "content_block": {
                    "type": "tool_use", "id": "call_test", "name": "synthetic_tool", "input": {}}},
                {"type": "content_block_delta", "index": 1,
                 "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                {"type": "content_block_stop", "index": 1},
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
                 "usage": {"output_tokens": 5}},
            ]
        return events
    if kind == "chat":
        delta = {"content": "LAST"}
        if tool:
            delta["tool_calls"] = [{"index": 0, "id": "call_test", "type": "function",
                "function": {"name": "synthetic_tool", "arguments": "{}"}}]
        return [{"id": "chatcmpl_test", "object": "chat.completion.chunk", "created": 1,
                 "model": "synthetic", "choices": [{"index": 0, "delta": delta,
                                                      "finish_reason": None}]}]
    item = ({"id": "fc_test", "type": "function_call", "call_id": "call_test",
             "name": "synthetic_tool", "arguments": "{}", "status": "completed"} if tool else
            {"id": "msg_test", "type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "LAST", "annotations": []}]})
    events = [{"type": "response.output_item.done", "sequence_number": 0,
               "output_index": 0, "item": item}]
    if terminal:
        events.append({"type": "response.completed", "sequence_number": 1, "response": {
            "id": "resp_test", "object": "response", "created_at": 1, "status": "completed",
            "model": "synthetic", "output": [],
            "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}}})
    return events


@contextmanager
def _upstream(kind, *, tool=False, terminal=False, events=None):
    requests, closes = [], []
    payload = _sse(events if events is not None else _events(kind, tool=tool, terminal=terminal),
                   typed=kind != "chat")

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                               stream=_body(httpx2, payload, closes))

    http = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    sdk_type = anthropic.Anthropic if kind == "anthropic" else openai.OpenAI
    sdk = sdk_type(api_key="synthetic", base_url="https://synthetic.invalid",
                   http_client=http, max_retries=0)
    client_type = {"chat": OpenAIClient, "anthropic": AnthropicClient,
                   "responses": ResponsesClient}[kind]
    client = client_type.__new__(client_type)
    client._client = sdk
    if kind == "responses":
        client._credential_headers = None
    try:
        yield client, requests, closes
    finally:
        client.close()
        http.close()


def _agent(kind, client, calls):
    profile = get_provider_profile("anthropic" if kind == "anthropic" else "openai")
    if kind == "responses":
        profile = replace(profile, api_mode="responses")
    return Agent(model="synthetic", provider=profile, client=client, max_iterations=1,
        tool_definitions=({"type": "function", "function": {"name": "synthetic_tool",
                          "parameters": {"type": "object", "properties": {}}}},),
        tool_dispatch=lambda name, args: calls.append(name) or "{}")


@pytest.mark.parametrize("kind", ["chat", "anthropic", "responses"])
@pytest.mark.parametrize("tool", [False, True], ids=["text", "complete-looking-tool"])
def test_gateway_refuses_unterminated_turn_before_assistant_tool_or_persistence(tmp_path, kind, tool):
    calls, frames = [], []
    with _upstream(kind, tool=tool) as (client, requests, closes), closing(SessionDB(tmp_path / "db")) as db:
        agent = _agent(kind, client, calls)
        db.create_session("test", model="synthetic")
        prompt = agent.system_prompt()
        result = GatewaySession("test", agent, db).submit("A", frames.append)
        assert agent.system_prompt() is prompt
        assert not result["completed"] and not result["interrupted"]
        assert "terminal" in result["error"] and result["final_response"] is None
        assert result["usage_total"] is None and result["usage"] is None
        assert not any(m["role"] == "assistant" for m in result["messages"])
        assert calls == [] and db.load_messages("test") == []
        complete = frames[-1]["params"]["payload"]
        assert complete["status"] == "error" and "terminal" in complete["warning"]
        assert len(requests) == 1 and closes == [True]
        assert not client._client.is_closed()


@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
@pytest.mark.parametrize("streaming", [False, True])
def test_server_propagates_internal_sse_eof_without_success_or_observed_usage(monkeypatch, path, streaming):
    workers = []
    start = StreamWorkers.start

    def record(self, *args, **kwargs):
        worker = start(self, *args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(StreamWorkers, "start", record)
    with _upstream("responses") as (upstream, requests, closes):
        service = CompletionService(lambda: _agent("responses", upstream, []))
        app = create_openai_app(service)
        try:
            with TestClient(app) as http:
                body = {"model": "synthetic", "stream": streaming}
                body.update({"input": "A"} if path.endswith("responses") else
                            {"messages": [{"role": "user", "content": "A"}]})
                response = http.post(path, json=body)
            if streaming:
                assert response.status_code == 200 and "terminal" in response.text
                assert "response.completed" not in response.text
                payloads = [json.loads(line[6:]) for line in response.text.splitlines()
                            if line.startswith("data: ") and "[DONE]" not in line]
                assert not any(p.get("usage") for p in payloads)
                assert not any(c.get("finish_reason") for p in payloads for c in p.get("choices", []))
                if path.endswith("responses"):
                    failed = next(p for p in payloads if p.get("type") == "response.failed")
                    # Legacy ordinary-error wire zeros remain #133; these are
                    # distinct from the receipt's absent observed usage.
                    assert failed["response"]["usage"]["input_tokens"] == 0
                    assert failed["response"]["usage"]["output_tokens"] == 0
                assert workers[0].bridge.receipt.disposition == "failed"
                assert workers[0].bridge.receipt.usage is None
            else:
                assert response.status_code == 502
                assert "terminal" in response.json()["error"]["message"]
                assert "usage" not in response.json()
            assert len(requests) == 1 and requests[0]["stream"] is True and closes == [True]
            assert not upstream._client.is_closed()
        finally:
            for worker in workers:
                worker.thread.join(2)
            assert all(not worker.thread.is_alive() for worker in workers)
            assert app.state.stream_workers.snapshot() == ()


@pytest.mark.parametrize("terminal", [False, True])
def test_cli_json_uses_real_responses_create_and_preserves_failure(monkeypatch, tmp_path, capsys, terminal):
    home = tmp_path / "home"
    own_login(home, expiry=10**12)
    with _upstream("responses", terminal=terminal) as (client, requests, closes):
        monkeypatch.setattr("lohra.subscription.provider.build_subscription_client", lambda home: client)
        code = cli.run_chat("A", model="synthetic", session="test", use_tools=False, json_output=True)
        envelope = json.loads(capsys.readouterr().out)
        assert len(requests) == 1 and requests[0]["stream"] is True and closes == [True]
        assert client._client.is_closed()
    with closing(SessionDB(home / "state.db")) as db:
        messages = db.load_messages("test")
    if terminal:
        assert code == 0 and envelope["completed"] and envelope["output"] == "LAST"
        assert [m["role"] for m in messages] == ["user", "assistant"]
    else:
        assert code == 1 and not envelope["completed"] and envelope["output"] is None
        assert "terminal" in envelope["error"] and messages == []
        assert envelope["usage"] is None and envelope["usage_total"] is None
        assert envelope["tool_calls"] == []


@pytest.mark.parametrize("kind", ["chat", "anthropic", "responses"])
def test_last_callback_exception_physically_closes_sdk_stream(kind):
    events = None if kind != "responses" else [
        {"type": "response.output_text.delta", "sequence_number": 0,
         "item_id": "msg_test", "output_index": 0, "content_index": 0, "delta": "LAST"}]
    with _upstream(kind, events=events) as (client, requests, closes):
        def callback(text):
            assert text == "LAST"
            raise RuntimeError("callback failed")

        kwargs = {"model": "synthetic", "max_tokens": 16,
                  "messages": [{"role": "user", "content": "A"}]}
        if kind == "responses":
            kwargs = {"model": "synthetic", "input": "A"}
        with pytest.raises(RuntimeError, match="callback failed"):
            client.stream(on_text=callback, **kwargs)
        assert len(requests) == 1 and closes == [True]


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_real_responses_sdk_preserves_terminal_usage_or_native_failure(status):
    response = {"id": "resp_test", "object": "response", "created_at": 1,
                "status": status, "model": "synthetic", "output": [],
                "usage": {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}}
    if status == "failed":
        response["error"] = {"code": "rate_limit_exceeded", "message": "synthetic quota"}
    events = [{"type": f"response.{status}", "sequence_number": 0, "response": response}]
    with _upstream("responses", events=events) as (client, requests, closes):
        if status == "failed":
            with pytest.raises(ProviderCallFailed, match="synthetic quota") as caught:
                client.create(model="synthetic", input="A")
            assert caught.value.code == "rate_limit_exceeded"
        else:
            result = client.create(model="synthetic", input="A")
            assert result["status"] == status
            assert (result["usage"].input_tokens, result["usage"].output_tokens) == (11, 5)
        assert len(requests) == 1 and closes == [True]
