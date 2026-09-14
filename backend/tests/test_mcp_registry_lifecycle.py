"""Identity must survive selection, partial listing failure and teardown."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, get_ident

import pytest

from lohra.mcp.config import MCPServerConfig
from lohra.mcp.manager import MCPManager
from lohra.mcp.tools import deregister_server
from lohra.tools.registry import ToolRegistry
from tests.test_mcp_identity import _install


def test_deregister_cannot_remove_a_foreign_replacement_after_unlock(monkeypatch):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)
    unlocked, release = Event(), Event()
    original_lock = registry._lock
    deleter = []

    class HeldAfterUnlock:
        def __enter__(self):
            original_lock.acquire()

        def __exit__(self, *exc):
            original_lock.release()
            if deleter == [get_ident()] and not unlocked.is_set():
                unlocked.set()
                assert release.wait(5)

    monkeypatch.setattr(registry, "_lock", HeldAfterUnlock())

    def remove():
        deleter.append(get_ident())
        deregister_server(registry, "github")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(remove)
        try:
            assert unlocked.wait(5)
            registry.deregister(name)
            assert _install(registry, "github_enterprise", "search", calls) == name
        finally:
            release.set()
        future.result(timeout=5)
    assert registry.names_in_toolset("mcp-github_enterprise") == [name]
    assert json.loads(registry.dispatch(name, {}))["ok"]
    assert calls == [("github_enterprise", "search", 1)]


@pytest.mark.parametrize("refresh", [False, True], ids=["connect", "refresh"])
def test_partial_collision_cleanup_keeps_other_server_and_no_partial_handlers(refresh):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)

    class Session:
        tools = [{"name": "old"}]
        closed = False

        def list_tools(self):
            return self.tools

        def call_tool(self, name, args):
            assert not self.closed
            return {"content": []}

        def close(self):
            self.closed = True

    session = Session()
    manager = MCPManager(registry, lambda config: session)
    config = MCPServerConfig(name="github_enterprise", command="unused-fake")
    try:
        if refresh:
            manager.connect_all([config])
        session.tools = [{"name": "valid_before_collision"}, {"name": "search"}]
        if refresh:
            with pytest.raises(ValueError):
                manager.refresh(config.name)
        else:
            manager.connect_all([config])
        assert session.closed is (not refresh)
        assert registry.names_in_toolset("mcp-github_enterprise") == []
        assert registry.names_in_toolset("mcp-github") == [name]
        registry.dispatch(name, {})
        assert calls == [("github", "enterprise_search", 1)]
    finally:
        manager.shutdown()


def test_final_guard_authorizes_the_entry_whose_handler_is_invoked():
    # The new seam does not exist on the baseline; its first import failure is
    # scaffolding evidence, not another observed production defect.
    from lohra.tools.registry import bind_dispatch_guard

    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)
    selected, release = Event(), Event()
    seen = []

    def guard(name, entry):
        seen.append(entry)
        selected.set()
        assert release.wait(5)
        return None if entry.toolset == "mcp-github" else '{"error":"foreign"}'

    dispatch = bind_dispatch_guard(registry.dispatch, guard)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch, name, {})
        try:
            assert selected.wait(5)
            deregister_server(registry, "github")
            assert _install(registry, "github_enterprise", "search", calls) == name
        finally:
            release.set()
        assert json.loads(future.result(timeout=5))["ok"]
    assert seen[0].toolset == "mcp-github"
    assert calls == [("github", "enterprise_search", 1)]
    assert "error" in json.loads(dispatch(name, {}))
    assert len(calls) == 1


@pytest.mark.parametrize("foreign", ["github_enterprise", "notion"])
def test_nested_handler_cannot_widen_the_outer_mcp_grant(tmp_path, foreign):
    from tests.test_mcp_identity import _wrapped

    registry, calls = ToolRegistry(), []
    target = _install(registry, foreign, "search", calls)
    inner = _wrapped(registry, tmp_path, allowed=("github", foreign))
    registry.register("mcp_github_bridge", "mcp-github", {},
                      lambda args: inner(target, {}))
    result = json.loads(_wrapped(registry, tmp_path)("mcp_github_bridge", {}))
    assert "error" in result and calls == [], (result, calls)


def test_connect_listing_failure_preserves_preexisting_same_server_entries():
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "read", calls)

    class Session:
        closed = False

        def list_tools(self):
            raise RuntimeError("synthetic listing failure before any registration")

        def close(self):
            self.closed = True

    session = Session()
    manager = MCPManager(registry, lambda config: session)
    manager.connect_all([MCPServerConfig(name="github", command="unused-fake")])
    assert session.closed
    assert registry.names_in_toolset("mcp-github") == [name]
    assert json.loads(registry.dispatch(name, {}))["ok"]
    assert calls == [("github", "read", 1)]


def test_rejected_batch_preserves_existing_same_owner_entries_and_generation():
    from lohra.mcp.tools import register_server_tools

    registry, calls = ToolRegistry(), []
    foreign = _install(registry, "github", "enterprise_search", calls)
    retained = _install(registry, "github_enterprise", "retained", calls)
    before = registry.generation
    with pytest.raises(ValueError):
        register_server_tools(registry, "github_enterprise",
            [{"name": "retained"}, {"name": "new"}, {"name": "search"}],
            call_tool=lambda name, args: pytest.fail("failed listing handler was published"))
    assert registry.generation == before
    assert registry.names_in_toolset("mcp-github") == [foreign]
    assert registry.names_in_toolset("mcp-github_enterprise") == [retained]
    assert json.loads(registry.dispatch(retained, {}))["ok"]
    assert calls == [("github_enterprise", "retained", 1)]


def test_published_batch_invalidates_availability_without_replacing_builtins():
    from lohra.mcp.tools import register_server_tools

    registry, checks = ToolRegistry(), []
    registry.register("mcp_srv_builtin", "builtin", {}, lambda args: '{"ok":true}',
                      check_fn=lambda: checks.append(1) or True)
    registry.get_definitions()
    registry.get_definitions()
    assert checks == [1]
    before = registry.generation
    names = register_server_tools(registry, "srv", [{"name": "a"}, {"name": "builtin"}],
                                  call_tool=lambda name, args: {"content": []})
    assert names == ["mcp_srv_a"] and registry.generation > before
    assert registry.entry("mcp_srv_builtin").toolset == "builtin"
    registry.get_definitions()
    assert checks == [1, 1]


def test_direct_batch_rejects_foreign_owners_sharing_a_name_before_publication():
    from lohra.tools.registry import MCPToolCollision, ToolEntry

    registry = ToolRegistry()
    name = "mcp_github_enterprise_search"
    entries = tuple(ToolEntry(name, owner, {}, lambda args: '{"ok":true}')
                    for owner in ("mcp-github", "mcp-github_enterprise"))
    before = registry.generation
    with pytest.raises(MCPToolCollision):
        registry.register_mcp_batch(entries)
    assert registry.generation == before
    assert registry.entry(name) is None
