"""SUP-05 fatia 2 — WorkflowService.start registra a CANDIDATA de spec inválida.

Quando ``WorkflowService.start`` rejeita uma spec em ``validate_spec`` com
``ValidationError``, isso é falha de AUTORIA de alta confiança — e, quando a
autoria é da AGENTE (superfície ``run_workflow``), deve registrar IMEDIATAMENTE
(antes do return didático) uma CANDIDATA em ``db.insights``:

- ``kind='candidate'`` — nunca promovida para insight/skill aqui;
- mechanism ``validation``, signal ``spec_shape``, confidence 1.0;
- summary didático bounded;
- o retorno didático NUNCA muda por causa do registro (fail isolation);
- spec repetida é dedup de linha (uma lição, uma linha);
- qualquer falha do store é logada e engolida.

Fail-closed quanto à ATRIBUIÇÃO: ``start`` também é chamado por operador/teste,
então a atribuição à agência é EXPLÍCITA — só ``agency_authored=True`` registra.
Specs válidas, quota/provider, downstream null, cancel e infra não registram
nada automaticamente (a evidência de run-time é domínio do slice de outcomes).
"""

from __future__ import annotations

import json
import logging

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.service import WorkflowService
from lohra.workflow.tools import WorkflowTool
from tests.test_loop import FakeClient, _text_response

INVALID_SPEC = {"meta": {}, "nodes": []}  # sem meta.name — rejeitado no validate_spec

SPEC = {
    "meta": {"name": "demo"},
    "nodes": [{"id": "a", "type": "agent", "prompt": "do ${args.task}"}],
}


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _child_factory(reply="ok"):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_text_response(reply)] * 8),
        )

    return factory


def _service(db, tmp_path, reply="ok"):
    return WorkflowService(base_child_factory=_child_factory(reply), db=db, home=tmp_path)


def _insight_rows(db):
    return db.insights.list()


# --- positivo: a candidata nasce antes do return didático ---------------------


def test_invalid_spec_from_tool_records_candidate_and_keeps_didactic_return(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        out = json.loads(WorkflowTool(svc).run({"spec": INVALID_SPEC}))
        # O retorno didático é PRESERVADO (a flag invalid_spec não sobrevive ao
        # tool wrapper — o que importa aqui é a CANDIDATA e a mensagem didática).
        assert "error" in out and "invalid workflow spec" in out["error"]
        rows = _insight_rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "candidate"  # NUNCA 'insight' neste caminho
        assert row["mechanism"] == "validation"
        assert row["responsibility"] == "agency"  # recomputado pelo store
        assert row["confidence"] == 1.0
        assert 0 < len(row["summary"]) <= 500
    finally:
        svc.shutdown()


def test_candidate_recorded_before_return_even_when_return_is_ok_path_error(db, tmp_path):
    """O registro é síncrono ANTES do return: quando start responde, já está no db."""
    svc = _service(db, tmp_path)
    try:
        result = svc.start(INVALID_SPEC, {}, agency_authored=True)
        assert "error" in result
        assert db.insights.count() == 1  # já visível no mesmo instante do return
    finally:
        svc.shutdown()


# --- fail-closed: sem a flag de autoria da agente, nada é atribuído -----------


def test_direct_start_without_agency_flag_records_nothing(db, tmp_path):
    """Operador/teste também chama start — spec inválida SEM a flag explícita
    não é atribuída à agência (proveniência decide, não o erro em si)."""
    svc = _service(db, tmp_path)
    try:
        out = svc.start(INVALID_SPEC, {})
        assert "error" in out and out.get("invalid_spec") is True
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


# --- dedup: a mesma spec inválida repetida é UMA lição ------------------------


def test_repeated_invalid_spec_is_deduplicated(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        for _ in range(3):
            out = svc.start(INVALID_SPEC, {}, agency_authored=True)
            assert "error" in out
        assert db.insights.count() == 1
    finally:
        svc.shutdown()


# --- fail isolation: store quebrado nunca muda o retorno didático -------------


def test_store_failure_is_swallowed_and_return_is_untouched(db, tmp_path, monkeypatch, caplog):
    svc = _service(db, tmp_path)
    try:

        def broken_record(**kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(db.insights, "record", broken_record)
        with caplog.at_level(logging.WARNING, logger="lohra.workflow.service"):
            out = svc.start(INVALID_SPEC, {}, agency_authored=True)
        assert "error" in out and out.get("invalid_spec") is True  # retorno intacto
        assert any("candidate" in rec.message.lower() for rec in caplog.records)
    finally:
        svc.shutdown()


def test_store_record_returning_false_is_fine(db, tmp_path, monkeypatch):
    """Um gate que recusa (False) não é falha: nenhum log de erro, retorno intacto."""
    svc = _service(db, tmp_path)
    try:
        monkeypatch.setattr(db.insights, "record", lambda **kwargs: False)
        out = svc.start(INVALID_SPEC, {}, agency_authored=True)
        assert "error" in out and out.get("invalid_spec") is True
    finally:
        svc.shutdown()


# --- negativos: nada além do validate_spec com agência registra sozinho -------


def test_valid_spec_records_nothing(db, tmp_path):
    svc = _service(db, tmp_path, reply="DONE")
    try:
        run_id = svc.start(SPEC, {"task": "x"}, agency_authored=True)["run_id"]
        final = svc.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


def test_cancelled_run_records_nothing(db, tmp_path):
    svc = _service(db, tmp_path, reply="R")
    try:
        run_id = svc.start(SPEC, {"task": "x"}, agency_authored=True)["run_id"]
        assert svc.cancel(run_id)["ok"] is True
        svc.status(run_id, wait=True, timeout=10)
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


def test_missing_spec_error_records_nothing(db, tmp_path):
    """Erro de launch ANTES do validate_spec (sem spec) não é falha de autoria."""
    svc = _service(db, tmp_path)
    try:
        out = svc.start(None, {}, agency_authored=True)
        assert "error" in out
        assert db.insights.count() == 0
    finally:
        svc.shutdown()


# --- proveniência na WorkflowTool: a flag segue a spec EXPLÍCITA ---------------


class _SpyService:
    """Captura a proveniência que a tool repassa ao start (sem rodar nada)."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start(self, spec, args=None, **kwargs) -> dict:
        self.calls.append((spec, kwargs))
        return {"run_id": "r", "status": "started"}


def test_tool_passes_agency_authored_true_only_for_explicit_spec():
    svc = _SpyService()
    assert "error" not in json.loads(WorkflowTool(svc).run({"spec": SPEC}))
    spec, kwargs = svc.calls[0]
    assert spec is SPEC
    assert kwargs["agency_authored"] is True


def test_tool_passes_agency_authored_false_on_pure_resume():
    """Resume SEM spec explícita repete a spec PERSISTIDA — autoria do passado,
    nunca da agência atual: a flag vai False e o serviço não atribui."""
    svc = _SpyService()
    assert "error" not in json.loads(WorkflowTool(svc).run({"resume_run_id": "r1"}))
    spec, kwargs = svc.calls[0]
    assert spec is None  # a spec herdada NUNCA é apresentada como autoria atual
    assert kwargs["agency_authored"] is False


def test_tool_passes_agency_authored_true_for_explicit_spec_on_resume():
    svc = _SpyService()
    WorkflowTool(svc).run({"spec": SPEC, "resume_run_id": "r1"})
    spec, kwargs = svc.calls[0]
    assert spec is SPEC
    assert kwargs["agency_authored"] is True


# --- shape non-object: a spec do agente chega ao validate_spec ----------------


@pytest.mark.parametrize("bad_spec", [["not", "a", "mapping"], "meta: name", 42])
def test_non_object_spec_reaches_service_and_records_candidate(db, tmp_path, bad_spec):
    """Uma spec explicitamente enviada pelo agente em shape non-object (lista,
    string, escalar) NÃO é recusada na porta da tool: chega ao
    ``validate_spec``, que a rejeita com erro didático — e a falha de AUTORIA
    registra a candidata como qualquer outra spec inválida."""
    svc = _service(db, tmp_path)
    try:
        out = json.loads(WorkflowTool(svc).run({"spec": bad_spec}))
        assert "error" in out and "invalid workflow spec" in out["error"]
        assert "mapping" in out["error"]  # didático: diz o shape esperado
        rows = _insight_rows(db)
        assert len(rows) == 1
        assert rows[0]["kind"] == "candidate"
        assert rows[0]["mechanism"] == "validation"
        assert rows[0]["responsibility"] == "agency"
    finally:
        svc.shutdown()


def test_non_object_spec_via_service_directly_records_candidate(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        result = svc.start(["nope"], {}, agency_authored=True)
        assert "error" in result and result.get("invalid_spec") is True
        assert db.insights.count() == 1
    finally:
        svc.shutdown()


def test_persisted_spec_replayed_on_resume_is_never_attributed(db, tmp_path):
    """Fail-closed no serviço: mesmo que alguém passe agency_authored=True sem
    spec explícita, a spec PERSISTIDA/HERDADA de um resume não é falha de
    autoria da agência ATUAL — nada é registrado."""
    svc = _service(db, tmp_path)
    try:
        run_id = svc.start(SPEC, {"task": "x"}, agency_authored=True)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
        # resume sem spec explícita; flag True NÃO pode atribuir a spec herdada
        out = svc.start(None, {}, resume_run_id=run_id, agency_authored=True)
        assert "error" not in out
        assert db.insights.count() == 0
    finally:
        svc.shutdown()
