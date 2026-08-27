"""Tests for SOUL.md persona loading and identity override."""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.agent.system_prompt import DEFAULT_IDENTITY
from lohra.memory.soul import load_soul
from lohra.providers import get_provider_profile


class _FakeClient(ModelClient):
    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": None}


def test_load_soul_absent_returns_none(tmp_path):
    assert load_soul(tmp_path) is None


def test_load_soul_reads_content(tmp_path):
    (tmp_path / "SOUL.md").write_text("You are Áureo, a terse assistant.", encoding="utf-8")
    assert load_soul(tmp_path) == "You are Áureo, a terse assistant."


def test_load_soul_empty_file_returns_none(tmp_path):
    (tmp_path / "SOUL.md").write_text("   \n", encoding="utf-8")
    assert load_soul(tmp_path) is None


def _agent(identity):
    return Agent(
        model="m",
        provider=get_provider_profile("anthropic"),
        client=_FakeClient(),
        identity=identity,
    )


def test_soul_overrides_identity_in_stable_tier():
    agent = _agent("You are Áureo, a terse assistant.")
    stable = agent.system_prompt().stable
    assert "Áureo" in stable
    assert DEFAULT_IDENTITY not in stable


def test_no_soul_falls_back_to_default_identity():
    agent = _agent(None)
    assert DEFAULT_IDENTITY in agent.system_prompt().stable


@pytest.mark.parametrize("persona", ["persona one", "persona two"])
def test_soul_content_appears_verbatim(persona):
    assert persona in _agent(persona).system_prompt().text
