"""End-to-end: the loop drives the REAL registry + fs tool to read a file.

Ties together tool dispatch, the registry, the filesystem tool, and the
transport — the Phase 2 'agent reads a file' spine, without a live API.
"""

from lohra.agent import Agent, run_conversation
from lohra.agent.client import ModelClient
from lohra.providers import get_provider_profile
from lohra.tools import load_builtin_tools, registry


class _ScriptedClient(ModelClient):
    """Turn 1: request read_file. Turn 2: confirm the result came back."""

    def __init__(self, path):
        self._path = path
        self.turn = 0

    def create(self, **kwargs):
        self.turn += 1
        if self.turn == 1:
            return {
                "content": [
                    {"type": "tool_use", "id": "tc1", "name": "read_file", "input": {"path": self._path}}
                ],
                "stop_reason": "tool_use",
                "usage": None,
            }
        return {"content": [{"type": "text", "text": "I read the file."}], "stop_reason": "end_turn", "usage": None}


def test_loop_reads_a_real_file_through_the_registry(tmp_path):
    load_builtin_tools()
    note = tmp_path / "note.txt"
    note.write_text("the secret is 42", encoding="utf-8")

    agent = Agent(
        model="claude-opus-4-8",
        provider=get_provider_profile("anthropic"),
        client=_ScriptedClient(str(note)),
        tool_definitions=tuple(registry.get_definitions()),
        tool_dispatch=registry.dispatch,
    )
    result = run_conversation(agent, "read the note")

    tool_msgs = [m for m in result["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert "the secret is 42" in tool_msgs[0]["content"]
    assert result["final_response"] == "I read the file."
    assert result["completed"] is True
    assert result["api_calls"] == 2
