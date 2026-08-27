"""Per-session taint tracking for workflow leaf capability (spec §8.2, control 3).

If the AUTHORING turn ingested untrusted content (web_fetch / web_search / MCP
tool results), a prompt-injection in that content could author a *valid* workflow
spec whose leaf reads a secret and exfiltrates it. So a workflow spawned from a
tainted context runs with reduced leaf capability (no fs read, no web egress).

Taint is sticky per session — once the context is tainted it stays tainted
(conservative: never under-protects; the untrusted content remains in history and
keeps informing later authoring). The fs/egress sandbox is default-deny anyway;
taint is the defense-in-depth layer on top.
"""

from __future__ import annotations

from typing import Callable

ToolDispatch = Callable[[str, dict], str]


def is_tainting_tool(name: str) -> bool:
    """Tools that bring external/untrusted content into the conversation.

    Scoped to web + MCP per spec §8.2. KNOWN under-scope (decision on record):
    ``vision_analyze`` with a remote URL also pulls external content that could
    carry an injected description — not yet treated as tainting."""
    return name in ("web_fetch", "web_search") or name.startswith("mcp_")


class TaintTracker:
    """A per-session sticky flag: set when a tainting tool runs."""

    def __init__(self) -> None:
        self._tainted = False

    @property
    def tainted(self) -> bool:
        return self._tainted

    def mark(self) -> None:
        self._tainted = True


def taint_wrap(base: ToolDispatch, tracker: TaintTracker) -> ToolDispatch:
    """Wrap a dispatch so it marks the tracker whenever a tainting tool runs."""

    def dispatch(name: str, args: dict) -> str:
        if is_tainting_tool(name):
            tracker.mark()
        return base(name, args)

    return dispatch
