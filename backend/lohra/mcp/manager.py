"""MCPManager — connect configured servers and keep their tools registered.

Orchestration only (SDK-free): a ``session_factory`` turns a config into a live
``MCPSession``, the manager lists its tools and registers them, and tracks each
session for ``refresh`` (nuke-and-repave on tools/list_changed) and ``shutdown``.
A server that fails to connect is logged and skipped, never fatal (mirrors
built-in tool discovery).
"""

from __future__ import annotations

import logging
from typing import Callable

from lohra.mcp.config import MCPServerConfig
from lohra.mcp.session import MCPSession
from lohra.mcp.tools import deregister_server, register_server_tools
from lohra.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

SessionFactory = Callable[[MCPServerConfig], MCPSession]


class MCPManager:
    """Owns the live MCP sessions and their registered tools."""

    def __init__(self, registry: ToolRegistry, session_factory: SessionFactory) -> None:
        self._registry = registry
        self._session_factory = session_factory
        self._sessions: dict[str, MCPSession] = {}

    def connect_all(self, configs: list[MCPServerConfig]) -> None:
        """Connect and register each server; skip (log) the ones that fail."""
        for config in configs:
            try:
                self._connect(config)
            except Exception as exc:  # one bad server must not sink the rest
                logger.warning("MCP server %r failed to connect: %s", config.name, exc)

    def _connect(self, config: MCPServerConfig) -> None:
        session = self._session_factory(config)
        try:
            tools = session.list_tools()
            register_server_tools(self._registry, config.name, tools, call_tool=session.call_tool)
        except Exception:
            session.close()  # don't leak a session we couldn't register
            raise
        self._sessions[config.name] = session

    def refresh(self, server: str) -> None:
        """Re-list a server's tools and re-register them (nuke-and-repave)."""
        session = self._sessions.get(server)
        if session is None:
            return
        deregister_server(self._registry, server)
        register_server_tools(self._registry, server, session.list_tools(), call_tool=session.call_tool)

    def shutdown(self) -> None:
        """Deregister every server's tools and close all sessions."""
        for name, session in self._sessions.items():
            deregister_server(self._registry, name)
            try:
                session.close()
            except Exception as exc:  # closing one must not block the others
                logger.warning("error closing MCP session %r: %s", name, exc)
        self._sessions.clear()
