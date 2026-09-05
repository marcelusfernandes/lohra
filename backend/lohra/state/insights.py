"""Durable cross-process store for workflow insights/candidates (SUP-05, slice 1).

A SQLite table in the shared SessionDB file holds the IMMEDIATE signals a
supervising session may learn from — the analog of ``workflow/insights.md``
(library.py) but queryable from ANY process, including sessions that did not
run the workflow.

Invariants (each enforced at the write boundary, not by convention):

- **learnable gate** — only ``FailureObservation.is_learnable`` observations
  enter; the store re-classifies from raw fields rather than trusting a
  caller-supplied verdict (fail-closed against a lying caller);
- **structural dedup (Wave 9, E1/#50)** — one fingerprint per (kind,
  responsibility, mechanism, sorted evidence signals). The fingerprint is
  computed over that STRUCTURE, never over the free-text ``summary`` — two
  callers reporting the exact same causal defect from different node ids or
  in different words are the SAME lesson and must land as one row, while two
  callers with different evidence (``signals``) are different lessons even
  when their prose happens to match. A repeat of the same structural cause
  increments ``hits`` and advances ``updated_at`` (``ON CONFLICT DO UPDATE``)
  instead of being silently dropped;
- **hits is a recurrence counter, not a default** — a row written before this
  column existed has no honest count to backfill (the old scheme used
  ``INSERT OR IGNORE`` with no counter at all, so any number of
  silently-ignored writes behind one legacy row is unrecoverable); a legacy
  row's ``hits`` reads as NULL forever, never coerced to 0 or 1. NULL is
  itself the marker that a row predates the structural fingerprint — no
  separate schema-version column is needed, because the old and new
  fingerprint schemes hash different inputs and therefore never collide, so
  a legacy row is never merged into a post-E1 one;
- **cap 200** — hardest bound, oldest-first eviction inside the same
  transaction as the insert (no window where the table is unbounded). A
  recurring lesson keeps advancing ``updated_at`` on every hit, so it is
  effectively immortal under the cap — intentional post-E1: a cause that
  keeps recurring is exactly the one that should survive eviction longest;
- **bounded text** — text fields clipped at the schema boundary;
- **short transaction** — one ``BEGIN IMMEDIATE`` per write; the read of the
  fingerprint and the INSERT share it, so two processes can never both
  observe "absent" and both win (the pattern ``steering_reserve`` uses).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from lohra.workflow.failure_taxonomy import (
    Mechanism,
    classify_failure,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_insight_candidates (
    fingerprint TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('insight', 'candidate')),
    status TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    responsibility TEXT NOT NULL,
    confidence REAL NOT NULL,
    summary TEXT NOT NULL,
    last_summary TEXT,
    payload_json TEXT,
    hits INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wic_updated ON workflow_insight_candidates(updated_at);
"""

# Columns added to a table that already ships in the wild (mirrors the
# ``_ADDED_COLUMNS`` pattern in state/db.py — this store owns its own
# connection/schema, so it needs its own idempotent migration). Additive and
# nullable only: an ALTER that already ran raises OperationalError, which IS
# the idempotence check, and an old row reads a new column as NULL forever.
_ADDED_COLUMNS = (
    ("workflow_insight_candidates", "last_summary", "TEXT"),
    ("workflow_insight_candidates", "hits", "INTEGER"),
)

# Hard bounds. The cap mirrors MAX_INSIGHTS in workflow/library.py: machine
# telemetry, newest kept, never unbounded.
MAX_CANDIDATES = 200
MAX_SUMMARY_CHARS = 500
_MAX_TEXT = 2000

_INSIGHT = "insight"
_CANDIDATE = "candidate"


def _fingerprint(
    kind: str, responsibility: str, mechanism: str, signals: tuple[str, ...] | list[str]
) -> str:
    """Structural fingerprint (Wave 9, E1): the CAUSE, never the prose.

    Canonical JSON over (kind, responsibility, mechanism, sorted signals) —
    ``sorted`` makes the caller's signal ORDER irrelevant, and hashing
    structure rather than ``summary`` means two summaries describing the
    same defect (different node id, different wording) collide on purpose,
    while two calls with different evidence (``signals``) never do."""
    basis = json.dumps(
        {
            "kind": kind,
            "responsibility": responsibility,
            "mechanism": mechanism,
            "signals": sorted(str(s) for s in signals),
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class InsightStore:
    """Cross-process store of learnable workflow insights/candidates.

    Owns its own connection (like the audit store): writers are leaf-completion
    threads and, in tests, whole other processes — this store must not convoy
    the main SessionDB lock behind a five-second busy timeout. Reads are
    lock-guarded against the writer connection only; cross-process reads are
    safe under WAL.
    """

    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.executescript(SCHEMA)
        self._add_missing_columns()
        self._connection.commit()

    def _add_missing_columns(self) -> None:
        """Bring a database created before E1 up to the current columns.

        Guarded per column: the ALTER raises OperationalError when the column
        is already there, which is every run after the first (including a
        table freshly created by ``SCHEMA`` above, which already declares
        both columns)."""
        for table, column, decl in _ADDED_COLUMNS:
            try:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass  # already present — the only expected outcome after run one

    # --- write path -------------------------------------------------------

    def record(
        self,
        *,
        kind: str,
        status: str,
        mechanism: Mechanism | str,
        signals: tuple[str, ...] | list[str] = (),
        confidence: float,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        """Store one learnable signal. False when the taxonomy refuses it.

        The verdict is RECOMPUTED here from (mechanism, signals, confidence):
        a caller cannot pass ``responsibility='agency'`` and slip a
        provider outage past the gate — the gate keys on evidence, not on
        trust. Two calls with the same STRUCTURAL cause (kind, mechanism,
        signals, responsibility) are one row whose ``hits`` counter
        increments and whose ``updated_at`` advances; ``summary`` keeps the
        FIRST wording (didactic anchor), ``last_summary`` tracks the most
        recent one — intermediate wordings/node ids are not individually
        kept, only the count and the two summary snapshots. Inserting past
        the cap evicts the oldest rows in the SAME transaction.
        """
        if kind not in (_INSIGHT, _CANDIDATE):
            raise ValueError("kind must be 'insight' or 'candidate'")
        observation = classify_failure(
            status=status,
            mechanism=mechanism,
            signals=signals,
            confidence=confidence,
            summary=summary,
        )
        if not observation.is_learnable:
            return False
        summary_text = str(summary or "")[:MAX_SUMMARY_CHARS]
        # Fingerprint over the CLASSIFIED (clipped, canonicalised) signals —
        # the same bound `classify_failure` already applies to what gets
        # stored, so the hash can never be inflated past what a reader sees.
        fp = _fingerprint(
            kind,
            observation.responsibility.value,
            observation.mechanism.value,
            observation.signals,
        )
        payload_json = None
        if payload is not None:
            payload_json = json.dumps(payload, ensure_ascii=True)[:_MAX_TEXT]
        now = time.time()
        with self._lock:
            try:
                # One short write transaction: fingerprint membership, insert
                # and cap eviction commit together or not at all. BEGIN
                # IMMEDIATE takes SQLite's write lock BEFORE the existence
                # read, so a concurrent process cannot answer "absent" for a
                # row another writer is inserting right now.
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """INSERT INTO workflow_insight_candidates
                       (fingerprint, kind, status, mechanism, responsibility,
                        confidence, summary, last_summary, payload_json, hits,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(fingerprint) DO UPDATE SET
                           hits = COALESCE(hits, 0) + 1,
                           updated_at = excluded.updated_at,
                           last_summary = excluded.last_summary""",
                    (
                        fp,
                        kind,
                        observation.status,
                        observation.mechanism.value,
                        observation.responsibility.value,
                        observation.confidence,
                        summary_text,
                        summary_text,
                        payload_json,
                        now,
                        now,
                    ),
                )
                self._evict_overflow()
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
        return True

    def _evict_overflow(self) -> None:
        """Hard cap, oldest first. Called inside the write transaction."""
        self._connection.execute(
            """DELETE FROM workflow_insight_candidates WHERE fingerprint IN (
                   SELECT fingerprint FROM workflow_insight_candidates
                   ORDER BY updated_at DESC, fingerprint DESC LIMIT -1 OFFSET ?
               )""",
            (MAX_CANDIDATES,),
        )

    # --- read path --------------------------------------------------------

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """Newest-first page of stored signals (bounded by the cap anyway)."""
        with self._lock:
            rows = self._connection.execute(
                """SELECT fingerprint, kind, status, mechanism, responsibility,
                          confidence, summary, last_summary, payload_json, hits,
                          created_at, updated_at
                     FROM workflow_insight_candidates
                    ORDER BY updated_at DESC, fingerprint DESC LIMIT ?""",
                (max(0, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM workflow_insight_candidates"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()
