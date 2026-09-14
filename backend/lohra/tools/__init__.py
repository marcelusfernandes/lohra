"""Tool system: registry, dispatch, approval gate.

See docs/specs/02-tool-system.md.
"""

from lohra.tools.approval import ApprovalManager, approval, bind_approval_dispatch, require_approval
from lohra.tools.registry import ToolRegistry, registry


def load_builtin_tools() -> None:
    """Import the built-in tool modules so they self-register (idempotent)."""
    from lohra.tools import fs, terminal  # noqa: F401 — import side effect registers tools
    from lohra.web import tool  # noqa: F401 — registers web_fetch / web_search


__all__ = [
    "ToolRegistry", "registry", "ApprovalManager", "approval", "bind_approval_dispatch",
    "require_approval", "load_builtin_tools",
]
