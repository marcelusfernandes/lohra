"""#56 review regression: match operator IDNs to HTTPX's actual host identity."""

import json
import socket
from functools import partial

import httpx
import pytest

import lohra.web.fetch as fetch_module
from lohra.agent.delegate import make_child_factory
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import registry
from lohra.tools.sandbox_denials import denial_of
from lohra.web import tool as web_tool  # noqa: F401 - register production handlers
from lohra.workflow.cell_stamp import policy_fingerprint
from lohra.workflow.sandbox import WorkflowPolicy, sandbox_dispatch
from lohra.workflow.service import WorkflowService
from tests.test_workflow_sandbox_denials import ScriptedClient


def _network(monkeypatch, location):
    resolved, sent = [], []
    def resolve(host, port):
        resolved.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]
    def request(req):
        sent.append(str(req.url))
        return (httpx.Response(302, headers={"location": location}) if req.url.path == "/start"
                else httpx.Response(200, text="arrived"))
    def forbidden(*args, **kwargs):
        raise AssertionError("test attempted a real connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch_module.httpx, "Client", partial(
        httpx.Client, transport=httpx.MockTransport(request),
    ))
    return resolved, sent


@pytest.mark.parametrize("unicode_host,ascii_host", [
    ("éxample.test", "xn--xample-9ua.test"),
    ("straße.test", "xn--strae-oqa.test"),
])
@pytest.mark.parametrize("policy_ascii,url_ascii", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("redirect", ["relative", "equivalent", "external"])
def test_unicode_policy_and_url_spellings_reach_the_same_real_leaf_destination(
    tmp_path, monkeypatch, unicode_host, ascii_host, policy_ascii, url_ascii, redirect,
):
    policy_host = ascii_host.upper() if policy_ascii else unicode_host
    url_host = ascii_host if url_ascii else unicode_host
    location = {"relative": "/final", "equivalent": f"https://{ascii_host}/final",
                "external": "https://outside.test/final?secret=CANARY"}[redirect]
    resolved, sent = _network(monkeypatch, location)
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"egress_allow": [policy_host]}))
    factory = make_child_factory(
        model="claude-opus-4-8", provider=get_provider_profile("anthropic"),
        client=ScriptedClient([("web_fetch", {"url": f"https://{url_host}/start"})]),
        tool_definitions=tuple(registry.get_definitions({"web"})),
    )
    db = SessionDB(":memory:")
    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        run_id = service.start({"meta": {"name": "idna"}, "nodes": [
            {"id": "fetcher", "type": "agent", "prompt": "fetch"},
        ]})["run_id"]
        result = service.status(run_id, wait=True, timeout=5)
        refused = redirect == "external"
        expected = [f"https://{ascii_host}/start"] + ([] if refused else [f"https://{ascii_host}/final"])
        assert sent == expected
        assert resolved == [url_host] + ([] if refused else [ascii_host])
        assert result["status"] == "complete" and result["outputs"] == {"fetcher": "done"}
        assert len(result["advisory_faults"]) == int(refused)
        assert service._audit.flush(timeout=5)
        audit = db.audit_query(run_id)
        completed = next(e for e in audit["events"] if e["event_type"] == "tool.completed")
        assert completed["data"]["result"].get("reason") == ("egress_redirect_not_allowed" if refused else None)
        assert audit["sandbox"]["denied_tool_calls"] == int(refused)
        assert "CANARY" not in json.dumps(audit)
    finally:
        service.shutdown()
        db.close()


@pytest.mark.parametrize("policy_host,url_host,location,initial_denial", [
    ("straße.test", "strasse.test", "/final", True),
    ("strasse.test", "straße.test", "/final", True),
    ("straße.test", "straße.test", "https://strasse.test/final", False),
    ("strasse.test", "strasse.test", "https://xn--strae-oqa.test/final", False),
])
def test_idna2008_never_grants_the_different_ss_domain(
    tmp_path, monkeypatch, policy_host, url_host, location, initial_denial,
):
    resolved, sent = _network(monkeypatch, location)
    dispatch = sandbox_dispatch(registry.dispatch, working_root=tmp_path,
                                policy=WorkflowPolicy(egress_allow=(policy_host,)), tainted=False)
    result = dispatch("web_fetch", {"url": f"https://{url_host}/start"})
    assert denial_of(result).reason == ("egress_not_allowed" if initial_denial else "egress_redirect_not_allowed")
    assert resolved == ([] if initial_denial else [url_host])
    assert len(sent) == int(not initial_denial)


@pytest.mark.parametrize("entry", [
    "api.test:443", "user@api.test", "api.test/path", "api.test?query", "api.test#fragment",
    "https://api.test", "\\api.test", "api.test ", "[bad", "\u0301api.test",
])
def test_policy_entry_is_a_host_not_a_url_or_authority(tmp_path, monkeypatch, entry):
    resolved, sent = _network(monkeypatch, "/final")
    dispatch = sandbox_dispatch(registry.dispatch, working_root=tmp_path,
                                policy=WorkflowPolicy(egress_allow=(entry,)), tainted=False)
    assert denial_of(dispatch("web_fetch", {"url": "https://api.test/start"})).reason == "egress_not_allowed"
    assert resolved == sent == []


def test_public_ipv6_host_grant_still_survives_relative_redirect(tmp_path, monkeypatch):
    resolved, sent = _network(monkeypatch, "/final")
    host = "2606:4700:4700::1111"
    dispatch = sandbox_dispatch(registry.dispatch, working_root=tmp_path,
                                policy=WorkflowPolicy(egress_allow=(host,)), tainted=False)
    result = dispatch("web_fetch", {"url": f"https://[{host}]/start"})
    assert json.loads(result)["text"] == "arrived"
    assert resolved == [host, host] and len(sent) == 2


def test_policy_fingerprint_tracks_idna_identity_not_spelling():
    unicode_policy = WorkflowPolicy(egress_allow=("éxample.test", "straße.test"))
    ascii_policy = WorkflowPolicy(egress_allow=("XN--STRAE-OQA.TEST", "xn--xample-9ua.test", "éxample.test"))
    assert policy_fingerprint(unicode_policy) == policy_fingerprint(ascii_policy)
    assert policy_fingerprint(WorkflowPolicy(egress_allow=("straße.test",))) != policy_fingerprint(
        WorkflowPolicy(egress_allow=("strasse.test",)))
