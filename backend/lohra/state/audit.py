"""SQLite persistence for the bounded workflow audit ledger."""

from __future__ import annotations

import json
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_audit_order (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    next_value INTEGER NOT NULL
);
INSERT OR IGNORE INTO workflow_audit_order (singleton, next_value) VALUES (1, 1);
CREATE TABLE IF NOT EXISTS workflow_audit_state (
    run_id TEXT PRIMARY KEY, next_seq INTEGER NOT NULL DEFAULT 1,
    touch_order INTEGER NOT NULL, retained_events INTEGER NOT NULL DEFAULT 0,
    retention_dropped INTEGER NOT NULL DEFAULT 0, dropped_before_seq INTEGER,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_audit_tombstones (
    run_id TEXT PRIMARY KEY, reason TEXT NOT NULL,
    next_seq INTEGER NOT NULL, evicted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_audit_events (
    run_id TEXT NOT NULL, seq INTEGER NOT NULL, segment_id TEXT, node_id TEXT,
    sub_id TEXT, event_type TEXT NOT NULL, provenance TEXT NOT NULL,
    payload_json TEXT NOT NULL, created_at REAL NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_wae_run_node ON workflow_audit_events(run_id, node_id, seq);
CREATE INDEX IF NOT EXISTS idx_wae_run_sub ON workflow_audit_events(run_id, sub_id, seq);
CREATE INDEX IF NOT EXISTS idx_was_updated ON workflow_audit_state(updated_at);
"""


def append(
    connection: Any,
    lock: Any,
    event: dict[str, Any],
    *,
    now: float,
    max_events: int,
    max_runs: int,
    retention_seconds: float,
) -> int:
    """Append one bounded audit event and return its durable per-run sequence.

    Sequence allocation, append and per-run pruning share one SQLite
    transaction.  A crash therefore commits all three or none; sequence
    numbers are never reserved outside the durable write.
    """
    identity = event.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("audit event identity must be an object")
    run_id = identity.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("audit event requires run_id")
    event_type = event.get("event_type")
    provenance = event.get("provenance")
    if not isinstance(event_type, str) or not isinstance(provenance, str):
        raise ValueError("audit event requires event_type and provenance")
    payload = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
    cutoff = now - max(1.0, retention_seconds)
    limit = max(1, int(max_events))
    with lock:
        try:
            prior = connection.execute(
                "SELECT next_seq FROM workflow_audit_tombstones WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            compacted = connection.execute(
                "SELECT 1 FROM workflow_audit_tombstones WHERE run_id = '$compacted'"
            ).fetchone()
            compacted_unknown = prior is None and compacted is not None
            prior_next_seq = int(prior[0]) if prior is not None else (2 if compacted_unknown else 1)
            connection.execute(
                "DELETE FROM workflow_audit_tombstones WHERE run_id = ?", (run_id,)
            )
            touch_row = connection.execute(
                "SELECT next_value FROM workflow_audit_order WHERE singleton = 1"
            ).fetchone()
            touch_order = int(touch_row[0])
            connection.execute(
                "UPDATE workflow_audit_order SET next_value = ? WHERE singleton = 1",
                (touch_order + 1,),
            )
            connection.execute(
                """INSERT OR IGNORE INTO workflow_audit_state
                   (run_id, next_seq, touch_order, retained_events,
                    retention_dropped, dropped_before_seq, updated_at)
                   VALUES (?, ?, ?, 0, ?, ?, ?)""",
                (
                    run_id, prior_next_seq, touch_order,
                    -1 if compacted_unknown else prior_next_seq - 1,
                    prior_next_seq if prior_next_seq > 1 else None,
                    now,
                ),
            )
            expired = connection.execute(
                """SELECT COUNT(*), MAX(seq) FROM workflow_audit_events
                   WHERE run_id = ? AND created_at < ?""",
                (run_id, cutoff),
            ).fetchone()
            expired_count = int(expired[0] or 0)
            if expired_count:
                # Wall clock is not causal order.  If an old timestamp appears
                # in the middle, remove the whole sequence prefix through it;
                # the reader can then place one honest prefix gap.
                expired_max_seq = int(expired[1] or 0)
                prefix = connection.execute(
                    """SELECT COUNT(*) FROM workflow_audit_events
                       WHERE run_id = ? AND seq <= ?""",
                    (run_id, expired_max_seq),
                ).fetchone()
                prefix_count = int(prefix[0] or 0)
                connection.execute(
                    "DELETE FROM workflow_audit_events WHERE run_id = ? AND seq <= ?",
                    (run_id, expired_max_seq),
                )
                connection.execute(
                    """UPDATE workflow_audit_state
                       SET retained_events = MAX(0, retained_events - ?),
                           retention_dropped = CASE
                               WHEN retention_dropped < 0 THEN -1
                               ELSE retention_dropped + ? END,
                           dropped_before_seq = MAX(COALESCE(dropped_before_seq, 0), ?)
                       WHERE run_id = ?""",
                    (prefix_count, prefix_count, expired_max_seq + 1, run_id),
                )
            row = connection.execute(
                "SELECT next_seq FROM workflow_audit_state WHERE run_id = ?", (run_id,)
            ).fetchone()
            seq = int(row[0])
            node_path = identity.get("node_path")
            node_id = node_path[-1] if isinstance(node_path, list) and node_path else None
            connection.execute(
                """INSERT INTO workflow_audit_events
                   (run_id, seq, segment_id, node_id, sub_id, event_type, provenance,
                    payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, seq, identity.get("segment_id"), node_id,
                    identity.get("sub_id"), event_type, provenance, payload, now,
                ),
            )
            connection.execute(
                """UPDATE workflow_audit_state
                   SET next_seq = ?, touch_order = ?, retained_events = retained_events + 1,
                       updated_at = ? WHERE run_id = ?""",
                (seq + 1, touch_order, now, run_id),
            )
            if event_type == "segment.completed":
                connection.execute(
                    """UPDATE workflow_run_state SET audit_segment_id = NULL
                       WHERE run_id = ? AND audit_segment_id = ?""",
                    (run_id, identity.get("segment_id")),
                )
            count_row = connection.execute(
                "SELECT retained_events FROM workflow_audit_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            excess = max(0, int(count_row[0]) - limit)
            if excess:
                removed = connection.execute(
                    """SELECT MAX(seq) FROM (
                           SELECT seq FROM workflow_audit_events
                           WHERE run_id = ? ORDER BY seq LIMIT ?
                       )""",
                    (run_id, excess),
                ).fetchone()
                before_seq = int(removed[0] or 0) + 1
                connection.execute(
                    """DELETE FROM workflow_audit_events WHERE run_id = ? AND seq IN (
                           SELECT seq FROM workflow_audit_events
                           WHERE run_id = ? ORDER BY seq LIMIT ?
                       )""",
                    (run_id, run_id, excess),
                )
                connection.execute(
                    """UPDATE workflow_audit_state
                       SET retained_events = retained_events - ?,
                           retention_dropped = CASE
                               WHEN retention_dropped < 0 THEN -1
                               ELSE retention_dropped + ? END,
                           dropped_before_seq = MAX(COALESCE(dropped_before_seq, 0), ?)
                       WHERE run_id = ?""",
                    (excess, excess, before_seq, run_id),
                )
            time_stale = connection.execute(
                "SELECT run_id, next_seq FROM workflow_audit_state WHERE updated_at < ?",
                (cutoff,),
            ).fetchall()
            for stale_row in time_stale:
                stale_id = str(stale_row[0])
                connection.execute(
                    "DELETE FROM workflow_audit_events WHERE run_id = ?", (stale_id,)
                )
                connection.execute(
                    "DELETE FROM workflow_audit_state WHERE run_id = ?", (stale_id,)
                )
                connection.execute(
                    """INSERT OR REPLACE INTO workflow_audit_tombstones
                       (run_id, reason, next_seq, evicted_at)
                       VALUES (?, 'time_retention', ?, ?)""",
                    (stale_id, int(stale_row[1]), now),
                )
            stale = connection.execute(
                """SELECT run_id, next_seq FROM workflow_audit_state
                   ORDER BY touch_order DESC, run_id DESC LIMIT -1 OFFSET ?""",
                (max(1, int(max_runs)),),
            ).fetchall()
            for stale_row in stale:
                stale_id = str(stale_row[0])
                connection.execute(
                    "DELETE FROM workflow_audit_events WHERE run_id = ?", (stale_id,)
                )
                connection.execute(
                    "DELETE FROM workflow_audit_state WHERE run_id = ?", (stale_id,)
                )
                connection.execute(
                    """INSERT OR REPLACE INTO workflow_audit_tombstones
                       (run_id, reason, next_seq, evicted_at)
                       VALUES (?, 'run_retention_limit', ?, ?)""",
                    (stale_id, int(stale_row[1]), now),
                )
            tombstone_limit = max(1, int(max_runs))
            compacted_rows = connection.execute(
                """SELECT run_id FROM workflow_audit_tombstones
                   WHERE run_id != '$compacted'
                   ORDER BY evicted_at DESC, run_id DESC LIMIT -1 OFFSET ?""",
                (tombstone_limit,),
            ).fetchall()
            if compacted_rows:
                connection.executemany(
                    "DELETE FROM workflow_audit_tombstones WHERE run_id = ?",
                    compacted_rows,
                )
                connection.execute(
                    """INSERT OR REPLACE INTO workflow_audit_tombstones
                       (run_id, reason, next_seq, evicted_at)
                       VALUES ('$compacted', 'tombstone_compaction', 1, ?)""",
                    (now,),
                )
            connection.commit()
            return seq
        except Exception:
            connection.rollback()
            raise


def events(connection: Any, lock: Any, run_id: str) -> list[dict[str, Any]]:
    """Read a run's audit sequence, making retention/corruption explicit."""
    with lock:
        state = connection.execute(
            """SELECT retention_dropped, dropped_before_seq
               FROM workflow_audit_state WHERE run_id = ?""",
            (run_id,),
        ).fetchone()
        rows = connection.execute(
            """SELECT seq, payload_json FROM workflow_audit_events
               WHERE run_id = ? ORDER BY seq""",
            (run_id,),
        ).fetchall()
        tombstone = connection.execute(
            "SELECT reason FROM workflow_audit_tombstones WHERE run_id = ?", (run_id,)
        ).fetchone()
    if state is None and tombstone is not None:
        return [
            {
                "schema_version": 1,
                "event_type": "audit.unavailable",
                "provenance": "unavailable",
                "identity": {"run_id": run_id},
                "data": {"reason": str(tombstone[0])},
            }
        ]
    events: list[dict[str, Any]] = []
    if state is not None and int(state[0] or 0):
        dropped = int(state[0])
        data: dict[str, Any] = {
            "reason": "tombstone_compaction" if dropped < 0 else "retention_limit",
            "dropped_count": None if dropped < 0 else dropped,
            "before_seq": int(state[1] or 0),
        }
        if dropped < 0:
            data["count_state"] = "unavailable"
        events.append(
            {
                "schema_version": 1,
                "event_type": "audit.gap",
                "provenance": "dropped",
                "identity": {"run_id": run_id},
                "data": data,
            }
        )
    for row in rows:
        seq = int(row[0])
        try:
            event = json.loads(row[1])
            if not isinstance(event, dict):
                raise ValueError("audit payload is not an object")
            events.append({**event, "seq": seq})
        except (TypeError, ValueError, json.JSONDecodeError):
            events.append(
                {
                    "schema_version": 1,
                    "event_type": "audit.unavailable",
                    "provenance": "unavailable",
                    "seq": seq,
                    "identity": {"run_id": run_id},
                    "data": {"reason": "corrupt_payload"},
                }
            )
    return events

