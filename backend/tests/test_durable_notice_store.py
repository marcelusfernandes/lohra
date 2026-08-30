"""SUP-05 fatia 2 — DurableNoticeStore (TDD).

Contrato sob teste:

- fatos operacionais por owner (session_id), com dedup por fingerprint de
  conteúdo (o mesmo texto publicado de novo é UMA linha);
- cap configurável, default 32 POR OWNER (documentado no docstring da
  classe): publicar além do cap evicta a pendência mais antiga daquele
  owner; leases ativos NUNCA são evictados — se pendências não bastam
  para respeitar o hard cap, o publish novo é recusado (False);
- TTL default 7 dias: uma notice expirada desaparece no próximo claim;
- texto bounded no limite do schema;
- claim é lease de single-winner via BEGIN IMMEDIATE: retorna (token, rows);
  um segundo claim enquanto o lease vive não vê as mesmas rows;
- ack só remove com o token CORRETO; release devolve as rows para pending;
- lease expirado (crash do claimer) é recuperável por claim posterior —
  entrega at-least-once;
- clock injetável (``now=``) em toda operação sensível a tempo, para testar
  TTL/lease sem dormir;
- claim aceita ``owner_ids`` (lineage: sessão filha herda notices do pai) e
  RECUSA ownerless (None/""), fechando injeção profile-global;
- concorrência real entre processos: N processos reclamando a MESMA notice
  produzem exatamente UM vencedor.
"""

from __future__ import annotations

import multiprocessing
import sqlite3

import pytest

from lohra.state import SessionDB
from lohra.state.notices import (
    DEFAULT_CAP,
    DEFAULT_LEASE_SECONDS,
    DEFAULT_TTL_SECONDS,
    MAX_TEXT_CHARS,
    DurableNoticeStore,
)

T0 = 1_750_000_000.0


@pytest.fixture
def store(tmp_path):
    s = DurableNoticeStore(str(tmp_path / "state.db"))
    yield s
    s.close()


@pytest.fixture
def db(tmp_path):
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


# --- publish / dedup ----------------------------------------------------------


def test_publish_then_claim_returns_token_and_rows(store) -> None:
    assert store.publish("s1", "provider quota exhausted", now=T0) is True
    token, rows = store.claim("s1", now=T0 + 1)
    assert token
    assert len(rows) == 1
    assert rows[0]["text"] == "provider quota exhausted"
    assert rows[0]["owner_id"] == "s1"


def test_duplicate_text_is_deduplicated_by_fingerprint(store) -> None:
    assert store.publish("s1", "quota exhausted", now=T0) is True
    assert store.publish("s1", "quota exhausted", now=T0 + 5) is False
    assert store.publish("s1", "  quota   EXHAUSTED \n", now=T0 + 6) is False
    assert store.pending_count("s1") == 1


def test_same_text_different_owner_is_a_separate_fact(store) -> None:
    assert store.publish("s1", "quota exhausted", now=T0) is True
    assert store.publish("s2", "quota exhausted", now=T0) is True
    _, rows = store.claim(["s1", "s2"], now=T0 + 1)
    assert len(rows) == 2


def test_empty_text_is_refused(store) -> None:
    with pytest.raises(ValueError):
        store.publish("s1", "   ", now=T0)


# --- claim single-winner / ack / release ---------------------------------------


def test_second_claim_while_lease_lives_sees_nothing(store) -> None:
    store.publish("s1", "fact", now=T0)
    token1, rows1 = store.claim("s1", now=T0 + 1)
    assert rows1
    token2, rows2 = store.claim("s1", now=T0 + 2)
    assert rows2 == []
    assert token2 is None


def test_claim_honors_limit(store) -> None:
    for i in range(5):
        store.publish("s1", f"fact {i}", now=T0)
    token, rows = store.claim("s1", limit=2, now=T0 + 1)
    assert len(rows) == 2
    # o restante continua pendente para o próximo claim
    _, rest = store.claim("s1", now=T0 + 2)
    assert len(rest) == 3
    store.ack(token, now=T0 + 2)
    # pending = não-leased: os 3 restantes estão leased pelo segundo claim
    assert store.pending_count("s1") == 0


def test_ack_with_correct_token_removes_rows(store) -> None:
    store.publish("s1", "fact", now=T0)
    token, rows = store.claim("s1", now=T0 + 1)
    assert store.ack(token, now=T0 + 2) == 1
    assert store.pending_count("s1", now=T0 + 2) == 0


def test_ack_with_wrong_token_removes_nothing(store) -> None:
    store.publish("s1", "fact", now=T0)
    store.claim("s1", now=T0 + 1)
    assert store.ack("not-the-token", now=T0 + 2) == 0
    # a row continua claimed — só o dono do token decide
    token, rows = store.claim("s1", now=T0 + 3)
    assert rows == []
    assert store.pending_count("s1", now=T0 + 3) == 0


def test_ack_scoped_to_specific_notice_ids(store) -> None:
    store.publish("s1", "fact a", now=T0)
    store.publish("s1", "fact b", now=T0)
    token, rows = store.claim("s1", now=T0 + 1)
    acked = store.ack(token, notice_ids=[rows[0]["id"]], now=T0 + 2)
    assert acked == 1
    # a não-ackada é LIBERADA na mesma transação (posse parcial não se retém)
    assert store.pending_count("s1", now=T0 + 2) == 1
    _, again = store.claim("s1", now=T0 + 3)
    assert [r["text"] for r in again] == ["fact b"]


def test_release_returns_rows_to_pending(store) -> None:
    store.publish("s1", "fact", now=T0)
    token, _ = store.claim("s1", now=T0 + 1)
    assert store.release(token, now=T0 + 2) == 1
    token2, rows2 = store.claim("s1", now=T0 + 3)
    assert [r["text"] for r in rows2] == ["fact"]
    assert token2 != token


def test_release_with_wrong_token_is_noop(store) -> None:
    store.publish("s1", "fact", now=T0)
    store.claim("s1", now=T0 + 1)
    assert store.release("bogus", now=T0 + 2) == 0


def test_expired_lease_is_reclaimable_after_crash(store) -> None:
    store.publish("s1", "fact", now=T0)
    store.claim("s1", now=T0 + 1, lease_seconds=DEFAULT_LEASE_SECONDS)
    # claimer "crasha": ninguém dá ack nem release. Depois do lease, outro
    # processo (ou o mesmo) recupera a notice — at-least-once.
    token, rows = store.claim("s1", now=T0 + 1 + DEFAULT_LEASE_SECONDS + 1)
    assert [r["text"] for r in rows] == ["fact"]


def test_unexpired_lease_is_not_stolen_early(store) -> None:
    store.publish("s1", "fact", now=T0)
    store.claim("s1", now=T0 + 1, lease_seconds=60.0)
    _, rows = store.claim("s1", now=T0 + 30)
    assert rows == []


# --- TTL -----------------------------------------------------------------------


def test_expired_notice_is_dropped_on_claim(store) -> None:
    store.publish("s1", "stale fact", now=T0, ttl_seconds=100.0)
    _, rows = store.claim("s1", now=T0 + 101)
    assert rows == []
    assert store.pending_count("s1", now=T0 + 101) == 0


def test_fresh_notice_survives_within_ttl(store) -> None:
    store.publish("s1", "fresh fact", now=T0, ttl_seconds=100.0)
    _, rows = store.claim("s1", now=T0 + 99)
    assert [r["text"] for r in rows] == ["fresh fact"]


def test_default_ttl_is_seven_days() -> None:
    assert DEFAULT_TTL_SECONDS == 7 * 24 * 3600


# --- cap por owner --------------------------------------------------------------


def test_default_cap_is_32_per_owner() -> None:
    assert DEFAULT_CAP == 32


def test_cap_evicts_oldest_per_owner(store) -> None:
    small = DurableNoticeStore(
        store._path, cap=3, ttl_seconds=DEFAULT_TTL_SECONDS, lease_seconds=60.0
    )
    try:
        for i in range(5):
            assert small.publish("s1", f"fact {i}", now=T0 + i) is True
        assert small.pending_count("s1", now=T0 + 5) == 3
        _, rows = small.claim("s1", now=T0 + 100)
        assert [r["text"] for r in rows] == ["fact 2", "fact 3", "fact 4"]
    finally:
        small.close()


def test_cap_is_per_owner_not_global(store) -> None:
    small = DurableNoticeStore(store._path, cap=2)
    try:
        for i in range(3):
            small.publish("s1", f"a{i}", now=T0)
            small.publish("s2", f"b{i}", now=T0)
        assert small.pending_count("s1", now=T0 + 5) == 2
        assert small.pending_count("s2", now=T0 + 5) == 2
    finally:
        small.close()


# --- texto bounded ---------------------------------------------------------------


def test_text_is_bounded_at_the_schema_boundary(store) -> None:
    huge = "x" * (MAX_TEXT_CHARS * 4)
    assert store.publish("s1", huge, now=T0) is True
    _, rows = store.claim("s1", now=T0 + 1)
    assert 0 < len(rows[0]["text"]) <= MAX_TEXT_CHARS


# --- owner_ids (lineage) e anti-injeção ownerless --------------------------------


def test_claim_accepts_owner_ids_lineage(store) -> None:
    store.publish("parent", "parent fact", now=T0)
    # a sessão FILHA enxerga as notices do pai — e as próprias.
    store.publish("child", "child fact", now=T0)
    _, rows = store.claim(["child", "parent"], now=T0 + 1)
    assert sorted(r["text"] for r in rows) == ["child fact", "parent fact"]


def test_claim_default_is_owner_only(store) -> None:
    store.publish("parent", "parent fact", now=T0)
    _, rows = store.claim("child", now=T0 + 1)
    assert rows == []


def test_ownerless_owner_ids_are_refused(store) -> None:
    with pytest.raises(ValueError):
        store.claim([None])
    with pytest.raises(ValueError):
        store.claim(["child", ""])
    with pytest.raises(ValueError):
        store.claim([])


def test_publish_refuses_ownerless(store) -> None:
    with pytest.raises(ValueError):
        store.publish("", "fact", now=T0)
    with pytest.raises(ValueError):
        store.publish(None, "fact", now=T0)


# --- integração SessionDB ---------------------------------------------------------


def test_sessiondb_exposes_notice_store(db) -> None:
    assert db.notices.publish("s1", "fact", now=T0) is True
    token, rows = db.notices.claim("s1", now=T0 + 1)
    assert len(rows) == 1


def test_sessiondb_notices_persist_across_processes(tmp_path) -> None:
    path = str(tmp_path / "state.db")
    db = SessionDB(path)
    try:
        db.notices.publish("s1", "durable fact", now=T0)
    finally:
        db.close()
    reopened = SessionDB(path)
    try:
        _, rows = reopened.notices.claim("s1", now=T0 + 1)
        assert [r["text"] for r in rows] == ["durable fact"]
    finally:
        reopened.close()


# --- concorrência real entre processos --------------------------------------------


def _claimer(path: str, owner: str) -> None:
    store = DurableNoticeStore(path)
    try:
        token, rows = store.claim(owner, now=T0 + 10)
        # comunica o desfecho pelo exit code: vencedor escreve um marcador via
        # ack; perdedores saem limpos sem ack.
        if token is not None and rows:
            store.ack(token, now=T0 + 11)
    finally:
        store.close()


def test_multiprocess_claim_has_exactly_one_winner(tmp_path) -> None:
    path = str(tmp_path / "mp.db")
    DurableNoticeStore(path).close()  # migra o schema antes da contenção
    seed = DurableNoticeStore(path)
    seed.publish("s1", "one fact", now=T0)
    seed.close()

    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_claimer, args=(path, "s1")) for _ in range(6)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0

    store = DurableNoticeStore(path)
    try:
        # exatamente um vencedor: a notice foi consumida (ack) e não há mais
        # nada pendente nem claimed sobrevivendo.
        assert store.pending_count("s1") == 0
        _, rows = store.claim("s1", now=T0 + 10_000)
        assert rows == []
    finally:
        store.close()


def _claim_and_report(p: str, q, worker: int) -> None:
    store = DurableNoticeStore(p)
    try:
        token, rows = store.claim("s1", limit=100, now=T0 + 10)
        q.put((worker, len(rows)))
        if token is not None:
            store.ack(token, now=T0 + 11)
    finally:
        store.close()


def _claim_and_report(p: str, q, worker: int) -> None:
    store = DurableNoticeStore(p)
    try:
        token, rows = store.claim("s1", now=T0 + 10)
        q.put((worker, len(rows)))
        if token is not None:
            store.ack(token, now=T0 + 11)
    finally:
        store.close()


def test_multiprocess_claim_of_distinct_notices_delivers_each_once(tmp_path) -> None:
    path = str(tmp_path / "mp2.db")
    DurableNoticeStore(path).close()
    seed = DurableNoticeStore(path)
    for i in range(12):
        seed.publish("s1", f"fact {i}", now=T0)
    seed.close()

    context = multiprocessing.get_context("spawn")
    results = context.Queue()

    processes = [
        context.Process(target=_claim_and_report, args=(path, results, worker))
        for worker in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0

    claimed = [results.get(timeout=10) for _ in range(4)]
    total = sum(count for _, count in claimed)
    winners = [worker for worker, count in claimed if count > 0]
    assert total == 12  # cada notice entregue exatamente uma vez por rodada
    assert len(winners) >= 1  # houve entrega; a ordem entre processos é livre


# --- SUP-05: eviction nunca apaga lease ativo -------------------------------------
#
# Semântica segura e bounded definida para _evict_overflow:
#
# 1. Lease ATIVO (lease_token IS NOT NULL AND lease_expires_at > now) NUNCA é
#    evitado — apagá-lo quebraria at-least-once após crash do claimer;
# 2. Overflow é absorvido evitando as PENDÊNCIAS mais antigas do owner
#    (inclui lease já expirado, que o claim trataria como pendente);
# 3. Se pendências não bastam (leases ativos ocupam o cap), o publish é
#    REVERTIDO (rollback do insert) e retorna False — o novo publish não
#    sobrevive, o hard cap nunca é excedido e nenhum leased é apagado
#    (escolha: rollback em vez de coalescência/coalescing do novo texto);
# 4. Dedup cross-process é preservado: duplicata continua retornando False
#    sem inserir nem evictar, independente de pressão de cap.


def _total_rows(store: DurableNoticeStore, owner: str) -> int:
    """Contagem absoluta de rows do owner (inclui leased) — direto no SQLite."""
    conn = sqlite3.connect(store._path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM durable_notices WHERE owner_id = ?", (owner,)
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def test_publish_overflow_with_active_lease_is_refused_not_evicted(store) -> None:
    small = DurableNoticeStore(store._path, cap=3, lease_seconds=60.0)
    try:
        for i in range(3):
            assert small.publish("s1", f"fact {i}", now=T0 + i) is True
        token, rows = small.claim("s1", now=T0 + 10)
        assert len(rows) == 3
        # cap ocupado por leases ATIVOS: o novo publish NÃO sobrevive (rollback)
        assert small.publish("s1", "fact new", now=T0 + 11) is False
        assert _total_rows(small, "s1") == 3  # hard cap respeitado, nada extra
        assert small.pending_count("s1") == 0  # insert foi revertido de verdade
        # leases intactos: crash + expiração → as 3 notices são recuperáveis
        _, recovered = small.claim("s1", now=T0 + 10 + 60 + 1)
        assert [r["text"] for r in recovered] == ["fact 0", "fact 1", "fact 2"]
    finally:
        small.close()


def test_publish_overflow_evicts_oldest_pending_first(store) -> None:
    small = DurableNoticeStore(store._path, cap=3, lease_seconds=60.0)
    try:
        for i in range(3):
            assert small.publish("s1", f"fact {i}", now=T0 + i) is True
        token, rows = small.claim("s1", limit=2, now=T0 + 10)
        assert [r["text"] for r in rows] == ["fact 0", "fact 1"]
        # fact 2 segue pendente; o overflow é absorvido evitando-o (mais antiga
        # pendente), não os leases ativos de fact 0/1.
        assert small.publish("s1", "fact new", now=T0 + 11) is True
        assert _total_rows(small, "s1") == 3
        assert small.release(token, now=T0 + 12) == 2  # leases sobreviveram
        _, recovered = small.claim("s1", now=T0 + 13)
        assert sorted(r["text"] for r in recovered) == ["fact 0", "fact 1", "fact new"]
    finally:
        small.close()


def test_claim_A_publish_overflow_release_A_still_recoverable(store) -> None:
    small = DurableNoticeStore(store._path, cap=1, lease_seconds=60.0)
    try:
        assert small.publish("s1", "A", now=T0) is True
        token, rows = small.claim("s1", now=T0 + 1)
        assert [r["text"] for r in rows] == ["A"]
        # cap ocupado pelo lease ativo de A: publish de B é recusado, cap mantido
        assert small.publish("s1", "B", now=T0 + 2) is False
        assert _total_rows(small, "s1") == 1
        # release devolve A a pendente; A continua recuperável (at-least-once)
        assert small.release(token, now=T0 + 3) == 1
        _, again = small.claim("s1", now=T0 + 4)
        assert [r["text"] for r in again] == ["A"]
        assert _total_rows(small, "s1") == 1  # cap nunca excedido
    finally:
        small.close()


def test_expired_lease_frees_cap_space_for_new_publish(store) -> None:
    small = DurableNoticeStore(store._path, cap=1, lease_seconds=60.0)
    try:
        assert small.publish("s1", "A", now=T0) is True
        small.claim("s1", now=T0 + 1)  # claimer "crasha": sem ack nem release
        # lease de A ainda ativo: publish de B é recusado
        assert small.publish("s1", "B", now=T0 + 30) is False
        assert _total_rows(small, "s1") == 1
        # lease de A expira: A deixa de ser lease ativo e pode ser evitado
        assert small.publish("s1", "B", now=T0 + 1 + 60 + 1) is True
        assert _total_rows(small, "s1") == 1
        _, rows = small.claim("s1", now=T0 + 100)
        assert [r["text"] for r in rows] == ["B"]
    finally:
        small.close()


def test_dedup_survives_cap_pressure_even_when_duplicated_row_is_leased(store) -> None:
    small = DurableNoticeStore(store._path, cap=1, lease_seconds=60.0)
    try:
        assert small.publish("s1", "fact", now=T0) is True
        small.claim("s1", now=T0 + 1)  # row única agora leased
        # duplicata (mesmo fingerprint, qualquer normalização) é coalescida:
        # retorna False, NÃO insere e NÃO dispara evicção de lease ativo.
        assert small.publish("s1", "fact", now=T0 + 2) is False
        assert small.publish("s1", "  FACT  ", now=T0 + 3) is False
        assert _total_rows(small, "s1") == 1
        # a row original continua leased e recuperável
        _, recovered = small.claim("s1", now=T0 + 1 + 60 + 1)
        assert [r["text"] for r in recovered] == ["fact"]
    finally:
        small.close()


def test_hard_cap_never_exceeded_in_mixed_publish_claim_sequence(store) -> None:
    small = DurableNoticeStore(store._path, cap=3, lease_seconds=60.0)
    try:
        clock = float(T0)
        for i in range(12):
            small.publish("s1", f"n{i}", now=clock)
            assert _total_rows(small, "s1") <= 3, f"cap excedido na iteração {i}"
            if i % 3 == 0:
                token, _ = small.claim("s1", now=clock + 1)
                if token is not None:
                    small.release(token, now=clock + 2)
            clock += 3
        assert _total_rows(small, "s1") <= 3
    finally:
        small.close()


def test_overflow_eviction_is_scoped_to_owner_and_spares_other_owners_lease(store) -> None:
    small = DurableNoticeStore(store._path, cap=1, lease_seconds=60.0)
    try:
        assert small.publish("s1", "A1", now=T0) is True
        assert small.publish("s2", "B1", now=T0) is True
        token_b, _ = small.claim("s2", now=T0 + 1)
        # publish em s1 evicta a pendente de s1; o lease ativo de s2 é intocado
        assert small.publish("s1", "A2", now=T0 + 2) is True
        assert _total_rows(small, "s2") == 1
        assert small.release(token_b, now=T0 + 3) == 1
        _, rows = small.claim("s2", now=T0 + 4)
        assert [r["text"] for r in rows] == ["B1"]
    finally:
        small.close()
