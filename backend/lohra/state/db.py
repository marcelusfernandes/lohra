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
import logging
import os
import sqlite3
import threading
import time
from typing import Any

from lohra.state.audit import SCHEMA as AUDIT_SCHEMA
from lohra.state.audit import append as audit_store_append
from lohra.state.audit import events as audit_store_events
from lohra.state.audit_query import query as audit_store_query
from lohra.state.insights import InsightStore
from lohra.state.notices import DurableNoticeStore

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Audit connection busy wait (issue #34). 250ms absorbs the transient BUSY of a
# concurrent fan-out inside SQLite itself (its own connection/lock/thread — the
# wait convoys nothing but the sink and its readers), while staying small enough
# that the sink's bounded retry ladder never eats a whole flush() budget. The
# operator can turn it (contention repros use a tiny value); garbage → default.
ENV_AUDIT_BUSY_TIMEOUT = "LOHRA_AUDIT_BUSY_TIMEOUT_MS"
_AUDIT_BUSY_TIMEOUT_DEFAULT_MS = 250


def _audit_busy_timeout_ms() -> int:
    raw = os.environ.get(ENV_AUDIT_BUSY_TIMEOUT)
    if not raw:
        return _AUDIT_BUSY_TIMEOUT_DEFAULT_MS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "ignoring %s=%r: not an integer; using %d",
            ENV_AUDIT_BUSY_TIMEOUT, raw, _AUDIT_BUSY_TIMEOUT_DEFAULT_MS,
        )
        return _AUDIT_BUSY_TIMEOUT_DEFAULT_MS
    return value if value >= 0 else _AUDIT_BUSY_TIMEOUT_DEFAULT_MS

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
    artifact_verification TEXT,
    artifact_json         TEXT,
    policy_hash           TEXT,
    harness_version       TEXT,
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
CREATE TABLE IF NOT EXISTS workflow_run_fence (
    run_id TEXT PRIMARY KEY, fence INTEGER NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflow_steering_budget (
    run_id TEXT PRIMARY KEY,
    used INTEGER NOT NULL CHECK (used >= 0)
);
CREATE TABLE IF NOT EXISTS workflow_route_fallbacks (
    run_id TEXT NOT NULL,
    route_key TEXT NOT NULL,
    used INTEGER NOT NULL CHECK (used >= 0),
    PRIMARY KEY (run_id, route_key)
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
    # Tombstones ganharam next_seq (retomada de numeração pós-evicção); num
    # banco criado antes disso, TODO append de audit falhava com
    # OperationalError "no such column" — mascarado de contenção nos warnings
    # (causa raiz real do issue #34 observado ao vivo). DEFAULT 1 é a
    # semântica legada: sem tombstone prévio, a numeração começa do 1.
    ("workflow_audit_tombstones", "next_seq", "INTEGER NOT NULL DEFAULT 1"),
    # Fatia C: the cache/reasoning meters, next to the two the ledgers already
    # had. NULLABLE and additive on purpose — a run recorded before this ships
    # reads them as NULL, which every reader below coalesces to 0.
    ("workflow_node_cost", "cache_read_tokens", "INTEGER DEFAULT 0"),
    ("workflow_node_cost", "cache_write_tokens", "INTEGER DEFAULT 0"),
    ("workflow_node_cost", "reasoning_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "cache_read_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "cache_write_tokens", "INTEGER DEFAULT 0"),
    ("workflow_run_spend", "reasoning_tokens", "INTEGER DEFAULT 0"),
    # #45 E4: what the HARNESS measured for a cell whose schema is an artifact
    # manifest. Sidecar COLUMNS rather than a payload field on purpose — the
    # measurement must never reach ``output_json``, which is what flows into a
    # downstream ``${ref}``. NULL is the only reading an old row can have, and
    # every reader treats it as "nothing was measured" (replay as before).
    ("workflow_node_cache", "artifact_verification", "TEXT"),
    ("workflow_node_cache", "artifact_json", "TEXT"),
    # #75: under WHAT the cell ran — the operator's effective sandbox policy
    # (a canonical hash, never the paths themselves) and the harness version.
    # Metadata, never part of the key: the owner's decision is to MARK a
    # divergent replay, not to invalidate work the run already paid for. NULL is
    # the only reading an old row can have, and it means UNKNOWN — never
    # "different" — so a cell stored before this shipped replays in silence.
    ("workflow_node_cache", "policy_hash", "TEXT"),
    ("workflow_node_cache", "harness_version", "TEXT"),
)

# How many pre-authorized re-routes ONE dead route may buy inside ONE run (#63).
# Not configurable and not the operator's to raise: a SECOND guess for the same
# dead route is the harness insisting, and every other node still pointed at that
# route needs a spec edit rather than another attempt. The per-RUN ceiling is the
# operator's (``max_fallbacks_per_run`` in ``workflow_routes.json``); this one is
# the doctrine's.
ROUTE_FALLBACKS_PER_ROUTE = 1

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
            self._audit_connection.execute(
                f"PRAGMA busy_timeout={_audit_busy_timeout_ms()}"
            )
            self._audit_lock = threading.RLock()
        # Workflow insight candidates (SUP-05 slice 1): a SEPARATE connection
        # for the same reason the audit sink has one — writers are leaf threads
        # and foreign processes, and this store must not convoy the general
        # SessionDB lock. It shares the SessionDB FILE (one durable home for
        # cross-process state), which is why no engine/gateway wiring is needed
        # for it to be visible everywhere.
        self.insights = InsightStore(path)
        # Fatos operacionais por sessão (SUP-05 fatia 2): mesma lógica de
        # conexão própria do insight store — writers são outros processos e
        # este store não pode convoy o lock geral da SessionDB.
        self.notices = DurableNoticeStore(path)

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

    # --- ownership fencing for workflow runs (issue #12) ------------------
    #
    # The lease says who owns a run; the fence is what makes every write made
    # UNDER that ownership checkable. ``workflow_run_fence`` holds one counter
    # per run that only ever advances, bumped inside the same transaction as the
    # winning lease INSERT — so an acquisition, and only an acquisition, is what
    # moves it. A writer presents the fence it acquired; the write lands only
    # while no HIGHER fence exists.
    #
    # Two alternatives were rejected, for mechanical reasons rather than taste:
    #
    # - **holder token** (``WHERE holder = ?``, the shape ``renew_run_lease``
    #   already uses): the holder identifies a STORE INSTANCE, not an
    #   acquisition — ``RunStateStore`` mints it once per process
    #   (``os.getpid()``+uuid) — so a process that loses a run and later
    #   re-acquires it presents the SAME holder, and a straggler thread from the
    #   earlier stretch passes the check. Worse, ``release_run_lease`` DELETEs
    #   the row, so after a clean release there is no holder left to compare
    #   against at all: the authority has to outlive the lease, which is exactly
    #   why the fence is its own table and not a column on ``workflow_run_locks``;
    # - **drain before release** (finish every worker before handing the lease
    #   back): already done for the CLEAN path (``service._run`` shuts the core
    #   down before releasing) and structurally unable to cover this one — a
    #   process that is frozen, swapped out, or simply inside a node for longer
    #   than the TTL drains nothing, because it is not running. The new owner
    #   arrives by TTL, not by handshake, so the guard has to live at the WRITE,
    #   where the losing process still is.
    #
    # A NULL fence never rejects (``fence > NULL`` is NULL): a database written
    # before this shipped, and every legitimately ownerless path (cancelling a
    # run nobody holds), behave exactly as they did.
    _FENCE_GUARD = (
        " WHERE NOT EXISTS (SELECT 1 FROM workflow_run_fence "
        "WHERE run_id = ? AND fence > ?)"
    )

    # The second guard, for the write nobody makes UNDER ownership: cancelling a
    # run this process only knows from its line. "Nobody holds a live lease" was
    # checked in one transaction and the cancel written in another, so an owner
    # that acquired inside that window got its `running` line replaced by a
    # `cancelled` one while it was still working. Folded into the same statement,
    # the acquisition either wins the row or loses it — never both.
    _UNLEASED_GUARD = (
        " AND NOT EXISTS (SELECT 1 FROM workflow_run_locks "
        "WHERE run_id = ? AND expires_at > ?)"
    )

    def _fenced_write(
        self,
        sql: str,
        values: tuple,
        *,
        run_id: str,
        fence: int | None,
        what: str,
        unleased_at: float | None = None,
    ) -> bool:
        """One write that only lands while this fence is still the run's owner.

        True when it landed. False — with ONE warning naming the run — when a
        newer owner has taken it: the caller degrades (a straggler's bookkeeping
        is dropped), and nothing raises into the pool worker or sink thread it
        was called from. Check and write are a SINGLE statement, so an
        acquisition cannot slip between them.

        ``unleased_at`` adds the second condition to that same statement: the
        write also requires that NOBODY holds a live lease on the run as of that
        clock reading — what an ownerless cancel needs and what it used to check
        one transaction too early."""
        guard, guard_values = self._FENCE_GUARD, (run_id, fence)
        if unleased_at is not None:
            guard += self._UNLEASED_GUARD
            guard_values += (run_id, unleased_at)
        with self._lock:
            cursor = self._connection.execute(sql + guard, (*values, *guard_values))
            self._connection.commit()
            if cursor.rowcount:
                return True
        self._log_refusal(what, run_id, fence)
        return False

    @staticmethod
    def _log_refusal(what: str, run_id: str, fence: int | None) -> None:
        """The one sentence a refused write leaves behind. Never silent, always
        naming the run: a straggler's dropped bookkeeping has to be readable in
        the log of the process that dropped it."""
        logger.warning(
            "workflow: refused a stale %s write for run %s (fence %s) — another "
            "process owns this run now",
            what,
            run_id,
            fence,
        )

    def cache_get(self, run_id: str, content_hash: str) -> dict[str, Any] | None:
        """Workflow node cache lookup, scoped to the run (cross-run reuse OFF,
        spec §6.3). Returns {status, output_json, artifact_verification,
        artifact_json, policy_hash, harness_version} or None.

        The artifact columns ride along rather than being a second SELECT: every
        hit has to ask whether the cell's manifest still describes the
        filesystem (#45 E4), and that question must not cost a second round-trip
        per replay. They are NULL for every cell stored without a manifest. The
        stamp columns (#75) ride for the same reason and answer the same kind of
        question — under what did this cell run — and are NULL for every cell
        stored before they existed."""
        with self._lock:
            row = self._connection.execute(
                "SELECT status, output_json, artifact_verification, artifact_json, "
                "policy_hash, harness_version "
                "FROM workflow_node_cache WHERE run_id = ? AND content_hash = ?",
                (run_id, content_hash),
            ).fetchone()
        return dict(row) if row is not None else None

    def cache_put(
        self,
        run_id: str,
        content_hash: str,
        node_id: str,
        output_json: str | None,
        status: str,
        *,
        fence: int | None = None,
    ) -> bool:
        """Upsert a workflow node-cache cell (single-winner via PK; spec §6.2).

        Fenced (issue #12): this runs on a pipeline pool worker, which is where a
        stale owner's straggling leaf lands its cell. False = refused."""
        return self._fenced_write(
            "INSERT OR REPLACE INTO workflow_node_cache "
            "(content_hash, run_id, node_id, output_json, status, updated_at) "
            "SELECT ?, ?, ?, ?, ?, ?",
            (content_hash, run_id, node_id, output_json, status, time.time()),
            run_id=run_id,
            fence=fence,
            what="node cache",
        )

    _CELL_SQL = (
        "INSERT OR REPLACE INTO workflow_node_cache "
        "(content_hash, run_id, node_id, output_json, status, updated_at, "
        "artifact_verification, artifact_json, policy_hash, harness_version) "
        "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
    )
    _COST_SQL = (
        "INSERT OR REPLACE INTO workflow_node_cost "
        "(run_id, content_hash, tokens_in, tokens_out, cache_read_tokens, "
        "cache_write_tokens, reasoning_tokens) SELECT ?, ?, ?, ?, ?, ?, ?"
    )

    def cache_put_with_cost(
        self,
        run_id: str,
        content_hash: str,
        node_id: str,
        output_json: str | None,
        status: str,
        *,
        cost: tuple[int, int, int, int, int] | None = None,
        fence: int | None = None,
        artifact: tuple[str, str] | None = None,
        stamp: tuple[str | None, str | None] | None = None,
    ) -> bool:
        """The cell AND what it cost, in ONE transaction behind ONE guard.

        ``cost`` is (tokens_in, tokens_out, cache_read, cache_write, reasoning),
        or None for a cell that spent no leaf at all (a human's checkpoint
        answer).

        Two guarded writes in two transactions had a window between them, and it
        was the worst one available: a new owner acquiring there left the cell
        stored and its price refused — a cell that REPLAYS on the next resume
        while charging the token budget nothing for work the run really paid
        for. Priced or absent; never free.

        Only the cell carries the fence guard: the cost rides in the same
        transaction, so it cannot land without a cell that just proved the run
        is still ours. A rollback here undoes both.

        "Both or neither" holds on the EXCEPTION path too, which is the whole
        reason for the rollback below: a cost write that raises (a busy timeout
        under fan-out) would otherwise leave the cell INSERT sitting in an open
        transaction, for the next unrelated write on this connection to commit —
        priceless, and replayable. The raise still propagates: a caller that
        cannot store a cell learns it, exactly as it did before.

        ``artifact`` is ``(verification, manifest_json)`` — what the HARNESS
        measured for a cell declaring an artifact manifest (#45 E4). It rides in
        the cell's OWN insert for the same reason the cost rides in this
        transaction: a cell stored with its verification refused separately is a
        cell that replays unverified for the rest of the run's life. Never part
        of ``output_json``: downstream reads what the leaf said, not what the
        harness found.

        ``stamp`` is ``(policy_hash, harness_version)`` — under WHAT the leaf ran
        (#75). Same transaction, same guard, same reason: a cell stored without
        its stamp is a cell that replays UNKNOWN for the rest of the run's life,
        and unknown is the one reading that can never raise an advisory. ``None``
        (or a None inside it) writes NULL, which is what a cell nobody sandboxed
        — a human's checkpoint answer — honestly is.
        """
        verification, manifest_json = artifact if artifact is not None else (None, None)
        policy_hash, harness_version = stamp if stamp is not None else (None, None)
        with self._lock:
            try:
                cursor = self._connection.execute(
                    self._CELL_SQL + self._FENCE_GUARD,
                    (content_hash, run_id, node_id, output_json, status, time.time(),
                     verification, manifest_json, policy_hash, harness_version,
                     run_id, fence),
                )
                if not cursor.rowcount:
                    self._connection.rollback()
                    self._log_refusal("node cache", run_id, fence)
                    return False
                if cost is not None:
                    self._connection.execute(
                        self._COST_SQL, (run_id, content_hash, *(int(part) for part in cost))
                    )
                self._connection.commit()
            except sqlite3.Error:
                self._connection.rollback()
                raise
        return True

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
        fence: int | None = None,
    ) -> bool:
        """Record what one cached cell cost. A sidecar row rather than columns on
        workflow_node_cache: the schema script only ever CREATEs, so widening an
        existing table would need the ALTER this store has never had. A cell
        cached before M5 simply has no row here and reads as 0.

        The cache/reasoning meters (Fatia C) ride the same row, keyword-only and
        defaulted so a caller that only knows in/out still writes a valid line.

        WARNING for a future second caller: this is an INSERT OR **REPLACE**, so
        omitting those keywords does not leave the stored split alone — it
        rewrites the whole row and zeroes it. Pass the split you have, even when
        it is all you know.

        Fenced like the cell it prices (issue #12): the REPLACE semantics above
        are untouched — the guard decides whether the row is written at all,
        never which of its columns are.

        The live cache path no longer comes through here: a cell and its price
        are ONE transaction (``cache_put_with_cost``), because a priced cell
        whose cost write was refused separately replays for free. This stays for
        callers that price a cell already stored."""
        return self._fenced_write(
            "INSERT OR REPLACE INTO workflow_node_cost "
            "(run_id, content_hash, tokens_in, tokens_out, cache_read_tokens, "
            "cache_write_tokens, reasoning_tokens) SELECT ?, ?, ?, ?, ?, ?, ?",
            (
                run_id,
                content_hash,
                int(tokens_in),
                int(tokens_out),
                int(cache_read),
                int(cache_write),
                int(reasoning),
            ),
            run_id=run_id,
            fence=fence,
            what="cell cost",
        )

    def cache_hashes_for_node(
        self, run_id: str, node_id: str, *, include_fanout: bool = False
    ) -> list[str]:
        """Every content hash this run has cached under ``node_id`` (#44).

        Read-only and cheap (the run index narrows it): what lets a miss say
        whether the node never completed or its identity changed.

        ``include_fanout`` also matches the ``<node>#<item>#<stage>`` rows a
        pipeline writes — it LOOKS UP by the raw node id but STORES under the
        composite one, so an exact match would answer "never completed" for
        every fan-out cell, including one whose identity really moved. ``instr``
        rather than LIKE: a node id may contain ``_``, which LIKE would read as a
        wildcard. The answer only ever supports the WEAK claim (a row may be a
        sibling cell, D6) — the caller says so."""
        clause = "node_id = ?" if not include_fanout else (
            "(node_id = ? OR instr(node_id, ?) = 1)"
        )
        params: tuple[Any, ...] = (
            (run_id, node_id) if not include_fanout else (run_id, node_id, f"{node_id}#")
        )
        with self._lock:
            rows = self._connection.execute(
                f"SELECT content_hash FROM workflow_node_cache WHERE run_id = ? AND {clause}",
                params,
            ).fetchall()
        return [str(row["content_hash"]) for row in rows]

    def cache_cost_of(self, run_id: str, content_hash: str) -> tuple[int, int, int, int, int] | None:
        """What ONE cell cost: (in, out, cache_read, cache_write, reasoning), or
        None when no row prices it (cached before M5, or a checkpoint answer)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT tokens_in, tokens_out, cache_read_tokens, cache_write_tokens, "
                "reasoning_tokens FROM workflow_node_cost "
                "WHERE run_id = ? AND content_hash = ?",
                (run_id, content_hash),
            ).fetchone()
        if row is None:
            return None
        return (
            int(row["tokens_in"] or 0),
            int(row["tokens_out"] or 0),
            int(row["cache_read_tokens"] or 0),
            int(row["cache_write_tokens"] or 0),
            int(row["reasoning_tokens"] or 0),
        )

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
        fence: int | None = None,
    ) -> bool:
        """Upsert the run-level ledger, so a resume (even in a fresh process)
        starts from what the run already spent instead of from zero.

        REPLACE, like ``cache_cost_put``: a caller that omits the split keywords
        overwrites the stored meters with zeros rather than preserving them.

        Fenced (issue #12): a stale owner's final tally is what a later resume
        would otherwise seed the next stretch's budget from."""
        return self._fenced_write(
            "INSERT OR REPLACE INTO workflow_run_spend "
            "(run_id, token_budget, tokens_in, tokens_out, cache_read_tokens, "
            "cache_write_tokens, reasoning_tokens, updated_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?",
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
            run_id=run_id,
            fence=fence,
            what="run ledger",
        )

    # --- workflow durable run state + lease (WF-29; no migration, new tables) ---

    def run_state_put(
        self,
        run_id: str,
        fields: dict[str, Any],
        now: float,
        *,
        fence: int | None = None,
        unleased_at: float | None = None,
    ) -> bool:
        """Upsert the run's durable line — what a resume in a FRESH process needs.

        One row per run, last write wins AMONG THE CURRENT OWNER'S writes. This
        docstring used to claim the owner was the only writer, "arbitrated by
        the lease below"; it was not — the lease arbitrated who may START a run,
        never who may WRITE, so a stale owner's terminal line replaced the new
        owner's (issue #12). ``fence`` is what makes the claim true: the row
        moves for the current acquisition only. ``fields`` carries the
        already-encoded columns, so this layer stays what every other store
        method here is: a parameterised statement over values somebody else made
        storable."""
        return self._fenced_write(
            "INSERT OR REPLACE INTO workflow_run_state "
            "(run_id, name, owner, status, pause_reason, pause_payload_json, "
            "spec_json, args_json, token_budget, tainted, progress_json, "
            "audit_segment_id, updated_at) "
            "SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?",
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
            run_id=run_id,
            fence=fence,
            what="run line",
            unleased_at=unleased_at,
        )

    def run_state_get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM workflow_run_state WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def run_state_ids_by_prefix(self, prefix: str, limit: int = 10) -> list[str]:
        """Run ids starting with ``prefix``, newest first — how the short id the
        listing prints resolves back to a full run (issue #24). LIKE wildcards
        in the prefix are escaped: '%'/'_' are literals here, never patterns."""
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._lock:
            rows = self._connection.execute(
                "SELECT run_id FROM workflow_run_state WHERE run_id LIKE ? ESCAPE '\\' "
                "ORDER BY updated_at DESC LIMIT ?",
                (escaped + "%", max(0, limit)),
            ).fetchall()
        return [row["run_id"] for row in rows]

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
    ) -> int | None:
        """Claim the run's lease. None when somebody else holds a LIVE one.

        Same contract as ``acquire_compression_lock``: the PRIMARY KEY is the
        sole arbiter of single-winner across processes, and the DELETE ahead of
        it only clears a lease whose holder died without releasing. ``now`` is
        passed in so the whole policy is testable without sleeping.

        The winner gets back its OWNERSHIP FENCE (issue #12): a per-run counter
        bumped in the SAME transaction as the winning INSERT, so exactly one
        acquisition ever advances it, and every write made under this ownership
        can present it (see ``_fenced_write``). It lives in its own table
        because it has to OUTLIVE the lease row, which ``release_run_lease``
        deletes. Two statements rather than an UPSERT: the plain
        INSERT-OR-IGNORE + UPDATE pair needs no SQLite 3.24."""
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
                self._connection.execute(
                    "INSERT OR IGNORE INTO workflow_run_fence "
                    "(run_id, fence, updated_at) VALUES (?, 0, ?)",
                    (run_id, now),
                )
                self._connection.execute(
                    "UPDATE workflow_run_fence SET fence = fence + 1, updated_at = ? "
                    "WHERE run_id = ?",
                    (now, run_id),
                )
                row = self._connection.execute(
                    "SELECT fence FROM workflow_run_fence WHERE run_id = ?", (run_id,)
                ).fetchone()
            except (sqlite3.IntegrityError, sqlite3.OperationalError):
                self._connection.rollback()
                return None
            self._connection.commit()
            return int(row["fence"])

    def run_fence_of(self, run_id: str) -> int | None:
        """The run's CURRENT ownership fence, or None when it has never had one.

        The fence row OUTLIVES the lease (``release_run_lease`` deletes the lock,
        never this), so its absence is a durable fact: nobody has ever acquired
        this run under the fencing contract, and an unfenced write to it is the
        pre-#12 behaviour rather than a hole. That is the discriminator
        ``RunStateStore.fence_of`` needs when its own bounded memory has no
        answer — deliberately NOT usable as a fence to write with (it is the
        CURRENT owner's, which is exactly who a straggler must not impersonate).
        """
        with self._lock:
            row = self._connection.execute(
                "SELECT fence FROM workflow_run_fence WHERE run_id = ?", (run_id,)
            ).fetchone()
        return int(row["fence"]) if row is not None else None

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

    def save_messages(self, session_id: str, messages: list[dict[str, Any]]) -> int:
        """Persiste um LOTE de mensagens numa única transação — tudo-ou-nada.

        O bloco de persistência de um turno usa isto em vez de N chamadas a
        ``save_message``: uma falha no meio faz rollback do lote inteiro, então
        um turno nunca fica meio-persistido (meio-persistido = user sem
        assistant = alternância quebrada no resume, e um dead-turn notice de
        "descartado" que seria mentira). Retorna o número de linhas gravadas."""
        if not messages:
            return 0
        with self._lock:
            try:
                for message in messages:
                    tool_calls = message.get("tool_calls")
                    provider_data = message.get("provider_data")
                    self._connection.execute(
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
                    "UPDATE sessions SET message_count = message_count + ? WHERE id = ?",
                    (len(messages), session_id),
                )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
        return len(messages)

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
        max_runs: int, retention_seconds: float, fence: int | None = None,
    ) -> int:
        # Defense in depth: callers cannot bypass the metadata-only boundary by
        # reaching around AuditTrail and writing a hand-crafted event directly.
        from lohra.workflow.audit import sanitize_audit_event

        safe_event = sanitize_audit_event(event)
        return audit_store_append(
            self._audit_connection, self._audit_lock, safe_event, now=now,
            max_events=max_events, max_runs=max_runs,
            retention_seconds=retention_seconds, fence=fence,
        )

    def steering_reserve(self, run_id: str, *, limit: int) -> tuple[bool, int]:
        """Take one run-wide external steering slot. (accepted, used_after).

        The RUN-WIDE durable half of the steering budget: the in-process
        ``SteeringLimits`` dies with the process, so a resumed run would come
        back with its run-wide ceiling refilled. The PK arbitrates one row per
        run and ``used >= 0`` (a CHECK, not a hope) backs the release path.

        Single-winner under concurrency: the counter read-and-bump runs
        inside ``BEGIN IMMEDIATE`` — SQLite's write lock is taken BEFORE the
        read, so two threads (or two connections, or two processes) can never
        both observe the same ``used`` and both win the last slot. The
        session-wide ``_lock`` serializes this connection's own statements;
        ``busy_timeout`` covers the cross-connection case. The downlevel
        ``UPDATE ... WHERE used < ?`` guard is belt-and-braces on top of the
        transaction, keeping the invariant true even if a future caller
        forgets the immediate transaction.
        """
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT OR IGNORE INTO workflow_steering_budget "
                    "(run_id, used) VALUES (?, 0)",
                    (run_id,),
                )
                updated = self._connection.execute(
                    "UPDATE workflow_steering_budget SET used = used + 1 "
                    "WHERE run_id = ? AND used < ?",
                    (run_id, limit),
                ).rowcount
                row = self._connection.execute(
                    "SELECT used FROM workflow_steering_budget WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
            except sqlite3.Error:
                self._connection.rollback()
                raise
            if not updated:
                # Refused: the read above still happened inside the write
                # transaction, so commit the (no-op) transaction and answer
                # from the row it just saw.
                self._connection.commit()
                return False, int(row["used"])
            self._connection.commit()
            return True, int(row["used"])

    def steering_release(self, run_id: str) -> bool:
        """Return one steering slot to the run's durable budget.

        True when a slot was actually returned (``used`` fell); False when the
        run had no open slot — never released below zero, and an unknown run
        is not an error."""
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    "INSERT OR IGNORE INTO workflow_steering_budget "
                    "(run_id, used) VALUES (?, 0)",
                    (run_id,),
                )
                updated = self._connection.execute(
                    "UPDATE workflow_steering_budget SET used = used - 1 "
                    "WHERE run_id = ? AND used > 0",
                    (run_id,),
                ).rowcount
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
            return bool(updated)

    def route_fallback_try(self, run_id: str, route_key: str, max_per_run: int) -> bool:
        """Buy ONE pre-authorized re-route for this run. True only if allowed.

        The durable brake of the operator's route envelope (#63), and durable is
        the whole point: the in-process engine dies with the stretch, so a run
        that resumed would come back with its allowance refilled and could walk
        an outage one node at a time — the resume-without-adapting loop the
        envelope exists to bound. Two ceilings, both enforced here:

        - ``MAX_FALLBACKS_PER_ROUTE`` (1) per ``(run, dead route)``: a second
          guess for the same dead route is not resilience, it is the harness
          insisting. Enforced by the row itself, whose PK is that exact pair.
        - ``max_per_run`` for the whole run, summed across routes.

        Single-winner under concurrency for the reason ``steering_reserve``
        gives: the read runs INSIDE ``BEGIN IMMEDIATE``, so SQLite's write lock
        is taken before it and two owners can never both spend the last slot.
        Never released: a re-route is spent the moment it is granted — the leaf
        it buys may still fail, and giving the slot back for a failed attempt is
        precisely how an unbounded chain would reappear.
        """
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                total = self._connection.execute(
                    "SELECT COALESCE(SUM(used), 0) AS used "
                    "FROM workflow_route_fallbacks WHERE run_id = ?",
                    (run_id,),
                ).fetchone()["used"]
                if int(total) >= int(max_per_run):
                    self._connection.commit()
                    return False
                self._connection.execute(
                    "INSERT OR IGNORE INTO workflow_route_fallbacks "
                    "(run_id, route_key, used) VALUES (?, ?, 0)",
                    (run_id, route_key),
                )
                updated = self._connection.execute(
                    "UPDATE workflow_route_fallbacks SET used = used + 1 "
                    "WHERE run_id = ? AND route_key = ? AND used < ?",
                    (run_id, route_key, ROUTE_FALLBACKS_PER_ROUTE),
                ).rowcount
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
            return bool(updated)

    def route_fallbacks_used(self, run_id: str) -> int:
        """How many pre-authorized re-routes this run has already spent (0 for an
        unknown run). Read-only; the didactic hint says so when the allowance is
        gone, and a fresh process must read the same number the last one wrote."""
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(SUM(used), 0) AS used "
                "FROM workflow_route_fallbacks WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["used"]) if row is not None else 0

    def steering_used(self, run_id: str) -> int:
        """The run's durable external steering count (0 for an unknown run)."""
        with self._lock:
            row = self._connection.execute(
                "SELECT used FROM workflow_steering_budget WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["used"]) if row is not None else 0

    def audit_events(self, run_id: str) -> list[dict[str, Any]]:
        return audit_store_events(self._audit_connection, self._audit_lock, run_id)

    def audit_query(self, run_id: str, **filters: Any) -> dict[str, Any]:
        """Read one bounded audit page without constructing a provider client."""
        return audit_store_query(
            self._audit_connection, self._audit_lock, run_id, **filters
        )

    def close(self) -> None:
        self.notices.close()
        self.insights.close()
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
