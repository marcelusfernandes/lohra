"""Live-dispatch lifetime and more restrictive child consumers remain explicit."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.delegate import make_child_factory
from lohra.agent.equip import build_session_dispatch, build_session_stores
from lohra.gateway.manager import SessionManager
from lohra.providers import get_provider_profile
from lohra.server.agentic import build_allowed_tools
from lohra.state import SessionDB
from lohra.tools import ApprovalManager, bind_approval_dispatch, require_approval
from lohra.tools.registry import registry
from lohra.workflow.sandbox import WorkflowPolicy, make_sandboxed_leaf_factory
from tests.approval_lab import lab as lab
from tests.approval_lab import COMMAND, ScriptedClient, text, tool


def submit(session):
    events = []
    session.submit("synthetic", events.append)
    results = [
        event["params"]["payload"]["result"]
        for event in events
        if event["params"]["type"] == "tool.complete"
    ]
    assert len(results) == 1
    return json.loads(results[0])


def test_compaction_keeps_live_dispatch_but_sqlite_revival_does_not_restore_approval(lab):
    path = lab.root / "approval-lifetime.db"
    created, prompts = [], []

    def factory(session_id):
        owned = ApprovalManager()
        if not created:  # only the first live consumer has an operator callback
            owned.set_callback(lambda *a, **kw: prompts.append(True) or "session")
        created.append(session_id)
        return Agent(
            model="synthetic",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient([tool(), text(), tool(), text()]),
            tool_dispatch=build_session_dispatch(
                *build_session_stores(lab.root), session_id=session_id, approval_manager=owned
            ),
            tool_definitions=tuple(registry.get_definitions({"terminal"})),
        )

    db = SessionDB(path)
    sessions = SessionManager(db, factory)
    parent = sessions.create_session(session_id="parent")
    try:
        assert submit(parent)["ok"] is True
        child_id = sessions.fork_for_compaction("parent", parent.agent, db.load_messages("parent"))
        child = sessions.get(child_id)
        assert child is not None and child.agent is parent.agent
        assert child._base_dispatch is parent._base_dispatch
        assert child._busy is parent._busy
        assert submit(child)["ok"] is True
        assert created == ["parent"] and prompts == [True]
        assert lab.commands == [COMMAND, COMMAND]
    finally:
        db.close()
    # Another manager/new DB object models a restart. The same durable ID is not authority.
    db = SessionDB(path)
    try:
        revived = SessionManager(db, factory).get(child_id)
        assert revived.agent is not parent.agent
        assert "not approved" in submit(revived)["error"]
        assert created == ["parent", child_id] and prompts == [True]
        assert lab.commands == [COMMAND, COMMAND]
    finally:
        db.close()


@pytest.mark.parametrize("surface", ["subagent", "serve", "workflow", "tainted-workflow"])
def test_real_child_factories_and_serve_guard_deny_under_synchronous_yolo_parent(lab, surface):
    owned = ApprovalManager()
    owned.set_yolo(True)

    def parent(name, args):
        assert require_approval(COMMAND)  # a real active parent grant
        if surface == "serve":
            _, dispatch = build_allowed_tools(["terminal"])
        else:
            factory = make_child_factory(
                model="synthetic",
                provider=get_provider_profile("anthropic"),
                client=ScriptedClient([]),
                tool_definitions=tuple(registry.get_definitions({"terminal"})),
            )
            if "workflow" in surface:
                factory = make_sandboxed_leaf_factory(
                    base_factory=factory,
                    working_root=lab.root,
                    policy=WorkflowPolicy(allow_terminal=True),
                    tainted=surface.startswith("tainted"),
                )
            dispatch = factory().tool_dispatch
        return dispatch(name, args)

    result = bind_approval_dispatch(parent, manager=owned)("terminal", {"command": COMMAND})
    assert "error" in json.loads(result)
    assert lab.commands == [] and not require_approval(COMMAND)
