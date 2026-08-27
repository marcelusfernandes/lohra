"""Cross-process compaction lock — a thin wrapper over the DB lock table.

The lineage split (end the session, fork a child with the compressed transcript)
must run single-winner across processes; the in-process busy lock can't span
processes. ``compression_lock`` yields whether the lock was acquired so the
caller can back off cleanly when another process is already forking the session.

See docs/specs/03-memory-skills-state.md §1 (compression_locks).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator
from uuid import uuid4

if TYPE_CHECKING:
    from lohra.state.db import SessionDB

# A crashed holder's lease expires after this so the session is never wedged.
DEFAULT_LOCK_TTL_SECONDS = 300.0


def holder_token() -> str:
    """A token that uniquely identifies this acquirer (pid + a random suffix)."""
    return f"{os.getpid()}-{uuid4().hex}"


@contextmanager
def compression_lock(
    db: SessionDB, session_id: str, *, ttl_seconds: float = DEFAULT_LOCK_TTL_SECONDS
) -> Iterator[bool]:
    """Hold the session's compaction lock for the block; yield whether we got it.

    Only releases a lock we actually acquired, so a contended call (yielding
    False) never frees the holder that owns it.
    """
    holder = holder_token()
    acquired = db.acquire_compression_lock(session_id, holder, ttl_seconds=ttl_seconds)
    try:
        yield acquired
    finally:
        if acquired:
            db.release_compression_lock(session_id, holder)
