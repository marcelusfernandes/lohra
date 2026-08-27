"""Tests for memory injection into the agent's frozen system prompt."""

import pytest

from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.memory.store import MemoryStore
from lohra.providers import get_provider_profile


class _FakeClient(ModelClient):
    def create(self, **kwargs):
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": None}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def _agent(store):
    return Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=_FakeClient(),
        memory_store=store,
    )


def test_memory_injected_into_volatile_tier(store):
    store.memory.add("user prefers tabs")
    store.user.add("name: Marcelus")
    agent = _agent(store)
    text = agent.system_prompt().text
    assert "user prefers tabs" in text
    assert "name: Marcelus" in text


def test_memory_frozen_after_first_build(store):
    store.memory.add("initial fact")
    agent = _agent(store)
    first = agent.system_prompt().text
    store.memory.add("added mid-session")
    assert agent.system_prompt().text == first  # frozen
    assert "added mid-session" not in agent.system_prompt().text


def test_no_memory_store_means_no_memory_block():
    agent = Agent(
        model="m", provider=get_provider_profile("anthropic"), client=_FakeClient()
    )
    # Should build fine without a memory store (chat-only path unchanged).
    assert "<memory>" not in agent.system_prompt().text
