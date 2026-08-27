"""Tests for the OpenAI server's agentic tool allow-list (spec Fase 6).

Agentic mode runs Lohra's tools server-side. Security rests on an explicit
allow-list (default empty = relay): only named, stateless tools are exposed, the
intercepted/stateful tools are never reachable, and dangerous shell commands are
auto-denied (no operator to approve over HTTP).
"""

import json

import pytest

from lohra.server.agentic import build_allowed_tools


@pytest.fixture(autouse=True)
def _registered():
    # ensure built-in + intercepted schemas exist for filtering
    from lohra.agent.equip import register_all_tools

    register_all_tools()


def test_allow_list_filters_to_named_tools():
    defs, _dispatch = build_allowed_tools(["read_file", "terminal"])
    names = {d["function"]["name"] for d in defs}
    assert names == {"read_file", "terminal"}


def test_empty_allow_list_yields_no_tools():
    defs, _dispatch = build_allowed_tools([])
    assert defs == ()


def test_intercepted_tools_are_never_exposed_even_if_requested():
    # memory/skills/session_search/delegate_task need session state — excluded
    defs, _dispatch = build_allowed_tools(["read_file", "memory", "delegate_task"])
    names = {d["function"]["name"] for d in defs}
    assert names == {"read_file"}


def test_unknown_tool_names_are_ignored():
    defs, _dispatch = build_allowed_tools(["read_file", "does_not_exist"])
    names = {d["function"]["name"] for d in defs}
    assert names == {"read_file"}


def test_dispatch_refuses_a_tool_not_in_the_allow_list():
    # CRITICAL regression: the allow-list must gate EXECUTION, not just the
    # definitions the model sees. read_file is allowed; terminal is NOT.
    _defs, dispatch = build_allowed_tools(["read_file"])
    out = json.loads(dispatch("terminal", {"command": "cat /etc/passwd"}))
    assert "error" in out
    assert "allow-list" in out["error"]
    assert "stdout" not in out  # the command never reached the registry


def test_dispatch_refuses_write_file_when_only_read_allowed(tmp_path):
    target = tmp_path / "pwn.txt"
    _defs, dispatch = build_allowed_tools(["read_file"])
    out = json.loads(dispatch("write_file", {"path": str(target), "content": "x"}))
    assert "error" in out
    assert not target.exists()  # nothing was written


def test_dispatch_auto_denies_dangerous_commands():
    _defs, dispatch = build_allowed_tools(["terminal"])
    out = json.loads(dispatch("terminal", {"command": "rm -rf /"}))
    assert "error" in out
    assert "rm" not in out.get("stdout", "")  # never executed


def test_dispatch_blocks_intercepted_tools():
    _defs, dispatch = build_allowed_tools(["read_file"])
    out = json.loads(dispatch("memory", {"action": "add", "text": "x"}))
    assert "error" in out


def test_dispatch_runs_an_allowed_tool(tmp_path):
    target = tmp_path / "f.txt"
    target.write_text("file body here")
    _defs, dispatch = build_allowed_tools(["read_file"])
    out = json.loads(dispatch("read_file", {"path": str(target)}))
    assert "file body here" in json.dumps(out)
