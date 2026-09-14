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
from contextvars import ContextVar
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
    # An authoring-time decision (which model/route/workflow/memory/skill to
    # use) or a stateful tool bound to a parent-only store — never something a
    # delegated subagent should see. Machine-readable so the exclusion is a
    # rule over this flag, alongside legacy exclusions. Child, server and leaf
    # guards judge the same registered entry they invoke (#84/#130); root author
    # dispatch stays permitted. Schemas and tool arguments cannot set this flag.
    author_time_only: bool = False


class MCPToolCollision(ValueError):
    """Different original MCP servers claimed the same public tool name."""


DispatchGuard = Callable[[str, ToolEntry | None], str | None]
_dispatch_guards: ContextVar[tuple[DispatchGuard, ...]] = ContextVar("dispatch_guards", default=())


def dispatch_denial(name: str, entry: ToolEntry | None) -> str | None:
    """Every active guard judges the SAME immutable entry; none can widen another."""
    for guard in _dispatch_guards.get():
        refusal = guard(name, entry)
        if refusal is not None:
            return refusal
    return None


def bind_dispatch_guard(base: Callable[[str, dict], str], guard: DispatchGuard):
    """Bind inside the executing worker, preserve wrappers, restore even on BaseException.

    Trusted Python code supplies guards; tool JSON never carries this authority.
    Nested guards compose by intersection, including calls made inside handlers.
    """
    def dispatch(name: str, args: dict) -> str:
        token = _dispatch_guards.set((*_dispatch_guards.get(), guard))
        try:
            return base(name, args)
        finally:
            _dispatch_guards.reset(token)

    return dispatch


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
        author_time_only: bool = False,
    ) -> None:
        """Register a tool. Rejects shadowing across toolsets unless override."""
        with self._lock:
            existing = self._entries.get(name)
            if existing and existing.toolset != toolset:
                both_mcp = existing.toolset.startswith("mcp-") and toolset.startswith("mcp-")
                if both_mcp and not override:
                    raise MCPToolCollision(
                        f"MCP tool {name!r} belongs to {existing.toolset!r}, not {toolset!r}"
                    )
                if not override:
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
                author_time_only=author_time_only,
            )
            self._bump()

    def register_mcp_batch(self, entries: tuple[ToolEntry, ...]) -> list[str]:
        """Validate a prepared MCP listing before publishing any of its entries.

        No server callback runs under this lock. Builtins retain their names;
        ambiguous foreign MCP ownership rejects the whole listing. There is no
        rollback that could erase an unrelated concurrent registration.
        """
        with self._lock:
            accepted: dict[str, ToolEntry] = {}
            for entry in entries:
                if not entry.toolset.startswith("mcp-"):
                    raise ValueError("MCP batches require MCP toolsets")
                existing = accepted.get(entry.name) or self._entries.get(entry.name)
                if existing and existing.toolset != entry.toolset:
                    if existing.toolset.startswith("mcp-"):
                        raise MCPToolCollision(
                            f"MCP tool {entry.name!r} belongs to {existing.toolset!r}, "
                            f"not {entry.toolset!r}"
                        )
                    continue  # builtin collision: retain its registration
                accepted[entry.name] = entry
            if accepted:
                self._entries.update(accepted)
                self._bump()
            return list(accepted)

    def deregister(self, name: str) -> None:
        with self._lock:
            if self._entries.pop(name, None) is not None:
                self._bump()

    def deregister_toolset(self, toolset: str) -> None:
        """Select and remove under one lock; an old name snapshot is not ownership."""
        with self._lock:
            retained = {name: entry for name, entry in self._entries.items()
                        if entry.toolset != toolset}
            if len(retained) != len(self._entries):
                self._entries = retained
                self._bump()

    def entry(self, name: str) -> ToolEntry | None:
        """A trusted registration snapshot, not authority parsed from tool JSON."""
        with self._lock:
            return self._entries.get(name)

    def names_in_toolset(self, toolset: str) -> list[str]:
        """Names of every tool registered under a toolset (for nuke-and-repave)."""
        with self._lock:
            return [name for name, entry in self._entries.items() if entry.toolset == toolset]

    def author_time_only_names(self) -> frozenset[str]:
        """Names of every currently-registered ``author_time_only`` tool.

        Used by the builtin scope inventory (#84). New delegated definitions
        filter this flag; frozen older definitions remain snapshots, with
        current execution judged on the actual selected entry (#130).
        """
        with self._lock:
            return frozenset(
                name for name, entry in self._entries.items() if entry.author_time_only
            )

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
        try:
            refusal = dispatch_denial(name, entry)
            if refusal is not None:
                return refusal
            if entry is None:
                return tool_error(f"Unknown tool: {name}")
            # Preserve internal argument metadata (e.g. workflow fetch hosts).
            # Converting to dict/JSON here would discard trusted restrictions.
            return entry.handler(args, **kwargs)
        except Exception as exc:  # defense-in-depth: never raise into the loop
            return tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")


# Module-level singleton — importing a tool module registers its tools.
registry = ToolRegistry()
