"""#56: operator policy reaches the real fetcher's redirects, before DNS."""

import json
from copy import deepcopy

import httpx
import pytest

import lohra.web.fetch as fetch_module
import lohra.web.safety as safety
from lohra.agent.agent import Agent
from lohra.agent.delegate import subagent_dispatch
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import registry
from lohra.tools.sandbox_denials import denial_of
from lohra.web import tool as web_tool
from lohra.web.egress import RestrictedFetchArgs
from lohra.workflow.cache import content_hash
from lohra.workflow.sandbox import WorkflowPolicy, sandbox_dispatch
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient as TextClient
from tests.test_workflow_sandbox_denials import ScriptedClient


@pytest.fixture
def network(monkeypatch):
    resolved, connected = [], []

    def resolver(host, port):
        resolved.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    def handler(request):
        connected.append(request.url.host)
        if request.url.host == "api.test":
            return httpx.Response(302, headers={
                "location": "https://outside-CANARY.test/private-CANARY?secret=CANARY",
            })
        return httpx.Response(200, text="<p>arrived</p>")

    monkeypatch.setattr(safety.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(fetch_module, "PublicTransport", lambda **_: httpx.MockTransport(handler))
    monkeypatch.setattr(fetch_module, "require_direct", lambda _: None)
    return resolved, connected


@pytest.mark.parametrize("hosts,tainted,expected_hosts,reason", [
    ([], False, [], "egress_not_allowed"),
    (["api.test"], False, ["api.test"], "egress_redirect_not_allowed"),
    (["API.TEST", "outside-canary.test"], False, ["api.test", "outside-canary.test"], None),
    (["api.test", "outside-canary.test"], True, [], "tainted_egress"),
])
@pytest.mark.parametrize("nested,audit_enabled", [(False, True), (True, True), (False, False)])
def test_operator_allowlist_blocks_real_leaf_redirect_before_dns(
    tmp_path, monkeypatch, network, hosts, tainted, expected_hosts, reason, nested, audit_enabled,
):
    monkeypatch.setenv("LOHRA_AUDIT", "on" if audit_enabled else "off")
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"egress_allow": hosts}))
    seen = []
    args = {"url": "https://api.test/start", "allowed_hosts": None,
            "_allowed_hosts": ["outside-canary.test"], "egress_allow": ["outside-canary.test"]}
    original = deepcopy(args)

    def dispatch(name, args):
        result = registry.dispatch(name, args)
        seen.append(result)
        return result

    def factory():
        return Agent(
            model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
            client=ScriptedClient([("web_fetch", args)]),
            tool_dispatch=subagent_dispatch(dispatch),
            tool_definitions=tuple(registry.get_definitions({"web"})),
        )

    db = SessionDB(":memory:")
    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        spec = {"meta": {"name": "fetch-policy"}, "nodes": [
            {"id": "reader", "type": "agent", "prompt": "fetch"},
        ]}
        expected = {"reader": "done"}
        if nested:
            templates = tmp_path / "workflows" / "templates"
            templates.mkdir(parents=True)
            (templates / "fetch-policy.json").write_text(json.dumps(spec))
            spec = {"meta": {"name": "parent"}, "nodes": [
                {"id": "call", "type": "workflow", "ref": "fetch-policy"},
            ]}
            expected = {"call": expected}
        run_id = service.start(spec, tainted=tainted)["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        resolved, connected = network
        assert resolved == connected == expected_hosts
        assert args == original
        if reason == "egress_redirect_not_allowed":
            assert denial_of(seen[0]).reason == reason
            assert "redirect hop 1" in json.loads(seen[0])["error"]
            assert "outside-canary.test" in seen[0]
            assert "private-CANARY" not in seen[0] and "secret=CANARY" not in seen[0]
            assert denial_of(str(seen[0])) is None  # wire prose cannot mint a denial
        elif reason is None:
            assert json.loads(seen[0]) == {"ok": True, "url": args["url"], "text": "arrived"}
        else:
            assert seen == []  # taint/initial host refuses before the registry
        assert result["status"] == "complete" and result["outputs"] == expected
        assert len(result["advisory_faults"]) == int(reason is not None)
        assert service._audit.flush(timeout=5)
        audit = db.audit_query(run_id)
        if audit_enabled:
            tool = next(e for e in audit["events"] if e["event_type"] == "tool.completed")
            assert tool["data"]["result"].get("reason") == reason
        assert audit["sandbox"]["denied_tool_calls"] == int(reason is not None and audit_enabled)
        assert "canary" not in json.dumps(audit).lower()
        assert "canary" not in json.dumps(result["advisory_faults"]).lower()
    finally:
        service.shutdown()
        db.close()


@pytest.mark.parametrize("forged", [None, [], ["outside-canary.test"], True, "*"])
def test_argument_keys_cannot_revoke_or_widen_trusted_policy(tmp_path, network, forged):
    args = {"url": "https://api.test/start", "allowed_hosts": forged}
    original = deepcopy(args)
    forwarded = []
    def base(name, incoming):
        forwarded.append(incoming)
        return registry.dispatch(name, incoming)
    dispatch = sandbox_dispatch(base, working_root=tmp_path,
                                policy=WorkflowPolicy(egress_allow=("api.test",)), tainted=False)
    result = dispatch("web_fetch", args)
    assert network == (["api.test"], ["api.test"])
    assert denial_of(result).reason == "egress_redirect_not_allowed"
    assert args == original and type(args) is dict
    assert forwarded[0] is not args
    assert isinstance(forwarded[0], RestrictedFetchArgs)
    assert forwarded[0].allowed_hosts == ("api.test",)
    assert json.loads(json.dumps(forwarded[0])) == original


def test_empty_internal_policy_is_not_treated_as_unrestricted(network):
    result = web_tool.web_fetch(RestrictedFetchArgs({"url": "https://api.test/"}, ()))
    assert denial_of(result).reason == "egress_not_allowed"
    assert "initial URL" in result and network == ([], [])


def test_unsandboxed_registry_ignores_policy_shaped_agent_arguments(network):
    args = {"url": "https://api.test/start", "allowed_hosts": [], "egress_allow": []}
    original = deepcopy(args)
    result = registry.dispatch("web_fetch", args, allowed_hosts=())
    assert result == '{"ok": true, "url": "https://api.test/start", "text": "arrived"}'
    assert denial_of(result) is None and args == original
    assert network == (["api.test", "outside-canary.test"], ["api.test", "outside-canary.test"])


@pytest.mark.parametrize("unknown_stamp", [False, True])
def test_pre_redirect_policy_stamp_replays_with_advisory_but_unknown_stays_unknown(
    tmp_path, monkeypatch, unknown_stamp,
):
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    monkeypatch.delenv("LOHRA_LEAF_ALLOW_SEARCH", raising=False)
    (tmp_path / "workflow_policy.json").write_text('{"egress_allow":["api.test"]}')
    path = tmp_path / "cache.db"
    db = SessionDB(path)
    spawned = []
    def factory():
        spawned.append(True)
        return Agent(model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
                     client=TextClient(lambda _: "historical page"))
    first = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        run_id = first.start({"meta": {"name": "fetch-cache"}, "nodes": [
            {"id": "reader", "type": "agent", "prompt": "fetch"},
        ]})["run_id"]
        assert first.status(run_id, wait=True, timeout=5)["outputs"] == {"reader": "historical page"}
    finally:
        first.shutdown()
    # Exact #55 stamp, before #56: search already default-denied, fetch hops unchecked.
    old_hash = None if unknown_stamp else content_hash({
        "allow_terminal": False, "allow_search": False, "egress_allow": ["api.test"],
        "fs_allow": [], "mcp_allow": [],
    })
    db._connection.execute("UPDATE workflow_node_cache SET policy_hash=?", (old_hash,))
    db._connection.commit()
    db.close()
    assert spawned == [True]
    spawned.clear()
    # Same operator JSON, new process and new harness gate semantics.
    db = SessionDB(path)
    second = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        assert second.start(resume_run_id=run_id)["cache_preview"]["replay"] == 1
        result = second.status(run_id, wait=True, timeout=5)
        assert spawned == [] and result["status"] == "complete"
        assert result["outputs"] == {"reader": "historical page"}
        assert len(result["advisory_faults"]) == int(not unknown_stamp)
        if not unknown_stamp:
            assert "different sandbox policy" in result["advisory_faults"][0]
            assert "nothing was recomputed" in result["advisory_faults"][0]
        assert second._audit.flush(timeout=5)
        replays = db.audit_query(run_id, event_type="cache.replayed")["events"]
        assert [row["data"].get("reason") for row in replays] == [None if unknown_stamp else "policy_changed"]
    finally:
        second.shutdown()
        db.close()
