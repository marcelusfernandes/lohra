"""#130 author-time entry authority; no provider or external tools.

Prepared REDs on #115 candidate 5ab87748; source verified identical at claim.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from types import SimpleNamespace

import pytest

from lohra.agent import delegate
from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.server import agentic
from lohra.tools.registry import ToolRegistry, bind_dispatch_guard, tool_result
from lohra.workflow import sandbox

KINDS = ("child", "server", "leaf")


class NoCallsClient(ModelClient):
    def create(self, **kwargs):
        raise AssertionError("provider request must not happen")


@pytest.fixture
def catalog(monkeypatch, tmp_path):
    registry = ToolRegistry()
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(delegate, "registry", registry)
    monkeypatch.setattr(agentic, "registry", registry)
    monkeypatch.setattr(sandbox, "registry", registry)
    # Isolated synthetic catalog; the builder/filter/dispatch themselves are real.
    monkeypatch.setattr(agentic, "register_all_tools", lambda: None)
    return registry


def _install(registry, name, calls, *, author=False, toolset="synthetic", label=None):
    # Authored schema metadata deliberately contradicts the trusted entry flag.
    schema = {"author_time_only": not author, "parameters": {"type": "object"}}

    def handler(args):
        calls.append(label or name)
        return tool_result(label or name)

    registry.register(name, toolset, schema, handler, author_time_only=author)


def _consumer(kind, registry, root, *, allowed=None):
    definitions = tuple(registry.get_definitions())
    common = dict(model="synthetic", provider=get_provider_profile("anthropic"),
                  client=NoCallsClient(), tool_definitions=definitions)
    if kind == "server":
        definitions, dispatch = agentic.build_allowed_tools(
            list(registry._entries) if allowed is None else allowed)
        return SimpleNamespace(tool_definitions=definitions, tool_dispatch=dispatch)
    if kind == "child":
        return delegate.make_child_factory(**common)()
    # Real leaf factory over a supported custom Agent factory. It still reaches
    # the real registry; no arbitrary callback or fake prospective guard is used.
    return sandbox.make_sandboxed_leaf_factory(
        base_factory=lambda: Agent(**common, tool_dispatch=registry.dispatch),
        working_root=root, policy=sandbox.WorkflowPolicy(), tainted=True,
        tool_registry=registry,
    )()


def _names(consumer):
    return {d["function"]["name"] for d in consumer.tool_definitions}


@pytest.mark.parametrize("kind", KINDS)
def test_marked_entry_is_hidden_and_refused_but_author_stays_allowed(catalog, tmp_path, kind):
    calls = []
    _install(catalog, "future_author", calls, author=True)
    consumer = _consumer(kind, catalog, tmp_path)
    author_result = json.loads(catalog.dispatch("future_author", {}))
    result = json.loads(consumer.tool_dispatch("future_author", {"author_time_only": False}))
    assert author_result["ok"]
    assert ("future_author" in _names(consumer), "error" in result, calls) == (
        False, True, ["future_author"]), result
    assert result["error"]  # A concrete refusal, without pinning new prose.


@pytest.mark.parametrize("kind", KINDS)
def test_unmarked_runtime_and_forged_json_metadata_do_not_change_authority(catalog, tmp_path, kind):
    calls = []
    _install(catalog, "runtime", calls)
    consumer = _consumer(kind, catalog, tmp_path)
    assert "runtime" in _names(consumer)
    assert json.loads(consumer.tool_dispatch("runtime", {"author_time_only": True}))["ok"]
    assert calls == ["runtime"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("remove_first", [False, True], ids=["reregister", "deregister-rebind"])
def test_rebind_while_dispatch_waits_preserves_snapshot_but_refuses_new_author(
    catalog, tmp_path, monkeypatch, kind, remove_first,
):
    calls = []
    _install(catalog, "runtime", calls)
    original = catalog.dispatch
    entered, release = Event(), Event()

    def held(name, args):
        entered.set()
        assert release.wait(5)
        return original(name, args)

    monkeypatch.setattr(catalog, "dispatch", held)
    consumer = _consumer(kind, catalog, tmp_path)
    definitions = consumer.tool_definitions
    before = deepcopy(definitions)
    prompt = consumer.system_prompt() if isinstance(consumer, Agent) else None
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consumer.tool_dispatch, "runtime", {})
        try:
            assert entered.wait(5)
            if remove_first:
                catalog.deregister("runtime")
            _install(catalog, "runtime", calls, author=True, label="new-author")
        finally:
            release.set()
        result = json.loads(future.result(timeout=5))
    assert consumer.tool_definitions is definitions and definitions == before
    if prompt is not None:
        assert consumer.system_prompt() is prompt
    assert "error" in result and calls == [], (result, calls)


def test_actual_entry_capture_keeps_its_handler_even_if_metadata_is_replaced(
    catalog, tmp_path, monkeypatch,
):
    calls = []
    _install(catalog, "runtime", calls, label="captured-runtime")
    original = catalog.dispatch
    selected, release = Event(), Event()
    entries = []

    def observe(name, entry):
        entries.append(entry)
        selected.set()
        assert release.wait(5)
        return None

    # This existing #115 guard only observes. It does not simulate #130 denial.
    monkeypatch.setattr(catalog, "dispatch", bind_dispatch_guard(original, observe))
    consumer = _consumer("child", catalog, tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(consumer.tool_dispatch, "runtime", {})
        try:
            assert selected.wait(5)
            _install(catalog, "runtime", calls, author=True, label="replacement-author")
        finally:
            release.set()
        assert json.loads(future.result(timeout=5))["data"] == "captured-runtime"
    assert len(entries) == 1 and not entries[0].author_time_only
    assert catalog.entry("runtime").author_time_only
    assert calls == ["captured-runtime"]
    assert json.loads(original("runtime", {}))["data"] == "replacement-author"


@pytest.mark.parametrize("child_outer", [False, True])
def test_mcp_and_author_restrictions_compose_in_both_wrapper_orders(catalog, tmp_path, child_outer):
    calls = []
    _install(catalog, "mcp_github_author", calls, author=True, toolset="mcp-github")
    _install(catalog, "mcp_foreign_runtime", calls, toolset="mcp-foreign")
    _install(catalog, "mcp_github_runtime", calls, toolset="mcp-github")

    def leaf(base):
        return sandbox.sandbox_dispatch(base, working_root=tmp_path,
            policy=sandbox.WorkflowPolicy(mcp_allow=("github",)), tainted=False,
            tool_registry=catalog)

    dispatch = (delegate.subagent_dispatch(leaf(catalog.dispatch)) if child_outer
                else leaf(delegate.subagent_dispatch(catalog.dispatch)))
    results = [json.loads(dispatch(name, {})) for name in (
        "mcp_github_author", "mcp_foreign_runtime", "mcp_github_runtime")]
    assert ["error" in result for result in results] == [True, True, False], results
    assert calls == ["mcp_github_runtime"]


@pytest.mark.parametrize("kind", KINDS)
def test_nested_registry_call_in_runtime_handler_keeps_the_consumer_guard(catalog, tmp_path, kind):
    calls = []
    _install(catalog, "author_target", calls, author=True)
    catalog.register("bridge", "synthetic", {}, lambda args: catalog.dispatch("author_target", {}))
    consumer = _consumer(kind, catalog, tmp_path)
    result = json.loads(consumer.tool_dispatch("bridge", {}))
    assert "error" in result and calls == [], (result, calls)


def test_marked_preflight_refuses_before_an_opaque_interceptor(catalog):
    calls = []
    _install(catalog, "author_target", calls, author=True)
    base_calls = []

    def opaque(name, args):
        base_calls.append(name)
        return tool_result("opaque")

    result = json.loads(delegate.subagent_dispatch(opaque)("author_target", {}))
    assert "error" in result and base_calls == [] and calls == [], result
    # Unmarked callbacks remain a trusted integration boundary, not an OS sandbox.
    _install(catalog, "runtime", calls)
    assert json.loads(delegate.subagent_dispatch(opaque)("runtime", {}))["ok"]


@pytest.mark.parametrize("kind", ["child", "server"])
def test_unmarked_legacy_names_and_dangerous_terminal_still_deny(catalog, tmp_path, kind):
    calls = []
    for name in ("memory", "delegate_task", "terminal"):
        _install(catalog, name, calls)
    consumer = _consumer(kind, catalog, tmp_path)
    assert _names(consumer) == {"terminal"}
    for name in ("memory", "delegate_task"):
        assert "error" in json.loads(consumer.tool_dispatch(name, {}))
    assert "error" in json.loads(consumer.tool_dispatch("terminal", {"command": "rm -rf /"}))
    assert calls == []  # No shell; the terminal handler is synthetic too.
    assert json.loads(consumer.tool_dispatch("terminal", {"command": "synthetic-safe"}))["ok"]


def test_server_empty_allowlist_stays_tool_less_after_late_registration(catalog, tmp_path):
    calls = []
    consumer = _consumer("server", catalog, tmp_path, allowed=[])
    _install(catalog, "late_runtime", calls)
    assert consumer.tool_definitions == ()
    assert "error" in json.loads(consumer.tool_dispatch("late_runtime", {}))
    assert calls == []


class _Stopped(BaseException):
    pass


@pytest.mark.parametrize("exception", [RuntimeError, _Stopped])
def test_author_guard_restores_after_exception_in_an_existing_wrapper(catalog, exception):
    calls, nested = [], []
    _install(catalog, "author_target", calls, author=True)
    _install(catalog, "runtime", calls)

    def wrapper(name, args):
        nested.append(json.loads(catalog.dispatch("author_target", {})))
        raise exception("synthetic stop")

    with pytest.raises(exception, match="synthetic stop"):
        delegate.subagent_dispatch(wrapper)("runtime", {})
    # Root authority is restored even when a wrapper raises outside the registry.
    assert json.loads(catalog.dispatch("author_target", {}))["ok"]
    assert "error" in nested[0] and calls == ["author_target"], (nested, calls)


def test_active_child_guard_does_not_revoke_a_concurrent_author(catalog, tmp_path):
    calls = []
    _install(catalog, "author_target", calls, author=True)
    entered, release = Event(), Event()

    def runtime(args):
        entered.set()
        assert release.wait(5)
        return tool_result("child")

    catalog.register("runtime", "synthetic", {}, runtime)
    child = _consumer("child", catalog, tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(child.tool_dispatch, "runtime", {})
        try:
            assert entered.wait(5)
            assert json.loads(catalog.dispatch("author_target", {}))["ok"]
        finally:
            release.set()
        assert json.loads(future.result(timeout=5))["data"] == "child"
    assert calls == ["author_target"]
