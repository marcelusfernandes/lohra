"""#115 exact MCP identity on real registration/dispatch and composed wrappers."""

from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event

import pytest

from lohra.agent.delegate import subagent_dispatch
from lohra.agent.taint import TaintTracker, taint_wrap
from lohra.mcp.tools import deregister_server, register_server_tools
from lohra.tools.approval import bind_approval_dispatch
from lohra.tools.registry import ToolRegistry
from lohra.workflow.sandbox import WorkflowPolicy, load_policy, sandbox_dispatch, sandbox_tool_definitions


def _install(registry, server, tool, calls, *, epoch=1, gate=None):
    def call(original, args):
        if gate:
            gate[0].set()
            assert gate[1].wait(5), "handler was not released"
        calls.append((server, original, epoch))
        return {"content": [{"type": "text", "text": str(epoch)}]}

    return register_server_tools(registry, server, [{"name": tool,
        "description": f"epoch {epoch}", "inputSchema": {"type": "object"}}], call_tool=call)[0]


def _wrapped(registry, tmp_path, *, allowed=("github",), base=None):
    return sandbox_dispatch(base or registry.dispatch, working_root=tmp_path,
                            policy=WorkflowPolicy(mcp_allow=allowed), tainted=False,
                            tool_registry=registry)


def _definitions(registry, policy, definitions=None):
    return sandbox_tool_definitions(tuple(registry.get_definitions()) if definitions is None
                                    else definitions, policy=policy, tainted=False,
                                    tool_registry=registry)


def test_rebind_while_existing_wrapper_is_held_cannot_reach_foreign_handler(tmp_path):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)
    entered, release = Event(), Event()
    tracker = TaintTracker()

    def held(name, args):
        entered.set()
        assert release.wait(5), "wrapper was not released"
        return registry.dispatch(name, args)

    dispatch = _wrapped(registry, tmp_path, base=bind_approval_dispatch(
        subagent_dispatch(taint_wrap(held, tracker), tool_registry=registry)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(dispatch, name, {})
        try:
            assert entered.wait(5) and tracker.tainted
            deregister_server(registry, "github")
            assert _install(registry, "github_enterprise", "search", calls) == name
        finally:
            release.set()
        result = json.loads(future.result(timeout=5))
    assert calls == [] and "error" in result, (calls, result)


def test_same_server_refresh_during_wrapper_delay_uses_current_handler(tmp_path):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "search_with_underscores", calls)
    frozen = tuple(registry.get_definitions())
    entered, release = Event(), Event()

    def held(name, args):
        entered.set()
        assert release.wait(5)
        return registry.dispatch(name, args)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_wrapped(registry, tmp_path, base=held), name, {})
        try:
            assert entered.wait(5)
            assert _install(registry, "github", "search_with_underscores", calls, epoch=2) == name
        finally:
            release.set()
        assert json.loads(future.result(timeout=5))["ok"] is True
    assert calls == [("github", "search_with_underscores", 2)]
    assert frozen[0]["function"]["description"] == "epoch 1"
    assert registry.get_definitions()[0]["function"]["description"] == "epoch 2"


def test_rebind_after_handler_entry_does_not_rewrite_the_captured_call(tmp_path):
    registry, calls = ToolRegistry(), []
    entered, release = Event(), Event()
    name = _install(registry, "github", "enterprise_search", calls, gate=(entered, release))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_wrapped(registry, tmp_path), name, {})
        try:
            assert entered.wait(5)
            deregister_server(registry, "github")
            assert _install(registry, "github_enterprise", "search", calls) == name
        finally:
            release.set()
        assert json.loads(future.result(timeout=5))["ok"] is True
    assert calls == [("github", "enterprise_search", 1)]


@pytest.mark.parametrize("servers", [("gh-team", "gh_team"), ("GitHub", "github")])
def test_distinct_original_servers_cannot_silently_replace_a_shared_slug(servers):
    registry, calls = ToolRegistry(), []
    name = _install(registry, servers[0], "search", calls)
    with pytest.raises(ValueError):
        _install(registry, servers[1], "search", calls)
    assert registry.names_in_toolset("mcp-" + servers[0]) == [name]
    assert registry.names_in_toolset("mcp-" + servers[1]) == []
    registry.dispatch(name, {})
    assert calls == [(servers[0], "search", 1)]


def test_two_tool_slugs_from_one_listing_keep_first_original_name():
    registry, calls = ToolRegistry(), []
    names = register_server_tools(registry, "github", [{"name": "read-file"}, {"name": "read_file"}],
        call_tool=lambda name, args: calls.append(name) or {"content": []})
    assert names == ["mcp_github_read_file"]
    registry.dispatch(names[0], {})
    assert calls == ["read-file"]


def test_authored_schema_and_arguments_cannot_supply_missing_registry_provenance(tmp_path):
    registry, calls = ToolRegistry(), []
    name = "mcp_github_search"
    forged = {"server": "github", "toolset": "mcp-github", "mcp_allow": ["github"]}
    registry.register(name, "synthetic-builtins", {"parameters": {}, **forged},
                      lambda args: calls.append(args) or '{"ok":true}')
    visible = _definitions(registry, WorkflowPolicy(mcp_allow=("github",)))
    result = json.loads(_wrapped(registry, tmp_path)(name, forged))
    assert (visible, calls, "error" in result) == ((), [], True)


def test_frozen_public_definition_does_not_claim_authority_after_foreign_rebind():
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)
    frozen = tuple(registry.get_definitions())
    deregister_server(registry, "github")
    assert _install(registry, "github_enterprise", "search", calls) == name
    visible = _definitions(registry, WorkflowPolicy(mcp_allow=("github",)), frozen)
    assert visible == ()
    assert frozen[0]["function"]["name"] == name
    assert calls == []


def test_policy_file_and_environment_preserve_original_configured_identity(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"mcp_allow": ["gh-team", "GitHub", "gh-team", None]}))
    monkeypatch.setenv("LOHRA_LEAF_MCP_ALLOW", "gh_team, GitHub")
    assert load_policy(path).mcp_allow == ("gh-team", "GitHub", "gh_team")


def test_two_concurrent_owners_keep_their_own_policy_in_existing_wrappers(tmp_path):
    registry, calls = ToolRegistry(), []
    names = [_install(registry, server, "read", calls) for server in ("github", "notion")]
    entered = Barrier(2)

    def held(name, args):
        entered.wait(timeout=5)
        return registry.dispatch(name, args)

    dispatchers = [_wrapped(registry, tmp_path, allowed=(server,), base=held)
                   for server in ("github", "notion")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch, name, {}) for dispatch, name in zip(dispatchers, names)]
        assert all(json.loads(f.result(timeout=5))["ok"] for f in futures)
    assert sorted(calls) == [("github", "read", 1), ("notion", "read", 1)]


class _Stopped(BaseException):
    pass


@pytest.mark.parametrize("exception", [RuntimeError, _Stopped])
def test_nested_exception_restores_outer_policy_and_unbound_dispatch(tmp_path, exception):
    registry, calls = ToolRegistry(), []
    outer_name = _install(registry, "github", "read", calls)
    inner_name = _install(registry, "notion", "read", calls)

    def broken(name, args):
        raise exception("synthetic wrapper stop")

    inner = _wrapped(registry, tmp_path, allowed=("notion",), base=broken)

    def nested(name, args):
        with pytest.raises(exception):
            inner(inner_name, {})
        return registry.dispatch(name, args)

    # Both scopes permit the inner call so its exception, rather than an outer
    # authority refusal, exercises finally restoration.
    outer = _wrapped(registry, tmp_path, allowed=("github", "notion"), base=nested)
    assert json.loads(outer(outer_name, {}))["ok"]
    # Ordinary unbound registry calls retain their pre-workflow behavior.
    assert json.loads(registry.dispatch(inner_name, {}))["ok"]
    assert calls == [("github", "read", 1), ("notion", "read", 1)]


def test_authorized_mcp_keeps_taint_subagent_and_approval_wrapper_chain(tmp_path):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "read", calls)
    tracker = TaintTracker()
    base = bind_approval_dispatch(subagent_dispatch(
        taint_wrap(registry.dispatch, tracker), tool_registry=registry))
    dispatch = sandbox_dispatch(base, working_root=tmp_path, tainted=False,
                               policy=WorkflowPolicy(mcp_allow=("github",), allow_terminal=True),
                               tool_registry=registry)
    assert json.loads(dispatch(name, {}))["ok"] and tracker.tainted
    assert "error" in json.loads(dispatch("delegate_task", {}))
    assert "error" in json.loads(dispatch("terminal", {"command": "rm -rf /synthetic"}))
    assert calls == [("github", "read", 1)]


@pytest.mark.parametrize(("allow_mcp", "tainted"), [(False, False), (True, False), (True, True)])
def test_mcp_entry_named_terminal_has_the_same_definition_and_dispatch_gate(tmp_path, allow_mcp, tainted):
    registry, calls = ToolRegistry(), []
    registry.register("terminal", "mcp-github", {},
                      lambda args: calls.append("MCP") or '{"ok":true}')
    policy = WorkflowPolicy(allow_terminal=True, mcp_allow=("github",) if allow_mcp else ())
    visible = sandbox_tool_definitions(tuple(registry.get_definitions()), policy=policy,
                                      tainted=tainted, tool_registry=registry)
    dispatch = sandbox_dispatch(registry.dispatch, working_root=tmp_path, policy=policy,
                                tainted=tainted, tool_registry=registry)
    allowed = allow_mcp and not tainted
    assert bool(visible) is allowed
    assert ("error" not in json.loads(dispatch("terminal", {"command": "echo fixture"}))) is allowed
    assert calls == (["MCP"] if allowed else [])


def test_trusted_explicit_override_keeps_api_but_never_the_previous_server_grant(tmp_path):
    registry, calls = ToolRegistry(), []
    name = _install(registry, "github", "enterprise_search", calls)
    dispatch = _wrapped(registry, tmp_path)
    registry.register(name, "mcp-github_enterprise", {},
                      lambda args: calls.append("foreign") or '{"ok":true}', override=True)
    assert "error" in json.loads(dispatch(name, {})) and calls == []
    assert json.loads(registry.dispatch(name, {}))["ok"]
    assert calls == ["foreign"]
