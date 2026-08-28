"""Tests for the `lohra chat --json` envelope (orchestration output)."""


import json

from lohra.agent.result_json import build_envelope, error_envelope
from lohra.agent.types import Usage

# the turn's messages live in result["messages"] after the user prompt
_MESSAGES = [
    {"role": "user", "content": "the question"},
    {"role": "assistant", "content": "", "reasoning": "thinking", "finish_reason": "tool_calls",
     "tool_calls": [{"id": "c1", "type": "function",
                     "function": {"name": "read_file", "arguments": '{"path":"x"}'}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "DATA"},
    {"role": "assistant", "content": "the answer", "finish_reason": "stop"},
]
_RESULT = {
    "final_response": "the answer",
    "completed": True,
    "error": None,
    "api_calls": 2,
    "usage": Usage(input_tokens=10, output_tokens=5, reasoning_tokens=3),
    "messages": _MESSAGES,
}


def _env(**kw):
    base = dict(model="gpt-5.5", temperature=None, session_id="S1")
    base.update(kw)
    return build_envelope("the question", _RESULT, **base)


def test_envelope_has_all_fields():
    env = _env()
    assert env["input"] == "the question"
    assert env["output"] == "the answer"
    assert env["model"] == "gpt-5.5"
    assert env["session_id"] == "S1"
    assert env["completed"] is True and env["error"] is None
    assert env["stop_reason"] == "stop"  # last assistant finish_reason


def test_reasoning_collected():
    assert _env()["reasoning"] == "thinking"


def test_tool_calls_paired_with_results():
    calls = _env()["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["name"] == "read_file" and calls[0]["arguments"] == '{"path":"x"}'
    assert calls[0]["result"] == "DATA"  # matched to the tool message by id


def test_usage_serialized_with_nonzero_extras():
    u = _env()["usage"]
    assert u["input_tokens"] == 10 and u["output_tokens"] == 5
    assert u["reasoning_tokens"] == 3
    assert "cache_read_tokens" not in u  # zero extras omitted


def test_temperature_passthrough():
    assert _env(temperature=0.7)["temperature"] == 0.7


def test_empty_turn_is_clean():
    env = build_envelope("q", {"final_response": "a", "messages": []},
                         model="m", temperature=None, session_id="S")
    assert env["tool_calls"] == [] and env["reasoning"] is None
    assert env["usage"] is None and env["output"] == "a"


def test_error_turn_carries_error():
    env = build_envelope("q", {"final_response": None, "error": "boom", "completed": False, "messages": []},
                         model="m", temperature=None, session_id="S")
    assert env["error"] == "boom" and env["completed"] is False and env["output"] is None


def test_this_turn_after_compaction_uses_last_user_not_a_slice():
    # compaction rewrites messages (head + summary + tail); the turn is still
    # everything after the LAST user message, so a positional slice isn't needed.
    compacted = {
        "final_response": "ans",
        "messages": [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "summary of the middle"},  # injected summary
            {"role": "user", "content": "the real prompt"},  # this turn
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t1", "function": {"name": "now_tool", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "RES"},
            {"role": "assistant", "content": "ans", "finish_reason": "stop"},
        ],
    }
    env = build_envelope("the real prompt", compacted, model="m", temperature=None, session_id="S")
    assert [c["name"] for c in env["tool_calls"]] == ["now_tool"]  # only THIS turn's call


# --- usage_total + cost ---


def test_usage_total_serialized_alongside_usage():
    result = dict(
        _RESULT,
        usage_total=Usage(input_tokens=250, output_tokens=30, cache_read_tokens=130),
    )
    env = build_envelope("q", result, model="m", temperature=None, session_id="S")
    assert env["usage"]["input_tokens"] == 10  # last call, unchanged contract
    total = env["usage_total"]
    assert total == {"input_tokens": 250, "output_tokens": 30, "cache_read_tokens": 130}


def test_cost_computed_from_usage_total_and_real_table():
    from lohra.pricing import PRICES

    price = PRICES[("openai", "gpt-4o")]
    usage_total = Usage(input_tokens=2_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    result = dict(_RESULT, usage_total=usage_total)
    env = build_envelope(
        "q", result, model="gpt-4o", temperature=None, session_id="S", provider="openai"
    )
    # input_tokens is the UNCACHED part (Fatia C): 2M uncached + 1M cache read.
    expected = (
        2_000_000 * price.input_usd
        + 1_000_000 * price.cached_input_usd
        + 1_000_000 * price.output_usd
    ) / 1e6
    assert env["cost"]["usd"] == round(expected, 6)
    assert env["cost"]["basis"] == "api_list_price"


def test_cost_null_for_unknown_model():
    result = dict(_RESULT, usage_total=Usage(input_tokens=100))
    env = build_envelope(
        "q", result, model="mystery-9000", temperature=None, session_id="S", provider="openai"
    )
    assert env["cost"] is None


def test_cost_null_without_provider():
    result = dict(_RESULT, usage_total=Usage(input_tokens=100))
    env = build_envelope("q", result, model="gpt-4o", temperature=None, session_id="S")
    assert env["cost"] is None


def test_error_envelope_has_same_schema():
    env = error_envelope("q", "provider boom", model=None, session_id="")
    # same keys as a success envelope so an orchestrator parses both the same way
    assert set(env) == set(build_envelope("q", {"messages": []}, model="m", temperature=None, session_id="S"))
    assert env["error"] == "provider boom" and env["output"] is None and env["completed"] is False


def test_envelope_json_serializable_with_lone_surrogate():
    # a lone surrogate in provider content must serialize (ensure_ascii) not crash
    env = build_envelope("q", {"final_response": "hi \ud83e", "messages": []},
                         model="m", temperature=None, session_id="S")
    text = json.dumps(env, ensure_ascii=True)  # how the CLI emits it
    assert json.loads(text)["output"] == "hi \ud83e"  # round-trips, no UnicodeError


def test_cost_carries_gross_saving_and_source():
    """Bruto x real, com a economia do cache e a fonte do preco — sempre."""
    from lohra.pricing import PRICES_AS_OF

    usage_total = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000)
    env = build_envelope(
        "q",
        dict(_RESULT, usage_total=usage_total),
        model="gpt-4o", temperature=None, session_id="S", provider="openai",
    )
    cost = env["cost"]
    assert cost["gross_usd"] > cost["usd"]  # o cache barateou o turno
    assert cost["saved_usd"] == round(cost["gross_usd"] - cost["usd"], 6)
    assert cost["source"] == f"snapshot {PRICES_AS_OF}"
