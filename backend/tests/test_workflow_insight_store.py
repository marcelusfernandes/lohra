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


def test_same_lesson_different_words_is_deduplicated(store) -> None:
    variant = dict(GOOD, summary="leaf  output   MISSING required field\n")
    store.record(kind="insight", **GOOD)
    store.record(kind="insight", **variant)
    assert store.count() == 1


def test_fingerprint_includes_mechanism_and_kind() -> None:
    """A chave de dedup é (kind, responsibility, mechanism, texto normalizado).

    Hoje só VALIDATION chega a agency (as demais responsibilities nunca são
    learnable), então a diferença de mechanism entre dois sinais learnable não
    ocorre ainda — o teste pina a FUNÇÃO, para o dia em que outro par
    (mechanism, evidência) virar learnable sem mudar o dedup."""
    from lohra.state.insights import _fingerprint

    base = _fingerprint("insight", "agency", "validation", "same words")
    assert _fingerprint("candidate", "agency", "validation", "same words") != base
    assert _fingerprint("insight", "agency", "timeout", "same words") != base
    assert _fingerprint("insight", "agency", "validation", "Same   WORDS\n") == base


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
    for i in range(MAX_CANDIDATES + 25):
        assert (
            store.record(
                kind="insight",
                status="failed",
                mechanism="validation",
                signals=(SIGNAL_SPEC_SHAPE,),
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
            signals=(SIGNAL_SPEC_SHAPE,),
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
                    signals=(SIGNAL_SPEC_SHAPE,),
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
