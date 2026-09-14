"""Real SDK/ASGI usage provenance, including downstream Lohra consumers."""
from dataclasses import replace

import openai
import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import OpenAIClient, ResponsesClient
from lohra.agent.loop import run_conversation
from lohra.agent.types import Usage
from lohra.providers import get_provider_profile
from lohra.providers.transports import get_transport
from tests.relay_helpers import chat, isolated as isolated, relay, request


METERS = {"input_tokens": 11, "output_tokens": 19, "cache_read_tokens": 13,
          "cache_write_tokens": 17, "reasoning_tokens": 7}
NATIVE = {"prompt_tokens": 41, "completion_tokens": 19,
          "prompt_tokens_details": {"cached_tokens": 13, "cache_write_tokens": 17},
          "completion_tokens_details": {"reasoning_tokens": 7}}
FLOOR = {"status": "lower_bound", "observed": METERS}


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_reported_native_wire_keeps_all_five_disjoint_meters(mode):
    if mode == "chat_completions":
        raw = chat(usage=NATIVE)
    else:
        raw = {"status": "completed", "output": [], "usage": {
            "input_tokens": 41, "output_tokens": 19,
            "input_tokens_details": {"cached_tokens": 13, "cache_write_tokens": 17},
            "output_tokens_details": {"reasoning_tokens": 7}}}
    assert get_transport(mode).normalize_response(raw).usage == Usage(**METERS)


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("order", ["unknown", "missing-first", "missing-last", "complete"])
def test_actual_sdk_usage_is_nullable_and_known_floor_is_explicit(endpoint, streamed, order):
    script = ([chat(usage=None)] if order == "unknown" else [
        chat(usage=None if order == "missing-first" else NATIVE, tool=True),
        chat(usage=None if order == "missing-last" else NATIVE)])
    with relay(script) as (sdk, local, calls, requests, closes):
        raw = request(sdk, endpoint, streamed)
        if streamed:
            events = list(raw)
            if endpoint == "chat":
                raw = next(e for e in reversed(events) if not e.choices)
            else:
                raw = next(e.response for e in reversed(events) if e.type == "response.completed")
        assert len(calls) == len(script) and len(requests) == len(closes) == 1
        if order == "complete":
            assert raw.usage is not None
            assert not getattr(raw, "lohra_usage", None)
            total = raw.usage.prompt_tokens if endpoint == "chat" else raw.usage.input_tokens
            assert total == 82
        else:
            assert raw.usage is None
            assert raw.lohra_usage == ({"status": "unknown"} if order == "unknown" else FLOOR)


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
@pytest.mark.parametrize("ending", ["success", "failure", "unknown"])
def test_downstream_lohra_keeps_floor_and_incompleteness(endpoint, ending):
    script = [chat(usage=None)] if ending == "unknown" else [chat(usage=NATIVE, tool=True),
        RuntimeError("synthetic failure") if ending == "failure" else chat(usage=None)]
    with relay(script) as (sdk, local, calls, requests, closes):
        cls = OpenAIClient if endpoint == "chat" else ResponsesClient
        client = cls.__new__(cls)
        client._client, client._credential_headers = sdk, None
        mode = "chat_completions" if endpoint == "chat" else "responses"
        agent = Agent(model="synthetic", provider=replace(get_provider_profile("openai"), api_mode=mode),
                      client=client, max_iterations=1)
        result = run_conversation(agent, "A", stream_delta_callback=lambda text: None)
        assert len(calls) == len(script) and len(requests) == len(closes) == 1
        assert result["usage_total"] == (None if ending == "unknown" else Usage(**METERS))
        assert result.get("usage_complete") is False
        assert bool(result["error"]) == (ending == "failure")
        assert result["completed"] == (ending != "failure")


@pytest.mark.parametrize("endpoint", ["chat", "responses"])
def test_json_error_keeps_known_floor_inside_standard_error(endpoint):
    with relay([chat(usage=NATIVE, tool=True), RuntimeError("synthetic failure")]) as (
            sdk, local, calls, requests, closes):
        with pytest.raises(openai.APIStatusError) as caught:
            request(sdk, endpoint, False)
        assert caught.value.status_code == 502
        assert caught.value.body["lohra_usage"] == FLOOR
        assert len(calls) == 2 and len(requests) == len(closes) == 1


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
@pytest.mark.parametrize("invalid", [None, {}, {**METERS, "input_tokens": -1},
    {**METERS, "input_tokens": True}, {**METERS, "input_tokens": "11"},
    {**METERS, "input_tokens": 11.5}])
def test_malformed_floor_never_invents_an_observed_meter(mode, invalid):
    raw = chat(usage=None) if mode == "chat_completions" else {
        "status": "completed", "output": [], "usage": None}
    raw["lohra_usage"] = {"status": "lower_bound", "observed": invalid}
    response = get_transport(mode).normalize_response(raw)
    assert response.usage is None and response.usage_complete is False


@pytest.mark.parametrize("mode", ["chat_completions", "responses"])
def test_zero_floor_is_an_observation_but_not_a_complete_bill(mode):
    raw = chat(usage=None) if mode == "chat_completions" else {
        "status": "completed", "output": [], "usage": None}
    raw["lohra_usage"] = {"status": "lower_bound", "observed": dict.fromkeys(METERS, 0)}
    response = get_transport(mode).normalize_response(raw)
    assert response.usage == Usage() and response.usage_complete is False
