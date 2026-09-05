"""Which cells of ONE run declared the same artifact ``path`` (#65).

The manifest (``artifact.py``) measures a path per CELL and knows nothing about
the other cells of the run. Experiment #62 found the two holes that leaves:

1. two sibling cells declaring the same ``path`` produce, at best, the
   claim-vs-measurement divergence — whose text says *the leaf lied about the
   hash* when the fact is *your sibling overwrote this file*. The author gets
   the wrong remedy, and in the worst case (a manifest with only ``path``, the
   single ``required`` field) nothing fires at all: both cells measure the same
   final file and agree;
2. the resume's recheck then RE-SPAWNS the cell whose file a sibling
   legitimately moved on (``c3-jitter`` rep 3) — and the re-spawn repeats the
   write, duplicating data in a run that was correct.

The index here closes both with the same fact, and the fact is the PATH alone.
Never the sha: in the barrier case both cells measured the same final file and
stored the SAME digest, so an equal hash is the signature of the damage, not
evidence of safety — it must never suppress the warning.

Two properties are load-bearing:

**Keyed by node id, never by content hash.** An ``identity_changed`` cell leaves
its old row in ``workflow_node_cache`` forever, so keying by hash would make a
node's OWN previous version look like a sibling and suppress a genuine
invalidation. A pipeline's cells already carry distinct synthetic ids
(``chain#0#0`` / ``chain#1#0``), which is exactly the grain an author reads.

**Two declarations compared, the disk never consulted.** The in-run collision is
detected when the second cell is STORED, under this object's lock — so it does
not depend on which writer won a race, which is what made #62's detection a
lottery. The resume half is derived from the run's stored rows, which is what
makes it durable: the cell that gets re-spawned is the one stored FIRST, and a
flag written on the second cell could never have reached it.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# The remedy, one sentence, in every collision advisory: an author who reads it
# has to know what to change without opening the report.
REMEDY = "give each cell its own path, or have one cell own the file"


def _paths_of(raw: Any) -> list[str]:
    """The declared paths inside one stored ``artifact_json`` blob.

    Tolerant on purpose: this reads rows written by an older Lohra and rows a
    corrupt sidecar left unparseable, and a shape it does not recognise
    contributes nothing rather than raising inside a cache lookup."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            logger.warning("workflow: unreadable artifact manifest while indexing paths")
            return []
    if not isinstance(raw, list):
        return []
    return [
        entry["path"]
        for entry in raw
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]


class RunPaths:
    """``path -> the node ids of this run that declared it``.

    Mutable by construction — it accumulates as the run stores cells — and
    therefore guarded: a pipeline stores its cells from pool workers, and the
    reads (``is_shared``) happen on the same workers' cache lookups."""

    __slots__ = ("_owners", "_told", "_lock")

    def __init__(self, owners: dict[str, set[str]] | None = None) -> None:
        self._owners: dict[str, set[str]] = {
            path: set(nodes) for path, nodes in (owners or {}).items()
        }
        # Paths already reported. ONE advisory per path, not one per colliding
        # cell: a 500-item pipeline on one path is one fact about the spec's
        # shape, and 499 identical lines would bury it.
        self._told: set[str] = set()
        self._lock = threading.Lock()

    @classmethod
    def load(cls, cache: Any) -> "RunPaths":
        """The index this run already has on disk — every cell, every stretch.

        An empty index on ANY failure: not knowing which cells share a path must
        degrade to today's behaviour (every mismatch re-spawns), never to
        suppressing an invalidation the harness cannot justify."""
        owners: dict[str, set[str]] = {}
        try:
            rows = cache.artifact_rows()
        except Exception:
            logger.exception("workflow: artifact path index unavailable; none loaded")
            return cls()
        for node_id, blob in rows:
            for path in _paths_of(blob):
                owners.setdefault(path, set()).add(node_id)
        return cls(owners)

    def claim(self, node_id: str, paths: Iterable[str]) -> list[tuple[str, tuple[str, ...]]]:
        """Record that ``node_id`` declared ``paths``; report the NEW collisions.

        ``[(path, (siblings...)), ...]`` — at most once per path for the whole
        run. A cell re-storing its own path collides with nobody: the owners are
        a set of node ids, so a re-spawn of the same node is idempotent here."""
        collisions: list[tuple[str, tuple[str, ...]]] = []
        with self._lock:
            for path in paths:
                owners = self._owners.setdefault(path, set())
                siblings = tuple(sorted(owners - {node_id}))
                owners.add(node_id)
                if siblings and path not in self._told:
                    self._told.add(path)
                    collisions.append((path, siblings))
        return collisions

    def is_shared(self, path: Any) -> bool:
        """True when MORE THAN ONE cell of this run declared this path."""
        if not isinstance(path, str):
            return False
        with self._lock:
            return len(self._owners.get(path, ())) > 1

    def owners_of(self, path: Any) -> tuple[str, ...]:
        """Every cell of this run that declared this path (sorted, for text)."""
        if not isinstance(path, str):
            return ()
        with self._lock:
            return tuple(sorted(self._owners.get(path, ())))


def collision_message(node_id: str, path: str, siblings: tuple[str, ...]) -> str:
    """The advisory a SECOND declaration of one path earns, at store time.

    Names both sides and the remedy. Deliberately says nothing about hashes:
    the harness has not compared any content here, and #62 showed that borrowing
    the divergence text ("the leaf claimed sha X") sends the author to debug a
    leaf that told the truth."""
    named = ", ".join(siblings)
    return (
        f"{node_id}: artifact {path} is also declared by sibling {named} of this run — "
        f"same-run cells writing one file lose each other's work invisibly; "
        f"{REMEDY} (advisory: the harness cannot tell which write survived)"
    )


def shared_replay_message(path: str, owners: tuple[str, ...], cells: int) -> str:
    """...and the one a REPLAY earns when a sibling explains the change.

    Written at the seal, one line per path with the cell count, for the same
    reason the stamp advisories are: a wide fan-out on one shared path is one
    fact, and the count is not known until the last cell has replayed."""
    named = ", ".join(owners)
    unit = "cell" if cells == 1 else "cells"
    return (
        f"artifact {path} changed after it was measured, and it is declared by "
        f"{named} in this run — a sibling explains the change, so {cells} {unit} "
        f"replayed instead of re-spawning; {REMEDY} "
        f"(advisory: an OUTSIDE mutation of this path is no longer distinguishable)"
    )
