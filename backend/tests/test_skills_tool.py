"""Tests for the skill tools (skill_view, skill_manage) and prompt injection."""

import json

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.skills.store import SkillStore
from lohra.skills.tool import SkillTool


@pytest.fixture
def tool(tmp_path):
    return SkillTool(SkillStore(tmp_path))


def _call(handler, **args):
    return json.loads(handler(args))


def test_manage_create_then_view(tool):
    out = _call(tool.manage, action="create", name="deploy", description="Deploy it", body="# Deploy\nstep 1")
    assert out["ok"] is True
    viewed = _call(tool.view, name="deploy")
    assert viewed["ok"] is True
    assert "step 1" in viewed["body"]


def test_view_unknown_skill_errors(tool):
    out = _call(tool.view, name="ghost")
    assert "error" in out


def test_manage_create_missing_body_errors(tool):
    out = _call(tool.manage, action="create", name="x", description="d")
    assert "error" in out


def test_manage_invalid_name_returns_error_envelope(tool):
    out = _call(tool.manage, action="create", name="Bad Name", description="d", body="b")
    assert "error" in out


def test_manage_delete(tool):
    _call(tool.manage, action="create", name="temp", description="d", body="b")
    out = _call(tool.manage, action="delete", name="temp")
    assert out["ok"] is True
    assert _call(tool.view, name="temp").get("error")


def test_manage_unknown_action_errors(tool):
    out = _call(tool.manage, action="frob", name="x")
    assert "error" in out


# --- index injected into the frozen system prompt ---


class _FakeClient(ModelClient):
    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": None}


def test_skills_index_in_system_prompt_metadata_only(tmp_path):
    store = SkillStore(tmp_path)
    store.create("deploy-backend", "Deploy the backend safely", "SECRET BODY")
    agent = Agent(
        model="m",
        provider=get_provider_profile("anthropic"),
        client=_FakeClient(),
        skill_store=store,
    )
    text = agent.system_prompt().text
    assert "deploy-backend" in text
    assert "Deploy the backend safely" in text
    assert "SECRET BODY" not in text  # progressive disclosure


def test_skills_index_frozen(tmp_path):
    store = SkillStore(tmp_path)
    store.create("one", "first", "body")
    agent = Agent(
        model="m", provider=get_provider_profile("anthropic"), client=_FakeClient(), skill_store=store
    )
    first = agent.system_prompt().text
    store.create("two", "second", "body")
    assert agent.system_prompt().text == first  # frozen until next session
