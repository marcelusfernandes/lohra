"""Tests for the MCP session bridge and manager (spec §8).

The ThreadedMCPSession bridge is driven by a fake async session (no SDK needed),
and the MCPManager by a fake sync session factory — so connection orchestration,
refresh (nuke-and-repave), and shutdown are pinned without a live server.
"""

import json

import pytest

from lohra.mcp.config import MCPServerConfig
from lohra.mcp.manager import MCPManager
from lohra.mcp.session import ThreadedMCPSession
from lohra.tools.registry import ToolRegistry


@pytest.fixture
def registry():
    return ToolRegistry()


# --- ThreadedMCPSession (sync<->async bridge) ---


class _FakeAsyncSession:
    def __init__(self):
        self.calls = []

    async def list_tools(self):
        return [{"name": "t", "inputSchema": {}}]

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"content": [{"type": "text", "text": "ok"}]}


class _FakeOpener:
    def __init__(self, session):
        self.session = session
        self.closed = False

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def test_bridge_lists_and_calls_across_threads():
    session = _FakeAsyncSession()
    opener = _FakeOpener(session)
    bridge = ThreadedMCPSession(opener)
    try:
        assert bridge.list_tools() == [{"name": "t", "inputSchema": {}}]
        bridge.call_tool("t", {"x": 1})
        assert session.calls == [("t", {"x": 1})]
    finally:
        bridge.close()
    assert opener.closed is True


def test_bridge_surfaces_connect_error():
    class _BoomOpener:
        async def __aenter__(self):
            raise RuntimeError("connect failed")

        async def __aexit__(self, *exc):
            return False

    with pytest.raises(RuntimeError, match="connect failed"):
        ThreadedMCPSession(_BoomOpener())


def test_bridge_times_out_on_a_hung_connect(monkeypatch):
    import asyncio as _asyncio

    monkeypatch.setattr("lohra.mcp.session.CONNECT_TIMEOUT_SECONDS", 0.05)

    class _HangOpener:
        async def __aenter__(self):
            await _asyncio.sleep(3600)  # never becomes ready

        async def __aexit__(self, *exc):
            return False

    with pytest.raises(TimeoutError):
        ThreadedMCPSession(_HangOpener())


def test_bridge_call_after_close_fails_fast():
    bridge = ThreadedMCPSession(_FakeOpener(_FakeAsyncSession()))
    bridge.close()
    # a dead session must raise immediately, not block for the call timeout
    with pytest.raises(RuntimeError, match="not connected"):
        bridge.call_tool("t", {})


def test_connect_session_routes_by_transport():
    from lohra.mcp.session import connect_session

    calls = []
    stdio = lambda c: calls.append(("stdio", c.name)) or "stdio-session"  # noqa: E731
    http = lambda c: calls.append(("http", c.name)) or "http-session"  # noqa: E731

    s = connect_session(MCPServerConfig(name="a", command="x"), stdio=stdio, http=http)
    h = connect_session(
        MCPServerConfig(name="b", transport="http", url="https://x.test/mcp"), stdio=stdio, http=http
    )
    assert s == "stdio-session" and h == "http-session"
    assert calls == [("stdio", "a"), ("http", "b")]


def test_connect_session_rejects_unknown_transport():
    from lohra.mcp.session import connect_session

    cfg = MCPServerConfig(name="r", transport="carrier-pigeon")
    with pytest.raises(RuntimeError, match="not supported"):
        connect_session(cfg, stdio=lambda c: None, http=lambda c: None)


# --- MCPManager ---


class _FakeSyncSession:
    def __init__(self, tools):
        self._tools = tools
        self.closed = False

    def list_tools(self):
        return self._tools

    def call_tool(self, name, args):
        return {"content": [{"type": "text", "text": f"{name}!"}]}

    def close(self):
        self.closed = True


def _cfg(name):
    return MCPServerConfig(name=name, command="x")


def test_manager_connects_and_registers_tools(registry):
    mgr = MCPManager(registry, lambda c: _FakeSyncSession([{"name": "t", "inputSchema": {}}]))
    mgr.connect_all([_cfg("s")])
    assert "mcp_s_t" in registry.names_in_toolset("mcp-s")
    out = json.loads(registry.dispatch("mcp_s_t", {}))
    assert out["content"] == "t!"


def test_manager_skips_a_failing_server(registry):
    def factory(c):
        if c.name == "bad":
            raise RuntimeError("nope")
        return _FakeSyncSession([{"name": "t", "inputSchema": {}}])

    mgr = MCPManager(registry, factory)
    mgr.connect_all([_cfg("bad"), _cfg("good")])
    assert registry.names_in_toolset("mcp-bad") == []
    assert "mcp_good_t" in registry.names_in_toolset("mcp-good")


def test_manager_refresh_replaces_tools(registry):
    session = _FakeSyncSession([{"name": "old", "inputSchema": {}}])
    mgr = MCPManager(registry, lambda c: session)
    mgr.connect_all([_cfg("s")])
    session._tools = [{"name": "new", "inputSchema": {}}]
    mgr.refresh("s")
    assert set(registry.names_in_toolset("mcp-s")) == {"mcp_s_new"}


def test_manager_refresh_unknown_server_is_noop(registry):
    mgr = MCPManager(registry, lambda c: _FakeSyncSession([]))
    mgr.refresh("ghost")  # must not raise


def test_manager_shutdown_deregisters_and_closes(registry):
    session = _FakeSyncSession([{"name": "t", "inputSchema": {}}])
    mgr = MCPManager(registry, lambda c: session)
    mgr.connect_all([_cfg("s")])
    mgr.shutdown()
    assert registry.names_in_toolset("mcp-s") == []
    assert session.closed is True


# --- register_configured_mcp_servers entry ---


def test_register_configured_no_file_returns_none(registry, tmp_path):
    from lohra.mcp import register_configured_mcp_servers

    mgr = register_configured_mcp_servers(registry, config_path=tmp_path / "absent.json")
    assert mgr is None


def test_register_configured_connects_from_file(registry, tmp_path):
    from lohra.mcp import register_configured_mcp_servers

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"s": {"command": "x"}}}))
    mgr = register_configured_mcp_servers(
        registry,
        config_path=path,
        session_factory=lambda c: _FakeSyncSession([{"name": "t", "inputSchema": {}}]),
    )
    assert mgr is not None
    assert "mcp_s_t" in registry.names_in_toolset("mcp-s")
    mgr.shutdown()


def test_register_configured_malformed_returns_none(registry, tmp_path):
    from lohra.mcp import register_configured_mcp_servers

    path = tmp_path / "mcp.json"
    path.write_text("{bad json")
    assert register_configured_mcp_servers(registry, config_path=path) is None


def test_register_configured_default_path_and_factory_skip_without_sdk(registry, tmp_path, monkeypatch):
    # Default wiring: reads ~/.lohra/mcp.json and uses the real stdio factory,
    # which fails gracefully (no mcp SDK) — the server is skipped, not fatal.
    from lohra.mcp import register_configured_mcp_servers

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    (tmp_path / "mcp.json").write_text(json.dumps({"mcpServers": {"s": {"command": "x"}}}))
    mgr = register_configured_mcp_servers(registry)
    assert mgr is not None
    assert registry.names_in_toolset("mcp-s") == []  # connection failed -> nothing registered


def test_connect_failure_after_session_closes_it(registry):
    class _BadList(_FakeSyncSession):
        def list_tools(self):
            raise RuntimeError("list failed")

    session = _BadList([])
    mgr = MCPManager(registry, lambda c: session)
    mgr.connect_all([_cfg("s")])  # logged + skipped
    assert registry.names_in_toolset("mcp-s") == []
    assert session.closed is True  # the half-open session was closed


def test_shutdown_tolerates_close_errors(registry):
    class _BadClose(_FakeSyncSession):
        def close(self):
            raise RuntimeError("close failed")

    mgr = MCPManager(registry, lambda c: _BadClose([{"name": "t", "inputSchema": {}}]))
    mgr.connect_all([_cfg("s")])
    mgr.shutdown()  # must not raise
    assert registry.names_in_toolset("mcp-s") == []
