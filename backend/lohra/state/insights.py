"""Durable cross-process store for workflow insights/candidates (SUP-05, slice 1).

A SQLite table in the shared SessionDB file holds the IMMEDIATE signals a
supervising session may learn from — the analog of ``workflow/insights.md``
(library.py) but queryable from ANY process, including sessions that did not
run the workflow.

Invariants (each enforced at the write boundary, not by convention):

- **learnable gate** — only ``FailureObservation.is_learnable`` observations
  enter; the store re-classifies from raw fields rather than trusting a
  caller-supplied verdict (fail-closed against a lying caller);
- **semantic dedup** — one fingerprint per (kind, responsibility, mechanism,
  normalized text). Two writers describing the same lesson in different
  words land once, because the fingerprint is content-addressed and the PK
  arbitrates the winner (``INSERT OR IGNORE``);
- **cap 200** — hardest bound, oldest-first eviction inside the same
  transaction as the insert (no window where the table is unbounded);
- **bounded text** — text fields clipped at the schema boundary;
- **short transaction** — one ``BEGIN IMMEDIATE`` per write; the read of the
  fingerprint and the INSERT share it, so two processes can never both
  observe "absent" and both win (the pattern ``steering_reserve`` uses).
"""

from __future__ import annotations

import hashlib
import json
import re
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
    payload_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wic_updated ON workflow_insight_candidates(updated_at);
"""

# Hard bounds. The cap mirrors MAX_INSIGHTS in workflow/library.py: machine
# telemetry, newest kept, never unbounded.
MAX_CANDIDATES = 200
MAX_SUMMARY_CHARS = 500
_MAX_TEXT = 2000

_WHITESPACE = re.compile(r"\s+")

_INSIGHT = "insight"
_CANDIDATE = "candidate"


def _normalize(text: str) -> str:
    """Fold a summary into its dedup form: whitespace-collapsed, lowercase."""
    return _WHITESPACE.sub(" ", str(text or "")).strip().lower()


def _fingerprint(kind: str, responsibility: str, mechanism: str, summary: str) -> str:
    basis = "|".join((kind, responsibility, mechanism, _normalize(summary)))
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
        self._connection.commit()

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
        trust. Two calls with the same lesson are one row (dedup); inserting
        past the cap evicts the oldest rows in the SAME transaction.
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
        fp = _fingerprint(
            kind,
            observation.responsibility.value,
            observation.mechanism.value,
            summary_text,
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
                    """INSERT OR IGNORE INTO workflow_insight_candidates
                       (fingerprint, kind, status, mechanism, responsibility,
                        confidence, summary, payload_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        fp,
                        kind,
                        observation.status,
                        observation.mechanism.value,
                        observation.responsibility.value,
                        observation.confidence,
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
                          confidence, summary, payload_json, created_at, updated_at
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
