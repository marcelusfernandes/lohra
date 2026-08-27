"""ToolRegistry — thread-safe singleton where tools self-register at import.

Each tool module calls ``registry.register(...)`` at module top level. The
canonical internal schema is the OpenAI function-calling format; Anthropic
``input_schema`` conversion happens at the transport boundary, not here.

See docs/specs/02-tool-system.md §1-3.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

_CHECK_FN_TTL_SECONDS = 30.0

Handler = Callable[..., str]  # handlers always return a JSON string
CheckFn = Callable[[], bool]


@dataclass(frozen=True)
class ToolEntry:
    """One registered tool. Immutable; re-register to change."""

    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Handler
    check_fn: CheckFn | None = None
    requires_env: tuple[str, ...] = ()
    is_async: bool = False
    description: str = ""
    emoji: str = "⚡"
    max_result_size_chars: int | None = None


def tool_error(message: str, **extra: Any) -> str:
    """Build an error result. Handlers return this as their JSON string."""
    return json.dumps({"error": message, **extra})


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """Build a success result envelope as a JSON string."""
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(kwargs)
    return json.dumps(payload)


class ToolRegistry:
    """Thread-safe tool registry with a generation counter and check_fn cache."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, ToolEntry] = {}
        self._generation = 0
        self._check_cache: dict[int, tuple[float, bool]] = {}

    @property
    def generation(self) -> int:
        return self._generation

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Handler,
        *,
        check_fn: CheckFn | None = None,
        requires_env: tuple[str, ...] = (),
        is_async: bool = False,
        description: str = "",
        emoji: str = "⚡",
        max_result_size_chars: int | None = None,
        override: bool = False,
    ) -> None:
        """Register a tool. Rejects shadowing across toolsets unless override."""
        with self._lock:
            existing = self._entries.get(name)
            if existing and existing.toolset != toolset:
                both_mcp = existing.toolset.startswith("mcp-") and toolset.startswith("mcp-")
                if not (both_mcp or override):
                    raise ValueError(
                        f"tool {name!r} already registered under {existing.toolset!r}"
                    )
            self._entries[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema={**schema, "name": name},
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env,
                is_async=is_async,
                description=description or schema.get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
            )
            self._bump()

    def deregister(self, name: str) -> None:
        with self._lock:
            if self._entries.pop(name, None) is not None:
                self._bump()

    def names_in_toolset(self, toolset: str) -> list[str]:
        """Names of every tool registered under a toolset (for nuke-and-repave)."""
        with self._lock:
            return [name for name, entry in self._entries.items() if entry.toolset == toolset]

    def _bump(self) -> None:
        self._generation += 1
        self._check_cache.clear()

    def _is_available(self, entry: ToolEntry) -> bool:
        if entry.check_fn is None:
            return True
        key = id(entry.check_fn)
        now = time.monotonic()
        cached = self._check_cache.get(key)
        if cached and now - cached[0] < _CHECK_FN_TTL_SECONDS:
            return cached[1]
        try:
            result = bool(entry.check_fn())
        except Exception:
            result = False
        self._check_cache[key] = (now, result)
        return result

    def get_definitions(self, enabled: set[str] | None = None) -> list[dict[str, Any]]:
        """OpenAI tools array, filtered by availability and optional toolset set."""
        with self._lock:
            out = []
            for entry in self._entries.values():
                if enabled is not None and entry.toolset not in enabled:
                    continue
                if not self._is_available(entry):
                    continue
                out.append({"type": "function", "function": dict(entry.schema)})
            return out

    def dispatch(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        """Route a tool call to its handler. All errors become a JSON envelope."""
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")
        try:
            return entry.handler(args, **kwargs)
        except Exception as exc:  # defense-in-depth: never raise into the loop
            return tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")


# Module-level singleton — importing a tool module registers its tools.
registry = ToolRegistry()
