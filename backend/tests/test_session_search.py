"""Tests for SessionDB FTS5 search and the session_search tool."""

import json

import pytest

from lohra.state import SessionDB
from lohra.state.search import SessionSearchTool


@pytest.fixture
def db(tmp_path):
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _seed(db):
    db.create_session("s1", title="auth work")
    db.save_message("s1", {"role": "user", "content": "how do I configure OAuth login?"})
    db.save_message("s1", {"role": "assistant", "content": "Use the device-code flow for OAuth."})
    db.create_session("s2", title="deploy")
    db.save_message("s2", {"role": "user", "content": "deploy the backend to production"})


def test_fts_enabled_on_standard_sqlite(db):
    assert db.fts_enabled is True  # FTS5 ships with CPython's sqlite3


def test_search_finds_matching_messages(db):
    _seed(db)
    hits = db.search("OAuth")
    assert len(hits) == 2
    assert {h["session_id"] for h in hits} == {"s1"}
    assert any("OAuth" in h["snippet"] or "oauth" in h["snippet"].lower() for h in hits)


def test_search_ranks_and_limits(db):
    _seed(db)
    assert len(db.search("deploy", limit=1)) == 1


def test_search_no_match_returns_empty(db):
    _seed(db)
    assert db.search("kubernetes") == []


def test_search_malformed_query_returns_empty(db):
    _seed(db)
    assert db.search('"unterminated') == []  # invalid FTS syntax -> [] not a crash


def test_search_backfills_preexisting_messages(tmp_path):
    path = str(tmp_path / "state.db")
    db1 = SessionDB(path)
    db1.create_session("s1")
    db1.save_message("s1", {"role": "user", "content": "indexed via trigger"})
    db1.close()
    # reopening rebuilds the connection; the trigger already indexed it
    db2 = SessionDB(path)
    assert len(db2.search("indexed")) == 1
    db2.close()


# --- session_search tool ---


@pytest.fixture
def tool(db):
    _seed(db)
    return SessionSearchTool(db)


def test_tool_discovery(tool):
    out = json.loads(tool.handle({"mode": "discovery", "query": "OAuth"}))
    assert out["ok"] is True
    assert len(out["hits"]) == 2


def test_tool_browse(tool):
    out = json.loads(tool.handle({"mode": "browse"}))
    assert {s["id"] for s in out["sessions"]} == {"s1", "s2"}


def test_tool_read(tool):
    out = json.loads(tool.handle({"mode": "read", "session_id": "s1"}))
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]


def test_tool_discovery_requires_query(tool):
    out = json.loads(tool.handle({"mode": "discovery"}))
    assert "error" in out


def test_tool_unknown_mode(tool):
    out = json.loads(tool.handle({"mode": "teleport"}))
    assert "error" in out
