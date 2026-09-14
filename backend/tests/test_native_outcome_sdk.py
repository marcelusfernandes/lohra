"""#132 SDK regressions adopted from the frozen 13-case author preparation."""

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import socket
import subprocess

import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ResponsesClient
from lohra.agent.loop import run_conversation
from lohra.agent.result_json import build_envelope
from lohra.providers import get_provider_profile
from lohra.providers.transports import get_transport
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService


TOOL = {"type": "function", "function": {"name": "synthetic_tool",
        "parameters": {"type": "object", "properties": {}}}}
FIRST = {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16,
         "input_tokens_details": {"cached_tokens": 2},
         "output_tokens_details": {"reasoning_tokens": 3}}
SECOND = {"input_tokens": 7, "output_tokens": 4, "total_tokens": 11,
          "input_tokens_details": {"cached_tokens": 1},
          "output_tokens_details": {"reasoning_tokens": 2}}
PROFILE = replace(get_provider_profile("openai"), api_mode="responses")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    def blocked(*args, **kwargs):
        raise AssertionError("no external network/process in native preparation")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


def _call(status="completed", name="synthetic_tool"):
    item = {"type": "function_call", "id": "fc_test", "call_id": "call_test",
            "name": name, "arguments": "{}"}
    if status is not None:
        item["status"] = status
    return item


def _response(status="completed", *, item=None, usage=None):
    return {"id": "resp_test", "object": "response", "created_at": 1,
            "status": status, "model": "synthetic", "output": [item] if item else [],
            "usage": usage, "error": {"code": "rate_limit_exceeded", "message": "synthetic"}
            if status == "failed" else None}


def _text():
    return {"type": "message", "id": "msg_test", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "OK", "annotations": []}]}


@contextmanager
def _sdk(script):
    requests, closes = [], []

    class Body(httpx2.SyncByteStream):
        def __init__(self, events):
            self.events = events

        def __iter__(self):
            for event in self.events:
                yield f'data: {json.dumps(event)}\n\n'.encode()

        def close(self):
            closes.append(True)

    def handle(request):
        parsed = json.loads(request.content)
        index = len(requests)
        requests.append(parsed)
        raw = script[index](parsed) if callable(script[index]) else script[index]
        events = [{"type": "response.output_item.done", "sequence_number": n,
                   "output_index": n, "item": item} for n, item in enumerate(raw["output"])]
        events.append({"type": f'response.{raw["status"]}', "sequence_number": len(events),
                       "response": {**raw, "output": []}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Body(events))

    transport = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    sdk = openai.OpenAI(api_key="synthetic", base_url="https://synthetic.invalid",
                        http_client=transport, max_retries=0)
    client = ResponsesClient.__new__(ResponsesClient)
    client._client, client._credential_headers = sdk, None
    try:
        yield client, requests, closes
    finally:
        client.close()
        transport.close()


def _run(script):
    effects = []
    with _sdk(script) as (client, requests, closes):
        agent = Agent(model="synthetic", provider=PROFILE, client=client, max_iterations=2,
            tool_definitions=(TOOL,), tool_dispatch=lambda name, args: effects.append(name) or "{}")
        snapshot = agent.system_prompt()
        result = run_conversation(agent, "A", stream_delta_callback=lambda text: None)
        assert agent.system_prompt() is snapshot
        envelope = build_envelope("A", result, model="synthetic", temperature=None, session_id="test")
        assert len(closes) == len(requests)
        print(json.dumps({"result": result, "envelope": envelope, "effects": effects,
                          "requests": len(requests), "closes": len(closes)}, default=asdict))
        return result, envelope, effects, len(requests)


@pytest.mark.parametrize("reported", [False, True])
def test_second_failed_stream_preserves_exact_reported_usage_once(reported):
    result, envelope, effects, requests = _run([
        _response(item=_call(), usage=FIRST),
        _response("failed", usage=SECOND if reported else None),
    ])
    assert effects == ["synthetic_tool"] and requests == 2
    assert not result["completed"] and result["error_kind"] == "quota_exhausted"
    assert result["final_response"] is None
    total = result["usage_total"]
    assert (total.input_tokens, total.output_tokens, total.cache_read_tokens,
            total.cache_write_tokens, total.reasoning_tokens) == (
                (15, 9, 3, 0, 5) if reported else (9, 5, 2, 0, 3))


def test_second_failure_has_native_diagnostic_without_replaying_previous_tool_stop():
    result, envelope, effects, requests = _run([
        _response(item=_call(), usage=FIRST), _response("failed"),
    ])
    assert result["error"] and effects == ["synthetic_tool"] and requests == 2
    # Turn diagnostics exist even when the rejected call appends no assistant.
    native = envelope.get("native_outcome")
    assert native is not None and native["status"] == "failed"
    assert native["error_code"] == "rate_limit_exceeded"
    assert envelope["stop_reason"] is None


@pytest.mark.parametrize("item_status", [None, "completed", "incomplete", "in_progress", "unknown"])
def test_function_item_status_does_not_override_completed_authority(item_status):
    result, envelope, effects, requests = _run([
        _response(item=_call(item_status), usage=FIRST), _response(item=_text(), usage=SECOND),
    ])
    valid = item_status in (None, "completed")
    if valid:
        assert effects == ["synthetic_tool"] and requests == 2 and result["completed"]
        assert result["final_response"] == "OK"
    else:
        assert effects == [] and requests == 1 and result["error"]
        assert not any(m["role"] == "assistant" for m in result["messages"])


@pytest.mark.parametrize("item_status,terminal_status", [
    (None, "completed"), ("incomplete", "completed"),
    # Three terminal-status controls adapted from the historical forced-cache probe.
    ("completed", "completed"), ("completed", "incomplete"), ("completed", "failed"),
])
def test_forced_item_status_controls_cold_workflow_cache(
    tmp_path, monkeypatch, item_status, terminal_status,
):
    home = tmp_path / "home"
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    def reply(request):
        response = _response(terminal_status,
            item=_call(item_status, request["tool_choice"]["name"]), usage=FIRST)
        if terminal_status == "failed":
            response["error"] = {"code": "server_error", "message": "synthetic failure"}
        return response

    with _sdk([reply]) as (
            client, requests, closes):
        def factory():
            return Agent(model="synthetic", provider=PROFILE, client=client, max_iterations=1)

        db = SessionDB(home / "state.db")
        service = WorkflowService(base_child_factory=factory, db=db, home=home)
        try:
            accepted = service.start({"meta": {"name": "synthetic-native-item"}, "nodes": [
                {"id": "leaf", "type": "agent", "prompt": "A", "tool_less": True,
                 "schema": {"type": "object"}, "retries": 0}]})
            rid = accepted["run_id"]
            result = service.status(rid, wait=True, timeout=5)
            cached = db._connection.execute('SELECT COUNT(*) FROM workflow_node_cache').fetchone()[0]
        finally:
            service.shutdown()
            db.close()
        replay = None
        if result["status"] == "complete":
            db = SessionDB(home / "state.db")
            service = WorkflowService(base_child_factory=factory, db=db, home=home)
            try:
                accepted = service.start(resume_run_id=rid)
                replay = service.status(rid, wait=True, timeout=5)
            finally:
                service.shutdown()
                db.close()
        print(json.dumps({"item_status": item_status, "result": result, "cache_rows": cached,
                          "replay": replay, "requests": len(requests), "closes": len(closes)}))
        assert len(requests) == len(closes) == 1
        if terminal_status == "completed" and item_status in (None, "completed"):
            assert result["status"] == replay["status"] == "complete" and cached == 1
            assert result["outputs"]["leaf"] == replay["outputs"]["leaf"] == {}
        else:
            assert result["status"] != "complete" and cached == 0


@pytest.mark.parametrize("mode", ["chat_completions", "anthropic_messages", "responses"])
def test_existing_metadata_container_roundtrips_cold_without_new_model_input(tmp_path, mode):
    native = {"api_mode": mode, "status": "completed", "reason": "synthetic_reason"}
    provider_data = {"native_outcome": native}
    if mode == "responses":
        provider_data["reasoning_items"] = [{"type": "reasoning", "summary": [],
                                             "encrypted_content": "SYNTHETIC_ENCRYPTED"}]
    elif mode == "anthropic_messages":
        provider_data["thinking_blocks"] = [{"type": "thinking", "thinking": "synthetic",
                                             "signature": "SYNTHETIC_SIGNATURE"}]
    db = SessionDB(tmp_path / "db")
    try:
        db.create_session("test", model="synthetic")
        db.save_messages("test", [{"role": "user", "content": "A"},
            {"role": "assistant", "content": "OK", "finish_reason": "stop",
             "provider_data": provider_data, "native_outcome": native}])
    finally:
        db.close()
    db = SessionDB(tmp_path / "db")
    try:
        loaded = db.load_messages("test")
    finally:
        db.close()
    assert loaded[-1]["provider_data"]["native_outcome"] == native
    assert "native_outcome" not in loaded[-1]  # top-level extra would be lost
    transport = get_transport(mode)
    request = transport.build_kwargs(model="synthetic", messages=loaded)
    without = [{**m, "provider_data": {k: v for k, v in (m.get("provider_data") or {}).items()
                                      if k != "native_outcome"}} for m in loaded]
    assert request == transport.build_kwargs(model="synthetic", messages=without)
    assert "native_outcome" not in json.dumps(request)
