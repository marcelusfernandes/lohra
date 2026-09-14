"""Accepted/rejected native diagnostics across turn, envelope and cold storage."""
import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.agent.result_json import build_envelope
from lohra.agent.stream_abort import AbortedStream
from lohra.agent.types import NativeOutcome, Usage
from lohra.providers import get_provider_profile
from lohra.providers.errors import ProviderCallFailed
from lohra.providers.transports import get_transport
from lohra.state import SessionDB
from tests.test_loop import FakeClient
from tests.test_native_outcome_contract import isolated as isolated, raw_response, run


@pytest.mark.parametrize("second", ["reported", "absent", "abort", "ordinary", "accepted_absent"])
def test_last_call_usage_is_separate_from_aggregate_floor(second):
    first = raw_response("anthropic_messages", "tool_use", True)
    # Native Anthropic has four meters; reasoning is independently covered by
    # real Responses receipts and by the canonical exception below.
    first["usage"] = {"input_tokens": 11, "output_tokens": 13,
                      "cache_read_input_tokens": 17, "cache_creation_input_tokens": 19}
    usage = Usage(2, 3, 5, 7, 11) if second == "reported" else None
    native = NativeOutcome("responses", status="failed", error_code="rate_limit_exceeded")
    following = (AbortedStream() if second == "abort" else RuntimeError("synthetic")
                 if second == "ordinary" else {**raw_response("anthropic_messages", "end_turn"),
                                              "usage": None} if second == "accepted_absent" else
                 ProviderCallFailed("synthetic quota", code="rate_limit_exceeded",
                                    native_outcome=native, usage=usage, retry_after=7.5))
    effects = []
    client = FakeClient([first, following])
    agent = Agent(model="synthetic", provider=get_provider_profile("anthropic"), client=client,
                  max_iterations=2, tool_dispatch=lambda n, a: effects.append(n) or "{}")
    result = run_conversation(agent, "A", stream_delta_callback=lambda t: None)
    assert effects == ["fake_effect"] and len(client.calls) == 2
    assert result["usage"] == usage
    assert result["usage_total"] == (Usage(13, 16, 22, 26, 11) if second == "reported" else
                                      Usage(11, 13, 17, 19, 0))
    envelope = build_envelope("A", result, model="synthetic", temperature=None, session_id="s")
    if second in ("reported", "absent"):
        assert result["error_kind"] == "quota_exhausted" and result["retry_after"] == 7.5
        assert envelope["native_outcome"]["status"] == "failed"
    if second != "accepted_absent":
        assert not result["completed"] and envelope["stop_reason"] is None
    if second == "abort":
        assert result["interrupted"] and result["usage_uncertain"]


@pytest.mark.parametrize("mode", ["chat_completions", "anthropic_messages", "responses"])
@pytest.mark.parametrize("forced", [False, True])
def test_actual_native_metadata_roundtrips_cold_without_model_input(tmp_path, mode, forced):
    reason = ("tool_calls" if forced else "stop") if mode == "chat_completions" else (
        "tool_use" if forced else "end_turn") if mode == "anthropic_messages" else "completed"
    raw = raw_response(mode, reason, forced)
    if mode == "responses":
        raw["output"].insert(0, {"type": "reasoning", "summary": [],
                                 "encrypted_content": "SYNTHETIC_ENCRYPTED"})
    elif mode == "anthropic_messages":
        raw["content"].insert(0, {"type": "thinking", "thinking": "thought",
                                  "signature": "SYNTHETIC_SIGNATURE"})
    result, effects, requests = run(mode, raw, forced)
    assert result["completed"] and not effects and len(requests) == 1
    native = result["native_outcome"]
    assert result["messages"][-1]["provider_data"]["native_outcome"] == native
    original_native = dict(native)
    native["reason"] = "mutated-result"
    assert result["messages"][-1]["provider_data"]["native_outcome"] == original_native
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("s", model="synthetic")
        db.save_messages("s", result["messages"])
    finally:
        db.close()
    db = SessionDB(tmp_path / "state.db")
    try:
        messages = db.load_messages("s")
    finally:
        db.close()
    assert messages[-1]["provider_data"]["native_outcome"] == original_native
    if mode == "responses":
        assert messages[-1]["provider_data"]["reasoning_items"][0]["encrypted_content"] == "SYNTHETIC_ENCRYPTED"
    if mode == "anthropic_messages":
        assert messages[-1]["provider_data"]["thinking_blocks"][0]["signature"] == "SYNTHETIC_SIGNATURE"
    transport = get_transport(mode)
    request = transport.build_kwargs(model="synthetic", messages=messages)
    stripped = [{**m, "provider_data": {k: v for k, v in m.get("provider_data", {}).items()
                                        if k != "native_outcome"}} for m in messages]
    assert request == transport.build_kwargs(model="synthetic", messages=stripped)
    assert "native_outcome" not in json.dumps(request)


def test_unknown_usage_of_first_rejection_stays_unknown():
    raw = raw_response("responses", "failed")
    raw["usage"] = None
    result, effects, requests = run("responses", raw)
    assert result["usage"] is None and result["usage_total"] is None
    assert result["error"] and not effects and len(requests) == 1
