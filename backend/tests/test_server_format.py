"""Tests for the OpenAI-compatible response formatting (pure, no network)."""

import json

import pytest

from lohra.server.format import (
    CompletionError,
    build_chat_completion,
    build_chunk,
    build_done,
    build_models_list,
    split_messages,
    sse_event,
)


# --- split_messages ---


def test_split_messages_separates_history_and_last_user():
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    history, user = split_messages(messages)
    assert user == "second"
    assert history == messages[:-1]


def test_split_messages_requires_a_trailing_user_message():
    with pytest.raises(CompletionError, match="user message"):
        split_messages([{"role": "assistant", "content": "x"}])


def test_split_messages_rejects_empty():
    with pytest.raises(CompletionError):
        split_messages([])


# --- build_chat_completion ---


def test_build_chat_completion_shape():
    out = build_chat_completion(
        completion_id="chatcmpl-1",
        model="gpt-4o",
        content="hello",
        finish_reason="stop",
        usage={"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        created=1700000000,
    )
    assert out["id"] == "chatcmpl-1"
    assert out["object"] == "chat.completion"
    assert out["model"] == "gpt-4o"
    assert out["created"] == 1700000000
    choice = out["choices"][0]
    assert choice["index"] == 0
    assert choice["message"] == {"role": "assistant", "content": "hello"}
    assert choice["finish_reason"] == "stop"
    assert out["usage"]["total_tokens"] == 4


# --- build_chunk + sse ---


def test_build_chunk_carries_delta_and_object_type():
    chunk = build_chunk(
        completion_id="chatcmpl-1", model="gpt-4o", delta={"content": "hi"}, created=1700000000
    )
    assert chunk["object"] == "chat.completion.chunk"
    assert chunk["choices"][0]["delta"] == {"content": "hi"}
    assert chunk["choices"][0]["finish_reason"] is None


def test_build_chunk_with_finish_reason():
    chunk = build_chunk(
        completion_id="c", model="m", delta={}, finish_reason="stop", created=1
    )
    assert chunk["choices"][0]["finish_reason"] == "stop"


def test_sse_event_frames_json_as_data_line():
    line = sse_event({"a": 1})
    assert line == 'data: {"a": 1}\n\n'


def test_build_done_is_the_sentinel():
    assert build_done() == "data: [DONE]\n\n"


# --- models list ---


def test_build_models_list():
    out = build_models_list(["gpt-4o", "claude-opus-4-8"], created=1700000000)
    assert out["object"] == "list"
    ids = [m["id"] for m in out["data"]]
    assert ids == ["gpt-4o", "claude-opus-4-8"]
    assert all(m["object"] == "model" for m in out["data"])
    assert all(m["owned_by"] == "lohra" for m in out["data"])


def test_chunk_roundtrips_through_sse():
    chunk = build_chunk(completion_id="c", model="m", delta={"content": "x"}, created=1)
    line = sse_event(chunk)
    assert line.startswith("data: ")
    payload = json.loads(line[len("data: ") :].strip())
    assert payload["choices"][0]["delta"]["content"] == "x"
