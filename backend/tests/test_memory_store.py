"""Tests for MemoryStore — MEMORY.md/USER.md persistence + frozen snapshot (§2)."""

import pytest

from lohra.memory.store import (
    MEMORY_CHAR_LIMIT,
    AmbiguousEntry,
    EntryNotFound,
    MemoryLimitExceeded,
    MemoryStore,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


def test_add_appends_entry(store):
    store.memory.add("user prefers tabs over spaces")
    assert store.memory.entries() == ["user prefers tabs over spaces"]


def test_add_multiple_entries_delimited(store):
    store.memory.add("fact one")
    store.memory.add("fact two")
    assert store.memory.entries() == ["fact one", "fact two"]
    # on disk, entries are §-delimited
    raw = (store.memory.path).read_text(encoding="utf-8")
    assert "§" in raw


def test_add_dedups_exact_duplicate(store):
    store.memory.add("same fact")
    store.memory.add("same fact")
    assert store.memory.entries() == ["same fact"]


def test_replace_by_unique_substring(store):
    store.memory.add("user lives in Recife")
    store.memory.replace("Recife", "user lives in São Paulo")
    assert store.memory.entries() == ["user lives in São Paulo"]


def test_replace_ambiguous_substring_raises(store):
    store.memory.add("project uses Python")
    store.memory.add("project uses pytest")
    with pytest.raises(AmbiguousEntry):
        store.memory.replace("project uses", "x")


def test_replace_missing_substring_raises(store):
    store.memory.add("a fact")
    with pytest.raises(EntryNotFound):
        store.memory.replace("nonexistent", "x")


def test_remove_by_substring(store):
    store.memory.add("keep this")
    store.memory.add("delete this")
    store.memory.remove("delete")
    assert store.memory.entries() == ["keep this"]


def test_char_limit_enforced_and_file_unchanged(store):
    store.memory.add("small")
    before = store.memory.entries()
    with pytest.raises(MemoryLimitExceeded):
        store.memory.add("x" * (MEMORY_CHAR_LIMIT + 1))
    assert store.memory.entries() == before  # rejected write left disk intact


def test_user_file_is_separate(store):
    store.memory.add("agent note")
    store.user.add("name: Marcelus")
    assert store.memory.entries() == ["agent note"]
    assert store.user.entries() == ["name: Marcelus"]
    assert store.memory.path != store.user.path


def test_persists_across_instances(tmp_path):
    MemoryStore(tmp_path).memory.add("durable fact")
    assert MemoryStore(tmp_path).memory.entries() == ["durable fact"]


# --- frozen snapshot (Invariante #1) ---


def test_snapshot_is_frozen_until_reload(store):
    store.memory.add("initial")
    store.load_snapshot()
    snap = store.snapshot()
    store.memory.add("added mid-session")  # disk updated...
    assert store.snapshot() == snap  # ...but snapshot stays frozen
    assert "added mid-session" not in store.snapshot()["memory"]


def test_snapshot_refreshes_on_reload(store):
    store.memory.add("initial")
    store.load_snapshot()
    store.memory.add("new fact")
    store.load_snapshot()  # next session
    assert "new fact" in store.snapshot()["memory"]


def test_snapshot_includes_both_files(store):
    store.memory.add("agent memory")
    store.user.add("user profile")
    store.load_snapshot()
    snap = store.snapshot()
    assert "agent memory" in snap["memory"]
    assert "user profile" in snap["user"]
