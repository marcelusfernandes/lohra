"""Author regressions for public PR156 F1, using real SDK snapshot accumulation."""
from copy import deepcopy
from dataclasses import replace

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.server.stream_bridge import StreamLimits
from tests.relay_helpers import isolated as isolated, relay
from tests.test_native_outcome_authority_repair import sdk_client

PROFILE = replace(get_provider_profile("openai"), api_mode="responses")


def native_events(parts, *, terminal_only=False, starts="added"):
    content = [{"type": kind, "refusal" if kind == "refusal" else "text": text,
                **({"annotations": []} if kind == "output_text" else {})} for kind, text in parts]
    item = {"type": "message", "id": "msg_empty_parts", "role": "assistant",
            "status": "completed", "content": content}
    raw = {"id": "resp_empty_parts", "object": "response", "model": "synthetic", "created_at": 1,
           "status": "completed", "output": [item], "usage": {"input_tokens": 9, "output_tokens": 3}}
    events = []
    if not terminal_only:
        for index, part in enumerate(content):
            kind = part["type"]
            field = "refusal" if kind == "refusal" else "text"
            common = {"item_id": item["id"], "output_index": 0, "content_index": index}
            if starts not in ("done-only", "empty-delta"):
                events.append({"type": "response.content_part.added", **common,
                               "part": {**part, field: ""}})
                if starts == "repeated":
                    events.append(deepcopy(events[-1]))
            if part[field] or starts == "empty-delta":
                events.append({"type": "response." + kind + ".delta", **common,
                               "delta": part[field], **({"logprobs": []} if kind == "output_text" else {})})
            events.append({"type": "response.content_part.done", **common, "part": part})
        events.append({"type": "response.output_item.done", "output_index": 0, "item": item})
    events.append({"type": "response.completed", "response": raw})
    return [{**deepcopy(event), "sequence_number": seq} for seq, event in enumerate(events)]


CASES = [
    pytest.param([("output_text", ""), ("refusal", "DENIED")], False, id="empty-text-before-refusal"),
    pytest.param([("refusal", ""), ("output_text", "VISIBLE")], False, id="empty-refusal-before-text"),
    pytest.param([("output_text", "FIRST"), ("refusal", ""), ("output_text", "LAST")], False,
                 id="empty-refusal-between-text"),
    pytest.param([("output_text", "ONLY")], False, id="single-text-control"),
    pytest.param([("output_text", "INTRO"), ("refusal", "NO"), ("output_text", "END")], False,
                 id="mixed-nonempty-control"),
    pytest.param([("output_text", ""), ("refusal", "NO"), ("output_text", "END")], True,
                 id="terminal-only-control"),
]


@pytest.mark.parametrize("parts,terminal_only", CASES)
def test_single_response_empty_parts_preserve_sdk_snapshots(parts, terminal_only):
    check_snapshots(parts, terminal_only=terminal_only)


@pytest.mark.parametrize("starts", ["added", "repeated", "done-only", "empty-delta"])
@pytest.mark.parametrize("parts", [
    [("output_text", ""), ("refusal", ""), ("output_text", "")],
    [("refusal", ""), ("output_text", "VISIBLE"), ("refusal", "")],
], ids=["all-empty", "empty-at-both-ends"])
def test_silent_and_repeated_part_notifications_keep_identity(starts, parts):
    check_snapshots(parts, starts=starts)


def check_snapshots(parts, *, terminal_only=False, starts="added"):
    events = []
    with sdk_client(native_events(parts, terminal_only=terminal_only, starts=starts), True) as (upstream, sent, upstream_closes):
        def factory():
            return Agent(model="synthetic", provider=PROFILE, client=upstream, max_iterations=1)

        with relay([], agent_factory=factory, stream_limits=StreamLimits(pieces=1, bytes=4)) as (sdk, local, calls, requests, closes):
            try:
                with sdk.responses.stream(model="synthetic", input="A") as stream:
                    events.extend(stream)
                    final = stream.get_final_response()
            finally:
                # These run on a semantic/assertion failure as well; relay additionally
                # checks that the producer inventory is empty in its own finally.
                assert len(sent) == len(upstream_closes) == len(requests) == len(closes) == 1
    assert final.status == "completed"
    assert (final.usage.input_tokens, final.usage.output_tokens) == (9, 3)
    added = [e for e in events if e.type == "response.content_part.added"]
    assert [(e.content_index, e.part.type) for e in added] == list(enumerate(kind for kind, _ in parts))
    assert [e.type for e in events if e.type in ("response.completed", "response.incomplete", "response.failed")] == ["response.completed"]
    assert [e.sequence_number for e in events] == list(range(len(events)))
    for index, (kind, text) in enumerate(parts):
        field = "refusal" if kind == "refusal" else "text"
        deltas = [e for e in events if e.type in ("response.output_text.delta", "response.refusal.delta")
                  and e.content_index == index]
        assert all(e.type == "response." + kind + ".delta" and e.delta for e in deltas)
        assert "".join(e.delta for e in deltas) == text
        if kind == "output_text" and deltas:
            assert deltas[-1].snapshot == text
        done = [e for e in events if e.type == "response." + kind + ".done" and e.content_index == index]
        assert len(done) == 1 and getattr(done[0], field) == text
        finished = [e for e in events if e.type == "response.content_part.done" and e.content_index == index]
        assert len(finished) == 1 and finished[0].part.type == kind and getattr(finished[0].part, field) == text
        assert final.output[0].content[index].type == kind
        assert getattr(final.output[0].content[index], field) == text
        assert all(e.item_id == final.output[0].id and e.output_index == 0 for e in deltas + done + finished)
    assert [e.item.model_dump() for e in events if e.type == "response.output_item.done"] == [
        item.model_dump(exclude={"content": {"__all__": {"parsed"}}}) for item in final.output]
    for event in events:
        type(event).model_validate(event.model_dump())


@pytest.mark.parametrize("starts", ["added", "repeated", "done-only", "empty-delta"])
def test_legacy_sdk_callback_gets_no_structural_notifications(starts):
    parts = [("output_text", ""), ("refusal", "NO"), ("output_text", "TEXT")]
    received = []
    with sdk_client(native_events(parts, starts=starts), True) as (upstream, sent, closes):
        try:
            result = upstream.stream(model="synthetic", input="A", on_text=received.append)
        finally:
            assert len(sent) == len(closes) == 1
    assert received == ["NO", "TEXT"]
    assert all(isinstance(text, str) for text in received)
    assert result["status"] == "completed"


def test_chat_does_not_publish_structural_empty_deltas():
    parts = [("output_text", ""), ("refusal", "NO")]
    with sdk_client(native_events(parts, starts="repeated"), True) as (upstream, sent, upstream_closes):
        def factory():
            return Agent(model="synthetic", provider=PROFILE, client=upstream, max_iterations=1)

        with relay([], agent_factory=factory) as (sdk, local, calls, requests, closes):
            try:
                with sdk.chat.completions.create(model="synthetic", messages=[{"role": "user", "content": "A"}],
                                                 stream=True, stream_options={"include_usage": True}) as stream:
                    chunks = list(stream)
            finally:
                assert len(sent) == len(upstream_closes) == len(requests) == len(closes) == 1
    deltas = [choice.delta for chunk in chunks for choice in chunk.choices]
    assert [d.refusal for d in deltas if d.refusal is not None] == ["NO"]
    assert not any(d.content is not None for d in deltas)
    assert [choice.finish_reason for chunk in chunks for choice in chunk.choices if choice.finish_reason] == ["stop"]
    assert (chunks[-1].usage.prompt_tokens, chunks[-1].usage.completion_tokens) == (9, 3)
