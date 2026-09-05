"""#75 — a célula replaiada sob OUTRA política do operador (ou outro harness).

Experimento da issue (H5): um run pausa depois da célula 1 sob
``allow_terminal: true``; o operador fecha a política (``false``); o resume
replaia a célula 1. A predição de H5 é que o replay é SILENCIOSO — hit sem
fault, sem ``reason`` no ``cache.replayed`` — e portanto a auditoria de um run
resumido não consegue dizer sob qual política cada célula rodou.

Decisão do dono (B): marcar, não invalidar. Nada é recomputado — trabalho pago é
trabalho pago — mas o fato fica visível: fault ADVISORY (não degrada; o nó
concluiu) e ``reason`` no evento ``cache.replayed``.

O simulador honesto de "outro processo com outra política" é o mesmo de
``test_workflow_durable_state``: dois ``WorkflowService`` sobre o MESMO
``SessionDB`` em arquivo e o mesmo home — o segundo nasce com ``_runs`` vazio,
como um restart — cada um construído com a sua ``WorkflowPolicy``.
"""

from __future__ import annotations

from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.service import WorkflowService
from tests.test_workflow_pipeline import ScriptedClient

# A célula que conclui ANTES do portão humano, e nada depois dele: o resume
# replaia a célula e responde o portão, então uma implementação correta spawna
# exatamente zero leaves no segundo processo.
_GATED: dict[str, Any] = {
    "meta": {"name": "policy-gated", "version": 1},
    "nodes": [
        {"id": "draft", "type": "agent", "prompt": "Draft ${args.topic}"},
        {"id": "ask", "type": "checkpoint", "prompt": "Ship it?", "depends_on": ["draft"]},
    ],
}


@pytest.fixture
def db(tmp_path):
    """Um store em ARQUIVO: o ponto é o estado que sobrevive ao objeto Python."""
    database = SessionDB(str(tmp_path / "state.db"))
    yield database
    database.close()


def _counting(reply: str = "R"):
    """(responder, counter): todo spawn de leaf incrementa counter[0]."""
    counter = [0]

    def responder(_prompt: str) -> str:
        counter[0] += 1
        return reply

    return responder, counter


def _service(db, home, responder, *, policy: WorkflowPolicy) -> WorkflowService:
    def factory() -> Agent:
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return WorkflowService(
        base_child_factory=factory, db=db, home=home, policy=policy
    )


def _pause_at_gate(svc: WorkflowService) -> str:
    run_id = svc.start(_GATED, {"topic": "kites"})["run_id"]
    out = svc.status(run_id, wait=True, timeout=10)
    assert out["status"] == "paused" and out["reason"] == CHECKPOINT
    return run_id


def _replays(db, run_id: str) -> list[dict[str, Any]]:
    page = db.audit_query(run_id, event_type="cache.replayed", limit=100)
    assert page["availability"] == "available"
    return page["events"]


def test_a_cell_replayed_under_a_narrower_policy_says_so(db, tmp_path):
    """O experimento da issue, escrito no comportamento DESEJADO (decisão B).

    Antes da intervenção ele falha na primeira asserção — que é exatamente o
    registro de H5: o replay não carrega ``reason`` nenhum."""
    responder, calls = _counting()
    svc = _service(db, tmp_path, responder, policy=WorkflowPolicy(allow_terminal=True))
    try:
        run_id = _pause_at_gate(svc)
        assert calls[0] == 1  # o leaf do draft, uma vez
    finally:
        svc.shutdown()

    # O operador FECHA a política entre a pausa e o resume.
    responder2, calls2 = _counting()
    svc2 = _service(db, tmp_path, responder2, policy=WorkflowPolicy(allow_terminal=False))
    try:
        out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out, out
        rollup = svc2.status(run_id, wait=True, timeout=10)
    finally:
        svc2.shutdown()

    # 1. o audit diz POR QUE aquele replay é digno de nota (identificador, nunca prosa)
    replays = _replays(db, run_id)
    assert [row["data"].get("reason") for row in replays] == ["policy_changed"]
    # 2. ...e o run carrega o aviso, nomeando o nó
    advisories = [f for f in rollup["faults"] if "replayed under a different" in f]
    assert len(advisories) == 1
    assert advisories[0].startswith("draft: ")
    # 3. nada foi invalidado: a célula replaiou (zero spawns) e o run fecha limpo —
    #    o aviso é sobre um nó que CONCLUIU, então não degrada.
    assert calls2[0] == 0
    assert rollup["status"] == "complete"
