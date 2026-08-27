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
        usage = self._usage(result.get("usage"), messages, content)
        return {
            "model": model,
            "content": content,
            "finish_reason": "length" if result["partial"] else "stop",
            "usage": usage,
        }

    @staticmethod
    def _usage(reported: Any, messages: list[dict], content: str) -> dict[str, int]:
        """Prefer the provider's real token counts; estimate only if absent."""
        if reported is not None:
            prompt_tokens = reported.input_tokens or 0
            completion_tokens = reported.output_tokens or 0
        else:
            prompt_tokens = _estimate_tokens(
                "".join(str(m.get("content") or "") for m in messages)
            )
            completion_tokens = max(1, _estimate_tokens(content))
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
