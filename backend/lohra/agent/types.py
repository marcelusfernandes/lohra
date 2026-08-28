"""Canonical response types — the ONLY shapes the conversation loop reads.

Every provider quirk (Anthropic, OpenAI Responses, chat completions) is pushed
into transports and into ``provider_data``; the loop never branches on api_mode
when reading a response. See docs/specs/01-agent-core.md §2.

These dataclasses are frozen: transformations return new copies, never mutate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Usage:
    """Token accounting for a single API call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


def combine_usage(a: Usage | None, b: Usage | None) -> Usage | None:
    """Field-wise sum of two usages as a NEW Usage (frozen dataclasses; never
    mutate). None means "the provider reported nothing" and is absorbing only
    when both sides are None."""
    if a is None:
        return b
    if b is None:
        return a
    return Usage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        reasoning_tokens=a.reasoning_tokens + b.reasoning_tokens,
    )


@dataclass(frozen=True)
class ToolCall:
    """A normalized tool call extracted from a provider response.

    ``arguments`` is always a JSON string. ``provider_data`` carries
    protocol-specific state (codex call_id/response_item_id, gemini thought
    signature) that protocol-aware code reads back; the loop ignores it.
    """

    id: str | None
    name: str
    arguments: str
    provider_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class NormalizedResponse:
    """The single response type the conversation loop consumes.

    ``finish_reason`` is normalized to one of: "stop", "tool_calls", "length",
    "content_filter", "pause". "pause" means the provider suspended the turn
    (e.g. Anthropic ``pause_turn``) and the request must be resent to continue —
    it is never a final answer. Opaque reasoning blobs live in ``provider_data``
    and must be preserved unmodified — several providers 400 on replay without
    them.
    """

    content: str | None
    finish_reason: str
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict[str, Any] | None = None


FINISH_REASONS = ("stop", "tool_calls", "length", "content_filter", "pause")


def map_finish_reason(reason: str | None, mapping: dict[str, str]) -> str:
    """Translate a provider stop reason to the canonical set, default "stop"."""
    if reason is None:
        return "stop"
    return mapping.get(reason, "stop" if reason not in FINISH_REASONS else reason)
