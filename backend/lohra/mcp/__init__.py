"""MCP client — connect to Model Context Protocol servers and expose their
tools as ordinary registry tools (spec §8).

The registration core (``tools``) is pure and SDK-free; the live connection
(``session``) lazily imports the optional ``mcp`` SDK.
"""

from __future__ import annotations

import logging
from pathlib import Path

from lohra.mcp.config import MCPConfigError, load_mcp_config
from lohra.mcp.manager import MCPManager, SessionFactory
from lohra.mcp.tools import (
    convert_mcp_schema,
    deregister_server,
    mcp_tool_name,
    register_server_tools,
    wrap_call_result,
)
from lohra.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "MCPManager",
    "convert_mcp_schema",
    "deregister_server",
    "mcp_tool_name",
    "register_configured_mcp_servers",
    "register_server_tools",
    "wrap_call_result",
]


def register_configured_mcp_servers(
    registry: ToolRegistry,
    *,
    config_path: str | Path | None = None,
    session_factory: SessionFactory | None = None,
) -> MCPManager | None:
    """Load ``mcp.json`` and connect its servers, registering their tools.

    Returns the live MCPManager (call ``shutdown()`` to clean up), or None when
    there is nothing to do (no config file / no servers) or the config is
    malformed. Best-effort — never raises into the caller.
    """
    if config_path is None:
        from lohra.memory.paths import mcp_config_path

        config_path = mcp_config_path()
    try:
        configs = load_mcp_config(config_path)
    except MCPConfigError as exc:
        logger.warning("ignoring MCP config: %s", exc)
        return None
    if not configs:
        return None
    if session_factory is None:
        from lohra.mcp.session import connect_session

        session_factory = connect_session
    manager = MCPManager(registry, session_factory)
    manager.connect_all(configs)
    return manager
