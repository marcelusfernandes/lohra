"""Tests for the memory tool and the intercepted-dispatch composer."""

import json

import pytest

from lohra.memory.store import MemoryStore
from lohra.memory.tool import MemoryTool
from lohra.tools.intercept import compose_dispatch


@pytest.fixture
def tool(tmp_path):
    return MemoryTool(MemoryStore(tmp_path))


def _call(tool, **args):
    return json.loads(tool.handle(args))


def test_add_saves_to_memory(tool):
    out = _call(tool, action="add", text="user prefers concise answers")
    assert out["ok"] is True
    assert tool.store.memory.entries() == ["user prefers concise answers"]


def test_add_to_user_target(tool):
    _call(tool, action="add", target="user", text="name: Marcelus")
    assert tool.store.user.entries() == ["name: Marcelus"]
    assert tool.store.memory.entries() == []


def test_replace(tool):
    _call(tool, action="add", text="lives in Recife")
    out = _call(tool, action="replace", old_text="Recife", new_text="lives in SP")
    assert out["ok"] is True
    assert tool.store.memory.entries() == ["lives in SP"]


def test_remove(tool):
    _call(tool, action="add", text="temporary note")
    _call(tool, action="remove", old_text="temporary")
    assert tool.store.memory.entries() == []


def test_missing_action_errors(tool):
    out = _call(tool)
    assert "error" in out


def test_add_missing_text_errors(tool):
    out = _call(tool, action="add")
    assert "error" in out


def test_replace_ambiguous_returns_error_envelope(tool):
    _call(tool, action="add", text="uses Python")
    _call(tool, action="add", text="uses pytest")
    out = _call(tool, action="replace", old_text="uses", new_text="x")
    assert "error" in out
    assert "specific" in out["error"].lower() or "entries" in out["error"].lower()


def test_unknown_action_errors(tool):
    out = _call(tool, action="frobnicate")
    assert "error" in out


# --- compose_dispatch ---


def test_compose_routes_intercepted_and_base():
    calls = []
    base = lambda name, args: f'base:{name}'  # noqa: E731
    handler = lambda args: 'intercepted'  # noqa: E731
    dispatch = compose_dispatch(base, {"memory": handler})
    assert dispatch("memory", {"action": "add"}) == "intercepted"
    assert dispatch("read_file", {"path": "x"}) == "base:read_file"
    assert calls == []
