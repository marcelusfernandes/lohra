"""OpenAI Chat Completions response shapes (pure, no network).

Builds the ``chat.completion`` / ``chat.completion.chunk`` / models-list objects
and the SSE framing, and splits an incoming messages array into Lohra's
(history, last-user-turn) form. Kept separate from the app so the wire format is
unit-tested without HTTP.
"""

from __future__ import annotations

import json
from typing import Any


class CompletionError(ValueError):
    """A bad request (400) — e.g. malformed messages."""


class UpstreamError(CompletionError):
    """The upstream provider/turn failed (502), not the client's fault."""


def split_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """Return (history, last_user_text). The request must end with a user turn."""
    if not messages:
        raise CompletionError("'messages' must not be empty")
    last = messages[-1]
    if last.get("role") != "user":
        raise CompletionError("the last message must be a user message")
    content = last.get("content")
    return messages[:-1], content if isinstance(content, str) else ""


def build_chat_completion(
    *,
    completion_id: str,
    model: str,
    content: str,
    finish_reason: str,
    usage: dict[str, int],
    created: int,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


def build_chunk(
    *,
    completion_id: str,
    model: str,
    delta: dict[str, Any],
    created: int,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse_event(payload: dict[str, Any]) -> str:
    """Frame a JSON payload as one Server-Sent Event data line."""
    return f"data: {json.dumps(payload)}\n\n"


def build_done() -> str:
    """The terminal SSE sentinel an OpenAI client waits for."""
    return "data: [DONE]\n\n"


def build_models_list(model_ids: list[str], *, created: int) -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {"id": model_id, "object": "model", "created": created, "owned_by": "lohra"}
            for model_id in model_ids
        ],
    }
