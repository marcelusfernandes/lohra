"""SDK/HTTPX2 bytes, physical ownership and native decisions (no sockets).

Six Responses paths are adapted from the historical bb191e6 SDK probe. The
Chat/Anthropic paths exercise the default HTTP family of SDKs used in this run.
"""
from contextlib import contextmanager
import json

import anthropic
import httpx2
import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import AnthropicClient, ModelClient, OpenAIClient, ResponsesClient
from lohra.agent.loop import run_conversation
from lohra.agent.types import Usage
from tests.test_native_outcome_contract import TOOL, isolated as isolated, raw_response
from tests.test_native_outcome_sdk import PROFILE
from lohra.providers import get_provider_profile


def _events(mode, raw):
    if mode == "responses":
        events = [{"type": "response.output_item.done", "sequence_number": n,
                   "output_index": n, "item": item} for n, item in enumerate(raw["output"])]
        return events + [{"type": f'response.{raw["status"]}', "sequence_number": len(events),
                          "response": {**raw, "output": []}}]
    if mode == "chat_completions":
        choice = raw["choices"][0]
        msg = choice["message"]
        delta = {**msg, "tool_calls": [{**c, "index": n} for n, c in
                                      enumerate(msg.get("tool_calls", []))]}
        return [{"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
                 "model": "synthetic", "choices": [{"index": 0, "delta": delta,
                                                       "finish_reason": choice["finish_reason"]}]},
                {"id": "chat_test", "object": "chat.completion.chunk", "created": 1,
                 "model": "synthetic", "choices": [], "usage": raw["usage"]}]
    events = [{"type": "message_start", "message": {**raw, "content": [], "stop_reason": None}}]
    for n, block in enumerate(raw["content"]):
        events.extend([{"type": "content_block_start", "index": n, "content_block": block},
                       {"type": "content_block_stop", "index": n}])
    return events + [{"type": "message_delta", "delta": {"stop_reason": raw["stop_reason"],
                     "stop_sequence": None}, "usage": raw["usage"]}, {"type": "message_stop"}]


@contextmanager
def wire(mode, raw, streaming):
    requests, closes = [], []

    class Body(httpx2.SyncByteStream):
        def __init__(self, payload):
            self.payload = payload

        def __iter__(self):
            yield self.payload

        def close(self):
            closes.append(True)

    def handle(request):
        requests.append(json.loads(request.content))
        response = raw if len(requests) == 1 else raw_response(mode,
            "end_turn" if mode == "anthropic_messages" else "completed" if mode == "responses" else "stop")
        response = {"id": "synthetic", "model": "synthetic", "created_at": 1, "created": 1,
                    "object": "response" if mode == "responses" else "chat.completion",
                    "type": "message", "role": "assistant", "stop_sequence": None, **response}
        payload = ("".join((f'event: {e["type"]}\n' if mode != "chat_completions" else "") +
                         f'data: {json.dumps(e)}\n\n' for e in _events(mode, response)).encode()
                   if streaming else json.dumps(response).encode())
        return httpx2.Response(200, headers={"content-type": "text/event-stream" if streaming else
                                             "application/json"}, stream=Body(payload))

    http = httpx2.Client(transport=httpx2.MockTransport(handle), trust_env=False)
    cls = anthropic.Anthropic if mode == "anthropic_messages" else openai.OpenAI
    sdk = cls(api_key="synthetic", base_url="https://synthetic.invalid", http_client=http, max_retries=0)
    wrapper = {"responses": ResponsesClient, "chat_completions": OpenAIClient,
               "anthropic_messages": AnthropicClient}[mode]
    client = wrapper.__new__(wrapper)
    client._client, client._credential_headers = sdk, None
    if mode == "responses" and not streaming:
        # A genuine SDK JSON create; ResponsesClient.create itself always uses SSE.
        class JSONClient(ModelClient):
            def create(self, **kwargs):
                return sdk.responses.create(**kwargs)
        client = JSONClient()
    try:
        yield client, requests, closes
    finally:
        sdk.close()
        http.close()


@pytest.mark.parametrize("streaming,status,item_status", [
    (False, "failed", "completed"), (False, "cancelled", "completed"),
    (True, "completed", "completed"), (True, "incomplete", "completed"),
    (True, "incomplete", "incomplete"), (True, "failed", "completed"),
])
def test_responses_historical_sdk_paths(streaming, status, item_status):
    raw = raw_response("responses", status, True)
    raw["output"][0].update(id="fc_test", status=item_status)
    if status == "failed":
        raw["error"] = {"code": "server_error", "message": "synthetic failure"}
    if status == "incomplete":
        raw["incomplete_details"] = {"reason": "max_output_tokens"}
    effects = []
    with wire("responses", raw, streaming) as (client, requests, closes):
        agent = Agent(model="synthetic", provider=PROFILE, client=client, max_iterations=2,
                      tool_definitions=(TOOL,), tool_dispatch=lambda n, a: effects.append(n) or "{}")
        result = run_conversation(agent, "A", stream_delta_callback=(lambda t: None) if streaming else None)
        valid = status == "completed"
        assert effects == (["fake_effect"] if valid else [])
        assert result["completed"] is valid
        assert result["usage_total"] == (Usage(10, 6) if valid else Usage(5, 3))
        assert len(closes) == len(requests) == (2 if valid else 1)
        if not valid:
            assert [m["role"] for m in result["messages"]] == ["user"]
            if status == "incomplete":
                assert result["native_outcome"]["incomplete_reason"] == "max_output_tokens"


@pytest.mark.parametrize("mode,reason,valid", [
    ("chat_completions", "tool_calls", True), ("chat_completions", "new_unknown", False),
    ("anthropic_messages", "tool_use", True), ("anthropic_messages", "new_unknown", False),
])
@pytest.mark.parametrize("streaming", [False, True])
def test_real_sdk_forced_reason_controls(mode, reason, valid, streaming):
    raw = raw_response(mode, reason, True)
    effects = []
    with wire(mode, raw, streaming) as (client, requests, closes):
        agent = Agent(model="synthetic", provider=get_provider_profile(
            "anthropic" if mode == "anthropic_messages" else "openai"), client=client,
            max_iterations=1, tool_dispatch=lambda n, a: effects.append(n))
        agent.forced_tool = TOOL
        result = run_conversation(agent, "A", stream_delta_callback=(lambda t: None) if streaming else None)
        assert not effects and len(requests) == len(closes) == 1
        assert result["completed"] is valid
        assert result["final_response"] == ("{}" if valid else None)
        assert result["usage_total"] == Usage(5, 3)
        assert bool(result["error"]) is not valid
