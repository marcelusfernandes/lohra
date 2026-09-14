"""Atomic functional run transitions, separate from financial ownership fences.

Callers hold only SessionDB's connection lock. Transactions contain local data
work and SQLite, never service callbacks, timers, engine cancellation or cleanup.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal


FINISHED = frozenset({"complete", "degraded", "failed"})
_FIELDS = (
    "name", "owner", "status", "pause_reason", "pause_payload_json", "spec_json",
    "args_json", "token_budget", "tainted", "progress_json", "audit_segment_id",
)


WriteKind = Literal["written", "cancelled", "missing", "finished", "not_paused", "busy", "publication_busy", "conflict", "storage_error"]
WriteMode = Literal["launch", "refuse_launch", "finish", "snapshot"]


@dataclass(frozen=True)
class StateWrite:
    kind: WriteKind
    row: dict[str, Any] | None = None
    fence: int | None = None

    @property
    def accepted(self) -> bool:
        return self.kind in {"written", "cancelled"}

    @property
    def revision(self) -> int:
        return int((self.row or {}).get("revision") or 0)


def _read(connection: sqlite3.Connection, run_id: str) -> tuple[dict | None, int | None]:
    row = connection.execute(
        "SELECT * FROM workflow_run_state WHERE run_id = ?", (run_id,)
    ).fetchone()
    fence = connection.execute(
        "SELECT fence FROM workflow_run_fence WHERE run_id = ?", (run_id,)
    ).fetchone()
    return (dict(row) if row is not None else None, int(fence[0]) if fence else None)


def _put(connection: sqlite3.Connection, run_id: str, fields: dict, now: float, revision: int):
    columns = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    connection.execute(
        f"INSERT OR REPLACE INTO workflow_run_state "
        f"(run_id, {columns}, updated_at, revision) VALUES (?, {placeholders}, ?, ?)",
        (run_id, *(fields.get(key) for key in _FIELDS), now, revision),
    )


def write(
    connection: sqlite3.Connection, run_id: str, fields: dict, now: float, *,
    fence: int | None, mode: WriteMode, expected_revision: int | None,
) -> StateWrite:
    """Commit a launch, functional result, or same-state snapshot.

    Result decisions re-evaluate current status, so unrelated progress does not
    prevent completion. Deferred snapshots must match their captured revision.
    """
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row, current_fence = _read(connection, run_id)
        if fence != current_fence:
            return StateWrite("conflict", row, current_fence)
        if mode in {"launch", "refuse_launch"} and int((row or {}).get("revision") or 0) != expected_revision:
            return StateWrite("conflict", row, current_fence)
        if mode not in {"launch", "refuse_launch"}:
            if row is None:
                return StateWrite("missing", None, current_fence)
            if mode == "finish":
                if row["status"] != "running":
                    kind = "cancelled" if row["status"] == "cancelled" else "finished"
                    return StateWrite(kind, row, current_fence)
            elif mode == "snapshot":
                if int(row.get("revision") or 0) != expected_revision or row["status"] != fields["status"]:
                    return StateWrite("conflict", row, current_fence)
            else:
                raise ValueError(f"unknown run write mode {mode!r}")
        values = dict(fields)
        if row is not None and mode not in {"launch", "refuse_launch"}:
            # The audit connection owns marker closure. A delayed functional
            # snapshot cannot reinstate a segment that it already closed.
            values["audit_segment_id"] = row["audit_segment_id"]
        revision = int((row or {}).get("revision") or 0) + 1
        _put(connection, run_id, values, now, revision)
        if mode == "refuse_launch":
            # Restore configuration with the same functional receipt, never
            # rewind any usage meter or rewrite a successor's ledger (#138).
            connection.execute(
                "UPDATE workflow_run_spend SET token_budget = ? WHERE run_id = ?",
                (fields["token_budget"], run_id),
            )
        return StateWrite("written", _read(connection, run_id)[0], current_fence)


def cancel(
    connection: sqlite3.Connection, run_id: str, now: float, *, fence: int | None,
    extra_faults: list[str] | None = None, expected_revision: int | None = None,
) -> StateWrite:
    """Decide and patch CURRENT metadata in one transaction; None is ownerless."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row, current_fence = _read(connection, run_id)
        if row is None:
            return StateWrite("missing")
        if fence is not None and fence != current_fence:
            return StateWrite("conflict", row, current_fence)
        if fence is None and connection.execute(
            "SELECT 1 FROM workflow_run_locks WHERE run_id = ? AND expires_at > ?",
            (run_id, now),
        ).fetchone():
            return StateWrite("busy", row, current_fence)
        if row["status"] in FINISHED:
            return StateWrite("finished", row, current_fence)
        if expected_revision is not None and int(row.get("revision") or 0) != expected_revision:
            return StateWrite("conflict", row, current_fence)
        if row["status"] == "cancelled":
            return StateWrite("cancelled", row, current_fence)
        payload = json.loads(row.get("pause_payload_json") or "{}")
        payload = {**payload, "checkpoint": None, "route_fault": None, "resume_at": None}
        if extra_faults:
            payload["prior_faults"] = list(payload.get("prior_faults") or []) + extra_faults
        updated = {
            **row, "status": "cancelled", "pause_reason": None,
            "pause_payload_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        }
        _put(connection, run_id, updated, now, int(row.get("revision") or 0) + 1)
        return StateWrite("cancelled", _read(connection, run_id)[0], current_fence)


def acquire(
    connection: sqlite3.Connection, run_id: str, holder: str, *, now: float,
    ttl_seconds: float, pause_token: tuple[int, int | None] | None = None,
) -> StateWrite:
    """Acquire ownership and, for pause-only launches, validate the prior atomically."""
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        row, fence = _read(connection, run_id)
        if pause_token is not None:
            if row is None:
                return StateWrite("missing")
            if row["status"] != "paused":
                return StateWrite("not_paused", row, fence)
            if (int(row.get("revision") or 0), fence) != pause_token:
                return StateWrite("conflict", row, fence)
        if connection.execute(
            "SELECT 1 FROM workflow_run_locks WHERE run_id = ? AND expires_at > ?",
            (run_id, now),
        ).fetchone():
            return StateWrite("busy", row, fence)
        connection.execute(
            "DELETE FROM workflow_run_locks WHERE run_id = ? AND expires_at <= ?", (run_id, now)
        )
        connection.execute(
            "INSERT INTO workflow_run_locks (run_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
            (run_id, holder, now, now + ttl_seconds),
        )
        connection.execute(
            "INSERT OR IGNORE INTO workflow_run_fence (run_id, fence, updated_at) VALUES (?, 0, ?)",
            (run_id, now),
        )
        connection.execute(
            "UPDATE workflow_run_fence SET fence = fence + 1, updated_at = ? WHERE run_id = ?",
            (now, run_id),
        )
        return StateWrite("written", row, (fence or 0) + 1)
