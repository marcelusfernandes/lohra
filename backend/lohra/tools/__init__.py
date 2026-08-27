"""Tool system: registry, dispatch, approval gate.

See docs/specs/02-tool-system.md.
"""

from lohra.tools.approval import approval
from lohra.tools.registry import ToolRegistry, registry

# Toolsets safe to enable by default on the local CLI (Phase 2).
DEFAULT_TOOLSETS = frozenset({"file", "terminal", "web"})


def load_builtin_tools() -> None:
    """Import the built-in tool modules so they self-register (idempotent)."""
    from lohra.tools import fs, terminal  # noqa: F401 — import side effect registers tools
    from lohra.web import tool  # noqa: F401 — registers web_fetch / web_search


__all__ = ["ToolRegistry", "registry", "approval", "load_builtin_tools", "DEFAULT_TOOLSETS"]
