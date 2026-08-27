"""MCP server config — parses ``~/.lohra/mcp.json`` (spec §8).

Uses the de-facto standard shape (Claude Desktop / Cursor):

    {"mcpServers": {"github": {"command": "npx", "args": [...], "env": {...}},
                    "remote": {"url": "https://..."}}}

A server with ``command`` is stdio; one with ``url`` is http. ``disabled: true``
skips it. Validation fails fast at the boundary; a missing file is simply "no
servers" (zero overhead when MCP is unused).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class MCPConfigError(ValueError):
    """Raised on a malformed mcp.json (never on a missing file)."""


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: str = "stdio"  # "stdio" | "http"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None


def _parse_server(name: str, spec: dict) -> MCPServerConfig:
    if not isinstance(spec, dict):
        raise MCPConfigError(f"server {name!r} must be an object")
    url = spec.get("url")
    command = spec.get("command")
    if url:
        return MCPServerConfig(name=name, transport="http", url=url)
    if command:
        return MCPServerConfig(
            name=name,
            transport="stdio",
            command=command,
            args=tuple(spec.get("args") or ()),
            env=dict(spec.get("env") or {}),
        )
    raise MCPConfigError(f"server {name!r} needs a 'command' (stdio) or 'url' (http)")


def load_mcp_config(path: str | Path) -> list[MCPServerConfig]:
    """Load enabled MCP server configs. Missing file -> []; malformed -> raise."""
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise MCPConfigError(f"could not parse {path}: {exc}") from exc

    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        raise MCPConfigError("'mcpServers' must be an object")

    configs: list[MCPServerConfig] = []
    for name, spec in servers.items():
        if isinstance(spec, dict) and spec.get("disabled"):
            continue
        configs.append(_parse_server(name, spec))
    return configs
