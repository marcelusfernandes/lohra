"""Native authority, including the 28 cases first observed on bb191e6.

Historical probes asserted the old defect. These assert the intended contract;
extra malformed/contradictory cases distinguish absence from invalid evidence.
"""
from dataclasses import replace
import json
import socket
import subprocess

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import assemble_responses_stream
from lohra.agent.loop import run_conversation
from lohra.agent.result_json import build_envelope
from lohra.agent.types import Usage
from lohra.providers import get_provider_profile
from lohra.providers.errors import ProviderCallFailed
from lohra.providers.transports import get_transport

TOOL = {"type": "function", "function": {"name": "fake_effect", "parameters": {"type": "object"}}}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)

    def blocked(*args, **kwargs):
        raise AssertionError("synthetic native outcomes must not use network/processes")

    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


def raw_response(mode, reason, calls=False):
    if mode == "responses":
        output = [{"type": "function_call", "call_id": "c", "name": "fake_effect",
                   "arguments": "{}", "status": "completed"}] if calls else [
            {"type": "message", "content": [{"type": "output_text", "text": "PART"}]}]
        return {"status": reason, "output": output, "usage": {"input_tokens": 5, "output_tokens": 3}}
    if mode == "chat_completions":
        msg = {"content": "PART"}
        if calls:
            msg["tool_calls"] = [{"id": "c", "function": {"name": "fake_effect", "arguments": "{}"}}]
        return {"choices": [{"message": msg, "finish_reason": reason}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
    content = [{"type": "text", "text": "PART"}]
    if calls:
        content.append({"type": "tool_use", "id": "c", "name": "fake_effect", "input": {}})
    return {"content": content, "stop_reason": reason,
            "usage": {"input_tokens": 5, "output_tokens": 3}}


def run(mode, raw, forced=False):
    profile = replace(get_provider_profile("anthropic" if mode == "anthropic_messages" else "openai"),
                      api_mode=mode)
    effects, requests = [], []

    class Client:
        def create(self, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return raw
            return raw_response(mode, "end_turn" if mode == "anthropic_messages" else
                                "completed" if mode == "responses" else "stop")

    agent = Agent(model="synthetic", provider=profile, client=Client(), max_iterations=2,
                  tool_definitions=(TOOL,), tool_dispatch=lambda n, a: effects.append(n) or "{}")
    if forced:
        agent.forced_tool = TOOL
    snapshot = agent.system_prompt()
    result = run_conversation(agent, "synthetic")
    assert agent.system_prompt() is snapshot
    return result, effects, requests


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "in_progress", "queued",
                                    "new_unknown", None])
def test_responses_text_authority(status):
    result, effects, requests = run("responses", raw_response("responses", status))
    assert result["completed"] is (status == "completed")
    assert not effects and len(requests) == 1
    assert result["usage_total"] == Usage(5, 3)
    if status != "completed":
        assert result["error"] and not any(m["role"] == "assistant" for m in result["messages"])


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter", "steered", "new_unknown"])
def test_incomplete_text_keeps_cause_and_truncation(reason):
    raw = raw_response("responses", "incomplete")
    raw["incomplete_details"] = {"reason": reason}
    result, effects, requests = run("responses", raw)
    assert result["completed"] and result["partial"] and result["final_response"] == "PART"
    assert not effects and len(requests) == 1
    assert result["native_outcome"]["incomplete_reason"] == reason


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete", "in_progress"])
def test_responses_call_requires_completed(status):
    result, effects, requests = run("responses", raw_response("responses", status, True))
    assert effects == (["fake_effect"] if status == "completed" else [])
    assert result["completed"] is (status == "completed")
    assert len(requests) == (2 if status == "completed" else 1)


def test_responses_forced_failed_is_not_certified():
    result, effects, requests = run("responses", raw_response("responses", "failed", True), True)
    assert not result["completed"] and result["final_response"] is None
    assert result["error"] and not effects and len(requests) == 1
    assert [m["role"] for m in result["messages"]] == ["user"]


@pytest.mark.parametrize("mode,reason,valid", [
    *(('chat_completions', r, r in ('stop', 'content_filter', 'length'))
      for r in ('stop', 'content_filter', 'length', 'new_unknown', None)),
    *(('anthropic_messages', r, r in ('end_turn', 'refusal', 'max_tokens', 'pause_turn'))
      for r in ('end_turn', 'refusal', 'max_tokens', 'pause_turn', 'new_unknown', None)),
])
def test_reason_vocabulary(mode, reason, valid):
    result, effects, requests = run(mode, raw_response(mode, reason))
    assert result["completed"] is valid and not effects
    assert len(requests) == (2 if reason == "pause_turn" else 1)
    if not valid:
        assert result["error"] and [m["role"] for m in result["messages"]] == ["user"]


@pytest.mark.parametrize("mode,reason", [
    ("chat_completions", "stop"), ("chat_completions", "length"),
    ("chat_completions", "content_filter"), ("anthropic_messages", "end_turn"),
    ("anthropic_messages", "max_tokens"), ("anthropic_messages", "refusal"),
    ("anthropic_messages", "pause_turn"),
])
@pytest.mark.parametrize("forced", [False, True])
def test_calls_cannot_override_reason_even_when_forced(mode, reason, forced):
    result, effects, requests = run(mode, raw_response(mode, reason, True), forced)
    assert not result["completed"] and result["error"] and result["final_response"] is None
    assert not effects and len(requests) == 1
    assert [m["role"] for m in result["messages"]] == ["user"]


@pytest.mark.parametrize("event,status", [
    ("completed", "incomplete"), ("incomplete", "completed"), ("completed", "in_progress"),
    ("completed", ""), ("completed", False), ("completed", ["completed"]),
])
def test_stream_event_cannot_erase_supplied_status(event, status):
    raw = raw_response("responses", status, True)
    with pytest.raises(ProviderCallFailed):
        folded = assemble_responses_stream(iter([{"type": f"response.{event}", "response": raw}]))
        get_transport("responses").normalize_response(folded)


@pytest.mark.parametrize("mode", ["responses", "chat_completions", "anthropic_messages"])
@pytest.mark.parametrize("invalid", ["", " ", False, [], {}])
def test_invalid_authority_is_distinct_from_absence(mode, invalid):
    result, effects, requests = run(mode, raw_response(mode, invalid))
    assert result["error"] and not result["completed"] and not effects and len(requests) == 1
    native = result["native_outcome"]
    assert native["status" if mode == "responses" else "reason"] == "<invalid>"


@pytest.mark.parametrize("reason", ["tool_calls", "function_call", "tool_use"])
def test_forced_authorized_calls_remain_valid(reason):
    mode = "anthropic_messages" if reason == "tool_use" else "chat_completions"
    result, effects, requests = run(mode, raw_response(mode, reason, True), True)
    assert result["completed"] and result["final_response"] == "{}"
    assert not effects and len(requests) == 1
    assert not result["messages"][-1].get("tool_calls")
    assert result["messages"][-1]["provider_data"]["native_outcome"]["reason"] == reason


def test_bounded_metadata_does_not_copy_payload_or_error_text():
    raw = raw_response("responses", "failed")
    raw["error"] = {"code": "rate_limit_exceeded", "message": "PRIVATE" * 10000}
    raw["incomplete_details"] = {"reason": "x" * 10000, "secret": "PRIVATE"}
    result, _, _ = run("responses", raw)
    native = result["native_outcome"]
    assert native["error_code"] == "rate_limit_exceeded"
    assert native["incomplete_reason"] == "<invalid>"
    envelope = build_envelope("synthetic", result, model="synthetic", temperature=None, session_id="s")
    assert len(json.dumps(native)) < 1000 and "PRIVATE" not in json.dumps(envelope)


@pytest.mark.parametrize("forced", [False, True])
@pytest.mark.parametrize("error", [{"code": "rate_limit_exceeded", "message": "synthetic"}, {}, False])
def test_completed_with_error_never_authorizes_calls(error, forced):
    raw = raw_response("responses", "completed", True)
    raw["error"] = error
    result, effects, requests = run("responses", raw, forced)
    assert not effects and len(requests) == 1
    assert not result["completed"] and result["error"]
    assert [m["role"] for m in result["messages"]] == ["user"]
    assert result["native_outcome"]["error_present"] is True


@pytest.mark.parametrize("statuses", [("incomplete", "completed"), ("completed", "incomplete")])
def test_conflicting_stream_terminals_cannot_replace_first_authority(statuses):
    events = [{"type": f"response.{status}", "response": raw_response("responses", status, True)}
              for status in statuses]
    with pytest.raises(ProviderCallFailed) as caught:
        folded = assemble_responses_stream(iter(events))
        get_transport("responses").normalize_response(folded)
    assert caught.value.usage == Usage(5, 3)
    assert caught.value.native_outcome.status == statuses[0]


@pytest.mark.parametrize("second_usage", [None, {"input_tokens": 7, "output_tokens": 4}])
def test_repeated_terminal_usage_is_latest_known_measurement_not_sum(second_usage):
    first = raw_response("responses", "completed")
    second = {**first, "usage": second_usage}
    raw = assemble_responses_stream(iter([{"type": "response.completed", "response": r}
                                         for r in (first, second)]))
    normalized = get_transport("responses").normalize_response(raw)
    assert normalized.usage == (Usage(5, 3) if second_usage is None else Usage(7, 4))


@pytest.mark.parametrize("statuses", [("incomplete", "completed"), ("in_progress",)])
def test_last_callback_abort_wins_over_native_rejection(statuses):
    from lohra.agent.stream_abort import is_aborted
    stopped, seen, closed = [], [], []

    class Stream:
        def __iter__(self):
            for status in statuses:
                yield {"type": "response.completed" if status == "in_progress" else
                       f"response.{status}", "response": raw_response("responses", status, True)}
            yield {"type": "response.output_text.delta", "delta": "LAST"}

        def close(self):
            closed.append(True)

    def callback(text):
        seen.append(text)
        stopped.append(True)

    result = assemble_responses_stream(Stream(), on_text=callback, abort_check=lambda: bool(stopped))
    assert is_aborted(result) and seen == ["LAST"] and closed == [True]


@pytest.mark.parametrize("reported", [False, True])
def test_failed_terminal_keeps_last_observed_stream_receipt(reported):
    first = raw_response("responses", "completed")
    failed = {"status": "failed", "error": {"code": "rate_limit_exceeded", "message": "quota"},
              "usage": {"input_tokens": 7, "output_tokens": 4} if reported else None}
    with pytest.raises(ProviderCallFailed, match="quota") as caught:
        assemble_responses_stream(iter([{"type": "response.completed", "response": first},
                                        {"type": "response.failed", "response": failed}]))
    assert caught.value.usage == (Usage(7, 4) if reported else Usage(5, 3))
    assert caught.value.native_outcome.status == "failed"
    assert caught.value.code == "rate_limit_exceeded"
