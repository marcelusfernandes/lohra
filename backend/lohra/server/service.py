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
from lohra.server.cancellation import RequestCancellation
from lohra.server.format import UpstreamError, split_messages
from lohra.server.usage import usage_fields

AgentFactory = Callable[[], Agent]
DeltaCallback = Callable[[str], None]


class CompletionResult(dict):
    """The existing mapping plus local provenance, absent from its wire JSON."""

    def __init__(self, *, usage_observed: bool, observed_usage: dict | None = None, **values: Any) -> None:
        super().__init__(values)
        self.usage_observed = usage_observed
        self.observed_usage = observed_usage if observed_usage is not None else (
            values.get("usage") if usage_observed else None)


class CompletionInterrupted(UpstreamError):
    """An interrupted turn, with optional observed usage (never an estimate)."""

    def __init__(self, usage: dict | None, usage_uncertain: bool) -> None:
        message = "request interrupted"
        if usage_uncertain:
            message += "; usage is incomplete (reported usage is a known lower bound)"
        super().__init__(message, usage=usage)
        self.usage_uncertain = usage_uncertain



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
        return self._run(model=model, messages=messages, temperature=temperature,
                         max_tokens=max_tokens, on_delta=on_delta, cancellation=None)

    def run_cancellable(
        self, *, cancellation: RequestCancellation, model: str, messages: list[dict],
        temperature: float | None = None, max_tokens: int | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> dict[str, Any]:
        """Optional server/embedder protocol; legacy ``run`` stays unchanged."""
        return self._run(model=model, messages=messages, temperature=temperature,
                         max_tokens=max_tokens, on_delta=on_delta, cancellation=cancellation)

    def _run(
        self, *, model: str, messages: list[dict], temperature: float | None,
        max_tokens: int | None, on_delta: DeltaCallback | None,
        cancellation: RequestCancellation | None,
    ) -> dict[str, Any]:
        history, user_message = split_messages(messages)
        agent = self._agent_factory()
        agent.model = model  # the request picks the model; Lohra owns the provider
        if temperature is not None:
            agent.temperature = temperature
        if max_tokens is not None:
            agent.max_tokens = max_tokens

        token = cancellation.bind(agent.request_interrupt) if cancellation else None
        try:
            result = run_conversation(
                agent, user_message, conversation_history=history, stream_delta_callback=on_delta
            )
        finally:
            if cancellation is not None and token is not None:
                cancellation.unbind(token)
        reported = result.get("usage_total") or result.get("usage")
        observed = self._usage(reported)
        if result.get("interrupted") or (cancellation is not None and cancellation.cancelled):
            raise CompletionInterrupted(
                observed,
                bool(result.get("usage_uncertain")),
            )
        if result["error"]:
            raise UpstreamError(result["error"], usage=observed)
        finish = result.get("stop_reason")
        if not result.get("completed") or finish not in ("stop", "length", "content_filter"):
            raise UpstreamError("turn ended without a valid completion", usage=observed)
        native = result.get("native_outcome") or {}
        parts = result.get("output_parts")
        return CompletionResult(
            usage_observed=observed is not None, observed_usage=observed,
            model=model, content=result["final_response"], finish_reason=finish,
            native_outcome=native, output_parts=parts,
            **usage_fields(observed, bool(result.get("usage_complete"))),
        )

    @staticmethod
    def _usage(reported: Any) -> dict[str, Any] | None:
        """Reinclude cache meters once; absence stays unknown, never estimated."""
        if reported is None:
            return None
        cached, written = reported.cache_read_tokens or 0, reported.cache_write_tokens or 0
        prompt = (reported.input_tokens or 0) + cached + written
        completion = reported.output_tokens or 0
        return {
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": written},
            "completion_tokens_details": {"reasoning_tokens": reported.reasoning_tokens or 0},
        }
