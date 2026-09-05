"""SUP-05 fatia 1 — durable store de insights/candidates (TDD).

Contrato sob teste:

- só sinal learnable entra (gate recomputado no store, não confiada ao caller);
- dedup semântico entre processos e entre formulações diferentes do mesmo
  lesson-body;
- cap 200 com eviction oldest-first na MESMA transação;
- texto bounded;
- concorrência real: N processos escrevendo a MESMA lição produzem UMA linha
  e N processos escrevendo lições distintas produzem N linhas — sem lost
  update, sem duplicata.
"""

from __future__ import annotations

import multiprocessing

import pytest

from lohra.state import SessionDB
from lohra.state.insights import MAX_CANDIDATES, InsightStore
from lohra.workflow.failure_taxonomy import (
    SIGNAL_PROVIDER_SIDE,
    SIGNAL_SPEC_SHAPE,
    Mechanism,
    Responsibility,
)

GOOD = dict(
    status="failed",
    mechanism="validation",
    signals=(SIGNAL_SPEC_SHAPE,),
    confidence=0.9,
    summary="leaf output missing required field",
)


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture
def store(tmp_path):
    s = InsightStore(str(tmp_path / "state.db"))
    yield s
    s.close()


# --- gate learnable (fail-closed contra caller mentiroso) ---------------------


def test_only_learnable_signal_is_stored(store) -> None:
    assert store.record(kind="insight", **GOOD) is True


@pytest.mark.parametrize(
    "overrides",
    [
        dict(signals=(SIGNAL_PROVIDER_SIDE,)),  # provider side -> environment
        dict(confidence=0.5),  # below the floor
        dict(signals=()),  # no evidence
        dict(mechanism=Mechanism.UNKNOWN),  # unknown mechanism
    ],
)
def test_non_learnable_variants_are_refused(store, overrides) -> None:
    fields = {**GOOD, **overrides}
    assert store.record(kind="insight", **fields) is False
    assert store.count() == 0


def test_store_recomputes_verdict_caller_cannot_assert_agency(store) -> None:
    """O gate reclassifica: um caller que afirma agency sem evidência é recusado."""
    with pytest.raises(TypeError):
        store.record(kind="insight", responsibility=Responsibility.AGENCY, **GOOD)
    assert store.count() == 0


def test_sessiondb_exposes_insight_store(tmp_path) -> None:
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        assert db.insights.record(kind="candidate", **GOOD) is True
        assert db.insights.count() == 1
    finally:
        db.close()


def test_sessiondb_persists_across_processes(tmp_path) -> None:
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    try:
        assert db.insights.record(kind="insight", **GOOD) is True
    finally:
        db.close()
    reopened = SessionDB(path)
    try:
        rows = reopened.insights.list()
        assert len(rows) == 1
        assert rows[0]["mechanism"] == "validation"
        assert rows[0]["responsibility"] == "agency"
    finally:
        reopened.close()


# --- dedup semântico ----------------------------------------------------------


def test_exact_duplicate_is_deduplicated(store) -> None:
    assert store.record(kind="insight", **GOOD) is True
    store.record(kind="insight", **GOOD)
    store.record(kind="insight", **GOOD)
    assert store.count() == 1
    assert store.list()[0]["hits"] == 3


def test_same_lesson_different_words_is_deduplicated(store) -> None:
    variant = dict(GOOD, summary="leaf  output   MISSING required field\n")
    store.record(kind="insight", **GOOD)
    store.record(kind="insight", **variant)
    assert store.count() == 1
    row = store.list()[0]
    assert row["hits"] == 2
    # First wording is the didactic anchor; last_summary tracks the repeat.
    assert row["summary"] == GOOD["summary"]
    assert row["last_summary"] == variant["summary"]


def test_fingerprint_is_structural_not_prose() -> None:
    """The dedup key is (kind, responsibility, mechanism, sorted signals) —
    NEVER the summary text. Different signals or mechanism/kind fingerprint
    differently; different summary wording (even signal ORDER) does not."""
    from lohra.state.insights import _fingerprint

    base = _fingerprint("insight", "agency", "validation", (SIGNAL_SPEC_SHAPE,))
    assert _fingerprint("candidate", "agency", "validation", (SIGNAL_SPEC_SHAPE,)) != base
    assert _fingerprint("insight", "agency", "timeout", (SIGNAL_SPEC_SHAPE,)) != base
    assert _fingerprint("insight", "agency", "validation", ("rule:x", SIGNAL_SPEC_SHAPE)) != base
    # Signal ORDER must not matter — the basis sorts before hashing.
    assert (
        _fingerprint("insight", "agency", "validation", (SIGNAL_SPEC_SHAPE, "rule:x"))
        == _fingerprint("insight", "agency", "validation", ("rule:x", SIGNAL_SPEC_SHAPE))
    )


def test_timeout_with_spec_shape_is_refused_gate_consistency(store) -> None:
    """Um mechanism não-learnable nunca entra, mesmo com texto de lição válido."""
    other = dict(GOOD, mechanism="timeout", signals=(SIGNAL_SPEC_SHAPE,))
    store.record(kind="insight", **GOOD)
    assert store.record(kind="insight", **other) is False
    assert store.count() == 1


def test_different_kind_is_not_deduplicated(store) -> None:
    store.record(kind="insight", **GOOD)
    store.record(kind="candidate", **GOOD)
    assert store.count() == 2


# --- cap e bounds -------------------------------------------------------------


def test_cap_200_evicts_oldest_first(store) -> None:
    # Structurally DISTINCT rows now need a distinguishing signal, not just a
    # different summary — the fingerprint no longer looks at prose (E1).
    for i in range(MAX_CANDIDATES + 25):
        assert (
            store.record(
                kind="insight",
                status="failed",
                mechanism="validation",
                signals=(SIGNAL_SPEC_SHAPE, f"case:{i}"),
                confidence=0.9,
                summary=f"distinct lesson {i}",
            )
            is True
        )
    assert store.count() == MAX_CANDIDATES
    summaries = {row["summary"] for row in store.list(limit=MAX_CANDIDATES)}
    assert "distinct lesson 0" not in summaries  # oldest evicted
    assert "distinct lesson 224" in summaries  # newest kept


def test_text_is_bounded(store) -> None:
    store.record(
        kind="insight",
        status="failed",
        mechanism="validation",
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=0.9,
        summary="x" * 10_000,
        payload={"traceback": "y" * 10_000},
    )
    row = store.list()[0]
    assert len(row["summary"]) <= 500
    assert len(row["payload_json"]) <= 2000


def test_list_is_newest_first_and_bounded(store) -> None:
    for i in range(5):
        store.record(
            kind="insight",
            status="failed",
            mechanism="validation",
            signals=(SIGNAL_SPEC_SHAPE, f"case:{i}"),  # structurally distinct (E1)
            confidence=0.9,
            summary=f"lesson {i}",
        )
    rows = store.list(limit=3)
    assert [row["summary"] for row in rows] == ["lesson 4", "lesson 3", "lesson 2"]
    assert len(store.list(limit=10_000)) == 5


# --- concorrência real entre processos ----------------------------------------


def _writer_same(path: str) -> None:
    store = InsightStore(path)
    try:
        for _ in range(20):
            store.record(kind="insight", **GOOD)
    finally:
        store.close()


def _writer_distinct(path: str, worker: int, per_worker: int) -> None:
    store = InsightStore(path)
    try:
        for i in range(per_worker):
            assert (
                store.record(
                    kind="insight",
                    status="failed",
                    mechanism="validation",
                    # Structurally distinct per (worker, i) — E1 fingerprints
                    # the cause, not the summary, so "distinct lesson" now
                    # means a distinct SIGNAL, not just different prose.
                    signals=(SIGNAL_SPEC_SHAPE, f"worker:{worker}:{i}"),
                    confidence=0.9,
                    summary=f"worker {worker} lesson {i}",
                )
                is True
            )
    finally:
        store.close()


def test_concurrent_processes_writing_same_lesson_land_one_row(tmp_path) -> None:
    path = str(tmp_path / "mp.db")
    InsightStore(path).close()  # migrate before contention
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_writer_same, args=(path,)) for _ in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    store = InsightStore(path)
    try:
        assert store.count() == 1
        # 4 processes x 20 writes of the SAME structural cause: hits must
        # land at exactly 80 — a lost increment under contention would show
        # up here as < 80 (BEGIN IMMEDIATE + ON CONFLICT DO UPDATE serialize
        # the read-increment-write across processes).
        assert store.list()[0]["hits"] == 80
    finally:
        store.close()


def test_concurrent_distinct_writers_have_no_lost_update(tmp_path) -> None:
    path = str(tmp_path / "mp2.db")
    InsightStore(path).close()
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_writer_distinct, args=(path, worker, 25)) for worker in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    store = InsightStore(path)
    try:
        assert store.count() == 100  # 4 writers x 25 — every write landed
    finally:
        store.close()


# --- E1 migration: a pre-E1 database opens cleanly, legacy rows never merge --

# Verbatim copy of the pre-E1 CREATE TABLE (no `hits`/`last_summary`, exactly
# what shipped before Wave 9). This pins the migration against the ACTUAL old
# shape, not a paraphrase of it.
_LEGACY_SCHEMA = """
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


def test_pre_e1_database_migrates_and_legacy_row_is_never_merged(tmp_path) -> None:
    path = str(tmp_path / "legacy.db")
    import sqlite3

    # 1. Build a database on the OLD schema and insert one row the OLD way
    #    (the free-text fingerprint an old InsightStore would have computed).
    raw = sqlite3.connect(path)
    try:
        raw.executescript(_LEGACY_SCHEMA)
        legacy_fp = "deadbeef" * 4  # any 32-char stand-in; only its shape matters
        raw.execute(
            """INSERT INTO workflow_insight_candidates
               (fingerprint, kind, status, mechanism, responsibility, confidence,
                summary, payload_json, created_at, updated_at)
               VALUES (?, 'candidate', 'invalid_spec', 'validation', 'agency', 1.0,
                       'authored workflow spec rejected: legacy row', NULL, 1.0, 1.0)""",
            (legacy_fp,),
        )
        raw.commit()
    finally:
        raw.close()

    # 2. Open it with the CURRENT InsightStore — the migration must not raise,
    #    and the legacy row must read back with `hits` absent (NULL), never a
    #    silent 0 or 1 (doctrine: absence is never a masked default).
    store = InsightStore(path)
    try:
        rows = store.list()
        assert len(rows) == 1
        legacy_row = rows[0]
        assert legacy_row["fingerprint"] == legacy_fp
        assert legacy_row["hits"] is None
        assert legacy_row["last_summary"] is None

        # 3. A fresh record() with the SAME (kind, mechanism, signals,
        #    responsibility) as the legacy row must land as a SECOND row —
        #    the old and new fingerprint schemes hash different inputs, so a
        #    legacy row is never merged with a post-E1 one, by construction.
        assert (
            store.record(
                kind="candidate",
                status="invalid_spec",
                mechanism="validation",
                signals=(SIGNAL_SPEC_SHAPE,),
                confidence=1.0,
                summary="authored workflow spec rejected: fresh row",
            )
            is True
        )
        assert store.count() == 2
        fresh_row = next(r for r in store.list() if r["fingerprint"] != legacy_fp)
        assert fresh_row["hits"] == 1
    finally:
        store.close()


# --- E1 RED: structural fingerprint + recurrence counter ---------------------
#
# Method gate (Wave 9, issue #50, épico E1): today's fingerprint hashes free
# prose (`_normalize(summary)`), so the SAME causal defect reported from two
# different node ids / two differently-worded summaries lands as TWO rows and
# neither `hits` nor `updated_at` ever advances on a repeat. These two tests
# encode the DESIRED post-E1 behaviour and are expected to FAIL on today's
# code (that failure — plus the diagnostic AssertionError — is the RED
# evidence saved to scratchpad/w9/red-e1.txt). A structural fingerprint must
# also NOT over-collapse: two calls with different `signals` describe
# different causal evidence and must stay two rows (the negative direction).


def test_e1_same_structural_cause_different_nodes_is_one_row_with_hits(
    store, monkeypatch
) -> None:
    """Same (kind, mechanism, signals, responsibility), different node id and
    different free-text summary -> ONE row, hits == 2, updated_at advances.

    Today (pre-E1): fingerprint hashes the summary, so this is TWO rows and
    there is no `hits` column at all (KeyError) — that IS the RED failure.
    """
    clock = iter([100.0, 200.0])
    monkeypatch.setattr(
        "lohra.state.insights.time.time", lambda: next(clock)
    )  # two distinct instants — a real tie must not mask a broken UPDATE
    first = dict(
        kind="candidate",
        status="invalid_spec",
        mechanism="validation",
        signals=(SIGNAL_SPEC_SHAPE,),
        confidence=1.0,
        summary="authored workflow spec rejected: node 'alpha' — [unknown_tier] "
        "tier 'xl' is not one of small/medium/big",
    )
    second = dict(
        first,
        summary="authored workflow spec rejected: node 'beta' — [unknown_tier] "
        "tier 'xl' is not one of small/medium/big",
    )
    assert store.record(**first) is True
    first_rows = store.list()
    assert len(first_rows) == 1
    first_updated_at = first_rows[0]["updated_at"]

    assert store.record(**second) is True

    rows = store.list()
    assert len(rows) == 1, "same structural cause in two nodes must dedupe to one row"
    row = rows[0]
    assert row["hits"] == 2, "a repeat of the same structural cause must increment hits"
    assert row["updated_at"] > first_updated_at, "updated_at must advance on a repeat"


def test_e1_different_signals_stay_two_rows(store) -> None:
    """Negative direction: different evidence (signals) is a different cause —
    it must NOT be collapsed into the same row even with identical summary
    text, mechanism and responsibility."""
    base = dict(
        kind="candidate",
        status="invalid_spec",
        mechanism="validation",
        confidence=1.0,
        summary="same wording on purpose",
    )
    assert store.record(signals=(SIGNAL_SPEC_SHAPE,), **base) is True
    assert store.record(signals=(SIGNAL_SPEC_SHAPE, "rule:unknown_field"), **base) is True
    assert store.count() == 2, "different structural signals must not be merged"


def test_sqlite_integrity_after_concurrent_writes(tmp_path) -> None:
    path = str(tmp_path / "mp3.db")
    InsightStore(path).close()
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_writer_distinct, args=(path, worker, 10)) for worker in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    db = SessionDB(path)
    try:
        with db._lock:
            assert db._connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        db.close()
