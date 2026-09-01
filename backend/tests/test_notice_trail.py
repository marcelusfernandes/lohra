"""Rastro de acks das notices (issue #39) — tombstone leve, mesma transação.

O ack (e todo outro sumiço de row: TTL, evicção de cap) deixa um tombstone
bounded na tabela ``notice_trail``; o contrato publish/claim/ack/release fica
INTOCADO. Decisão tombstone-sobre-flag verificada adversarialmente: a flag
``acked_at`` quebraria a republicação via UNIQUE(owner,fingerprint), seria
ressuscitada pela recuperação de lease morto e ocuparia o cap com cadáveres.
"""

from __future__ import annotations

import sqlite3

from lohra.state.notice_trail import TRAIL_CAP, TRAIL_TTL_SECONDS
from lohra.state.notices import DurableNoticeStore


def _store(tmp_path, **kw) -> DurableNoticeStore:
    return DurableNoticeStore(str(tmp_path / "state.db"), **kw)


def _consumed(store, owners, **kw):
    return store.consumed(owners if isinstance(owners, list) else [owners], **kw)


# --- ack deixa rastro ------------------------------------------------------


def test_ack_total_grava_tombstone(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "quota exhausted", now=100.0)
    token, rows = store.claim("s1", now=101.0)
    store.ack(token, now=102.0)

    assert store.pending_count("s1") == 0
    trail = _consumed(store, "s1", now=103.0)
    assert len(trail) == 1
    entry = trail[0]
    assert entry["owner_id"] == "s1"
    assert entry["text"] == "quota exhausted"
    assert entry["created_at"] == 100.0
    assert entry["removed_at"] == 102.0
    assert entry["reason"] == "acked"
    assert entry["lease_token"] == token  # a tentativa vencedora, identificada
    assert entry["notice_id"] == rows[0]["id"]
    assert entry["fingerprint"]  # a tupla nomeada na issue


def test_ack_parcial_distingue_acked_de_released(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "fato A", now=100.0)
    store.publish("s1", "fato B", now=100.5)
    token, rows = store.claim("s1", now=101.0)
    assert len(rows) == 2
    store.ack(token, notice_ids=[rows[0]["id"]], now=102.0)

    trail = _consumed(store, "s1", now=103.0)
    assert [e["text"] for e in trail] == ["fato A"]  # só o ackado
    assert store.pending_count("s1") == 1  # o released voltou a pendente


def test_release_e_lease_expirado_nao_gravam(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "fato", now=100.0)
    token, _ = store.claim("s1", now=101.0, lease_seconds=10.0)
    store.release(token)
    store.claim("s1", now=200.0)  # recupera lease morto do 2º ciclo
    assert _consumed(store, "s1", now=201.0) == []  # a notice segue viva


def test_token_errado_nao_grava(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "fato", now=100.0)
    store.claim("s1", now=101.0)
    assert store.ack("token-invalido", now=102.0) == 0
    assert _consumed(store, "s1", now=103.0) == []


# --- os outros sumiços também explicam-se ----------------------------------


def test_ttl_purge_no_claim_gera_expired_sem_claim_previo(tmp_path):
    store = _store(tmp_path, ttl_seconds=10.0)
    store.publish("s1", "fato efêmero", now=100.0)
    store.claim("s1", now=200.0)  # purga global de expiradas
    trail = _consumed(store, "s1", now=201.0)
    assert len(trail) == 1
    assert trail[0]["reason"] == "expired"
    assert trail[0]["lease_token"] is None  # nunca foi claimada


def test_ttl_purge_de_notice_em_voo_carrega_o_token(tmp_path):
    # TTL vence sobre lease vivo (docstring do publish) — a notice morre EM
    # VOO; sem o token no tombstone, "expirou sem claim" e "expirou em voo"
    # seriam indistinguíveis (o mistério que a #39 elimina).
    store = _store(tmp_path, ttl_seconds=10.0)
    store.publish("s1", "fato em voo", now=100.0)
    token, _ = store.claim("s1", now=101.0, lease_seconds=3600.0)
    store.claim("s1", now=200.0)  # outro claim purga a expirada (lease vivo)
    trail = _consumed(store, "s1", now=201.0)
    assert len(trail) == 1
    assert trail[0]["reason"] == "expired"
    assert trail[0]["lease_token"] == token


def test_purga_antecipada_no_publish_gera_expired(tmp_path):
    store = _store(tmp_path, ttl_seconds=10.0)
    store.publish("s1", "fato recorrente", now=100.0)
    assert store.publish("s1", "fato recorrente", now=200.0) is True  # pós-TTL
    trail = _consumed(store, "s1", now=201.0)
    assert [e["reason"] for e in trail] == ["expired"]
    assert store.pending_count("s1") == 1  # o fato fresco vive


def test_eviccao_de_cap_gera_evicted(tmp_path):
    store = _store(tmp_path, cap=2)
    store.publish("s1", "fato 1", now=100.0)
    store.publish("s1", "fato 2", now=101.0)
    store.publish("s1", "fato 3", now=102.0)  # evicta o mais antigo
    trail = _consumed(store, "s1", now=103.0)
    assert [(e["text"], e["reason"]) for e in trail] == [("fato 1", "evicted")]


def test_rollback_de_hard_cap_nao_deixa_trilha(tmp_path):
    # Overflow >= 2 exige dois stores com caps diferentes no mesmo path
    # (padrão do test_durable_notice_store): o de cap menor encontra overflow
    # de 2 com só 1 evictável (a outra tem lease vivo) → rollback total.
    path = tmp_path / "state.db"
    wide = DurableNoticeStore(str(path), cap=3)
    wide.publish("s1", "fato 1", now=100.0)
    wide.publish("s1", "fato 2", now=101.0)
    token, _ = wide.claim("s1", now=102.0, lease_seconds=3600.0)  # ambas em voo
    wide.publish("s1", "fato 3", now=103.0)
    narrow = DurableNoticeStore(str(path), cap=2)
    assert narrow.publish("s1", "fato 4", now=104.0) is False  # cap infringível
    assert narrow.consumed(["s1"], now=105.0) == []  # evicção parcial desfeita
    wide.close()
    narrow.close()


# --- contrato intacto ------------------------------------------------------


def test_republicacao_pos_ack_intacta(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "fato X", now=100.0)
    token, _ = store.claim("s1", now=101.0)
    store.ack(token, now=102.0)
    assert store.publish("s1", "fato X", now=103.0) is True  # dedup não vê a trilha
    trail = _consumed(store, "s1", now=104.0)
    assert [e["reason"] for e in trail] == ["acked"]  # o 1º ciclo permanece


def test_trilha_e_log_nao_dedup(tmp_path):
    store = _store(tmp_path)
    for cycle in range(2):
        store.publish("s1", "fato X", now=100.0 + cycle)
        token, _ = store.claim("s1", now=100.2 + cycle)
        store.ack(token, now=100.4 + cycle)
    trail = _consumed(store, "s1", now=200.0)
    assert len(trail) == 2  # N ciclos do mesmo texto são legítimos (sem UNIQUE)


# --- consulta: consumidas × pendentes --------------------------------------


def test_consumed_por_owner_e_lineage(tmp_path):
    store = _store(tmp_path)
    store.publish("root", "fato do pai", now=100.0)
    store.publish("child", "fato do filho", now=101.0)
    store.publish("child", "fato pendente", now=102.0)
    token, rows = store.claim(["root", "child"], now=103.0, limit=2)
    store.ack(token, now=104.0)

    trail = _consumed(store, ["root", "child"], now=105.0)
    assert {e["text"] for e in trail} == {"fato do pai", "fato do filho"}
    assert store.pending_count("child") == 1  # consumidas × pendentes


def test_consumed_ordena_com_tie_break(tmp_path):
    # ack total remove várias rows com o MESMO removed_at — a ordem precisa
    # de tie-break (id DESC) para ser definida.
    store = _store(tmp_path)
    store.publish("s1", "primeiro", now=100.0)
    store.publish("s1", "segundo", now=101.0)
    token, _ = store.claim("s1", now=102.0)
    store.ack(token, now=103.0)
    trail = _consumed(store, "s1", now=104.0)
    assert [e["text"] for e in trail] == ["segundo", "primeiro"]


# --- a trilha nunca vira a nova cauda infinita -----------------------------


def test_cap_da_trilha(tmp_path):
    store = _store(tmp_path)
    for n in range(TRAIL_CAP + 8):
        store.publish("s1", f"fato {n}", now=100.0 + n)
        token, _ = store.claim("s1", now=100.1 + n)
        store.ack(token, now=100.2 + n)
    trail = _consumed(store, "s1", now=1000.0, limit=TRAIL_CAP + 8)
    assert len(trail) == TRAIL_CAP
    assert trail[0]["text"] == f"fato {TRAIL_CAP + 7}"  # mantém as mais recentes


def test_ttl_da_trilha_purga_na_escrita_seguinte(tmp_path):
    store = _store(tmp_path)
    store.publish("s1", "fato velho", now=100.0)
    token, _ = store.claim("s1", now=101.0)
    store.ack(token, now=102.0)
    # muito depois, OUTRO owner acka — a varredura global de TTL alcança s1
    later = 102.0 + TRAIL_TTL_SECONDS + 1
    store.publish("s2", "fato novo", now=later)
    token2, _ = store.claim("s2", now=later + 1)
    store.ack(token2, now=later + 2)
    # a row VELHA foi purgada da própria tabela (não só filtrada na leitura)
    count = store._connection.execute(
        "SELECT COUNT(*) FROM notice_trail WHERE owner_id = 's1'"
    ).fetchone()[0]
    assert count == 0


def test_consumed_filtra_ttl_na_leitura_de_owner_dormente(tmp_path):
    # Owner que nunca mais escreve: a leitura não devolve trilha vencida.
    store = _store(tmp_path)
    store.publish("s1", "fato", now=100.0)
    token, _ = store.claim("s1", now=101.0)
    store.ack(token, now=102.0)
    assert _consumed(store, "s1", now=102.0 + TRAIL_TTL_SECONDS + 1) == []


def test_cap_alcanca_owner_purgado_por_claim_de_outra_lineage(tmp_path):
    # A purga TTL do claim é GLOBAL: tombstones nascem para owners FORA da
    # lineage claimada — o cap/TTL da trilha tem que alcançá-los pelos owners
    # das rows removidas, nunca pelos owner_ids do claim.
    store = _store(tmp_path, ttl_seconds=10.0, cap=128)
    for n in range(TRAIL_CAP + 4):
        store.publish("morto", f"fato {n}", now=100.0 + n * 0.001)
    store.publish("vivo", "gatilho", now=101.0)
    store.claim("vivo", now=500.0)  # purga global expira TUDO de "morto"
    trail = _consumed(store, "morto", now=501.0, limit=TRAIL_CAP + 8)
    assert len(trail) == TRAIL_CAP  # bounded mesmo sem evento próprio do owner


# --- durabilidade / cross-process ------------------------------------------


def test_cross_process_standalone_ve_a_trilha(tmp_path):
    path = tmp_path / "state.db"
    a = DurableNoticeStore(str(path))
    a.publish("s1", "fato", now=100.0)
    token, _ = a.claim("s1", now=101.0)
    a.ack(token, now=102.0)
    b = DurableNoticeStore(str(path))  # outro "processo"
    assert [e["reason"] for e in b.consumed(["s1"], now=103.0)] == ["acked"]
    a.close()
    b.close()


def test_atomicidade_trilha_e_delete(tmp_path):
    # Dropar a trilha por fora → o ack levanta e a notice segue leased e
    # não-deletada: tombstone e DELETE são atômicos (mesma transação).
    store = _store(tmp_path)
    store.publish("s1", "fato", now=100.0)
    token, _ = store.claim("s1", now=101.0)
    saboteur = sqlite3.connect(str(tmp_path / "state.db"))
    saboteur.execute("DROP TABLE notice_trail")
    saboteur.commit()
    saboteur.close()
    try:
        store.ack(token, now=102.0)
        raise AssertionError("ack deveria ter levantado")
    except sqlite3.OperationalError:
        pass
    row = store._connection.execute(
        "SELECT lease_token FROM durable_notices WHERE owner_id = 's1'"
    ).fetchone()
    assert row is not None and row["lease_token"] == token


# --- o caso real que motivou a issue (429) + superfície do operador --------


def test_caso_429_dead_turn_respondivel(tmp_path):
    """O mistério da Wave 6, agora respondível: turno morto por 429 publica a
    dead-turn notice, o turno seguinte a consome — e o rastro explica o quê,
    quando e por qual token, em vez de deixar 'nenhuma evidência'."""
    from lohra.agent.notices_overlay import DEAD_TURN_TTL_SECONDS, build_turn_notice
    from lohra.state import SessionDB

    db = SessionDB(str(tmp_path / "state.db"))
    text = build_turn_notice(
        status="error", error="quota exhausted (429)", error_kind="quota_exhausted"
    )
    assert db.notices.publish(
        "sess-429", text, ttl_seconds=DEAD_TURN_TTL_SECONDS, now=100.0
    )
    token, rows = db.notices.claim("sess-429", now=200.0)
    assert len(rows) == 1
    db.notices.ack(token, now=201.0)

    trail = db.notices.consumed(["sess-429"], now=202.0)
    assert len(trail) == 1
    entry = trail[0]
    assert entry["reason"] == "acked" and entry["lease_token"] == token
    assert "429" in entry["text"] and entry["created_at"] == 100.0
    assert db.notices.pending_count("sess-429") == 0
    db.close()


def test_cli_notices_lista_consumidas_e_pendentes(tmp_path, monkeypatch, capsys):
    from lohra import cli
    from lohra.state import SessionDB

    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    from lohra.memory.paths import state_db_path

    import time as _time

    base = _time.time()  # run_notices lê com o relógio real — o TTL da
    db = SessionDB(str(state_db_path()))  # leitura filtraria timestamps fake
    db.notices.publish("sess-1", "fato consumido", now=base)
    token, _ = db.notices.claim("sess-1", now=base + 1)
    db.notices.ack(token, now=base + 2)
    db.notices.publish("sess-1", "fato pendente", now=base + 3)
    db.close()

    assert cli.run_notices("sess-1") == 0
    out = capsys.readouterr().out
    assert "fato consumido" in out and "acked" in out
    assert "pendente" in out and "1" in out  # pendentes contadas
