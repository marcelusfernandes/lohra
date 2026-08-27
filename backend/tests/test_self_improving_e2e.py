"""E2E: the agent saves memory + creates a skill, and recovers them in a new
session (Phase 4 spine), driven through the real equip wiring with a fake LLM.
"""

import json


from lohra.agent.agent import Agent
from lohra.agent.client import ModelClient
from lohra.agent.equip import build_session_dispatch, build_session_stores, register_all_tools
from lohra.agent.loop import run_conversation
from lohra.providers import get_provider_profile
from lohra.tools import registry


class _ScriptedClient(ModelClient):
    """Replays a fixed sequence of raw responses (tool calls then a final text)."""

    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
        return self._responses.pop(0)


def _tool_call(cid, name, inp):
    return {
        "content": [{"type": "tool_use", "id": cid, "name": name, "input": inp}],
        "stop_reason": "tool_use",
        "usage": None,
    }


def _text(text):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": None}


def _make_agent(home, responses):
    register_all_tools()
    memory_store, skill_store = build_session_stores(home)
    return Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=_ScriptedClient(responses),
        tool_definitions=tuple(registry.get_definitions()),
        tool_dispatch=build_session_dispatch(memory_store, skill_store),
        memory_store=memory_store,
        skill_store=skill_store,
    )


def test_agent_saves_memory_and_skill_then_recovers_in_new_session(tmp_path):
    # --- session 1: the agent saves a memory and creates a skill via tools ---
    agent1 = _make_agent(
        tmp_path,
        [
            _tool_call("t1", "memory", {"action": "add", "text": "user prefers Portuguese"}),
            _tool_call("t2", "skill_manage", {
                "action": "create",
                "name": "run-tests",
                "description": "How to run the Lohra test suite",
                "body": "# Run tests\ncd backend && pytest",
            }),
            _text("Salvo na memória e criei a skill."),
        ],
    )
    result = run_conversation(agent1, "lembre que prefiro português e crie uma skill de testes")
    assert result["completed"] is True
    # the tool results confirm the writes happened
    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert all("error" not in json.loads(m["content"]) for m in tool_msgs)

    # --- session 2: a fresh agent over the same home sees them ---
    agent2 = _make_agent(tmp_path, [_text("ok")])
    prompt2 = agent2.system_prompt().text
    assert "user prefers Portuguese" in prompt2  # memory recovered in the frozen snapshot
    assert "run-tests" in prompt2  # skill indexed (progressive disclosure)
    assert "How to run the Lohra test suite" in prompt2
    assert "cd backend && pytest" not in prompt2  # body NOT in the index

    # and the agent can load the skill body on demand via skill_view
    viewed = json.loads(agent2.tool_dispatch("skill_view", {"name": "run-tests"}))
    assert "cd backend && pytest" in viewed["body"]
