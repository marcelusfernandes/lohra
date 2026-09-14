"""Tests for the terminal tool and its approval gating."""

import importlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from lohra.tools.approval import ApprovalManager, bind_approval_dispatch
from lohra.tools.terminal import terminal


@pytest.fixture(autouse=True)
def shell(monkeypatch):
    module = importlib.import_module("lohra.tools.terminal")
    state = SimpleNamespace(
        calls=[], result=SimpleNamespace(stdout="hi", stderr="", returncode=0), error=None
    )

    def run(command, **kwargs):
        state.calls.append((command, kwargs))
        if state.error is not None:
            raise state.error
        return state.result

    monkeypatch.setattr(
        module, "subprocess", SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)
    )
    return state


def test_terminal_runs_safe_command():
    out = json.loads(terminal({"command": "echo hi"}))
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]


def test_terminal_missing_command():
    out = json.loads(terminal({}))
    assert "command" in out["error"]


def test_terminal_nonzero_exit(shell):
    shell.result = SimpleNamespace(stdout="", stderr="synthetic missing path", returncode=2)
    out = json.loads(terminal({"command": "ls /definitely/not/here/xyz"}))
    assert out["exit_code"] != 0
    assert out["stderr"]


def test_terminal_dangerous_command_denied_without_approval(shell):
    out = json.loads(terminal({"command": "rm -rf /tmp/lohra-should-not-run"}))
    assert "not approved" in out["error"]
    assert shell.calls == []


def test_terminal_dangerous_command_runs_when_explicitly_bound(shell):
    owned = ApprovalManager()
    owned.set_callback(lambda cmd, desc, **kw: "once")
    dispatch = bind_approval_dispatch(lambda name, args: terminal(args), manager=owned)
    out = json.loads(dispatch("terminal", {"command": "sudo --version"}))
    assert out["ok"] is True
    assert len(shell.calls) == 1 and shell.calls[0][0] == "sudo --version"


def test_terminal_timeout(shell):
    shell.error = subprocess.TimeoutExpired("synthetic", 1)
    out = json.loads(terminal({"command": "sleep 2", "timeout": 1}))
    assert "timed out" in out["error"]


def test_terminal_registered():
    from lohra.tools.registry import registry

    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "terminal" in names
