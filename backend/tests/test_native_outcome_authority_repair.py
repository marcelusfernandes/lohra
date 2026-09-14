"""Author regressions for the public F1/F2 findings on PR154/33ce7c3.

Real SDK JSON/SSE over HTTPX2 MockTransport; no independent private fixtures.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import socket
import subprocess

import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient, ResponsesClient, assemble_responses_stream
from lohra.agent.loop import run_conversation
from lohra.agent.stream_abort import AbortedStream
from lohra.agent.types import Usage
from lohra.providers import get_provider_profile
from lohra.providers.errors import ProviderCallFailed
from lohra.providers.transports import get_transport


PROFILE = replace(get_provider_profile("openai"), api_mode="responses")
TOOL = {"type": "function", "function": {"name": "record_value", "parameters": {
    "type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}}
REPORTED = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2}}
KNOWN = Usage(8, 7, 3, 0, 2)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))

    def blocked(*args, **kwargs):
        raise AssertionError("authority repair tests forbid network/process effects")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


def call(status="completed"):
    item = {"type": "function_call", "id": "fc_record", "call_id": "call_record",
            "name": "record_value", "arguments": '{"n":7}'}
    if status is not None:
        item["status"] = status
    return item


def response(*, item=None, status="completed", reported=True):
    return {"id": "resp_record", "object": "response", "created_at": 1,
            "model": "synthetic", "status": status, "output": [item] if item else [],
            "usage": deepcopy(REPORTED) if reported else None, "error": None}


def events(raw, done=None):
    # Separate JSON values deliberately: mutating a done item must not change
    # the terminal snapshot, unlike deepcopy of a shared-object event list.
    out = [] if done is None else [{"type": "response.output_item.done",
        "output_index": 0, "sequence_number": 0, "item": deepcopy(done)}]
    return out + [{"type": f'response.{raw["status"]}', "sequence_number": len(out),
                   "response": deepcopy(raw)}]


@contextmanager
def sdk_client(payload, streaming):
    requests, closes = [], []

    class Body(httpx2.SyncByteStream):
        def __iter__(self):
            encoded = ("".join(f'data: {json.dumps(event)}\n\n' for event in payload).encode()
                       if streaming else json.dumps(payload).encode())
            for offset in range(0, len(encoded), 29):
                yield encoded[offset:offset + 29]

        def close(self):
            closes.append(True)

    def handle(request):
        requests.append(json.loads(request.content))
        assert len(requests) == 1
        return httpx2.Response(200, headers={"content-type": "text/event-stream" if streaming
                                             else "application/json"}, stream=Body())

    http = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    sdk = openai.OpenAI(api_key="synthetic", base_url="https://synthetic.invalid",
                        http_client=http, max_retries=0)
    if streaming:
        client = ResponsesClient.__new__(ResponsesClient)
        client._client, client._credential_headers = sdk, None
    else:
        class JSONClient(ModelClient):
            def create(self, **kwargs):
                return sdk.responses.create(**kwargs)
        client = JSONClient()
    try:
        yield client, requests, closes
    finally:
        sdk.close()
        http.close()


def run(payload, *, streaming=True, forced=False):
    effects = []
    with sdk_client(payload, streaming) as (client, requests, closes):
        agent = Agent(model="synthetic", provider=PROFILE, client=client, max_iterations=1,
                      tool_definitions=(TOOL,), tool_dispatch=lambda n, a: effects.append((n, a)) or "{}")
        if forced:
            agent.forced_tool = TOOL
        frozen = agent.system_prompt()
        result = run_conversation(agent, "record 7",
            stream_delta_callback=(lambda text: None) if streaming else None)
        assert agent.system_prompt() is frozen
        assert len(requests) == len(closes) == 1
        return result, effects


def assert_rejected(result, effects, reported):
    assert effects == []
    assert result["error"] and not result["completed"]
    assert result["final_response"] is None
    assert [m["role"] for m in result["messages"]] == ["user"]
    assert result["usage"] == result["usage_total"] == (KNOWN if reported else None)


@pytest.mark.parametrize("streaming", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("forced", [False, True], ids=["ordinary", "forced"])
@pytest.mark.parametrize("reported", [False, True], ids=["absent", "reported"])
def test_completed_with_explicit_incomplete_cause_cannot_authorize(streaming, forced, reported):
    raw = response(item=call(), reported=reported)
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    result, effects = run(events(raw) if streaming else raw, streaming=streaming, forced=forced)
    assert_rejected(result, effects, reported)
    assert result["native_outcome"]["status"] == "completed"
    assert result["native_outcome"]["incomplete_reason"] == "max_output_tokens"


@pytest.mark.parametrize("terminal_item_status", [None, "completed"], ids=["optional", "completed"])
@pytest.mark.parametrize("forced", [False, True], ids=["ordinary", "forced"])
@pytest.mark.parametrize("reported", [False, True], ids=["absent", "reported"])
def test_done_incomplete_evidence_survives_terminal_replacement(terminal_item_status, forced, reported):
    raw = response(item=call(terminal_item_status), reported=reported)
    payload = events(raw, call("incomplete"))
    assert payload[0]["item"]["status"] == "incomplete"
    assert payload[1]["response"]["output"][0].get("status") == terminal_item_status
    result, effects = run(payload, forced=forced)
    assert_rejected(result, effects, reported)
    assert result["native_outcome"]["item_status"] == "incomplete"


@pytest.mark.parametrize("done_status", [None, "completed"], ids=["optional", "completed"])
@pytest.mark.parametrize("forced", [False, True], ids=["ordinary", "forced"])
def test_consistent_items_preserve_terminal_output_and_effects(done_status, forced):
    result, effects = run(events(response(item=call()), call(done_status)), forced=forced)
    assert result["usage"] == result["usage_total"] == KNOWN
    if forced:
        assert not effects and result["completed"] and not result["error"]
        assert json.loads(result["final_response"]) == {"n": 7}
    else:
        assert effects == [("record_value", {"n": 7})]
        assert any(m["role"] == "assistant" for m in result["messages"])


@pytest.mark.parametrize("details", [None, {}, {"reason": None}])
def test_completed_without_supplied_cause_still_authorizes(details):
    raw = response(item=call())
    raw["incomplete_details"] = details
    normalized = get_transport("responses").normalize_response(raw)
    assert normalized.finish_reason == "tool_calls" and len(normalized.tool_calls) == 1
    assert normalized.usage == KNOWN


@pytest.mark.parametrize("reason", ["", {}, "opaque " * 200])
def test_invalid_supplied_incomplete_cause_is_bounded_and_cannot_authorize(reason):
    raw = response(item=call())
    raw["incomplete_details"] = {"reason": reason}
    with pytest.raises(ProviderCallFailed) as caught:
        get_transport("responses").normalize_response(raw)
    assert caught.value.usage == KNOWN
    assert caught.value.native_outcome.incomplete_reason == "<invalid>"


def test_incomplete_text_and_reasoning_item_do_not_become_function_rejections():
    text = {"type": "message", "id": "msg", "role": "assistant", "status": "incomplete",
            "content": [{"type": "output_text", "text": "PART", "annotations": []}]}
    raw = response(item=text, status="incomplete")
    raw["incomplete_details"] = {"reason": "max_output_tokens"}
    reasoning = {"type": "reasoning", "id": "rs", "status": "incomplete",
                 "summary": [{"type": "summary_text", "text": "THINK"}],
                 "encrypted_content": "opaque-reasoning"}
    raw["output"].insert(0, reasoning)
    with sdk_client(events(raw, reasoning), True) as (client, requests, closes):
        assembled = client.create(model="synthetic", input=[])
        normalized = get_transport("responses").normalize_response(assembled)
        assert len(requests) == len(closes) == 1
    assert normalized.finish_reason == "length" and normalized.content == "PART"
    assert normalized.reasoning == "THINK" and not normalized.tool_calls
    assert normalized.provider_data["reasoning_items"][0]["encrypted_content"] == "opaque-reasoning"
    assert normalized.usage == KNOWN


def test_last_callback_abort_still_precedes_discarded_item_validation():
    cancelled, closed = [], []
    payload = events(response(item=call()), call("incomplete"))
    payload.append({"type": "response.output_text.delta", "delta": "last"})

    class Stream:
        def __iter__(self):
            yield from payload

        def close(self):
            closed.append(True)

    result = assemble_responses_stream(Stream(), on_text=lambda text: cancelled.append(True),
                                       abort_check=lambda: bool(cancelled))
    assert isinstance(result, AbortedStream) and closed == [True]


@pytest.mark.parametrize("origin", ["done", "terminal"])
def test_first_invalid_item_status_is_not_replaced_by_later_snapshots(origin):
    if origin == "done":
        payload = [
            {"type": "response.output_item.done", "sequence_number": n, "output_index": 0,
             "item": call(status)} for n, status in enumerate(("incomplete", "in_progress"))
        ] + events(response(item=call()))
    else:
        payload = (events(response(item=call("incomplete")))
                   + events(response(item=call("in_progress"), reported=False))
                   + events(response(item=call(), reported=False)))
    for n, event in enumerate(payload):
        event["sequence_number"] = n
    result, effects = run(payload, forced=True)
    assert_rejected(result, effects, True)
    assert result["native_outcome"]["item_status"] == "incomplete"
