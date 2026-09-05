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

The index answers TWO different questions, and conflating them was the bug an
adversarial review caught:

**"Warn me" is keyed by the PATH alone.** In #62's barrier case both cells
measured the same final file and stored the SAME digest, so an equal hash is the
signature of the damage, not evidence of safety — it must never suppress the
warning. ``claim`` therefore compares declarations, never content.

**"Keep this replay" is keyed by the CONTENT.** ``is_shared(path)`` was a
PERMANENT, MONOTONE exemption from ``artifact_changed``: once two cells named a
path, an outside rewrite of it was kept forever, and a ghost row from a RENAMED
node immunised a single live writer. ``explained_by(path, disk_sha, exclude)``
replaces it with the narrow claim that is actually true — *some OTHER cell of
this run stored exactly what is on disk right now* — so the sibling's write is
kept and anything nobody in the run wrote goes back to being a re-spawn. The
trade-off is deliberate and fails toward the owner's default: if the LAST writer
stored no manifest of the final state, nothing explains the disk and the cell
re-spawns.

Two properties are load-bearing:

**Owners are node ids, never content hashes, and they carry the nesting scope.**
An ``identity_changed`` cell leaves its old row in ``workflow_node_cache``
forever, so keying by hash would make a node's OWN previous version look like a
sibling. A pipeline's cells already carry distinct synthetic ids (``chain#0#0`` /
``chain#1#0``); a NESTED template's do not — nested engines share the cache under
one ``run_id`` and store the raw node id, so two ``workflow`` nodes on one
template collapsed into a single owner and the collision was invisible inside
them. The owner written into the sidecar is therefore the SCOPED id
(``sub[build]:write``), and it is stored per entry so a recheck knows who it is
without being told.

**Two declarations compared, the disk never consulted (for the warning).** The
in-run collision is detected when the second cell is STORED, under this object's
lock — so it does not depend on which writer won a race, which is what made
#62's detection a lottery. The resume half is derived from the run's stored
rows, which is what makes it durable: the cell that gets re-spawned is the one
stored FIRST, and a flag written on the second cell could never have reached it.
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


def _entries_of(raw: Any, fallback_owner: str) -> list[tuple[str, str, str | None]]:
    """``(owner, path, sha)`` for each declared path inside one stored sidecar.

    ``owner`` is the SCOPED node id the store stamped on the entry; a row written
    before that existed falls back to the cache row's own ``node_id`` — right for
    every un-nested cell, and no worse than what it already was for a nested one.
    ``sha`` is the HARNESS's measurement (None for an entry it could not measure),
    which is what ``explained_by`` compares against the disk.

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
    found: list[tuple[str, str, str | None]] = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            continue
        owner = entry.get("owner")
        sha = entry.get("sha256")
        found.append((
            owner if isinstance(owner, str) and owner else fallback_owner,
            entry["path"],
            sha if isinstance(sha, str) else None,
        ))
    return found


class RunPaths:
    """``path -> {owner: the sha THAT owner measured there}``.

    The value is a mapping, not a set, because the two questions this index
    answers need different things: the WARNING needs only the owner names, the
    REPLAY decision needs the content each of them left behind.

    Mutable by construction — it accumulates as the run stores cells — and
    therefore guarded: a pipeline stores its cells from pool workers, and the
    reads happen on the same workers' cache lookups."""

    __slots__ = ("_owners", "_told", "_lock")

    def __init__(self, owners: dict[str, dict[str, str | None]] | None = None) -> None:
        self._owners: dict[str, dict[str, str | None]] = {
            path: dict(by_owner) for path, by_owner in (owners or {}).items()
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
        owners: dict[str, dict[str, str | None]] = {}
        try:
            rows = cache.artifact_rows()
        except Exception:
            logger.exception("workflow: artifact path index unavailable; none loaded")
            return cls()
        for node_id, blob in rows:
            for owner, path, sha in _entries_of(blob, node_id):
                owners.setdefault(path, {})[owner] = sha
        return cls(owners)

    def claim(
        self, owner: str, declared: Iterable[tuple[str, str | None]]
    ) -> list[tuple[str, tuple[str, ...]]]:
        """Record that ``owner`` declared these ``(path, sha)``; report the NEW
        collisions.

        ``[(path, (siblings...)), ...]`` — at most once per path for the whole
        run, and decided on the PATH alone: an equal sha is #62's signature of
        damage, so it must never suppress the warning. A cell re-storing its own
        path collides with nobody — owners are keyed by node id, so a re-spawn of
        the same cell overwrites its own sha instead of inventing a sibling."""
        collisions: list[tuple[str, tuple[str, ...]]] = []
        with self._lock:
            for path, sha in declared:
                by_owner = self._owners.setdefault(path, {})
                siblings = tuple(sorted(set(by_owner) - {owner}))
                by_owner[owner] = sha
                if siblings and path not in self._told:
                    self._told.add(path)
                    collisions.append((path, siblings))
        return collisions

    def explained_by(
        self, path: Any, disk_sha: Any, exclude: Any = None
    ) -> tuple[str, ...]:
        """Which OTHER cells of this run left exactly ``disk_sha`` at ``path``.

        The narrow, non-monotone claim that replaces ``is_shared``: not "this
        path is shared" (true forever, once) but "what is on disk right now is
        something a sibling of this run wrote". An outside rewrite matches
        nobody, so it goes back to being an ``artifact_changed`` re-spawn — the
        owner's decision, restored.

        ``exclude`` is the cell asking. It is dropped so a node's OWN ghost row
        (an ``identity_changed`` cell under the same id) cannot explain the
        change it caused: fail toward the re-spawn."""
        if not isinstance(path, str) or not isinstance(disk_sha, str):
            return ()
        with self._lock:
            by_owner = self._owners.get(path) or {}
            return tuple(sorted(
                owner for owner, sha in by_owner.items()
                if sha == disk_sha and owner != exclude
            ))

    def owners_of(self, path: Any) -> tuple[str, ...]:
        """Every cell of this run that declared this path (sorted, for text)."""
        if not isinstance(path, str):
            return ()
        with self._lock:
            return tuple(sorted(self._owners.get(path) or {}))


def collision_messages(
    index: "RunPaths | None", node_id: str, entries: Iterable[Any]
) -> tuple[str, ...]:
    """Register a cell's measured paths and say what a SECOND declaration earns.

    The whole store-time half, so the engine keeps one call site. The MEASURED
    path is what is registered (normalised, absolute), so two cells writing one
    file with two spellings of the same directories still meet — a SYMLINK to it
    does not, because ``measure`` stores the declared absolute path and not its
    ``realpath``. No index (a run with no cache) and any failure yield no
    messages: not knowing who else declared a path must never take a cache store
    down, and must never suppress an invalidation either."""
    if index is None:
        return ()
    declared = [
        (entry["path"], entry.get("sha256") if isinstance(entry.get("sha256"), str) else None)
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    ]
    try:
        collisions = index.claim(node_id, declared)
    except Exception:
        logger.exception("workflow: artifact path index failed for %s", node_id)
        return ()
    return tuple(
        collision_message(node_id, path, siblings) for path, siblings in collisions
    )


def replay_messages(
    pending: Iterable[tuple[str, tuple[int, set[str]]]]
) -> tuple[str, ...]:
    """...and the seal-time half: one line per PATH, with its cell count.

    Takes the explainers the RECHECKS actually found rather than re-reading the
    index: what the sentence claims is "this is what a sibling stored", and the
    only place that was ever true is the moment the disk was measured."""
    return tuple(
        shared_replay_message(path, tuple(sorted(owners)), cells)
        for path, (cells, owners) in pending
    )


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

    ``owners`` here are the cells that stored EXACTLY what is on disk now — not
    everyone who ever named the path — so the sentence is the narrow claim the
    decision actually rests on. Written at the seal, one line per path with the
    cell count, for the same reason the stamp advisories are: a wide fan-out on
    one shared path is one fact, and the count is not known until the last cell
    has replayed."""
    named = ", ".join(owners)
    unit = "cell" if cells == 1 else "cells"
    return (
        f"artifact {path} changed after it was measured, and what is on disk now "
        f"is what sibling {named} of this run stored — so {cells} {unit} replayed "
        f"instead of re-spawning; {REMEDY} (advisory: a change nobody in this run "
        f"wrote is still an artifact_changed re-spawn)"
    )
