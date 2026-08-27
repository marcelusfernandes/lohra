"""Tests for the cross-process compaction lock (spec §1, Phase 5).

The lock lives in the SQLite `compression_locks` table so two processes (e.g. a
CLI run and the gateway, or two `lohra chat --session <same>`) can't both
compact-and-fork the same session at once. The in-process busy lock can't span
processes; this table can.
"""

import pytest

from lohra.state import SessionDB
from lohra.state.compression_lock import (
    DEFAULT_LOCK_TTL_SECONDS,
    compression_lock,
    holder_token,
)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


# --- db primitive -------------------------------------------------------------


def test_acquire_succeeds_when_free(db):
    assert db.acquire_compression_lock("s1", "holderA") is True


def test_second_holder_blocked_while_held(db):
    assert db.acquire_compression_lock("s1", "holderA") is True
    assert db.acquire_compression_lock("s1", "holderB") is False


def test_release_lets_another_acquire(db):
    db.acquire_compression_lock("s1", "holderA")
    assert db.release_compression_lock("s1", "holderA") is True
    assert db.acquire_compression_lock("s1", "holderB") is True


def test_release_by_wrong_holder_is_noop(db):
    db.acquire_compression_lock("s1", "holderA")
    assert db.release_compression_lock("s1", "holderB") is False
    # holderA still owns it
    assert db.acquire_compression_lock("s1", "holderB") is False


def test_locks_are_per_session(db):
    assert db.acquire_compression_lock("s1", "h") is True
    assert db.acquire_compression_lock("s2", "h") is True


def test_expired_lock_can_be_reclaimed(db):
    # acquire an already-expired lease
    assert db.acquire_compression_lock("s1", "stale", ttl_seconds=-1.0) is True
    # a fresh acquirer reclaims it (the dead holder never released)
    assert db.acquire_compression_lock("s1", "fresh") is True


def test_release_idempotent_when_absent(db):
    assert db.release_compression_lock("nope", "h") is False


class _LockedConn:
    """A connection whose writes always raise 'database is locked'."""

    def execute(self, *_a, **_k):
        import sqlite3

        raise sqlite3.OperationalError("database is locked")

    def rollback(self):
        pass

    def commit(self):
        pass

    def close(self):
        pass


def test_acquire_backs_off_on_database_locked(db):
    db._connection = _LockedConn()  # simulate a peer holding the write lock
    # a contended write must not crash — it means "not ours"
    assert db.acquire_compression_lock("s1", "h") is False


def test_release_backs_off_on_database_locked(db):
    db._connection = _LockedConn()
    assert db.release_compression_lock("s1", "h") is False


# --- context manager ----------------------------------------------------------


def test_context_manager_yields_true_and_releases(db):
    with compression_lock(db, "s1") as acquired:
        assert acquired is True
        # held inside the block
        assert db.acquire_compression_lock("s1", "other") is False
    # released on exit
    assert db.acquire_compression_lock("s1", "other") is True


def test_context_manager_yields_false_when_contended(db):
    db.acquire_compression_lock("s1", "someone-else")
    with compression_lock(db, "s1") as acquired:
        assert acquired is False
    # the contender's lock is untouched (we never acquired, so we don't release it)
    assert db.acquire_compression_lock("s1", "x") is False


def test_context_manager_releases_on_exception(db):
    with pytest.raises(RuntimeError):
        with compression_lock(db, "s1") as acquired:
            assert acquired is True
            raise RuntimeError("boom")
    assert db.acquire_compression_lock("s1", "other") is True


def test_holder_token_is_unique():
    assert holder_token() != holder_token()


def test_default_ttl_is_positive():
    assert DEFAULT_LOCK_TTL_SECONDS > 0
