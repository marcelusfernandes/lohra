"""The 12 historical scenarios not already covered by the adopted SDK cohort.

The old abort-success assertions are deliberately replaced by #116 refusal plus
#133 unknown-usage assertions. No historical failure counts are carried forward.
"""
import openai
import pytest

from lohra.agent.stream_abort import AbortedStream
from tests.relay_helpers import chat, isolated as isolated, relay, request


@pytest.mark.parametrize("endpoint,reason,content", [
    ("chat", "length", "PART"), ("chat", "length", ""), ("responses", "length", ""),
    ("responses", "content_filter", "refused"), ("chat", "abort", None), ("responses", "abort", None),
])
@pytest.mark.parametrize("streamed", [False, True], ids=["json", "sse"])
def test_remaining_historical_relay_states(endpoint, reason, content, streamed):
    with relay([AbortedStream() if reason == "abort" else chat(reason, content)]) as (
            sdk, local, calls, requests, closes):
        if reason == "abort" and (endpoint == "chat" or not streamed):
            with pytest.raises(openai.APIError) as caught:
                raw = request(sdk, endpoint, streamed)
                if streamed:
                    list(raw)
            assert caught.value.body["lohra_usage"] == {"status": "unknown"}
        else:
            raw = request(sdk, endpoint, streamed)
            if endpoint == "chat":
                chunks = list(raw) if streamed else []
                finish = ([c.finish_reason for event in chunks for c in event.choices if c.finish_reason]
                          if streamed else [raw.choices[0].finish_reason])
                assert finish == [reason]
            else:
                if streamed:
                    terminal = [e for e in raw if e.type in
                                ("response.completed", "response.incomplete", "response.failed")]
                    assert len(terminal) == 1
                    raw = terminal[0].response
                expected = "failed" if reason == "abort" else "incomplete"
                assert raw.status == expected
                if streamed:
                    assert terminal[0].type == "response." + expected
                if reason == "abort":
                    assert raw.usage is None and raw.lohra_usage == {"status": "unknown"}
                else:
                    assert raw.incomplete_details.reason == (
                        "max_output_tokens" if reason == "length" else "content_filter")
        assert len(calls) == len(requests) == len(closes) == 1


@pytest.mark.parametrize("reason", [None, "max_output_tokens", "future_cause"])
@pytest.mark.parametrize("streamed", [False, True])
def test_responses_preserves_only_observed_supported_incomplete_cause(reason, streamed):
    raw = {"status": "incomplete", "incomplete_details": {"reason": reason},
           "output": [{"type": "message", "content": [{"type": "output_text", "text": "PART"}]}]}
    with relay([raw], "responses") as (sdk, local, calls, requests, closes):
        result = request(sdk, "responses", streamed)
        if streamed:
            terminal = [e for e in result if e.type in ("response.incomplete", "response.completed")]
            assert len(terminal) == 1
            result = terminal[0].response
        assert result.status == "incomplete"
        assert (result.incomplete_details.reason if result.incomplete_details else None) == (
            "max_output_tokens" if reason == "max_output_tokens" else None)
        assert len(calls) == len(requests) == len(closes) == 1


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("mixed", [False, True])
def test_native_refusal_parts_survive_real_sdk_response(streamed, mixed):
    parts = ([{"type": "output_text", "text": "INTRO"}] if mixed else []) + [
        {"type": "refusal", "refusal": "SAME"}]
    raw = {"status": "completed", "output": [{"type": "message", "content": parts}]}
    with relay([raw], "responses") as (sdk, local, calls, requests, closes):
        result = request(sdk, "responses", streamed)
        if streamed:
            terminal = [e for e in result if e.type in ("response.completed", "response.failed")]
            assert len(terminal) == 1
            result = terminal[0].response
        assert result.status == "completed" and result.error is None
        content = result.output[0].content
        assert content[-1].type == "refusal" and content[-1].refusal == "SAME"
        if mixed:
            assert content[0].type == "output_text" and content[0].text == "INTRO"
        assert len(calls) == len(requests) == len(closes) == 1
