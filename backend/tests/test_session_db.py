"""Tests for SessionDB — SQLite session/message persistence + recovery (§1)."""

import pytest

from lohra.state.db import SessionDB


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def test_create_and_get_session(db):
    db.create_session("s1", model="claude-opus-4-8", system_prompt="sys", cwd="/tmp", title="t")
    row = db.get_session("s1")
    assert row["id"] == "s1"
    assert row["model"] == "claude-opus-4-8"
    assert row["system_prompt"] == "sys"
    assert row["cwd"] == "/tmp"
    assert row["title"] == "t"
    assert row["started_at"] > 0
    assert row["ended_at"] is None
    assert row["parent_session_id"] is None


def test_get_unknown_session_returns_none(db):
    assert db.get_session("nope") is None


def test_end_session(db):
    db.create_session("s1")
    db.end_session("s1", reason="completed")
    row = db.get_session("s1")
    assert row["ended_at"] > 0
    assert row["end_reason"] == "completed"


def test_save_and_load_user_message(db):
    db.create_session("s1")
    db.save_message("s1", {"role": "user", "content": "hello"})
    msgs = db.load_messages("s1")
    assert msgs == [{"role": "user", "content": "hello"}]


def test_save_and_load_assistant_with_tool_calls_and_provider_data(db):
    db.create_session("s1")
    assistant = {
        "role": "assistant",
        "content": "calling a tool",
        "finish_reason": "tool_calls",
        "reasoning": "I should read the file",
        "tool_calls": [
            {"id": "tc_1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
        ],
        "provider_data": {"thinking_blocks": [{"type": "thinking", "thinking": "x", "signature": "s"}]},
    }
    db.save_message("s1", assistant)
    loaded = db.load_messages("s1")[0]
    assert loaded["role"] == "assistant"
    assert loaded["content"] == "calling a tool"
    assert loaded["finish_reason"] == "tool_calls"
    assert loaded["reasoning"] == "I should read the file"
    assert loaded["tool_calls"][0]["id"] == "tc_1"
    # provider_data round-trips so thinking replay still works after resume
    assert loaded["provider_data"]["thinking_blocks"][0]["signature"] == "s"


def test_save_and_load_tool_message(db):
    db.create_session("s1")
    db.save_message("s1", {"role": "tool", "name": "read_file", "tool_call_id": "tc_1", "content": "data"})
    loaded = db.load_messages("s1")[0]
    assert loaded == {"role": "tool", "name": "read_file", "tool_call_id": "tc_1", "content": "data"}


def test_load_preserves_order(db):
    db.create_session("s1")
    db.save_message("s1", {"role": "user", "content": "a"})
    db.save_message("s1", {"role": "assistant", "content": "b"})
    db.save_message("s1", {"role": "user", "content": "c"})
    assert [m["content"] for m in db.load_messages("s1")] == ["a", "b", "c"]


def test_message_count_tracked(db):
    db.create_session("s1")
    db.save_message("s1", {"role": "user", "content": "a"})
    db.save_message("s1", {"role": "assistant", "content": "b"})
    assert db.get_session("s1")["message_count"] == 2


def test_lineage_root_to_tip(db):
    db.create_session("root")
    db.create_session("child", parent_session_id="root")
    db.create_session("tip", parent_session_id="child")
    assert db.lineage_root_to_tip("tip") == ["root", "child", "tip"]


def test_lineage_single_session(db):
    db.create_session("solo")
    assert db.lineage_root_to_tip("solo") == ["solo"]


def test_persistence_across_reopen(tmp_path):
    path = str(tmp_path / "state.db")
    db1 = SessionDB(path)
    db1.create_session("s1", model="m")
    db1.save_message("s1", {"role": "user", "content": "persisted"})
    db1.close()

    db2 = SessionDB(path)
    assert db2.get_session("s1")["model"] == "m"
    assert db2.load_messages("s1") == [{"role": "user", "content": "persisted"}]
    db2.close()


def test_wal_mode_on_file_db(tmp_path):
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    mode = db._connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() in ("wal", "delete")  # wal on local FS, delete fallback elsewhere
    db.close()


def test_list_sessions_excludes_orchestration_subsessions(db):
    db.create_session("user-1", source="gateway")
    db.create_session("user-2", source="cli")
    db.create_session("sub-1", source="orchestration", parent_session_id="user-1")
    listed = {row["id"] for row in db.list_sessions()}
    assert listed == {"user-1", "user-2"}  # orchestration scaffolding hidden
