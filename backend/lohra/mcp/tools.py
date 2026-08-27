"""Register MCP server tools into the ToolRegistry (spec §8).

Pure and SDK-free: takes a server name, the tool list a session reported, and a
sync ``call_tool(original_name, args)`` callable, and registers each tool as
``mcp_{server}_{tool}`` under the ``mcp-{server}`` toolset. The handler routes
back to ``call_tool`` with the ORIGINAL (unprefixed) name and wraps the result
as a JSON envelope. Because these are ordinary registry entries, they flow
through ``get_definitions``/``dispatch`` with no agent changes.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from lohra.tools.registry import ToolRegistry, tool_error, tool_result

logger = logging.getLogger(__name__)

_EMPTY_SCHEMA = {"type": "object", "properties": {}}

# A sync bridge to one server: (original_tool_name, args) -> raw CallToolResult.
CallTool = Callable[[str, dict], Any]

_INVALID = re.compile(r"[^a-z0-9]+")


def mcp_tool_name(server: str, tool: str) -> str:
    """Deterministic registry name: ``mcp_{server}_{tool}`` (sanitized, lowercase)."""
    server_slug = _INVALID.sub("_", server.lower()).strip("_")
    tool_slug = _INVALID.sub("_", tool.lower()).strip("_")
    return f"mcp_{server_slug}_{tool_slug}"


def _field(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def convert_mcp_schema(tool: Any) -> dict:
    """MCP tool ({name, description, inputSchema}) -> registry schema.

    Reads dicts or SDK ``Tool`` objects, so the live session can hand its tools
    through unchanged.
    """
    # Never trust the server's schema: a non-dict inputSchema would poison the
    # whole tools array sent to the provider — fall back to the empty object.
    parameters = _field(tool, "inputSchema")
    if not isinstance(parameters, dict):
        parameters = dict(_EMPTY_SCHEMA)
    return {"description": _field(tool, "description", "") or "", "parameters": parameters}


def wrap_call_result(result: Any) -> str:
    """MCP CallToolResult (dict or SDK object) -> JSON envelope string."""
    blocks = _field(result, "content") or ()
    parts: list[str] = []
    for block in blocks:
        if _field(block, "type") == "text":
            parts.append(_field(block, "text") or "")
        else:
            parts.append(f"[{_field(block, 'type', 'content')} block]")
    text = "".join(parts)
    if _field(result, "isError"):
        return tool_error(text or "MCP tool reported an error")
    return tool_result(content=text)


def _make_handler(call_tool: CallTool, original_name: str) -> Callable[..., str]:
    def handler(args: dict, **_kwargs: Any) -> str:
        return wrap_call_result(call_tool(original_name, args))

    return handler


def register_server_tools(
    registry: ToolRegistry,
    server: str,
    tools: list[dict],
    *,
    call_tool: CallTool,
) -> list[str]:
    """Register every tool of one MCP server. Returns the registry names added.

    A name that collides with a non-MCP (built-in) tool is skipped, not fatal —
    the built-in keeps the name.
    """
    toolset = f"mcp-{server}"
    registered: list[str] = []
    for tool in tools:
        original = _field(tool, "name")
        if not original:
            continue
        name = mcp_tool_name(server, original)
        if name in registered:
            # Two distinct tools sanitized to the same registry name — keep the
            # first, warn rather than silently overwriting it.
            logger.warning("MCP tool %r/%r collides with an earlier tool as %r — skipped",
                           server, original, name)
            continue
        try:
            registry.register(
                name,
                toolset,
                convert_mcp_schema(tool),
                _make_handler(call_tool, original),
                emoji="🔌",
            )
        except ValueError:
            # Collides with a tool in a different (non-mcp) toolset — skip it.
            logger.warning("MCP tool %r shadows an existing %r — skipped", original, name)
            continue
        registered.append(name)
    return registered


def deregister_server(registry: ToolRegistry, server: str) -> None:
    """Nuke-and-repave: drop every tool a server registered (for refresh/shutdown)."""
    for name in registry.names_in_toolset(f"mcp-{server}"):
        registry.deregister(name)
