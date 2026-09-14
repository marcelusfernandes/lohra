"""Desired regressions for the three frozen #133 information-loss observations."""
from copy import deepcopy

import pytest

from lohra.agent.loop import run_conversation
from lohra.agent.types import Usage
from lohra.server.service import CompletionService
from tests.relay_helpers import KNOWN, ZERO, chat, factory, isolated as isolated


FLOOR = {"input_tokens": 9, "output_tokens": 5, "cache_read_tokens": 2,
         "cache_write_tokens": 0, "reasoning_tokens": 3}


def service(script, mode="chat_completions"):
    return CompletionService(factory(script, mode)).run(model="synthetic",
        messages=[{"role": "user", "content": "A"}])


@pytest.mark.parametrize("missing_first", [True, False])
def test_missing_usage_is_distinct_from_reported_zero_in_either_order(missing_first):
    absent = [chat(usage=None if missing_first else KNOWN, tool=True),
              chat(usage=KNOWN if missing_first else None)]
    zero = [chat(usage=ZERO if missing_first else KNOWN, tool=True),
            chat(usage=KNOWN if missing_first else ZERO)]
    incomplete = run_conversation(factory(absent)(), "A")
    complete = run_conversation(factory(zero)(), "A")
    assert incomplete["usage_total"] == complete["usage_total"] == Usage(**FLOOR)
    assert incomplete.get("usage_complete") is False and complete.get("usage_complete") is True
    assert incomplete["messages"] == complete["messages"]
    missing_result, zero_result = service(absent), service(zero)
    assert missing_result["usage"] is None
    assert missing_result["lohra_usage"] == {"status": "lower_bound", "observed": FLOOR}
    assert zero_result["usage"]["prompt_tokens"] == 11
    assert "lohra_usage" not in zero_result


def test_native_refusal_identity_survives_without_classifying_identical_text():
    raw = {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "SAME"}]}], "usage": {"input_tokens": 11, "output_tokens": 5}}
    plain = deepcopy(raw)
    plain["output"][0]["content"] = [{"type": "output_text", "text": "SAME"}]
    refusal, text = service([raw], "responses"), service([plain], "responses")
    assert refusal["content"] == text["content"] == "SAME"
    assert refusal.get("output_parts") == [{"type": "refusal", "refusal": "SAME"}]
    assert not text.get("output_parts")
    assert refusal["finish_reason"] == text["finish_reason"] == "stop"
    assert refusal["usage"] == text["usage"]


@pytest.mark.parametrize("reported", [None, ZERO, KNOWN])
def test_absent_single_call_never_becomes_a_character_estimate(reported):
    result = service([chat(usage=reported)])
    if reported is None:
        assert result["usage"] is None
        assert result["lohra_usage"] == {"status": "unknown"}
    else:
        assert result["usage"]["prompt_tokens"] == reported["prompt_tokens"]
        assert "lohra_usage" not in result
@pytest.mark.parametrize("content", [None, "INTRO"])
def test_chat_refusal_metadata_preserves_canonical_content_and_history(content):
    from lohra.providers.transports import get_transport
    from tests.relay_helpers import chat

    raw = chat(content=content)
    raw["choices"][0]["message"]["refusal"] = "NO"
    normalized = get_transport("chat_completions").normalize_response(raw)
    assert normalized.content == content
    assert normalized.output_parts[-1].as_dict() == {"type": "refusal", "refusal": "NO"}
    result = run_conversation(factory([raw])(), "A")
    assert result["final_response"] == content
    reference = run_conversation(factory([chat(content=content)])(), "A")
    assert result["messages"] == reference["messages"]
