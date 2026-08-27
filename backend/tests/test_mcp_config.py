"""Tests for MCP server config loading (~/.lohra/mcp.json)."""

import json

import pytest

from lohra.mcp.config import MCPConfigError, MCPServerConfig, load_mcp_config


def _write(tmp_path, data):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(data))
    return path


def test_missing_file_returns_empty(tmp_path):
    assert load_mcp_config(tmp_path / "nope.json") == []


def test_loads_stdio_server(tmp_path):
    path = _write(
        tmp_path,
        {"mcpServers": {"github": {"command": "npx", "args": ["-y", "srv"], "env": {"T": "1"}}}},
    )
    configs = load_mcp_config(path)
    assert len(configs) == 1
    cfg = configs[0]
    assert cfg == MCPServerConfig(
        name="github", transport="stdio", command="npx", args=("-y", "srv"), env={"T": "1"}
    )


def test_url_server_is_http_transport(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"remote": {"url": "https://x.test/mcp"}}})
    cfg = load_mcp_config(path)[0]
    assert cfg.transport == "http"
    assert cfg.url == "https://x.test/mcp"


def test_disabled_server_is_skipped(tmp_path):
    path = _write(
        tmp_path,
        {
            "mcpServers": {
                "on": {"command": "a"},
                "off": {"command": "b", "disabled": True},
            }
        },
    )
    names = [c.name for c in load_mcp_config(path)]
    assert names == ["on"]


def test_empty_config_returns_empty(tmp_path):
    assert load_mcp_config(_write(tmp_path, {})) == []


def test_malformed_json_raises(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{not json")
    with pytest.raises(MCPConfigError):
        load_mcp_config(path)


def test_server_without_command_or_url_raises(tmp_path):
    path = _write(tmp_path, {"mcpServers": {"bad": {"args": []}}})
    with pytest.raises(MCPConfigError, match="bad"):
        load_mcp_config(path)


def test_mcp_servers_must_be_object(tmp_path):
    with pytest.raises(MCPConfigError):
        load_mcp_config(_write(tmp_path, {"mcpServers": []}))


def test_non_object_server_spec_raises(tmp_path):
    with pytest.raises(MCPConfigError, match="x"):
        load_mcp_config(_write(tmp_path, {"mcpServers": {"x": "not-an-object"}}))
