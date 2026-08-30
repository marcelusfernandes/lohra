"""SUP-05 — fenced recovery: os fatos da recuperação são lidos DEPOIS da cerca.

Três contratos de corrida (issue #12 + SUP-05), restritos a isso:

1. **re-read pós-acquire**: ``orphaned``/``prior``/``owner`` usados pela
   recuperação são relidos DEPOIS de adquirir lease/fence — nunca do snapshot
   pré-acquire, que o último write cercado do dono anterior pode ter tornado
   mentira entre a leitura e a aquisição;
2. **persist cercado decide o launch**: só há registro, notice, leaf e engine
   se ``_persist_state(state)`` retornar True; um False cercado aborta o launch
   sem notice/leaf/PLAN e limpa registry/core/lease;
3. **notice vai ao prior owner do snapshot PÓS-acquire**: se a linha mudou de
   dono antes da cerca ser nossa, o fato da recuperação vai para quem PERDEU o
   run segundo a linha atual — nunca para um dono que o snapshot velho citava.

A infraestrutura é a mesma de ``test_workflow_recovery_notice``: dois serviços
sobre um SessionDB file-backed com clock injectado; a corrida é aberta por um
wrapper em ``_store.acquire`` do serviço recuperador, que faz o último write do
dono anterior pousar EXATAMENTE entre o snapshot pré-acquire e a aquisição.
"""

from __future__ import annotations

import threading

import pytest

from lohra.state import SessionDB
from lohra.workflow.runstate_store import RECOVERED_FAULT
from tests.test_workflow_durable_state import _TWO_NODE, _counting, _service
from tests.test_workflow_quota import TimerFactory


@pytest.fixture
def db(tmp_path):
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _blocked_service(db, home, now, owner: str):
    """O processo que VAI morrer: cria o run `running` e nunca mais renova.

    O responder é deterministamente BLOQUEADO numa `threading.Event`: a leaf
    original não pode terminar nem publicar nada enquanto a corrida de recovery
    acontece — o `lost` não contamina os asserts do recuperador. Solte SEMPRE
    com ``_unblock(svc)`` antes do shutdown (senão a thread fica pendurada).
    """
    gate = threading.Event()

    def blocked(_prompt):
        gate.wait(timeout=30)
        return "R"

    svc = _service(
        db,
        home,
        blocked,
        clock=lambda: now[0],
        lease_ttl=100.0,
        lease_timers=TimerFactory(),
    )
    svc._blocked_gate = gate
    run_id = svc.start(_TWO_NODE, {}, owner=owner)["run_id"]
    assert svc.status(run_id)["status"] == "running"
    return svc, run_id


def _unblock(svc):
    """Libera a leaf bloqueada e desliga o serviço 'perdido' — nunca vaza."""
    svc._blocked_gate.set()
    svc.shutdown()


def _intercepted_acquire(store, before_acquire):
    """Faz `before_acquire` pousar entre o snapshot pré-acquire e o acquire."""
    original = store.acquire

    def acquire(run_id: str) -> bool:
        before_acquire(run_id)
        return original(run_id)

    store.acquire = acquire  # type: ignore[method-assign]


# --- 1. os fatos de recovery são relidos DEPOIS da cerca --------------------


def test_recovery_facts_are_reread_after_the_fence(db, tmp_path):
    """O dono anterior TERMINA a linha (status 'failed') entre o snapshot
    pré-acquire do recuperador e o acquire: um run que já acabou não é órfão —
    sem notice, sem falta de recuperação, mesmo com o snapshot dizendo
    'running'."""
    now = [7000.0]
    lost, run_id = _blocked_service(db, tmp_path, now, "sess-1")
    now[0] = 7101.0  # lease lapa — o snapshot pré-acquire ainda dirá 'running'

    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:

        def last_write_lands(run_id: str) -> None:
            # O dono anterior, morrendo, consegue pousar seu ÚLTIMO write
            # cercado: a linha deixa de ser 'running' ANTES de perdermos a
            # corrida pela lease.
            lost._store.release(run_id)
            assert lost._store.save(
                run_id=run_id,
                owner="sess-1",
                status="failed",
                spec=_TWO_NODE,
                args={},
            )

        _intercepted_acquire(fresh._store, last_write_lands)
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"

        # A linha pós-acquire diz 'failed': NADA a recuperar.
        assert db.notices.pending_count("sess-1") == 0
        assert db.notices.pending_count("sess-2") == 0
        faults = fresh._store.load(run_id).prior_faults
        assert not any(RECOVERED_FAULT in fault for fault in faults)
    finally:
        fresh.shutdown()
        _unblock(lost)


def test_a_not_yet_dead_lease_at_snapshot_is_not_enough(db, tmp_path):
    """A leitura de liveness ('lease_expiry is None') é feita ANTES do acquire e
    o veredito de órfão DEPOIS: uma linha que o dono anterior marca terminal
    dentro da janela não vira recuperação só porque o lease parecia livre."""
    now = [7000.0]
    lost, run_id = _blocked_service(db, tmp_path, now, "sess-1")
    # lease ainda VIVA no snapshot; dentro da janela o dono solta e terminaliza.
    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:

        def last_write_lands(run_id: str) -> None:
            lost._store.release(run_id)
            assert lost._store.save(
                run_id=run_id,
                owner="sess-1",
                status="paused",
                pause_reason="checkpoint",
                spec=_TWO_NODE,
                args={},
            )

        _intercepted_acquire(fresh._store, last_write_lands)
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"
        assert db.notices.pending_count("sess-1") == 0
        faults = fresh._store.load(run_id).prior_faults
        assert not any(RECOVERED_FAULT in fault for fault in faults)
    finally:
        fresh.shutdown()
        _unblock(lost)


# --- 3. a notice vai ao prior owner do snapshot PÓS-acquire -----------------


def test_the_recovery_notice_goes_to_the_post_acquire_prior_owner(db, tmp_path):
    """A linha muda de DONO (sess-1 → sess-9) dentro da janela snapshot/acquire:
    quem perde o run segundo a linha atual é sess-9 — a notice é DELE, nunca do
    dono que o snapshot velho citava."""
    now = [7000.0]
    lost, run_id = _blocked_service(db, tmp_path, now, "sess-1")
    now[0] = 7101.0

    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    try:

        def owner_changes(run_id: str) -> None:
            lost._store.release(run_id)
            # O último write cercado do dono anterior transfere a linha.
            assert lost._store.save(
                run_id=run_id,
                owner="sess-9",
                status="running",
                spec=_TWO_NODE,
                args={},
            )

        _intercepted_acquire(fresh._store, owner_changes)
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "error" not in out, out
        assert fresh.status(run_id, wait=True, timeout=10)["status"] == "complete"

        assert db.notices.pending_count("sess-2") == 0
        assert db.notices.pending_count("sess-9") == 1  # o dono PÓS-acquire
        assert db.notices.pending_count("sess-1") == 0  # o snapshot velho mente
    finally:
        fresh.shutdown()
        _unblock(lost)


# --- 2. persist cercado recusado aborta o launch ----------------------------


def test_a_fenced_state_refusal_aborts_the_launch_cleanly(db, tmp_path):
    """``_persist_state`` False no launch (dono mais novo levou a linha entre o
    acquire e o primeiro write): erro cercado, sem notice, sem leaf, sem PLAN,
    e registry/core/lease limpos."""
    now = [7000.0]
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        fresh._persist_state = lambda state: False  # a cerca recusa o 1º write
        out = fresh.start(_TWO_NODE, {}, owner="sess-1")
        assert "lost its ownership fence" in out["error"]
        assert calls[0] == 0  # nenhuma leaf spawned
        # Sem registry entry (nada vai terminá-lo), sem linha escrita e nada
        # pendurado no store.
        assert fresh._runs == {}
        assert fresh._store.recent(10) == []
    finally:
        fresh.shutdown()


def test_a_fenced_refusal_on_a_recovery_publishes_no_notice(db, tmp_path):
    """O mesmo abort no caminho de RECUPERAÇÃO: a notice nunca é publicada — um
    stretch que não pode nem escrever sua linha não anuncia que recuperou o
    run."""
    now = [7000.0]
    lost, run_id = _blocked_service(db, tmp_path, now, "sess-1")
    now[0] = 7101.0
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        fresh._persist_state = lambda state: False
        out = fresh.start(resume_run_id=run_id, owner="sess-2")
        assert "lost its ownership fence" in out["error"]
        assert calls[0] == 0
        assert db.notices.pending_count("sess-1") == 0
        assert db.notices.pending_count("sess-2") == 0
        assert all(s.run_id != run_id for s in fresh._runs.values())
        # A linha de 'running' do dono anterior permanece intacta.
        assert fresh._store.load(run_id).status == "running"
    finally:
        fresh.shutdown()
        _unblock(lost)


def test_a_fenced_refusal_leaves_the_winner_intact(db, tmp_path):
    """CAS de verdade: quem recusou não destrói o estado do dono que venceu —
    o vencedor continua dono da linha e da lease."""
    now = [7000.0]
    winner = _service(db, tmp_path, lambda _p: "W", clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = winner.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        now[0] = 7101.0  # a lease do vencedor lapa; a linha continua 'running'
        loser = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
        try:
            # O vencedor RENOVEIA dentro da janela do perdedor: a cerca do
            # perdedor já não é a atual no primeiro write.
            def winner_reacquires(run_id: str) -> None:
                assert winner._store.acquire(run_id)

            _intercepted_acquire(loser._store, winner_reacquires)
            # O vencedor tem o run vivo no registry? Não — é outro processo;
            # o acquire dele é o que fala. O perdedor escreve com cerca velha.
            out = loser.start(resume_run_id=run_id, owner="sess-2")
            # O perdedor levou o busy (a lease do vencedor está viva de novo)
            # ou o abort cercado — em ambos, sem notice e sem segundo engine.
            assert "error" in out
            assert db.notices.pending_count("sess-1") == 0
            # A linha segue dizendo o que o VENCEDOR escreveu.
            assert loser._store.load(run_id).owner == "sess-1"
        finally:
            loser.shutdown()
        assert winner.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        winner.shutdown()


def test_a_launch_that_raises_after_the_persist_aborts_cleanly(db, tmp_path):
    """O caminho de exceção pós-registro continua devolvendo lease e core —
    regressão do cleanup que o abort cercado compartilha."""
    now = [7000.0]
    responder, calls = _counting()
    fresh = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
    try:
        original = fresh._persist_state

        def persist_then_raise(state):
            original(state)
            raise RuntimeError("boom after persist")

        fresh._persist_state = persist_then_raise
        with pytest.raises(RuntimeError):
            fresh.start(_TWO_NODE, {}, owner="sess-1")
        assert calls[0] == 0
        assert fresh._runs == {}
    finally:
        fresh.shutdown()


# --- 2b. o LEDGER cercado também decide o launch (SUP-05) --------------------


def test_a_fenced_ledger_refusal_after_an_accepted_line_aborts_the_launch(
    db, tmp_path
):
    """Corrida REAL e determinística entre dois processos sobre um SessionDB
    file-backed: a lease do recuperador (perdedor) lapsa DENTRO da janela
    linha→ledger do próprio launch e o dono original readquire ali, de modo
    que a linha do perdedor foi aceita sob a cerca antiga e a PRIMEIRA
    recusa cercada só acontece no ledger — ``_persist_spend`` DEPOIS do
    ``_persist_state`` aceito. O launch inteiro tem de abortar ali: erro
    cercado, sem notice, sem PLAN, sem audit gap, sem leaf, sem engine, e
    registry/core/lease limpos — sem tocar na linha do vencedor."""
    now = [7000.0]
    winner = _service(db, tmp_path, lambda _p: "W", clock=lambda: now[0], lease_ttl=100.0)
    try:
        run_id = winner.start(_TWO_NODE, {}, owner="sess-1")["run_id"]
        # A lease do vencedor é deixada LAPAR na linha (release + relógio) —
        # o snapshot pré-acquire do perdedor então diz "órfão de verdade". O
        # run do vencedor corre concorrente e sem controle durante a corrida;
        # o determinismo do teste vem da monotonicidade da cerca (todo write
        # da thread do vencedor apresenta a cerca velha e é recusado assim que
        # ela avança), não de segurar a leaf (achado 6 do review adversarial:
        # a coreografia de leaf bloqueada era código morto — o factory é
        # capturado por valor no lançamento).
        winner._store.release(run_id)  # lease solta; a linha segue 'running'
        now[0] = 7101.0

        responder, calls = _counting()
        loser = _service(db, tmp_path, responder, clock=lambda: now[0], lease_ttl=100.0)
        try:
            plan_events: list[tuple] = []
            original_emit = loser._events.emit

            def spy_emit(run_id_: str, kind: str, payload: dict) -> bool:
                plan_events.append((run_id_, kind))
                return original_emit(run_id_, kind, payload)

            loser._events.emit = spy_emit  # type: ignore[method-assign]

            original_persist_state = loser._persist_state

            def line_lands_then_winner_takes_over(state) -> bool:
                # A linha do perdedor pousa sob a cerca DELE — aceita. Então a
                # lease do perdedor LAPSA (relógio) dentro da janela linha→ledger
                # e o dono original readquire: a cerca avança, a linha do
                # vencedor pousa por cima, e o ledger do perdedor apresenta a
                # cerca VELHA — a PRIMEIRA recusa cercada do launch é no ledger.
                ok = original_persist_state(state)
                assert ok, "a linha do perdedor tinha de ser aceita sob a cerca dele"
                now[0] += 200.0  # lease do perdedor lapsa mid-launch
                assert winner._store.acquire(state.run_id)
                assert winner._store.save(
                    run_id=state.run_id,
                    name=_TWO_NODE["meta"]["name"],
                    owner="sess-1",
                    status="running",
                    spec=_TWO_NODE,
                    args={},
                )
                return ok

            loser._persist_state = line_lands_then_winner_takes_over  # type: ignore[method-assign]
            out = loser.start(resume_run_id=run_id, owner="sess-2")

            assert "lost its ownership fence" in out["error"], out
            assert calls[0] == 0  # nenhuma leaf spawned
            assert all(kind != "PLAN" for _, kind in plan_events)
            assert db.notices.pending_count("sess-1") == 0
            assert db.notices.pending_count("sess-2") == 0
            # Registry limpo: o abort cercado não pode deixar um estado que lê
            # como run vivo (nem um core pendurado por trás dele).
            assert all(s.run_id != run_id for s in loser._runs.values())
            # A linha do VENCEDOR está intacta.
            assert loser._store.load(run_id).owner == "sess-1"
        finally:
            loser.shutdown()
        assert winner.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        winner.shutdown()


def _leaf_factory(responder):
    from lohra.agent.agent import Agent
    from lohra.providers import get_provider_profile
    from tests.test_workflow_pipeline import ScriptedClient

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return factory
