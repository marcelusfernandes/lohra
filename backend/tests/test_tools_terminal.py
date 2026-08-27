"""Tests for the terminal tool and its approval gating."""

import json

import pytest

from lohra.tools.approval import approval
from lohra.tools.terminal import terminal


@pytest.fixture(autouse=True)
def _reset_approval():
    approval.set_callback(None)
    approval.set_yolo(False)
    approval.reset()
    yield
    approval.set_callback(None)
    approval.set_yolo(False)
    approval.reset()


def test_terminal_runs_safe_command():
    out = json.loads(terminal({"command": "echo hi"}))
    assert out["ok"] is True
    assert out["exit_code"] == 0
    assert "hi" in out["stdout"]


def test_terminal_missing_command():
    out = json.loads(terminal({}))
    assert "command" in out["error"]


def test_terminal_nonzero_exit():
    out = json.loads(terminal({"command": "ls /definitely/not/here/xyz"}))
    assert out["exit_code"] != 0
    assert out["stderr"]


def test_terminal_dangerous_command_denied_without_approval():
    out = json.loads(terminal({"command": "rm -rf /tmp/lohra-should-not-run"}))
    assert "not approved" in out["error"]


def test_terminal_dangerous_command_runs_when_approved(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    approval.set_callback(lambda cmd, desc, **kw: "once")
    out = json.loads(terminal({"command": f"rm -rf {victim}"}))
    assert out["ok"] is True
    assert not victim.exists()


def test_terminal_timeout():
    out = json.loads(terminal({"command": "sleep 2", "timeout": 1}))
    assert "timed out" in out["error"]


def test_terminal_registered():
    from lohra.tools.registry import registry

    names = {d["function"]["name"] for d in registry.get_definitions()}
    assert "terminal" in names
