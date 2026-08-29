"""SessionDB — SQLite persistence for sessions and messages (spec §1).

Stores the canonical schema (sessions + messages + state_meta) so later phases
can extend without migrations; the Phase 2 API surface persists and recovers
chat/tool history and walks the ``parent_session_id`` lineage. FTS5 search and
compression locks land with Phase 4/5.

WAL is used where available (concurrent reads during a write), with a DELETE
fallback for filesystems that reject it (NFS/SMB/FUSE).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any

from lohra.state.audit import SCHEMA as AUDIT_SCHEMA
from lohra.state.audit import append as audit_store_append
from lohra.state.audit import events as audit_store_events
from lohra.state.audit_query import query as audit_store_query

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
    model TEXT, model_config TEXT, system_prompt TEXT,
    parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    cwd TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
    title TEXT, api_call_count INTEGER DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
    reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
    platform_message_id TEXT, observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS compression_locks (
    session_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
    acquired_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_node_cache (
    content_hash TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    output_json  TEXT,
    status       TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (run_id, content_hash)
);
CREATE TABLE IF NOT EXISTS workflow_node_cost (
    run_id       TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens   INTEGER DEFAULT 0,
    PRIMARY KEY (run_id, content_hash)
);
CREATE TABLE IF NOT EXISTS workflow_run_spend (
    run_id       TEXT PRIMARY KEY,
    token_budget INTEGER,
    tokens_in    INTEGER NOT NULL DEFAULT 0,
    tokens_out   INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens   INTEGER DEFAULT 0,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_run_state (
    run_id       TEXT PRIMARY KEY,
    name         TEXT,
    owner        TEXT,
    status       TEXT NOT NULL,
    pause_reason TEXT,
    pause_payload_json TEXT,
    spec_json    TEXT,
    args_json    TEXT,
    token_budget INTEGER,
    tainted      INTEGER NOT NULL DEFAULT 0,
    progress_json TEXT,
    audit_segment_id TEXT,
    updated_at   REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_run_locks (
    run_id TEXT PRIMARY KEY, holder TEXT NOT NULL,
    acquired_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, active, id);
CREATE INDEX IF NOT EXISTS idx_wnc_content ON workflow_node_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_wnc_run ON workflow_node_cache(run_id);
CREATE INDEX IF NOT EXISTS idx_wrs_updated ON workflow_run_state(updated_at);
""" + AUDIT_SCHEMA

# Columns added to a table that already ships in the wild. SQLite has no
# "ADD COLUMN IF NOT EXISTS" and this project has no migration framework (every
# other change so far was a NEW table, which CREATE TABLE IF NOT EXISTS covers).
# An ADD COLUMN that already ran raises OperationalError — that IS the
# idempotence check. Additive and nullable only: an old row reads the new column
# as NULL, which every reader must already handle as "never written".
_ADDED_COLUMNS = (
    ("sessions", "priced_call_count", "INTEGER"),
    ("workflow_run_state", "progress_json", "TEXT"),
    ("workflow_run_state", "audit_segment_id", "TEXT"),
    # Fatia C: the cache/reasoning meters, next to the two the ledgers already
    # had. NULLABLE and additive on purpose — a run recorded before this ships
    # reads them as NULL, which every reader below coalesces to 0.
    ("workflow_node_cost", "cache_read_tokens", "INTEGER DEFAULT 0"),
    ("workflow_node_cost", "cache_write_tokens", "INTEGER DEFAULT 0"),
    ("workflow_node_cost", "reasoning_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "cache_read_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "cache_write_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "reasoning_tokens", "INTEGER DEFAULT 0"),
)

_LINEAGE_CAP = 100

# FTS5 full-text index over messages (content + tool_name + tool_calls), kept in
# sync by an insert trigger. Standalone (not external-content) so it survives a
# sqlite build with FTS5 but degrades when FTS5 is absent.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content, session_id UNINDEXED, message_id UNINDEXED
);
CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(content, session_id, message_id)
    VALUES (
        COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' '
            || COALESCE(new.tool_calls, ''),
        new.session_id, new.id
    );
END;
"""

_FTS_BACKFILL = """
INSERT INTO messages_fts(content, session_id, message_id)
SELECT COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' '
           || COALESCE(tool_calls, ''),
       session_id, id
FROM messages;
"""

_FTS_SEARCH = """
SELECT messages_fts.session_id AS session_id,
       messages_fts.message_id AS message_id,
       m.role AS role,
       snippet(messages_fts, 0, '[', ']', '…', 12) AS snippet
FROM messages_fts JOIN messages m ON m.id = messages_fts.message_id
WHERE messages_fts MATCH ?
ORDER BY bm25(messages_fts)
LIMIT ?
"""


class SessionDB:
    """Thread-safe SQLite store for sessions and their messages."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # Wait (instead of raising "database is locked") when another process
        # holds the write lock — e.g. a concurrent compaction lock acquisition.
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._set_journal_mode()
        self._connection.executescript(_SCHEMA)
        self._add_missing_columns()
        self._connection.execute(
            "INSERT OR IGNORE INTO state_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.fts_enabled = self._setup_fts()
        self._connection.commit()
        # Audit writes have their own connection/lock and a short busy timeout.
        # A locked audit sink must fail into an explicit gap; it must never hold
        # the general SessionDB lock (and convoy workflow/cache operations) for
        # the main connection's five-second timeout.
        if path == ":memory:":
            self._audit_connection = self._connection
            self._audit_lock = self._lock
        else:
            self._audit_connection = sqlite3.connect(path, check_same_thread=False)
            self._audit_connection.row_factory = sqlite3.Row
            self._audit_connection.execute("PRAGMA busy_timeout=50")
            self._audit_lock = threading.RLock()

    def _add_missing_columns(self) -> None:
        """Bring a database created by an older Lohra up to the current columns.

        Guarded per column: the ALTER raises OperationalError when the column is
        already there, which is every run after the first."""
        for table, column, decl in _ADDED_COLUMNS:
            try:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            except sqlite3.OperationalError:
                pass  # already present — the only expected outcome after run one

    def _set_journal_mode(self) -> None:
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            self._connection.execute("PRAGMA journal_mode=DELETE")

    def _setup_fts(self) -> bool:
        """Create the FTS5 index + insert trigger; backfill existing rows once.

        Degrades gracefully (returns False) when the sqlite build lacks FTS5.
        """
        try:
            self._connection.executescript(_FTS_SCHEMA)
        except sqlite3.OperationalError:
            return False
        indexed = self._connection.execute("SELECT count(*) FROM messages_fts").fetchone()[0]
        if indexed == 0:
            self._connection.execute(_FTS_BACKFILL)  # one-time for pre-existing messages
        return True

    # --- sessions ---

    def create_session(
        self,
        session_id: str,
        *,
        source: str = "cli",
        model: str | None = None,
        system_prompt: str | None = None,
        parent_session_id: str | None = None,
        cwd: str | None = None,
        title: str | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """INSERT INTO sessions
                   (id, source, model, system_prompt, parent_session_id, started_at, cwd, title)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, source, model, system_prompt, parent_session_id, time.time(), cwd, title),
            )
            self._connection.commit()

    def end_session(self, session_id: str, reason: str) -> None:
        with self._lock:
            self._connection.execute(
                "UPDATE sessions SET ended_at = ?, end_reason = ? WHERE id = ?",
                (time.time(), reason, session_id),
            )
            self._connection.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_sessions(self, *, limit: int = 50, include_archived: bool = False) -> list[dict[str, Any]]:
        """Recent sessions, newest first (summary columns)."""
        query = (
            "SELECT id, title, model, parent_session_id, started_at, ended_at, "
            "end_reason, message_count FROM sessions"
        )
        # Orchestration sub-sessions are internal scaffolding the agent spawns
        # (spawn_session / delegate_task) — keep them out of the user-facing list.
        clauses = ["source != 'orchestration'"]
        if not include_archived:
            clauses.append("archived = 0")
        query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at DESC LIMIT ?"
        with self._lock:
            rows = self._connection.execute(query, (limit,)).fetchall()
        return [dict(row) for row in rows]

    def cache_get(self, run_id: str, content_hash: str) -> dict[str, Any] | None:
        """Workflow node cache lookup, scoped to the run (cross-run reuse OFF,
        spec §6.3). Returns {status, output_json} or None."""
        with self._lock:
            row = self._connection.execute(
                "SELECT status, output_json FROM workflow_node_cache "
                "WHERE run_id = ? AND content_hash = ?",
                (run_id, content_hash),
            ).fetchone()
        return dict(row) if row is not None else None

    def cache_put(
        self, run_id: str, content_hash: str, node_id: str, output_json: str | None, status: str
    ) -> None:
        """Upsert a workflow node-cache cell (single-winner via PK; spec §6.2)."""
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO workflow_node_cache "
                "(content_hash, run_id, node_id, output_json, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (content_hash, run_id, node_id, output_json, status, time.time()),
            )
            self._connection.commit()

    # --- workflow token accounting (spec §7.1; sidecar tables, no migration) ---

    def cache_cost_put(
        self,
        run_id: str,
        content_hash: str,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_read: int = 0,
        cache_write: int = 0,
        reasoning: int = 0,
    ) -> None:
        """Record what one cached cell cost. A sidecar row rather than columns on
        workflow_node_cache: the schema script only ever CREATEs, so widening an
        existing table would need the ALTER this store has never had. A cell
        cached before M5 simply has no row here and reads as 0.

        The cache/reasoning meters (Fatia C) ride the same row, keyword-only and
        defaulted so a caller that only knows in/out still writes a valid line.

        WARNING for a future second caller: this is an INSERT OR **REPLACE**, so
        omitting those keywords does not leave the stored split alone — it
        rewrites the whole row and zeroes it. Pass the split you have, even when
        it is all you know."""
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO workflow_node_cost "
                "(run_id, content_hash, tokens_in, tokens_out, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    content_hash,
                    int(tokens_in),
                    int(tokens_out),
                    int(cache_read),
                    int(cache_write),
                    int(reasoning),
                ),
            )
            self._connection.commit()

    def cache_cost_total(self, run_id: str) -> tuple[int, int]:
        """(tokens_in, tokens_out) over every cached cell of this run — the two
        axes the token BUDGET charges, deliberately unchanged by Fatia C."""
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(tokens_in), 0) AS ti, COALESCE(SUM(tokens_out), 0) AS to_ "
                "FROM workflow_node_cost WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return (int(row["ti"]), int(row["to_"])) if row is not None else (0, 0)

    def cache_cost_split(self, run_id: str) -> tuple[int, int, int]:
        """(cache_read, cache_write, reasoning) over this run's cached cells.

        Separate from ``cache_cost_total`` on purpose: that one feeds the budget
        (two axes, unchanged), this one feeds the REPORT. COALESCE because a row
        written before these columns existed reads them as NULL."""
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(cache_read_tokens), 0) AS cr, "
                "COALESCE(SUM(cache_write_tokens), 0) AS cw, "
                "COALESCE(SUM(reasoning_tokens), 0) AS rt "
                "FROM workflow_node_cost WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return (int(row["cr"]), int(row["cw"]), int(row["rt"])) if row is not None else (0, 0, 0)

    def run_spend_get(self, run_id: str) -> dict[str, Any] | None:
        """The run-level token ledger: {token_budget, tokens_in, tokens_out} plus
        the cache/reasoning meters (NULL on a line written before Fatia C —
        every reader coalesces)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT token_budget, tokens_in, tokens_out, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens FROM workflow_run_spend "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def run_spend_put(
        self,
        run_id: str,
        token_budget: int | None,
        tokens_in: int,
        tokens_out: int,
        *,
        cache_read: int = 0,
        cache_write: int = 0,
        reasoning: int = 0,
    ) -> None:
        """Upsert the run-level ledger, so a resume (even in a fresh process)
        starts from what the run already spent instead of from zero.

        REPLACE, like ``cache_cost_put``: a caller that omits the split keywords
        overwrites the stored meters with zeros rather than preserving them."""
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO workflow_run_spend "
                "(run_id, token_budget, tokens_in, tokens_out, cache_read_tokens, "
                "cache_write_tokens, reasoning_tokens, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    token_budget,
                    int(tokens_in),
                    int(tokens_out),
                    int(cache_read),
                    int(cache_write),
                    int(reasoning),
                    time.time(),
                ),
            )
            self._connection.commit()

    # --- workflow durable run state + lease (WF-29; no migration, new tables) ---

    def run_state_put(self, run_id: str, fields: dict[str, Any], now: float) -> None:
        """Upsert the run's durable line — what a resume in a FRESH process needs.

        One row per run, last write wins (the run's owner is the only writer,
        arbitrated by the lease below). ``fields`` carries the already-encoded
        columns, so this layer stays what every other store method here is: a
        parameterised statement over values somebody else made storable."""
        with self._lock:
            self._connection.execute(
                "INSERT OR REPLACE INTO workflow_run_state "
                "(run_id, name, owner, status, pause_reason, pause_payload_json, "
                "spec_json, args_json, token_budget, tainted, progress_json, "
                "audit_segment_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    fields.get("name"),
                    fields.get("owner"),
                    fields.get("status"),
                    fields.get("pause_reason"),
                    fields.get("pause_payload_json"),
                    fields.get("spec_json"),
                    fields.get("args_json"),
                    fields.get("token_budget"),
                    1 if fields.get("tainted") else 0,
                    fields.get("progress_json"),
                    fields.get("audit_segment_id"),
                    now,
                ),
            )
            self._connection.commit()

    def run_state_get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_run_state WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def run_state_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """The most recently touched runs, newest first — the durable half of
        ``workflow_list``."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workflow_run_state ORDER BY updated_at DESC LIMIT ?",
                (max(0, limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def run_state_by_pause(self, pause_reason: str, limit: int = 50) -> list[dict[str, Any]]:
        """Paused runs waiting on one reason — what a cold start re-arms."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM workflow_run_state WHERE status = 'paused' AND "
                "pause_reason = ? ORDER BY updated_at DESC LIMIT ?",
                (pause_reason, max(0, limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def acquire_run_lease(
        self, run_id: str, holder: str, *, ttl_seconds: float, now: float
    ) -> bool:
        """Claim the run's lease. False when somebody else holds a LIVE one.

        Same contract as ``acquire_compression_lock``: the PRIMARY KEY is the
        sole arbiter of single-winner across processes, and the DELETE ahead of
        it only clears a lease whose holder died without releasing. ``now`` is
        passed in so the whole policy is testable without sleeping."""
        with self._lock:
            try:
                self._connection.execute(
                    "DELETE FROM workflow_run_locks WHERE run_id = ? AND expires_at <= ?",
                    (run_id, now),
                )
                self._connection.execute(
                    "INSERT INTO workflow_run_locks "
                    "(run_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (run_id, holder, now, now + ttl_seconds),
                )
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                self._connection.rollback()
                return False
            self._connection.commit()
            return True

    def renew_run_lease(
        self, run_id: str, holder: str, *, ttl_seconds: float, now: float
    ) -> bool:
        """Push our own lease out. False when it is not ours (or is gone).

        Contention is swallowed like a lock release is: the TTL is the safety
        net, and this runs on leaf-completion threads that must never die for a
        bookkeeping write."""
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "UPDATE workflow_run_locks SET expires_at = ? "
                    "WHERE run_id = ? AND holder = ?",
                    (now + ttl_seconds, run_id, holder),
                )
                self._connection.commit()
            except sqlite3.OperationalError:
                return False
            return cursor.rowcount > 0

    def release_run_lease(self, run_id: str, holder: str) -> bool:
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "DELETE FROM workflow_run_locks WHERE run_id = ? AND holder = ?",
                    (run_id, holder),
                )
                self._connection.commit()
            except sqlite3.OperationalError:
                return False
            return cursor.rowcount > 0

    def run_lease_expiry(self, run_id: str, now: float) -> float | None:
        """When the LIVE lease on this run expires, or None when nobody holds
        one (an expired lease is nobody's — its holder is gone)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT expires_at FROM workflow_run_locks WHERE run_id = ? AND expires_at > ?",
                (run_id, now),
            ).fetchone()
        return float(row["expires_at"]) if row is not None else None

    def lineage_root_to_tip(self, session_id: str) -> list[str]:
        """Walk parent_session_id from the given session to the root, cap 100."""
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = session_id
        for _ in range(_LINEAGE_CAP):
            if current is None or current in seen:
                break
            row = self.get_session(current)
            if row is None:
                break
            chain.append(current)
            seen.add(current)
            current = row["parent_session_id"]
        return list(reversed(chain))

    # --- compaction lock (cross-process; spec §1) ---

    def acquire_compression_lock(
        self, session_id: str, holder: str, *, ttl_seconds: float = 300.0
    ) -> bool:
        """Claim the session's compaction lock. Returns False if already held.

        The INSERT against the PRIMARY KEY on session_id is the sole arbiter of
        single-winner across processes (the preceding DELETE is just opportunistic
        cleanup of an expired lease from a crashed holder that never released).
        Lock-table writes that lose a cross-process race surface as IntegrityError
        (a live lock exists) or, under contention past the busy_timeout, as
        OperationalError; both mean "not ours" — back off cleanly, never crash.
        """
        now = time.time()
        with self._lock:
            try:
                self._connection.execute(
                    "DELETE FROM compression_locks WHERE session_id = ? AND expires_at <= ?",
                    (session_id, now),
                )
                self._connection.execute(
                    "INSERT INTO compression_locks "
                    "(session_id, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                    (session_id, holder, now, now + ttl_seconds),
                )
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                self._connection.rollback()
                return False
            self._connection.commit()
            return True

    def release_compression_lock(self, session_id: str, holder: str) -> bool:
        """Release a lock the given holder owns. Returns False if it didn't own it.

        A contended release (OperationalError) is swallowed — the TTL lease is the
        safety net, so a failed release self-heals on expiry rather than crashing.
        """
        with self._lock:
            try:
                cursor = self._connection.execute(
                    "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
                    (session_id, holder),
                )
                self._connection.commit()
            except sqlite3.OperationalError:
                return False
            return cursor.rowcount > 0

    # --- messages ---


    def session_add_usage(
        self, session_id: str, usage, *, real_usd=None, gross_usd=None, api_calls: int = 1
    ) -> None:
        """Acumula o usage de UM turno na linha da sessão (colunas da Fase 2/3,
        escritas pela primeira vez na Fatia de custo por sessão).

        Semântica: soma no momento do gasto — o preço usado é o do turno, nunca
        recalculado depois. ``actual_cost_usd`` acumula o custo REAL;
        ``estimated_cost_usd`` acumula o BRUTO como-se-sem-cache (o nome vem do
        schema original; o docstring é o contrato). COALESCE cobre linhas
        antigas com as colunas ainda NULL."""
        with self._lock:
            self._connection.execute(
                """UPDATE sessions SET
                     input_tokens = COALESCE(input_tokens, 0) + ?,
                     output_tokens = COALESCE(output_tokens, 0) + ?,
                     cache_read_tokens = COALESCE(cache_read_tokens, 0) + ?,
                     cache_write_tokens = COALESCE(cache_write_tokens, 0) + ?,
                     reasoning_tokens = COALESCE(reasoning_tokens, 0) + ?,
                     api_call_count = COALESCE(api_call_count, 0) + ?,
                     priced_call_count = COALESCE(priced_call_count, 0) +
                       CASE WHEN ? IS NULL THEN 0 ELSE ? END,
                     actual_cost_usd = CASE WHEN ? IS NULL THEN actual_cost_usd
                       ELSE COALESCE(actual_cost_usd, 0) + ? END,
                     estimated_cost_usd = CASE WHEN ? IS NULL THEN estimated_cost_usd
                       ELSE COALESCE(estimated_cost_usd, 0) + ? END
                   WHERE id = ?""",
                (
                    getattr(usage, "input_tokens", 0) or 0,
                    getattr(usage, "output_tokens", 0) or 0,
                    getattr(usage, "cache_read_tokens", 0) or 0,
                    getattr(usage, "cache_write_tokens", 0) or 0,
                    getattr(usage, "reasoning_tokens", 0) or 0,
                    max(1, int(api_calls or 1)), real_usd, max(1, int(api_calls or 1)),
                    real_usd, real_usd, gross_usd, gross_usd,
                    session_id,
                ),
            )
            self._connection.commit()

    def session_usage(self, session_id: str) -> dict | None:
        """O acumulado da sessão (tokens com split + custos), ou None."""
        with self._lock:
            row = self._connection.execute(
                """SELECT input_tokens, output_tokens, cache_read_tokens,
                          cache_write_tokens, reasoning_tokens, api_call_count,
                          priced_call_count, actual_cost_usd, estimated_cost_usd
                     FROM sessions WHERE id = ?""",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        keys = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens", "api_call_count",
                "priced_call_count", "actual_cost_usd", "estimated_cost_usd")
        return dict(zip(keys, row))

    def save_message(self, session_id: str, message: dict[str, Any]) -> int:
        tool_calls = message.get("tool_calls")
        provider_data = message.get("provider_data")
        with self._lock:
            cursor = self._connection.execute(
                """INSERT INTO messages
                   (session_id, role, content, tool_call_id, tool_calls, tool_name,
                    timestamp, finish_reason, reasoning, reasoning_details, active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    session_id,
                    message.get("role", ""),
                    message.get("content"),
                    message.get("tool_call_id"),
                    json.dumps(tool_calls) if tool_calls else None,
                    message.get("name"),
                    time.time(),
                    message.get("finish_reason"),
                    message.get("reasoning"),
                    json.dumps(provider_data) if provider_data else None,
                ),
            )
            self._connection.execute(
                "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
                (session_id,),
            )
            self._connection.commit()
            return int(cursor.lastrowid or 0)

    def load_messages(self, session_id: str, *, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM messages WHERE session_id = ?"
        if active_only:
            query += " AND active = 1"
        query += " ORDER BY id"
        with self._lock:
            rows = self._connection.execute(query, (session_id,)).fetchall()
        return [_reconstruct_message(row) for row in rows]

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """DISCOVERY: full-text search across messages, BM25-ranked (spec §1).

        Returns hits with session_id, message_id, role and a snippet. Empty list
        when FTS5 is unavailable or the query is malformed FTS5 syntax.
        """
        if not self.fts_enabled or not query.strip():
            return []
        with self._lock:
            try:
                rows = self._connection.execute(_FTS_SEARCH, (query, limit)).fetchall()
            except sqlite3.OperationalError:
                return []  # malformed FTS query — treat as no results
        return [dict(row) for row in rows]

    def audit_append(
        self, event: dict[str, Any], *, now: float, max_events: int,
        max_runs: int, retention_seconds: float,
    ) -> int:
        # Defense in depth: callers cannot bypass the metadata-only boundary by
        # reaching around AuditTrail and writing a hand-crafted event directly.
        from lohra.workflow.audit import sanitize_audit_event

        safe_event = sanitize_audit_event(event)
        return audit_store_append(
            self._audit_connection, self._audit_lock, safe_event, now=now,
            max_events=max_events, max_runs=max_runs,
            retention_seconds=retention_seconds,
        )

    def audit_events(self, run_id: str) -> list[dict[str, Any]]:
        return audit_store_events(self._audit_connection, self._audit_lock, run_id)

    def audit_query(self, run_id: str, **filters: Any) -> dict[str, Any]:
        """Read one bounded audit page without constructing a provider client."""
        return audit_store_query(
            self._audit_connection, self._audit_lock, run_id, **filters
        )

    def close(self) -> None:
        if self._audit_connection is not self._connection:
            with self._audit_lock:
                self._audit_connection.close()
        with self._lock:
            self._connection.close()


def _reconstruct_message(row: sqlite3.Row) -> dict[str, Any]:
    """Rebuild the in-memory message dict the loop produced (role-aware shape)."""
    role = row["role"]
    if role == "user":
        return {"role": "user", "content": row["content"]}
    if role == "tool":
        return {
            "role": "tool",
            "name": row["tool_name"],
            "tool_call_id": row["tool_call_id"],
            "content": row["content"],
        }
    if role == "assistant":
        message: dict[str, Any] = {
            "role": "assistant",
            "content": row["content"] or "",
            "finish_reason": row["finish_reason"],
        }
        if row["reasoning"]:
            message["reasoning"] = row["reasoning"]
        if row["tool_calls"]:
            message["tool_calls"] = json.loads(row["tool_calls"])
        if row["reasoning_details"]:
            message["provider_data"] = json.loads(row["reasoning_details"])
        return message
    return {"role": role, "content": row["content"]}
