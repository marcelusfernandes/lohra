"""Actual native SDK streams -> relay -> SDK, including typed part sequencing."""
from dataclasses import replace

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from tests.relay_helpers import isolated as isolated, relay, request
from tests.test_native_outcome_authority_repair import sdk_client
from tests.test_native_outcome_wire import wire
from tests import test_native_outcome_wire as native_wire


PROFILE = replace(get_provider_profile("openai"), api_mode="responses")


def native(parts, status="completed"):
    item = {"type": "message", "id": "msg_native", "role": "assistant", "status": status,
            "content": [{**part, **({"annotations": []} if part["type"] == "output_text" else {})}
                        for part in parts]}
    raw = {"id": "resp_native", "object": "response", "model": "synthetic", "created_at": 1,
           "status": status, "output": [item], "usage": {"input_tokens": 11, "output_tokens": 5},
           "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None}
    events = []
    for index, part in enumerate(parts):
        kind = part["type"]
        events.append({"type": "response." + kind + ".delta", "output_index": 0,
                       "content_index": index, "item_id": "msg_native",
                       "delta": part["refusal" if kind == "refusal" else "text"], "logprobs": []})
    events.extend([{"type": "response.output_item.done", "output_index": 0, "item": item},
                   {"type": "response." + status, "response": raw}])
    for seq, event in enumerate(events):
        event["sequence_number"] = seq
    return events


@pytest.mark.parametrize("parts", [
    [{"type": "refusal", "refusal": "NO"}],
    [{"type": "output_text", "text": "INTRO"}, {"type": "refusal", "refusal": "NO"},
     {"type": "output_text", "text": "TAIL"}],
    [{"type": "output_text", "text": "FIRST"}, {"type": "output_text", "text": "SECOND"}],
], ids=["pure-refusal", "text-refusal-text", "two-text-parts"])
def test_sse_part_types_indices_deltas_and_done_match_terminal(parts):
    with sdk_client(native(parts), True) as (upstream, upstream_requests, upstream_closes):
        factory = lambda: Agent(model="synthetic", provider=PROFILE, client=upstream, max_iterations=1)  # noqa: E731
        with relay([], agent_factory=factory) as (sdk, local, calls, requests, closes):
            events = list(request(sdk, "responses", True))
            for event in events:
                type(event).model_validate(event.model_dump())
            terminal = [e for e in events if e.type in ("response.completed", "response.incomplete", "response.failed")]
            assert len(terminal) == 1 and terminal[0].type == "response.completed"
            added = [e for e in events if e.type == "response.content_part.added"]
            assert [e.part.type for e in added] == [p["type"] for p in parts]
            assert [e.content_index for e in added] == list(range(len(parts)))
            finished = [e for e in events if e.type == "response.content_part.done"]
            assert len(finished) == len(parts)
            for index, part in enumerate(parts):
                kind = part["type"]
                deltas = [e for e in events if e.type == "response." + kind + ".delta" and e.content_index == index]
                expected = part["refusal" if kind == "refusal" else "text"]
                assert "".join(e.delta for e in deltas) == expected
                done = [e for e in events if e.type == "response." + kind + ".done" and e.content_index == index]
                assert len(done) == 1 and getattr(done[0], "refusal" if kind == "refusal" else "text") == expected
                assert all(e.output_index == 0 and e.item_id == terminal[0].response.output[0].id for e in deltas + done)
            assert [e.sequence_number for e in events] == list(range(len(events)))
            item_done = [e for e in events if e.type == "response.output_item.done"]
            assert len(item_done) == 1 and item_done[0].item == terminal[0].response.output[0]
            if parts[0]["type"] == "refusal":
                assert not any(e.type.startswith("response.output_text.") for e in events)
            assert len(upstream_requests) == len(upstream_closes) == len(requests) == len(closes) == 1


@pytest.mark.parametrize("refusal_first", [False, True])
def test_chat_mixed_chunk_preserves_part_order_in_responses_sse(refusal_first, monkeypatch):
    if refusal_first:
        original = native_wire._events

        def split_events(mode, raw):
            events = original(mode, raw)
            chunk = events[0]
            return [{**chunk, "choices": [{"index": 0, "delta": {"refusal": "NO"}, "finish_reason": None}]},
                    {**chunk, "choices": [{"index": 0, "delta": {"content": "INTRO"}, "finish_reason": "stop"}]},
                    *events[1:]]

        monkeypatch.setattr(native_wire, "_events", split_events)
    raw = {"choices": [{"message": {"role": "assistant", "content": "INTRO", "refusal": "NO"},
                        "finish_reason": "stop"}], "usage": None}
    with wire("chat_completions", raw, True) as (upstream, upstream_requests, upstream_closes):
        factory = lambda: Agent(model="synthetic", provider=get_provider_profile("openai"), client=upstream, max_iterations=1)  # noqa: E731
        with relay([], agent_factory=factory) as (sdk, local, calls, requests, closes):
            events = list(request(sdk, "responses", True))
            terminal = events[-1].response
            kinds = ["refusal", "output_text"] if refusal_first else ["output_text", "refusal"]
            assert [p.type for p in terminal.output[0].content] == kinds
            added = [e for e in events if e.type == "response.content_part.added"]
            assert [e.part.type for e in added] == kinds
            assert [(e.content_index, e.delta) for e in events if e.type.endswith(".delta")] == (
                [(0, "NO"), (1, "INTRO")] if refusal_first else [(0, "INTRO"), (1, "NO")])
            done = [e for e in events if e.type == "response.content_part.done"]
            assert [e.part for e in done] == terminal.output[0].content
            assert len(upstream_requests) == len(upstream_closes) == len(requests) == len(closes) == 1


@pytest.mark.parametrize("reason", ["refusal", "end_turn"])
def test_anthropic_final_reason_does_not_retype_native_text_deltas(reason, monkeypatch):
    original = native_wire._events

    def events(mode, raw):
        output = []
        for event in original(mode, raw):
            if event["type"] == "content_block_start":
                output.append({**event, "content_block": {"type": "text", "text": ""}})
                output.append({"type": "content_block_delta", "index": event["index"],
                               "delta": {"type": "text_delta", "text": "NATIVE TEXT"}})
            else:
                output.append(event)
        return output

    monkeypatch.setattr(native_wire, "_events", events)
    raw = {"stop_reason": reason, "content": [{"type": "text", "text": "NATIVE TEXT"}],
           "usage": {"input_tokens": 11, "output_tokens": 5}}
    with wire("anthropic_messages", raw, True) as (upstream, upstream_requests, upstream_closes):
        factory = lambda: Agent(model="synthetic", provider=get_provider_profile("anthropic"), client=upstream, max_iterations=1)  # noqa: E731
        with relay([], agent_factory=factory) as (sdk, local, calls, requests, closes):
            observed = list(request(sdk, "responses", True))
            for event in observed:
                type(event).model_validate(event.model_dump())
            terminal = observed[-1].response
            assert terminal.status == "completed"
            assert terminal.output[0].content[0].type == "output_text"
            assert terminal.output[0].content[0].text == "NATIVE TEXT"
            assert [e.delta for e in observed if e.type == "response.output_text.delta"] == ["NATIVE TEXT"]
            assert not any(e.type.startswith("response.refusal.") for e in observed)
            assert getattr(terminal, "lohra_native_outcome", None) == (
                {"api_mode": "anthropic_messages", "reason": "refusal"} if reason == "refusal" else None)
            assert len(upstream_requests) == len(upstream_closes) == len(requests) == len(closes) == 1


@pytest.mark.parametrize("mode", ["responses", "chat_completions"])
@pytest.mark.parametrize("streamed", [False, True])
def test_native_truncation_keeps_authority_over_refusal_content(mode, streamed):
    # Coordinator source check: content kind must not promote incomplete to completed.
    raw = ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "NO"}]}]}
           if mode == "responses" else {"choices": [{"message": {"role": "assistant", "content": None,
             "refusal": "NO"}, "finish_reason": "length"}]})
    raw["usage"] = None
    with wire(mode, raw, streamed) as (upstream, upstream_requests, upstream_closes):
        factory = lambda: Agent(model="synthetic", provider=replace(PROFILE, api_mode=mode), client=upstream, max_iterations=1)  # noqa: E731
        with relay([], agent_factory=factory) as (sdk, local, calls, requests, closes):
            result = request(sdk, "responses", streamed)
            if streamed:
                terminals = [e for e in result if e.type in ("response.completed", "response.incomplete", "response.failed")]
                assert len(terminals) == 1
                result = terminals[0].response
            type(result).model_validate(result.model_dump())
            assert result.status == "incomplete"
            assert result.incomplete_details.reason == "max_output_tokens"
            assert result.output[0].status == "incomplete"
            assert result.output[0].content[0].type == "refusal"
            assert len(upstream_requests) == len(upstream_closes) == len(requests) == len(closes) == 1
