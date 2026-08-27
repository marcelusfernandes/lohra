"""Context compression — keep long conversations under the window (spec §5).

ContextEngine is the pluggable contract; ContextCompressor protects the head
(first N turns) and tail (last N turns) verbatim and replaces the middle with a
single summary message, marked [CONTEXT COMPACTION — REFERENCE ONLY] so the
model treats it as background while MEMORY/USER stay authoritative.

Summarization is injected (a callable wrapping the cheap auxiliary model), so
the algorithm is testable without a network call. Orphan tool_use/tool_result
pairs at the new boundaries are cleaned so the result stays API-valid.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

COMPACTION_PREFIX = "[CONTEXT COMPACTION — REFERENCE ONLY]"

Summarize = Callable[[str], str]


class ContextEngine(ABC):
    @abstractmethod
    def should_compress(self, prompt_tokens: int, context_window: int) -> bool: ...

    @abstractmethod
    def compress(self, messages: list[dict], *, summarize: Summarize) -> list[dict]: ...


def _strip_orphan_tools(messages: list[dict]) -> list[dict]:
    """Drop tool results / tool_calls whose counterpart is not in this slice."""
    call_ids = {tc["id"] for m in messages for tc in (m.get("tool_calls") or ())}
    result_ids = {m["tool_call_id"] for m in messages if m.get("role") == "tool"}

    out: list[dict] = []
    for message in messages:
        if message.get("role") == "tool":
            if message.get("tool_call_id") in call_ids:
                out.append(message)  # else: orphan result -> drop
        elif message.get("tool_calls"):
            kept = [tc for tc in message["tool_calls"] if tc["id"] in result_ids]
            if kept:
                out.append({**message, "tool_calls": kept})
            else:
                without = {k: v for k, v in message.items() if k != "tool_calls"}
                if without.get("content"):
                    out.append(without)  # keep the text, drop the orphan tool_use
        else:
            out.append(message)
    return out


def _render(messages: list[dict]) -> str:
    lines = []
    for message in messages:
        role = message.get("role", "?")
        content = message.get("content") or ""
        if message.get("role") == "tool":
            lines.append(f"[tool:{message.get('name', '')}] {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


class ContextCompressor(ContextEngine):
    def __init__(
        self,
        *,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 20,
    ) -> None:
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n

    def should_compress(self, prompt_tokens: int, context_window: int) -> bool:
        return prompt_tokens > context_window * self.threshold_percent

    def compress(self, messages: list[dict], *, summarize: Summarize) -> list[dict]:
        protected = self.protect_first_n + self.protect_last_n
        if len(messages) <= protected:
            return messages  # nothing in the middle to summarize

        head = messages[: self.protect_first_n]
        middle = messages[self.protect_first_n : len(messages) - self.protect_last_n]
        tail = messages[len(messages) - self.protect_last_n :]

        summary_text = summarize(_render(middle))
        summary_message: dict[str, Any] = {
            "role": "user",
            "content": f"{COMPACTION_PREFIX}\n{summary_text}",
        }
        return _strip_orphan_tools(head) + [summary_message] + _strip_orphan_tools(tail)
