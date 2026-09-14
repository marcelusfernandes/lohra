"""Approval tests drive real consumers with scripted models and a fake shell."""

import importlib
import os
import subprocess
from types import SimpleNamespace

import pytest

from lohra import cli
from lohra.agent.client import ModelClient
from lohra.tools.approval import approval

COMMAND = "sudo --version"  # data only; never executed by these tests
OTHER = "sudo --help"
terminal_module = importlib.import_module("lohra.tools.terminal")


def text():
    return {
        "content": [{"type": "text", "text": "synthetic done"}],
        "stop_reason": "end_turn",
        "usage": None,
    }


def tool(command=COMMAND):
    return {
        "content": [
            {
                "type": "tool_use",
                "id": "synthetic-call",
                "name": "terminal",
                "input": {"command": command},
            }
        ],
        "stop_reason": "tool_use",
        "usage": None,
    }


class ScriptedClient(ModelClient):
    def __init__(self, responses):
        self.responses = list(responses)

    def create(self, **kwargs):
        return self.responses.pop(0)

    def stream(self, **kwargs):
        return self.create(**kwargs)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    # No personal provider, auth, project context, MCP, home or executable is used.
    for key in list(os.environ):
        monkeypatch.delenv(key)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOHRA_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-no-network")
    monkeypatch.setenv("LOHRA_MODEL", "synthetic")
    monkeypatch.chdir(tmp_path)
    state = SimpleNamespace(clients=[], commands=[], prompts=[], answer="s", root=tmp_path)

    def fake_run(command, **kwargs):
        state.commands.append(command)
        return SimpleNamespace(stdout="fake shell", stderr="", returncode=0)

    def answer(*args):
        state.prompts.append(True)
        return state.answer

    monkeypatch.setattr("lohra.agent.client.build_client", lambda *a, **k: state.clients.pop(0))
    monkeypatch.setattr("lohra.mcp.register_configured_mcp_servers", lambda *a, **k: None)
    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(
        terminal_module,
        "subprocess",
        SimpleNamespace(
            run=fake_run,
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    approval.reset()
    approval.set_yolo(False)
    approval.set_callback(None)
    yield state
    approval.reset()
    approval.set_yolo(False)
    approval.set_callback(None)


def chat(**kwargs):
    assert cli.run_chat("synthetic", provider="anthropic", **kwargs) == 0
