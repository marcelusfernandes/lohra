"""Operator JSON -> service -> canonical child -> registry -> owned pinned fetch."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.delegate import subagent_dispatch
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.tools.registry import registry
from lohra.workflow.service import WorkflowService
from tests.test_workflow_sandbox_denials import ScriptedClient
from tests.web_socket_lab import PUBLIC, Lab


@pytest.mark.parametrize(
    "hosts,tainted,rebind,expected_calls,expected_dials",
    [
        ([], False, False, [], 0),
        (["first.test"], False, False, ["first.test", "first.test", PUBLIC], 1),
        (
            ["first.test", "second.test"],
            False,
            False,
            ["first.test", "first.test", PUBLIC, "second.test", "second.test", PUBLIC],
            2,
        ),
        (
            ["first.test", "second.test"],
            False,
            True,
            ["first.test", "first.test", PUBLIC, "second.test", "second.test"],
            1,
        ),
        (["first.test", "second.test"], True, False, [], 0),
    ],
)
def test_real_leaf_host_taint_and_dns_gates(
    tmp_path, monkeypatch, hosts, tainted, rebind, expected_calls, expected_dials
):
    monkeypatch.setenv("LOHRA_AUDIT", "on")
    (tmp_path / "workflow_policy.json").write_text(json.dumps({"egress_allow": hosts}))
    templates = tmp_path / "workflows" / "templates"
    templates.mkdir(parents=True)
    (templates / "fetch.json").write_text(
        json.dumps(
            {
                "meta": {"name": "fetch"},
                "nodes": [
                    {"id": "reader", "type": "agent", "prompt": "fetch"},
                ],
            }
        )
    )
    args = {
        "url": "https://first.test/start",
        "allowed_hosts": None,
        "resolver": "forged",
        "client": "forged",
        "transport": "forged",
    }
    seen = []

    def dispatch(name, incoming):
        result = registry.dispatch(name, incoming)
        seen.append(result)
        return result

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient([("web_fetch", args)]),
            tool_dispatch=subagent_dispatch(dispatch),
            tool_definitions=tuple(registry.get_definitions({"web"})),
        )

    def addresses(host, count, port):
        return ["127.0.0.1" if rebind and host == "second.test" and count == 2 else PUBLIC]

    redirect = (
        b"HTTP/1.1 302 Found\r\nLocation: https://second.test/final?private-CANARY\r\n"
        b"Content-Length: 0\r\n\r\n"
    )
    db = SessionDB(":memory:")
    service = WorkflowService(base_child_factory=factory, db=db, home=tmp_path)
    try:
        with Lab(
            addresses, responses=[redirect], env={"LOHRA_HOME": str(tmp_path), "LOHRA_AUDIT": "on"}
        ) as lab:
            run_id = service.start(
                {
                    "meta": {"name": "parent"},
                    "nodes": [
                        {"id": "call", "type": "workflow", "ref": "fetch"},
                    ],
                },
                tainted=tainted,
            )["run_id"]
            result = service.status(run_id, wait=True, timeout=5)
            assert result["status"] == "complete"
            assert [row[0] for row in lab.dns] == expected_calls
            assert len(lab.connected) == expected_dials
            assert all(row[0] == PUBLIC for row in lab.connected)
        if rebind:
            assert "non-public" in json.loads(seen[-1])["error"]
        elif expected_dials == 2:
            assert json.loads(seen[-1])["text"] == "ok"
        assert service._audit.flush(timeout=5)
        assert "CANARY" not in json.dumps(db.audit_query(run_id))
    finally:
        service.shutdown()
        db.close()
