"""CompletionService — run one stateless agent turn for the OpenAI endpoint.

Splits the request's messages into (history, last-user-turn), runs a fresh agent
to completion (relay mode — no tools, no memory: an OpenAI-compatible gateway to
the configured provider), and maps the loop result to content/finish/usage.
Streaming is the same path with a per-delta callback.
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.agent.agent import Agent
from lohra.agent.loop import run_conversation
from lohra.server.format import UpstreamError, split_messages

AgentFactory = Callable[[], Agent]
DeltaCallback = Callable[[str], None]


def _estimate_tokens(text: str) -> int:
    return max(0, len(text) // 4)


class CompletionService:
    """Owns the agent factory; runs one completion per request."""

    def __init__(self, agent_factory: AgentFactory) -> None:
        self._agent_factory = agent_factory

    def run(
        self,
        *,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> dict[str, Any]:
        history, user_message = split_messages(messages)
        agent = self._agent_factory()
        agent.model = model  # the request picks the model; Lohra owns the provider
        if temperature is not None:
            agent.temperature = temperature
        if max_tokens is not None:
            agent.max_tokens = max_tokens

        result = run_conversation(
            agent, user_message, conversation_history=history, stream_delta_callback=on_delta
        )
        if result["error"]:
            raise UpstreamError(result["error"])

        content = result["final_response"] or ""
        # ``usage_total`` (every API call of the turn), not the last one: in
        # agentic mode a turn is several calls and the caller is billed for all.
        usage = self._usage(
            result.get("usage_total") or result.get("usage"), messages, content
        )
        return {
            "model": model,
            "content": content,
            "finish_reason": "length" if result["partial"] else "stop",
            "usage": usage,
        }

    @staticmethod
    def _usage(reported: Any, messages: list[dict], content: str) -> dict[str, Any]:
        """Prefer the provider's real token counts; estimate only if absent.

        RE-INCLUSIVE at the wire (Fatia C): inside Lohra ``input_tokens`` is the
        prompt that was NOT cached, but this envelope is the OpenAI shape, where
        ``prompt_tokens`` is the whole prompt and the details are a BREAKDOWN of
        it. Emitting the uncached number as ``prompt_tokens`` would let an
        SDK-strict client discount the cache twice."""
        cached = written = reasoning = 0
        if reported is not None:
            uncached = reported.input_tokens or 0
            completion_tokens = reported.output_tokens or 0
            cached = reported.cache_read_tokens or 0
            written = reported.cache_write_tokens or 0
            reasoning = reported.reasoning_tokens or 0
            prompt_tokens = uncached + cached + written
        else:
            prompt_tokens = _estimate_tokens(
                "".join(str(m.get("content") or "") for m in messages)
            )
            completion_tokens = max(1, _estimate_tokens(content))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "completion_tokens_details": {"reasoning_tokens": reasoning},
        }
