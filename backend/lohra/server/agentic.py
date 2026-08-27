"""Agentic mode for the OpenAI server — an opt-in tool allow-list (Fase 6).

Exposing tools over HTTP is dangerous (fs/terminal = remote code execution), so
this is OFF by default and gated by an explicit allow-list of tool names. The
exposed tools reuse the subagent guards: the intercepted/stateful tools
(memory/skills/session_search/delegate_task) are never reachable, and dangerous
shell commands are auto-denied (there is no operator to approve over HTTP).
"""

from __future__ import annotations

from lohra.agent.agent import ToolDispatch
from lohra.agent.delegate import child_tool_definitions, subagent_dispatch
from lohra.agent.equip import register_all_tools
from lohra.tools import registry
from lohra.tools.registry import tool_error


def build_allowed_tools(allowed: list[str]) -> tuple[tuple[dict, ...], ToolDispatch]:
    """Return (tool_definitions, dispatch) restricted to ``allowed`` tool names.

    The definitions are filtered to the allow-list AND stripped of the
    intercepted/stateful tools. CRITICAL: the dispatch ENFORCES the same
    allow-list — a tool the model names but that was not exposed is refused, so
    a hallucinated (or client-injected) tool_call can't reach the registry.
    On top of that the subagent guards apply (auto-deny dangerous commands,
    refuse intercepted tools).
    """
    register_all_tools()
    allowed_set = set(allowed)
    # child_tool_definitions drops the intercepted/delegate tools; then keep only
    # the explicitly allowed names.
    safe_defs = child_tool_definitions(tuple(registry.get_definitions()))
    definitions = tuple(
        d for d in safe_defs if d.get("function", {}).get("name") in allowed_set
    )
    exposed = {d["function"]["name"] for d in definitions}
    guarded = subagent_dispatch(registry.dispatch)

    def dispatch(name: str, args: dict) -> str:
        # The allow-list gates EXECUTION, not just what the model sees.
        if name not in exposed:
            return tool_error(f"tool {name!r} is not in the server allow-list")
        return guarded(name, args)

    return definitions, dispatch
