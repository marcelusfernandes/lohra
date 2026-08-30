"""SUP-06 (issue #32) — a matriz E2E da supervisão: propriedades CRUZADAS.

As issues SUP-01..SUP-05 provaram cada freio isolado. Esta matriz prova o que
nenhuma delas prova sozinha: o que acontece quando DOIS mecanismos disparam
sobre o MESMO run — cache + morte de processo + pivô de spec; steering +
cancel; cauda volátil + cap de notices + órfãos acumulados; aprendizado sob
repetição e concorrência; duas decisões de recuperação contra um cancel.

A propriedade que a issue pede não é "ela contornou", é "ela NÃO fez o que não
devia, e o que fez ficou registrado". Cada teste aqui é um discriminador: o
número de leaves spawnadas, o dono da notice, o contador durável, a contagem
de insights e a linha final do run são as testemunhas.

Infraestrutura (a mesma de ``test_workflow_recovery_notice`` /
``test_workflow_recovery_fencing``): dois ou mais ``WorkflowService`` sobre UM
``SessionDB`` file-backed (o simulacro honesto de "outro processo"), relógio
injetado como lista, ``TimerFactory`` no lugar de heartbeat real, leaves
travadas em ``threading.Event``. ZERO sleeps reais — toda espera é sobre uma
condição, com teto.
"""

from __future__ import annotations

import logging
import threading

import pytest

from lohra.agent.agent import Agent
from lohra.gateway.session import GatewaySession
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.state.notices import DEFAULT_CAP, MAX_CLAIM
from lohra.workflow.runstate_store import RECOVERED_FAULT
from lohra.workflow.spend import seed_spend
from lohra.workflow.steering import MAX_EXTERNAL_STEERS_PER_RUN
from tests.test_notice_turn_integration import RecordingClient, _text
from tests.test_workflow_durable_state import LEAF_COST, _counting, _service
from tests.test_workflow_quota import TimerFactory

# Teto de qualquer espera-por-condição neste módulo. Nunca é um sleep: o caso
# verde solta o Event em microssegundos; o teto só existe para que um bug de
# produção vire uma falha em segundos em vez de um hang.
GATE_TIMEOUT = 10.0

# --- specs -----------------------------------------------------------------

# Dois nós encadeados: `b` consome a saída de `a`. O PIVÔ do cenário 1 muda só
# `b` — `meta.name`/`meta.version` e o nó `a` ficam byte-idênticos, que é o que
# mantém o cell hash de `a` (namespaced por _spec_id) estável.
_CHAIN = {
    "meta": {"name": "chain", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "draft the thing"},
        {"id": "b", "type": "agent", "prompt": "then extend ${a}"},
    ],
}
_CHAIN_PIVOTED = {
    "meta": {"name": "chain", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "draft the thing"},
        {"id": "b", "type": "agent", "prompt": "instead summarize ${a}"},
    ],
}
_ONE_NODE = {
    "meta": {"name": "single", "version": 1},
    "nodes": [{"id": "a", "type": "agent", "prompt": "work on it"}],
}
# Node type que `validate_spec` recusa — o fault de AUTORIA do cenário 4.
_BAD_SPEC = {
    "meta": {"name": "broken", "version": 1},
    "nodes": [{"id": "x", "type": "telepathy", "prompt": "guess"}],
}


@pytest.fixture
def db(tmp_path):
    """File-backed: vários 'processos' compartilham os mesmos bytes."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


# --- helpers ---------------------------------------------------------------


def _recording(responder):
    """(responder, prompts): guarda o prompt de cada leaf spawnada."""
    prompts: list[str] = []
    lock = threading.Lock()

    def wrapped(prompt: str) -> str:
        with lock:
            prompts.append(prompt)
        return responder(prompt)

    return wrapped, prompts


def _dying_service(db, home, now, responder, **kwargs):
    """Um serviço cujo run VAI ficar órfão: relógio injetado, heartbeat fake e
    shutdown que solta a leaf travada (senão a thread fica pendurada)."""
    gate = kwargs.pop("gate", None)
    svc = _service(
        db,
        home,
        responder,
        clock=lambda: now[0],
        lease_ttl=100.0,
        lease_timers=TimerFactory(),
        **kwargs,
    )
    if gate is not None:
        original_shutdown = svc.shutdown

        def shutdown_releasing_the_leaf() -> None:
            gate.set()
            original_shutdown()

        svc.shutdown = shutdown_releasing_the_leaf  # type: ignore[method-assign]
    return svc


def _cached_cells(db, run_id: str) -> int:
    row = db._connection.execute(
        "SELECT COUNT(*) FROM workflow_node_cache WHERE run_id = ?", (run_id,)
    ).fetchone()
    return int(row[0])


def _owner_rows(db, owner: str) -> list[str]:
    rows = db.notices._connection.execute(
        "SELECT text FROM durable_notices WHERE owner_id = ?", (owner,)
    ).fetchall()
    return [str(row[0]) for row in rows]


def _live_leaf(svc, run_id: str) -> tuple[str, object]:
    """(sub_id, causal_context) da única leaf viva do run neste processo."""
    state = svc._get(run_id)
    assert state is not None and state.core is not None
    sub_ids = list(state.core._children)
    assert len(sub_ids) == 1, sub_ids
    sub_id = sub_ids[0]
    snapshot = state.core.causal_snapshot(sub_id)
    assert snapshot is not None
    return sub_id, snapshot["causal_context"]


# --- 1. morte no meio do pivô ----------------------------------------------


def test_a_pivot_after_process_loss_reuses_the_untouched_cell(db, tmp_path, caplog):
    """Morte no meio do PIVÔ (cache × recuperação × spec adaptada).

    O processo dono completa a célula `a`, trava dentro de `b` e morre (a lease
    lapsa pelo relógio). Um processo NOVO retoma o mesmo run com uma spec
    ADAPTADA — só o nó `b` mudou. Quatro propriedades, cruzadas:

    - a célula intocada é REUSADA: o processo novo spawna exatamente UMA leaf
      (a de `b`), e o prompt dela carrega a saída REAL de `a` vinda do cache —
      não basta contar chamadas, o conteúdo tem de ter fluído;
    - o recovery notice vai ao dono ANTERIOR (quem perdeu o run), nunca ao novo;
    - a cerca AVANÇOU: o straggler do processo morto escreve sob a cerca velha
      e não contamina o cache do vencedor (a célula velha de `b` nunca entra);
    - o run termina `complete`, com o fault de recuperação registrado.
    """
    now = [7000.0]
    gate = threading.Event()
    reached_b = threading.Event()

    def stalls_inside_b(prompt: str) -> str:
        if "then extend" in prompt:
            reached_b.set()
            gate.wait(timeout=GATE_TIMEOUT)
            return "B-from-the-dead-process"
        return "A-ORIGINAL"

    lost = _dying_service(db, tmp_path, now, stalls_inside_b, gate=gate)
    pivot_responder, pivot_prompts = _recording(lambda _p: "B-PIVOTED")
    fresh = _service(
        db, tmp_path, pivot_responder, clock=lambda: now[0], lease_ttl=100.0
    )
    try:
        run_id = lost.start(_CHAIN, {}, owner="sess-lost")["run_id"]
        assert reached_b.wait(timeout=GATE_TIMEOUT), "a leaf de `b` nunca começou"
        # `a` completou e está no cache; `b` está em voo e nunca completará.
        assert _cached_cells(db, run_id) == 1
        fence_before = db.run_fence_of(run_id)

        now[0] = 7101.0  # o dono nunca renovou: a lease lapsa, o run fica órfão
        out = fresh.start(_CHAIN_PIVOTED, resume_run_id=run_id, owner="sess-new")
        assert "error" not in out, out
        rollup = fresh.status(run_id, wait=True, timeout=GATE_TIMEOUT)
        assert rollup["status"] == "complete", rollup

        # (1) a célula intocada foi reusada — UMA leaf, e o conteúdo de `a`
        # chegou nela pelo cache (o processo novo nunca rodou `a`).
        assert len(pivot_prompts) == 1, pivot_prompts
        assert "A-ORIGINAL" in pivot_prompts[0]
        assert "instead summarize" in pivot_prompts[0]

        # (2) o fato da recuperação é do dono ANTERIOR.
        assert db.notices.pending_count("sess-new") == 0
        lost_notices = _owner_rows(db, "sess-lost")
        assert len(lost_notices) == 1
        assert run_id in lost_notices[0] and "recovered" in lost_notices[0]

        # (3) a cerca avançou (a aquisição do processo novo é outra).
        assert db.run_fence_of(run_id) > fence_before

        # (4) o rollup diz, em voz alta, que houve perda de processo.
        assert any(RECOVERED_FAULT in fault for fault in fresh._store.load(run_id).prior_faults)
    finally:
        with caplog.at_level(logging.WARNING, logger="lohra.state.db"):
            fresh.shutdown()
            lost.shutdown()  # solta a leaf presa: ela TENTA escrever a célula velha

    # O straggler do processo morto realmente tentou escrever (o discriminador:
    # sem esta linha, a contagem abaixo passaria só porque nada aconteceu)...
    assert any(
        "refused a stale node cache write" in record.message for record in caplog.records
    ), [record.message for record in caplog.records]
    # ...e foi recusado pela cerca: duas células no total — `a` (reusada) e o
    # `b` PIVOTADO. A célula do `b` VELHO nunca entrou.
    assert _cached_cells(db, run_id) == 2


# --- 2. steering × cancel concorrentes --------------------------------------


def test_steering_and_cancel_race_on_one_run_settle_consistently(db, tmp_path):
    """Duas decisões sobre a MESMA leaf viva: direcionar e cancelar.

    Três fases sobre a leaf travada (que, por construção, NUNCA pode ter LIDO
    o steer — está parada dentro do gate):

    - **fase A, ordem determinística**: o steer é aceito (``queued``) e cobra
      UM slot do orçamento externo DURÁVEL; o cancel que vem depois derruba a
      inbox e o steer é SETTLED como ``discarded`` — o slot durável volta. É a
      propriedade cruzada que nenhum teste por-issue prova: o contador
      cross-process não pode ficar preso num steer que nunca pousou;
    - **fase B, corrida real**: num SEGUNDO run, steer e cancel disparam de
      threads distintas sobre uma barreira. Sem deadlock (ambas retornam), o
      cancel vence limpo, e QUALQUER que seja o interleaving — recusa da
      orquestração ou aceite-e-descarte — o contador durável termina em zero;
    - **fase C**: um steer sobre run já cancelado é recusado no gate de
      liveness, sem tocar o orçamento.
    """
    now = [9000.0]
    gate = threading.Event()
    entered = threading.Semaphore(0)

    def blocks_forever(_prompt: str) -> str:
        entered.release()
        gate.wait(timeout=GATE_TIMEOUT)
        return "never-read"

    svc = _dying_service(db, tmp_path, now, blocks_forever, gate=gate)
    try:
        # --- fase A: steer aceito, depois cancel -> discarded, slot devolvido.
        run_a = svc.start(_ONE_NODE, {}, owner="sess-1")["run_id"]
        assert entered.acquire(timeout=GATE_TIMEOUT), "a leaf de A nunca começou"
        sub_a, ctx_a = _live_leaf(svc, run_a)
        assert db.steering_used(run_a) == 0

        accepted = svc.steer(
            run_a,
            sub_a,
            "corrija o rumo",
            segment_id=ctx_a.segment_id,
            attempt=ctx_a.attempt,
            turn=ctx_a.turn,
        )
        assert accepted["ok"] is True and accepted["queued"] is True, accepted
        assert accepted["receipts"]["run_used"] == 1
        assert accepted["receipts"]["run_used"] <= MAX_EXTERNAL_STEERS_PER_RUN
        assert db.steering_used(run_a) == 1  # o slot durável está cobrado

        assert svc.cancel(run_a) == {"ok": True, "run_id": run_a}
        assert svc._get(run_a).status == "cancelled"
        # A leaf continua travada no gate: o steer NÃO foi lido, foi descartado
        # — e o orçamento durável tem de refletir isso.
        assert db.steering_used(run_a) == 0

        # --- fase C (sobre o run já cancelado): recusa no gate de liveness.
        again = svc.steer(
            run_a,
            sub_a,
            "mais uma tentativa",
            segment_id=ctx_a.segment_id,
            attempt=ctx_a.attempt,
            turn=ctx_a.turn,
        )
        assert "error" in again and "is not running" in again["error"]
        assert "cancelled" in again["error"]
        assert db.steering_used(run_a) == 0

        # --- fase B: a corrida de verdade, num run novo.
        run_b = svc.start(_ONE_NODE, {}, owner="sess-1")["run_id"]
        assert entered.acquire(timeout=GATE_TIMEOUT), "a leaf de B nunca começou"
        sub_b, ctx_b = _live_leaf(svc, run_b)

        results: dict[str, dict] = {}
        barrier = threading.Barrier(2)

        def steer_it() -> None:
            barrier.wait(timeout=GATE_TIMEOUT)
            results["steer"] = svc.steer(
                run_b,
                sub_b,
                "corrija o rumo",
                segment_id=ctx_b.segment_id,
                attempt=ctx_b.attempt,
                turn=ctx_b.turn,
            )

        def cancel_it() -> None:
            barrier.wait(timeout=GATE_TIMEOUT)
            results["cancel"] = svc.cancel(run_b)

        threads = [threading.Thread(target=fn) for fn in (steer_it, cancel_it)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=GATE_TIMEOUT)
            assert not thread.is_alive(), "deadlock entre steer e cancel"

        assert results["cancel"] == {"ok": True, "run_id": run_b}
        assert svc._get(run_b).status == "cancelled"

        steer_out = results["steer"]
        if "error" in steer_out:
            # A orquestração recusou (a sub-sessão já não aceitava): o slot
            # local foi rolled back e o durável liberado junto.
            assert steer_out.get("rolled_back") or "is not running" in steer_out["error"]
        else:
            assert steer_out["queued"] is True

        # A invariante que vale para TODO interleaving: um steer que não pousou
        # não consome orçamento externo cross-process.
        assert db.steering_used(run_b) == 0, steer_out
    finally:
        svc.shutdown()


# --- 3. órfãos + notas acumuladas → cauda de abertura limitada ---------------


def test_a_flood_of_notices_and_orphans_still_opens_a_bounded_turn(db, tmp_path):
    """Cauda volátil sob acúmulo: muitas notas + vários runs órfãos recuperados.

    Publica MAIS notices que o cap do store para uma sessão, soma a isso os
    recovery notices de três runs órfãos recuperados por outro processo, e
    ainda uma notice EXPIRADA. Então a sessão abre um turno pelo caminho real
    (lineage → claim → overlay → run_conversation). Propriedades:

    - **o store respeita o cap por owner**: o total nunca passa de
      ``DEFAULT_CAP``, por mais que se publique;
    - **a entrega do turno é bounded**: no máximo ``MAX_CLAIM`` notices entram
      no overlay, o resto fica pendente para o próximo turno;
    - **expirada não entra**: a notice vencida é purgada no claim e não aparece;
    - **UMA claim, UMA user message**: o overlay entra DENTRO da mensagem do
      usuário — nunca user/user duplo, nunca no system prompt.
    """
    now = [5000.0]
    gate = threading.Event()
    started = threading.Semaphore(0)

    def blocks_forever(_prompt: str) -> str:
        started.release()
        gate.wait(timeout=GATE_TIMEOUT)
        return "lost"

    lost = _dying_service(db, tmp_path, now, blocks_forever, gate=gate)
    fresh = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    orphans: list[str] = []
    try:
        # (a) a enchente de notas operacionais — mais que o cap.
        flood = DEFAULT_CAP + 12
        for index in range(flood):
            assert db.notices.publish("sess-1", f"operational fact #{index}") is True
        # (b) três runs órfãos do mesmo dono, recuperados por outro processo.
        for _ in range(3):
            orphans.append(lost.start(_ONE_NODE, {}, owner="sess-1")["run_id"])
        for _ in orphans:
            assert started.acquire(timeout=GATE_TIMEOUT), "uma leaf órfã nunca começou"
        now[0] = 5101.0  # as três leases lapsam
        for run_id in orphans:
            out = fresh.start(resume_run_id=run_id, owner="sess-9")
            assert "error" not in out, out
            assert fresh.status(run_id, wait=True, timeout=GATE_TIMEOUT)["status"] == "complete"

        # (c) uma notice EXPIRADA, publicada por ÚLTIMO — de propósito. A purga
        #     por TTL do store é GLOBAL e roda em QUALQUER claim (as próprias
        #     leaves dos runs acima abrem turnos e claimam), então uma expirada
        #     publicada antes deles já teria sumido sem que o turno da sessão
        #     provasse nada. Publicada aqui, quem a descarta é o claim DESTE
        #     turno.
        assert db.notices.publish(
            "sess-1", "expired fact that must never reach a turn", ttl_seconds=-1.0
        ) is True

        # O cap por owner é DURO: publicou-se muito além dele e o total não passa.
        assert len(_owner_rows(db, "sess-1")) == DEFAULT_CAP
        before = db.notices.pending_count("sess-1")
        assert before == DEFAULT_CAP

        # (d) o turno da sessão dona, pelo caminho REAL.
        client = RecordingClient([_text("ok")])
        agent = Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
        )
        db.create_session("sess-1", model=agent.model)
        session = GatewaySession("sess-1", agent, db)
        session.submit("voltei", lambda _frame: None)

        assert len(client.calls) == 1
        call = client.calls[0]
        users = [m for m in call["messages"] if m["role"] == "user"]
        assert len(users) == 1, "o overlay nunca cria user/user"
        content = users[0]["content"]
        assert "voltei" in content
        assert "AVISOS OPERACIONAIS" in content
        assert "expired fact" not in content  # expirada foi purgada no claim
        assert "expired fact" not in call["system"]  # e não vazou pro Invariante #1

        delivered = [line for line in content.splitlines() if line.startswith("- ")]
        assert 1 <= len(delivered) <= MAX_CLAIM  # a cauda é BOUNDED

        # A conta fecha: entregues + a expirada purgada saíram do store; o
        # excedente continua pendente para o PRÓXIMO turno (nunca perdido).
        assert not any("expired fact" in text for text in _owner_rows(db, "sess-1"))
        after = db.notices.pending_count("sess-1")
        assert after == before - len(delivered) - 1
        assert after > 0
    finally:
        fresh.shutdown()
        lost.shutdown()


# --- 4. aprendizado sem ruído sob repetição e concorrência -------------------


def test_learning_is_deduped_under_repetition_and_concurrency(db, tmp_path):
    """O MESMO fault aprendível N vezes (e de dois 'processos') → UM insight.

    Três fases, nesta ordem, porque as contagens só discriminam encadeadas:

    - **não-aprendível**: uma spec inválida enviada por um caller que NÃO é a
      agência (``agency_authored=False``) não vira memória — zero candidatos;
    - **repetição**: a MESMA spec inválida autorada pela agência N vezes é UM
      candidato (dedup semântico por fingerprint de conteúdo);
    - **concorrência cross-process**: dois serviços sobre dois ``SessionDB``
      apontando para o MESMO arquivo, disparando juntos, continuam sendo UM —
      o ``BEGIN IMMEDIATE`` + PK arbitra o vencedor, não a ordem de chegada.
    """
    svc = _service(db, tmp_path, _counting()[0])
    other_db = SessionDB(str(tmp_path / "state.db"))  # o 'outro processo'
    other = _service(other_db, tmp_path, _counting()[0])
    try:
        # (1) fault NÃO-aprendível: spec inválida que a agência não autorou.
        out = svc.start(_BAD_SPEC, {})
        assert out["invalid_spec"] is True
        assert db.insights.count() == 0, db.insights.list()

        # (2) o MESMO fault aprendível, repetido: uma linha só.
        for _ in range(4):
            repeated = svc.start(_BAD_SPEC, {}, agency_authored=True)
            assert repeated["invalid_spec"] is True
        assert db.insights.count() == 1
        row = db.insights.list()[0]
        assert row["kind"] == "candidate"
        assert row["responsibility"] == "agency"
        assert "validate_spec" in row["summary"]

        # (3) dois 'processos' disparando o mesmo fault ao mesmo tempo.
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def author(service) -> None:
            try:
                barrier.wait(timeout=GATE_TIMEOUT)
                service.start(_BAD_SPEC, {}, agency_authored=True)
            except BaseException as exc:  # noqa: BLE001 — reportado no assert
                errors.append(exc)

        threads = [
            threading.Thread(target=author, args=(service,)) for service in (svc, other)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=GATE_TIMEOUT)
            assert not thread.is_alive()
        assert errors == []
        assert db.insights.count() == 1
        # E o outro processo LÊ a mesma linha única (é o mesmo arquivo).
        assert other_db.insights.count() == 1

        # Dedup NÃO é silêncio: um fault aprendível DIFERENTE continua sendo
        # um fato distinto (senão o "1 insight" acima seria só um store mudo).
        distinct = svc.start(
            {
                "meta": {"name": "other", "version": 1},
                "nodes": [{"id": "y", "type": "clairvoyance"}],
            },
            {},
            agency_authored=True,
        )
        assert distinct["invalid_spec"] is True
        assert db.insights.count() == 2
    finally:
        other.shutdown()
        other_db.close()
        svc.shutdown()


# --- 5. duas decisões sobre o mesmo run --------------------------------------


def test_two_resumes_and_an_ownerless_cancel_leave_one_consistent_line(db, tmp_path):
    """Dois processos retomam o MESMO run órfão enquanto um terceiro cancela.

    A corrida é aberta de forma determinística com o padrão intercepted do
    fencing: o segundo resumidor e o cancel pousam EXATAMENTE entre o snapshot
    pré-acquire do primeiro e o acquire dele. Propriedades:

    - **no máximo um resume vence**: o perdedor leva o erro cercado/busy e não
      spawna nada — nunca dois engines sobre um node cache;
    - **o cancel ownerless não atropela**: o processo que só conhece a LINHA do
      run (sem registry, sem lease) recusa com ``busy`` em vez de escrever
      "cancelled" por cima de quem está trabalhando;
    - **linha final consistente**: o dono da linha é o vencedor, o status é
      terminal e não sobra lease órfã;
    - **budget não duplicado**: o run pagou exatamente as duas leaves que
      realmente rodaram — a stretch morta travou ANTES de completar qualquer
      célula, então nada dela foi contabilizado duas vezes.
    """
    now = [4000.0]
    gate = threading.Event()
    entered = threading.Event()

    def blocks_at_the_first_node(_prompt: str) -> str:
        entered.set()
        gate.wait(timeout=GATE_TIMEOUT)
        return "dead-stretch"

    # A leaf do VENCEDOR também espera um gate: enquanto ela não solta, o run
    # dele não pode terminar, então a lease continua viva e o cancel ownerless
    # encontra deterministicamente um run em andamento (sem o gate, um run de
    # duas leaves rápidas podia acabar antes do cancel e o teste flakearia).
    winner_gate = threading.Event()

    def waits_for_the_test(_prompt: str) -> str:
        winner_gate.wait(timeout=GATE_TIMEOUT)
        return "winner-leaf"

    lost = _dying_service(db, tmp_path, now, blocks_at_the_first_node, gate=gate)
    loser_responder, loser_calls = _counting()
    loser = _service(db, tmp_path, loser_responder, clock=lambda: now[0], lease_ttl=100.0)
    winner = _service(
        db, tmp_path, waits_for_the_test, clock=lambda: now[0], lease_ttl=100.0
    )
    canceller = _service(db, tmp_path, _counting()[0], clock=lambda: now[0], lease_ttl=100.0)
    outcomes: dict[str, dict] = {}
    try:
        run_id = lost.start(_CHAIN, {}, owner="sess-lost")["run_id"]
        assert entered.wait(timeout=GATE_TIMEOUT)
        assert _cached_cells(db, run_id) == 0  # a stretch morta não completou nada
        lost._store.release(run_id)  # a lease do dono morto é solta...
        now[0] = 4101.0  # ...e o relógio anda: órfão de verdade

        def the_others_decide_first(_run_id: str) -> None:
            # Dentro da janela snapshot→acquire do PERDEDOR: o vencedor toma a
            # lease e o cancel ownerless chega logo atrás.
            outcomes["winner"] = winner.start(resume_run_id=run_id, owner="sess-win")
            outcomes["cancel"] = canceller.cancel(run_id)

        original_acquire = loser._store.acquire

        def acquire(target: str) -> bool:
            the_others_decide_first(target)
            return original_acquire(target)

        loser._store.acquire = acquire  # type: ignore[method-assign]
        outcomes["loser"] = loser.start(resume_run_id=run_id, owner="sess-lose")

        # (1) exatamente UM resume venceu.
        assert "run_id" in outcomes["winner"], outcomes["winner"]
        assert "error" in outcomes["loser"], outcomes["loser"]
        assert loser_calls[0] == 0, "o perdedor não pode ter spawnado nada"

        # (2) o cancel ownerless recusou em vez de escrever por cima.
        assert "error" in outcomes["cancel"], outcomes["cancel"]
        assert "another process" in outcomes["cancel"]["error"]
        # ...e a linha continua sendo a de um run EM ANDAMENTO, não "cancelled".
        assert winner._store.load(run_id).status == "running"

        winner_gate.set()  # só agora o vencedor termina
        assert winner.status(run_id, wait=True, timeout=GATE_TIMEOUT)["status"] == "complete"

        # (3) a linha final é a do vencedor, e não sobra lease.
        line = winner._store.load(run_id)
        assert line.owner == "sess-win"
        assert line.status == "complete"
        assert winner._store.lease_expiry(run_id) is None

        # (4) budget não duplicado: as duas leaves que realmente rodaram
        # (a stretch morta não completou nenhuma célula).
        assert sum(seed_spend(db, run_id)) == 2 * LEAF_COST
        assert _cached_cells(db, run_id) == 2
    finally:
        winner_gate.set()  # nunca deixa a leaf do vencedor pendurada
        canceller.shutdown()
        winner.shutdown()
        loser.shutdown()
        lost.shutdown()
