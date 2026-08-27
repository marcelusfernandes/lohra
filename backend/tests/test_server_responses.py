"""Tests for the OpenAI Responses API format (pure, no network).

`/v1/responses` is OpenAI's newer Responses API — different request/response
shape from chat completions, but it runs on the same CompletionService. These
tests pin the input parsing and the `response` object / typed SSE framing.
"""

import json

import pytest

from lohra.server.format import CompletionError
from lohra.server.responses import (
    build_response_completed_event,
    build_response_created_event,
    build_response_object,
    build_text_delta_event,
    parse_responses_input,
    responses_sse,
)

pytest.importorskip("openai")  # SDK round-trip validation needs the openai types


# --- parse_responses_input ---


def test_string_input_becomes_a_user_message():
    assert parse_responses_input("hi", None) == [{"role": "user", "content": "hi"}]


def test_instructions_prepend_a_system_message():
    out = parse_responses_input("hi", "be brief")
    assert out == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]


def test_list_input_of_role_content_items():
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert parse_responses_input(items, None) == items


def test_list_input_with_content_parts_extracts_text():
    items = [
        {"role": "user", "content": [{"type": "input_text", "text": "hello "}, {"type": "input_text", "text": "world"}]}
    ]
    assert parse_responses_input(items, None) == [{"role": "user", "content": "hello world"}]


def test_empty_input_raises():
    with pytest.raises(CompletionError):
        parse_responses_input("", None)


def test_non_string_non_list_input_raises():
    with pytest.raises(CompletionError):
        parse_responses_input(123, None)


def test_list_item_missing_role_raises():
    with pytest.raises(CompletionError):
        parse_responses_input([{"content": "x"}], None)


def test_empty_list_input_raises():
    with pytest.raises(CompletionError):
        parse_responses_input([], None)


def test_non_text_content_becomes_empty_string():
    out = parse_responses_input([{"role": "user", "content": None}], None)
    assert out == [{"role": "user", "content": ""}]


# --- build_response_object ---


def test_response_object_shape():
    obj = build_response_object(
        response_id="resp_1",
        model="gpt-4o",
        content="hello",
        status="completed",
        usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        created=1700000000,
    )
    assert obj["output_text"] == "hello"
    message = obj["output"][0]
    assert message["content"][0] == {"type": "output_text", "text": "hello", "annotations": []}
    # Responses usage uses input/output token names
    assert obj["usage"]["input_tokens"] == 3
    assert obj["usage"]["output_tokens"] == 1
    assert obj["usage"]["total_tokens"] == 4


# --- typed SSE events ---


def test_responses_sse_frames_event_and_data():
    line = responses_sse("response.created", {"a": 1})
    assert line == 'event: response.created\ndata: {"a": 1}\n\n'


def _payload(line):
    return json.loads(line.split("data: ", 1)[1])


def test_created_event_carries_in_progress_response():
    payload = _payload(
        build_response_created_event(response_id="resp_1", model="m", created=1, sequence_number=0)
    )
    assert payload["type"] == "response.created"
    assert payload["response"]["status"] == "in_progress"
    assert payload["sequence_number"] == 0


def test_text_delta_event():
    payload = _payload(build_text_delta_event(response_id="resp_1", delta="lo", sequence_number=3))
    assert payload["type"] == "response.output_text.delta"
    assert payload["delta"] == "lo"
    assert payload["sequence_number"] == 3


# --- the real test: every payload validates against the openai SDK models ---


def _completed_response():
    return build_response_object(
        response_id="resp_1", model="m", content="done", status="completed",
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, created=1,
    )


def test_response_object_validates_against_openai_sdk():
    from openai.types.responses import Response

    Response.model_validate(_completed_response())  # raises on a missing/invalid field


def test_stream_events_validate_against_openai_sdk():
    from lohra.server.responses import (
        build_content_part_added_event,
        build_output_item_added_event,
        build_response_failed_event,
    )
    from openai.types.responses import (
        ResponseCompletedEvent,
        ResponseContentPartAddedEvent,
        ResponseCreatedEvent,
        ResponseFailedEvent,
        ResponseOutputItemAddedEvent,
        ResponseTextDeltaEvent,
    )

    ResponseCreatedEvent.model_validate(
        _payload(build_response_created_event(response_id="resp_1", model="m", created=1, sequence_number=0))
    )
    ResponseOutputItemAddedEvent.model_validate(
        _payload(build_output_item_added_event(response_id="resp_1", sequence_number=1))
    )
    ResponseContentPartAddedEvent.model_validate(
        _payload(build_content_part_added_event(response_id="resp_1", sequence_number=2))
    )
    ResponseTextDeltaEvent.model_validate(
        _payload(build_text_delta_event(response_id="resp_1", delta="x", sequence_number=3))
    )
    ResponseCompletedEvent.model_validate(
        _payload(build_response_completed_event(_completed_response(), sequence_number=4))
    )
    failed = build_response_object(
        response_id="resp_1", model="m", content="", status="failed",
        usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, created=1,
        error={"code": "server_error", "message": "boom"},
    )
    ResponseFailedEvent.model_validate(_payload(build_response_failed_event(failed, sequence_number=5)))
