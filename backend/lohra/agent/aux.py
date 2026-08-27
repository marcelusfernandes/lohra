"""Auxiliary client — routes side tasks to a cheap, fast model (spec §8).

Compaction summaries and session titles don't need the main model. AuxClient
wraps a ModelClient + transport at the provider's aux model (e.g. Haiku) and
exposes ``summarize`` / ``title``, plus ``summarizer()`` to hand the compressor
a plain Callable[[str], str].
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.agent.client import ModelClient
from lohra.providers.transports.base import Transport

SUMMARY_SYSTEM = (
    "You are compacting a long conversation to fit the context window. Summarize "
    "the transcript below into a concise but complete reference. Under these "
    "headings, keep only what is still relevant: Active Task; Goal; Completed "
    "Actions (results, not narration); Active State (files/vars/decisions in "
    "play); Blocked/Open; Key Decisions (and why); Pending User Asks; Remaining "
    "Work. Be factual and terse. Never invent details."
)

TITLE_SYSTEM = (
    "Write a short (≤6 words) title for this conversation. Reply with the title "
    "only — no quotes, no punctuation at the end."
)


class AuxClient:
    def __init__(self, *, client: ModelClient, transport: Transport, model: str) -> None:
        self._client = client
        self._transport = transport
        self._model = model

    def complete(self, system: str, user: str, *, max_tokens: int = 1024) -> str:
        kwargs: dict[str, Any] = self._transport.build_kwargs(
            model=self._model,
            messages=[{"role": "user", "content": user}],
            system=system,
            max_tokens=max_tokens,
        )
        response = self._transport.normalize_response(self._client.create(**kwargs))
        return (response.content or "").strip()

    def summarize(self, transcript: str) -> str:
        return self.complete(SUMMARY_SYSTEM, transcript)

    def title(self, transcript: str) -> str:
        return self.complete(TITLE_SYSTEM, transcript, max_tokens=32)

    def summarizer(self) -> Callable[[str], str]:
        """A plain callable for ContextCompressor.compress(summarize=...)."""
        return self.summarize
