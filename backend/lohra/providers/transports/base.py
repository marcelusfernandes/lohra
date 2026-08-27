"""Transport contract — one implementation per api_mode.

A transport owns the two protocol-specific boundaries of an API call:
``build_kwargs`` (internal message schema -> provider request kwargs) and
``normalize_response`` (raw provider response -> NormalizedResponse). The
conversation loop only ever sees the canonical types; it never branches on
api_mode. See docs/specs/01-agent-core.md §2.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from lohra.agent.types import NormalizedResponse


def get_field(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a dict or an attribute-style SDK object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def parse_tool_arguments(arguments: str | None) -> dict:
    """Parse a tool-call JSON arguments string; malformed input becomes {}."""
    try:
        parsed = json.loads(arguments or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class Transport(ABC):
    """Protocol adapter for a single api_mode."""

    api_mode: str = ""

    @abstractmethod
    def build_kwargs(
        self,
        *,
        model: str,
        messages: list[dict],
        system: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tool_choice: str | None = None,
        effort: str | None = None,
    ) -> dict:
        """Build provider request kwargs from the internal message schema.

        ``tool_choice`` (a tool name) forces that tool; ``effort`` is the reasoning
        effort, emitted only by transports/providers that support it. ``None`` for
        either is byte-identical to today's behavior. Must NOT mutate ``messages``;
        always return new structures.
        """

    @abstractmethod
    def normalize_response(self, raw: Any) -> NormalizedResponse:
        """Convert a raw provider response into the canonical type."""


# --- Process-wide registry, keyed by api_mode (last-writer-wins) ---

_TRANSPORTS: dict[str, Transport] = {}


def register_transport(transport: Transport) -> None:
    """Register a transport instance under its api_mode."""
    _TRANSPORTS[transport.api_mode] = transport


def get_transport(api_mode: str) -> Transport | None:
    """Resolve a transport by api_mode. Returns None for unknown modes."""
    return _TRANSPORTS.get(api_mode)
