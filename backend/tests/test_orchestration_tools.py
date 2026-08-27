"""Tests for the orchestration tool triad (spawn/steer/collect) + exclusions."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.orchestration.core import OrchestrationCore
from lohra.orchestration.tools import OrchestrationTool, register_orchestration_tool_schemas
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from tests.test_loop import FakeClient, _text_response


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _core(db, outputs):
    queue = [[_text_response(o)] for o in outputs]

    def factory():
        responses = queue.pop(0) if queue else [_text_response("ok")]
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient(responses),
        )

    return OrchestrationCore(db, factory)


def test_spawn_returns_sub_id(db):
    core = _core(db, ["child output"])
    try:
        tool = OrchestrationTool(core, parent_session_id="parent-1")
        out = json.loads(tool.spawn({"prompt": "do work"}))
        assert out["ok"] is True
        assert "sub_id" in out
        # collect it to completion
        collected = json.loads(tool.collect({"sub_id": out["sub_id"], "wait": True}))
        assert collected["status"] == "complete"
        assert collected["output"] == "child output"
    finally:
        core.shutdown()


def test_spawn_requires_prompt(db):
    core = _core(db, [])
    try:
        out = json.loads(OrchestrationTool(core).spawn({"prompt": "  "}))
        assert "error" in out
    finally:
        core.shutdown()


def test_steer_and_collect_validate_args(db):
    core = _core(db, [])
    try:
        tool = OrchestrationTool(core)
        assert "error" in json.loads(tool.steer({"sub_id": "x"}))  # no text
        assert "error" in json.loads(tool.collect({}))  # no sub_id
    finally:
        core.shutdown()


def test_steer_unknown_sub_id_errors(db):
    core = _core(db, [])
    try:
        out = json.loads(OrchestrationTool(core).steer({"sub_id": "ghost", "text": "hi"}))
        assert "error" in out
    finally:
        core.shutdown()


def test_parent_id_threaded_into_persistence(db):
    core = _core(db, ["x"])
    try:
        tool = OrchestrationTool(core, parent_session_id="the-parent")
        sub_id = json.loads(tool.spawn({"prompt": "task"}))["sub_id"]
        tool.collect({"sub_id": sub_id, "wait": True})
        assert db.get_session(sub_id)["parent_session_id"] == "the-parent"
    finally:
        core.shutdown()


def test_schemas_registered_and_intercepted_fallback():
    from lohra.tools import registry

    register_orchestration_tool_schemas()
    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert {"spawn_session", "steer_session", "collect_session"} <= names
    # without a bound core the registry handler must fail safe
    assert "error" in json.loads(registry.dispatch("spawn_session", {"prompt": "x"}))


def test_triad_excluded_from_subagents():
    from lohra.agent.delegate import _CHILD_EXCLUDED_TOOLS

    assert {"spawn_session", "steer_session", "collect_session"} <= _CHILD_EXCLUDED_TOOLS
