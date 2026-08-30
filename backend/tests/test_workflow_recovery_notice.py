"""SUP-05 fatia 3 — notice durável da RECUPERAÇÃO de um run órfão (orphaned).

Quando ``WorkflowService._start_unlocked`` detecta um run ``running`` sem lease
vivo (``orphaned``) e o RECUPERA num resume, a sessão DONA ANTERIOR (``prior.owner``)
precisa saber — cross-process, via ``db.notices`` — que:

- o processo que rodava o run parou;
- as células completas foram replayadas do cache;
- o trabalho em voo foi perdido e está sendo re-executado.

Contratos desta fatia (restritos a isso):

- **positivo**: o resume vencedor publica UMA notice para o owner ANTERIOR;
- **target = prior.owner, nunca o owner novo**: um resume retomado por OUTRA
  sessão entrega o fato à sessão que perdeu o run, não à que o pegou;
- **ownerless**: prior.owner vazio/None → NENHUM publish (o store recusaria, e
  o publish nem é tentado — ownerless é recusa no limite do chamador);
- **loser da corrida**: um ``start`` que perde o acquire (busy) nunca publica;
  nada de notice por um resume que não aconteceu;
- **fail isolation**: o notice store quebrado (sqlite busy, disk full) não
  impede o resume — loga e prossegue;
- **dedup natural por texto determinístico**: o texto é uma função do run_id
  apenas (sem timestamp, sem contagem), então repetir a recuperação do MESMO
  run é uma row só; runs diferentes são fatos diferentes;
- **TTL default 7 dias**: o store aplica o default, e a fatia não passa ttl;
- **nenhum broadcast**: um owner só; nunca todas as sessões.

O cenário cross-process real usa DOIS ``WorkflowService`` sobre UM SessionDB
file-backed, com o clock injectado como lista (a mesma aritmética de
``test_workflow_durable_state``): o processo anterior morre, o lease lapa, o
novo processo resume.
"""

from __future__ import annotations

import json
import logging
import pathlib

import pytest

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.state.notices import DEFAULT_TTL_SECONDS
from lohra.workflow.service import WorkflowService
from tests.test_workflow_durable_state import _TWO_NODE, _counting, _service
from tests.test_workflow_quota import TimerFactory


@pytest.fixture
def db(tmp_path):
    """File-backed: dois 'processos' compartilham os mesmos bytes."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _notice_texts(db, owner: str) -> list[str]:
    token, rows = db.notices.claim(owner)
    return [str(row["text"]) for row in rows]


def _notice_rows_raw(db) -> list:
    """Todas as rows do store (para ownerless, que o claim recusa)."""
    return db.notices._connection.execute("SELECT owner_id, text FROM durable_notices").fetchall()


def _blocked_service(db, home, now, *, owner_timers=None):
    """O processo que VAI morrer: seu run fica preso dentro da folha e o lease
    nunca é renovado de novo."""
    return _service(
        db,
        home,
        lambda _p: "R",
        clock=lambda: now[0],
        lease_ttl=100.0,
        lease_timers=owner_timers or TimerFactory(),
    )


# --- positivo: o resume vencedor publica para o owner ANTERIOR ---------------


def test_recovering_an_orphaned_run_publishes_one_notice_to_the_prior_owner(db, tmp_path):
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        assert fresh.status(run_id)["status"] == "running"
        now[0] = 7101.0  # o dono nunca renovou: o lease lapa
        # O RESUME é retomado por OUTRA sessão — e o fato é da sessão antiga.
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        rollup = fresh.status(run_id, wait=True, timeout=10)
        assert rollup["status"] == "complete"
        assert calls[0] == 2  # o run realmente re-executou

        # Target = prior.owner, NUNCA o owner novo.
        assert db.notices.pending_count("sess-2") == 0
        assert db.notices.pending_count("sess-1") == 1
        texts = _notice_texts(db, "sess-1")
        assert len(texts) == 1
        text = texts[0]
        # O notice diz: run id, processo anterior parou, células replayadas,
        # trabalho em voo perdido/re-tomado.
        assert run_id in text
        assert "stopped" in text
        assert "replayed" in text
        assert "lost" in text
    finally:
        fresh.shutdown()
        lost.shutdown()


def test_the_recovery_notice_carries_the_default_ttl(db, tmp_path):
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        now[0] = 7101.0
        fresh.start(resume_run_id=run_id, owner="sess-1")
        fresh.status(run_id, wait=True, timeout=10)
    finally:
        fresh.shutdown()
        lost.shutdown()

    row = _notice_rows_raw(db)[0]
    assert row["owner_id"] == "sess-1"
    # TTL default do store: 7 dias — a fatia não passa ttl próprio.
    ttl = db.notices._connection.execute(
        "SELECT expires_at - created_at FROM durable_notices"
    ).fetchone()[0]
    assert ttl == DEFAULT_TTL_SECONDS


# --- ownerless: sem dono anterior, nenhum publish -----------------------------


def test_an_orphaned_run_with_no_prior_owner_publishes_nothing(db, tmp_path):
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner=None)["run_id"]
        now[0] = 7101.0
        out = fresh.start(resume_run_id=run_id)
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert calls[0] == 2
        # Nem no owner novo (também None), nem em lugar algum: ownerless é
        # recusa no CHAMADOR — o store nem é tentado.
        assert _notice_rows_raw(db) == []
    finally:
        fresh.shutdown()
        lost.shutdown()


def test_a_whitespace_prior_owner_publishes_nothing(db, tmp_path):
    """Um owner de branco não é um owner (o store recusaria com ValueError):
    a fatia trata como ownerless ANTES de tocar o store."""
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner="   ")["run_id"]
        now[0] = 7101.0
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert db.notices.pending_count("sess-2") == 0
        assert _notice_rows_raw(db) == []
    finally:
        fresh.shutdown()
        lost.shutdown()


# --- loser da corrida: resume recusado nunca publica --------------------------


def test_the_losing_resumer_never_publishes_a_recovery_notice(db, tmp_path):
    now = [8000.0]
    owner = _blocked_service(db, tmp_path, now)
    responder, calls = _counting()
    other = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = owner.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        # O lease está VIVO: o resume de `other` perde a corrida.
        refused = other.start(resume_run_id=run_id, owner="sess-2")
        assert "another process" in refused["error"]
        assert calls[0] == 0  # nada rodou atrás da recusa
        assert db.notices.pending_count("sess-1") == 0
        assert db.notices.pending_count("sess-2") == 0
        # E quando o mesmo resumer VENCE depois, publica uma vez só.
        now[0] = 8101.0
        out = other.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        assert other.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert db.notices.pending_count("sess-1") == 1
        assert db.notices.pending_count("sess-2") == 0
    finally:
        other.shutdown()
        owner.shutdown()


# --- fail isolation: notice store quebrado não impede o resume ----------------


def test_a_failing_notice_store_never_blocks_the_recovery(db, tmp_path, monkeypatch, caplog):
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        now[0] = 7101.0

        import sqlite3

        def broken_publish(_owner, _text, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(db.notices, "publish", broken_publish)
        with caplog.at_level(logging.WARNING, logger="lohra.workflow.service"):
            out = fresh.start(resume_run_id=run_id, owner="sess-1")
        # O resume PROSSEGUE: a falha do notice store custa só a notice.
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert calls[0] == 2
        assert any("recovery notice" in rec.message.lower() for rec in caplog.records)
    finally:
        fresh.shutdown()
        lost.shutdown()


# --- dedup: texto determinístico, mesmo run repetido é uma row ----------------


def test_repeating_the_same_recovery_is_one_notice_but_runs_are_distinct(db):
    """Sem processo, sem run: o contrato do TEXTO. Determinístico por run_id —
    republicar a recuperação do MESMO run é um no-op (dedup do store), e outro
    run é outro fato (não dedup)."""
    svc = WorkflowService(
        base_child_factory=lambda: None,  # nunca usado: só o helper de notice
        db=db,
        home=pathlib.Path("/tmp"),
    )
    try:
        svc._publish_recovery_notice("r1", "sess-1")
        svc._publish_recovery_notice("r1", "sess-1")  # mesma recuperação, de novo
        svc._publish_recovery_notice("r2", "sess-1")  # outro run, outro fato
        assert db.notices.pending_count("sess-1") == 2
        texts = sorted(_notice_texts(db, "sess-1"))
        assert "r1" in texts[0] and "r2" in texts[1]
        assert texts[0] != texts[1]
    finally:
        svc.shutdown()


def test_the_notice_text_is_deterministic_across_calls(db):
    svc = WorkflowService(
        base_child_factory=lambda: None,
        db=db,
        home=pathlib.Path("/tmp"),
    )
    try:
        svc._publish_recovery_notice("r1", "sess-1")
        first = _notice_texts(db, "sess-1")[0]
        db.notices.release(
            db.notices._connection.execute("SELECT lease_token FROM durable_notices").fetchone()[0]
        )
        svc._publish_recovery_notice("r1", "sess-1")
        assert _notice_texts(db, "sess-1") == [first]
    finally:
        svc.shutdown()


# --- ponta a ponta: a notice chega pela CAUDA VOLÁTIL da sessão nova ----------


def _turn_client(responses):
    from tests.test_notice_turn_integration import RecordingClient, _text

    return RecordingClient([_text(t) for t in responses])


def test_the_new_session_receives_the_recovery_notice_through_the_volatile_tail(
    tmp_path,
):
    """A prova ponta a ponta do requisito: a sessão DONA do run morre, a MESMA
    sessão (novo processo, nova conexão) volta a existir e o fato da recuperação
    chega a ela pela cauda volátil — request_overlay da PRIMEIRA chamada,
    dentro da user message, fora do system prompt e fora do transcript canônico.

    O cenário: processo 1 cria o run e morre; processo 2 recupera o órfão e
    publica a notice; a conexão do processo 2 é FECHADA e reaberta (nova
    SessionDB sobre o mesmo arquivo); o dono dá um turno e a notice é entregue
    pelo caminho real (claim_lineage_notices + format_notice_overlay +
    run_conversation).
    """
    from tests.test_notice_turn_integration import RecordingClient, _text

    db = SessionDB(str(tmp_path / "state.db"))  # file-backed: sobrevive ao close
    try:
        # --- processo 1: cria o run e morre ---
        now = [7000.0]
        lost = _blocked_service(db, tmp_path, now)
        run_id = lost.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        now[0] = 7101.0  # o dono nunca renovou: lease lapa, run ficou órfão

        # --- processo 2: recupera o órfão e publica ---
        fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
        try:
            out = fresh.start(resume_run_id=run_id, owner="sess-2")
            assert "error" not in out, out
            assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        finally:
            fresh.shutdown()
        lost.shutdown()  # a conexão do processo 1 "morre"

        # fecha e REABRE a conexão: a sessão nova não herda nada em memória
        db.close()
        db2 = SessionDB(str(tmp_path / "state.db"))
        try:
            assert db2.notices.pending_count("sess-1") == 1

            # --- a sessão dona (nova conexão) dá um turno ---
            agent = Agent(
                model="claude-opus-4-8",
                provider=get_provider_profile("anthropic"),
                client=RecordingClient([_text("ok")]),
            )
            db2.create_session("sess-1", model=agent.model)
            session = GatewaySession("sess-1", agent, db2)
            frames: list = []
            session.submit("voltando", frames.append)

            calls = agent.client.calls
            assert len(calls) == 1
            call = calls[0]
            users = [m for m in call["messages"] if m["role"] == "user"]
            assert len(users) == 1, "overlay nunca cria user/user"
            assert "voltando" in users[0]["content"]
            assert run_id in users[0]["content"]
            assert "recovered" in users[0]["content"]
            # fora do system prompt e fora do transcript canônico
            assert run_id not in call["system"]
            assert json.dumps(db2.load_messages("sess-1")).find(run_id) == -1
            # entregue uma vez: acked após persistência limpa
            assert db2.notices.pending_count("sess-1") == 0
        finally:
            db2.close()
    finally:
        db.close()


# --- nenhum broadcast: outros owners não veem a notice ------------------------


def test_no_other_owner_sees_the_recovery_notice(db, tmp_path):
    now = [7000.0]
    lost = _blocked_service(db, tmp_path, now)
    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = lost.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        now[0] = 7101.0
        fresh.start(resume_run_id=run_id, owner="sess-1")
        fresh.status(run_id, wait=True, timeout=10)
    finally:
        fresh.shutdown()
        lost.shutdown()

    # Só o owner anterior tem a notice; uma sessão ANY não herda fatos alheios.
    assert _notice_rows_raw(db)[0]["owner_id"] == "sess-1"
    assert db.notices.pending_count("sess-9") == 0
