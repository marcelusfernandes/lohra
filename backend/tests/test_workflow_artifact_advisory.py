"""#45 — a alegação divergente do leaf é um AVISO, não um veredito.

Decisão do dono: errar um `sha256`/`bytes` por má contagem não é defeito de FORMA
da spec — o arquivo foi escrito, o harness mediu, e a célula guarda a medida. O
que degrada um run é um nó que não conclui, nunca um aviso sobre um nó que
concluiu. `RunResult.advisory_faults` é a terceira lista com as três propriedades
do padrão da Q2 (#43):

- fica em `faults` VERBATIM (relato fail-closed intocado);
- é descontada como MULTISET do veredito, dentro do estirão (`unrecovered`) e
  entre estirões (`carried_faults`);
- é DURÁVEL (`prior_advisory`) e cumulativa, e `fold_nested` a carrega com o
  mesmo namespace `sub[ref]:` dos faults que ela precisa casar de volta.

O que NÃO é aviso (comportamento atual, pinado aqui): `unverifiable`/`missing`
não escrevem fault nenhum — são o VEREDITO da célula — e o estouro do cap de
entradas segue sendo um fault comum: quem não mediu tudo não está avisando sobre
uma alegação, está dizendo que olhou menos do que a spec declarou.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import artifact as artifacts
from lohra.workflow import library
from lohra.workflow.accounting import RunResult, derive_status, unrecovered
from lohra.workflow.artifact import ArtifactScope
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.runstate_store import carried_advisory, carried_faults
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_workflow_artifact_manifest import (
    _Client,
    _core,
    _manifest,
    _run,
    _sha256,
)

_WRONG_SHA = "0" * 64


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


@pytest.fixture
def project(tmp_path):
    """An operator-allowed root with one real artifact in it."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / "report.md"
    target.write_text("the first draft\n", encoding="utf-8")
    return target, ArtifactScope.of(None, WorkflowPolicy(fs_allow=(str(root),)))


def _manifest_node(node_id: str = "writer", **fields: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "prompt": "write the report",
        "schema_ref": "artifact_manifest",
        **fields,
    }


# --- (i) divergência + output = run limpo -----------------------------------


def test_a_divergent_claim_over_a_live_node_seals_the_run_complete(db, project):
    """O caso do dono: o nó produziu, a alegação estava errada, o run fecha
    `complete` — com o aviso na lista de faults, nomeado como aviso."""
    target, scope = project
    client = _Client([_manifest(target, sha256=_WRONG_SHA, bytes=999_999)])
    result = _run(db, "run-1", client, scope)

    assert result.status == "complete"
    assert result.outputs["writer"] is not None and result.null_count == 0
    # Relatado verbatim, e reconciliável: quem lê `complete` ao lado de um fault
    # encontra o mesmo texto na lista que explica por que ele não é um veredito.
    assert len(result.faults) == 1
    assert result.advisory_faults == result.faults
    assert result.faults[0].startswith("writer: artifact")
    assert "advisory" in result.faults[0]
    assert unrecovered(result) is False


def test_the_advisory_text_says_who_is_authoritative(db, project):
    """O texto carrega a divergência (alegado × medido) E quem manda."""
    target, scope = project
    result = _run(db, "run-1", _Client([_manifest(target, sha256=_WRONG_SHA)]), scope)

    fault = result.faults[0]
    assert _WRONG_SHA[:16] in fault and _sha256(target)[:16] in fault
    assert (
        "(advisory: the harness measurement is authoritative; "
        "the cell stores the measured values)"
    ) in fault


def test_a_certified_template_stamps_what_the_run_was_advised_about(tmp_path):
    """`library` certifica o run — e carimba a contagem ao lado de
    `leaf_respawns`, para o próximo autor ler "funciona, e o leaf errou 1
    alegação" em vez de inferir um run sem ressalva."""
    spec = {"meta": {"name": "advised"}, "nodes": [{"id": "a", "type": "agent", "prompt": "x"}]}
    fault = "a: artifact /x: the leaf claimed sha256 0000… (advisory: …)"
    library.record_outcome(
        tmp_path,
        spec,
        RunResult(status="complete", nodes_total=1, faults=[fault], advisory_faults=[fault]),
        artifact_divergences=1,
    )
    assert library.list_templates(tmp_path)[0]["artifact_divergences"] == 1
    assert library.get_template(tmp_path, "advised")["meta"]["artifact_divergences"] == 1


# --- (ii) um nó que NÃO conclui ainda degrada -------------------------------


def test_a_node_that_ends_null_still_degrades_beside_an_advisory(db, project):
    """O aviso não é um passe-livre: o que degrada é o nó que não conclui."""
    target, scope = project
    spec = {
        "meta": {"name": "artifacts", "version": 1},
        "nodes": [_manifest_node(), {"id": "second", "type": "agent", "prompt": "then"}],
    }
    client = _Client([_manifest(target, sha256=_WRONG_SHA), ""])
    result = _run(db, "run-2", client, scope, spec=spec)

    assert result.null_count == 1
    assert result.status == "degraded"


def test_the_verdict_degrades_on_the_null_not_on_the_advice():
    """O discriminador, sem provider no meio: com o aviso descontado, o que
    sobra para degradar é o null — e sozinho o aviso não degrada nada."""
    advice = "writer: artifact /x: … (advisory: …)"
    advised = RunResult(faults=[advice], advisory_faults=[advice], nodes_total=2)
    assert unrecovered(advised) is False
    assert derive_status(advised) == "complete"
    assert derive_status(
        RunResult(faults=[advice], advisory_faults=[advice], null_count=1, nodes_total=2)
    ) == "degraded"


# --- (v) multiset: um desconto retira exatamente um aviso -------------------


def test_two_identical_divergences_are_two_entries_and_nothing_is_laundered(db, project):
    """Uma célula pode errar a MESMA alegação duas vezes, com texto
    BYTE-IDÊNTICO (o mesmo arquivo declarado duas vezes na mesma lista). O
    desconto é multiset dos dois lados, então nada é lavado por semelhança."""
    target, scope = project
    spec = {
        "meta": {"name": "artifacts", "version": 1},
        "nodes": [_manifest_node(schema_ref="artifact_manifests")],
    }
    twice = json.dumps([{"path": str(target), "sha256": _WRONG_SHA}] * 2)
    result = _run(db, "run-3", _Client([twice]), scope, spec=spec)

    assert result.status == "complete"
    assert len(result.faults) == 2 and result.faults[0] == result.faults[1]
    assert result.advisory_faults == result.faults


def test_a_second_divergence_that_merely_reads_alike_is_not_covered():
    """A prova do multiset: uma lista de avisos menor que a de faults idênticos
    degrada — dentro do estirão e entre estirões."""
    advice = "writer: artifact /x: … (advisory: …)"
    twins = RunResult(faults=[advice, advice], advisory_faults=[advice], nodes_total=1)
    assert unrecovered(twins) is True
    assert derive_status(twins) == "degraded"
    assert carried_faults([], twins)[1] is True
    # ...e um par casado não degrada nem aqui nem lá.
    matched = RunResult(faults=[advice, advice], advisory_faults=[advice, advice], nodes_total=1)
    assert carried_faults(["x"], matched) == (["x", advice, advice], False)
    assert derive_status(matched) == "complete"


def test_the_cumulative_list_is_the_sibling_of_carried_faults():
    """`carried_advisory` é o irmão cumulativo de `carried_faults` (padrão da
    Q2): o estirão novo constrói um `RunResult` do zero, e sem a lista durável o
    aviso de um estirão anterior voltaria a ler como falha de ninguém."""
    advice = "writer: artifact /x: … (advisory: …)"
    assert carried_advisory(["old"], RunResult(advisory_faults=[advice])) == ["old", advice]
    assert carried_advisory(["old"], None) == ["old"]


# --- (iv) aninhado: o aviso do filho não condena o pai ----------------------


def test_a_divergence_inside_a_nested_workflow_does_not_degrade_the_parent(db, project):
    """`fold_nested` prefixa os faults do filho; se não prefixasse os AVISOS
    idênticos, o pai não conseguiria casá-los de volta e um sub-workflow que
    apenas errou uma alegação selaria o pai `degraded`."""
    target, scope = project
    child = {
        "meta": {"name": "child", "version": 1},
        "nodes": [_manifest_node("leaf")],
    }
    parent = validate_spec(
        {
            "meta": {"name": "parent"},
            "nodes": [{"id": "sub", "type": "workflow", "ref": "child"}],
        }
    )
    core = _core(db, _Client([_manifest(target, sha256=_WRONG_SHA)]))
    try:
        result = WorkflowEngine(
            core,
            budget=Budget(),
            cache=NodeCache(db, "run-4"),
            run_id="run-4",
            artifact_scope=scope,
            loader={"child": child}.get,
        ).run(parent, {})
    finally:
        core.shutdown()

    assert result.status == "complete"
    assert result.faults == result.advisory_faults
    assert result.faults[0].startswith("sub[child]: leaf: artifact")


# --- (vi) o que NÃO é aviso -------------------------------------------------


def test_a_path_the_harness_may_not_read_writes_no_fault_at_all(db, tmp_path):
    """`unverifiable` é o VEREDITO da célula, não uma queixa: o harness não
    mediu, então não tem alegação nenhuma para contradizer."""
    outside = tmp_path / "outside.md"
    outside.write_text("theirs\n", encoding="utf-8")
    result = _run(
        db,
        "run-5",
        _Client([_manifest(outside, sha256=_WRONG_SHA)]),
        ArtifactScope(),  # nenhuma root: nada é verificável
    )
    assert result.faults == [] and result.advisory_faults == []
    assert result.status == "complete"


def test_a_declared_path_that_is_not_there_writes_no_fault_either(db, tmp_path):
    """`missing` idem: é o veredito da célula, não uma queixa. E o replay não
    cobra nada por ele — `recheck` só re-hasheia entrada `verified`."""
    scope = ArtifactScope.of(tmp_path, None)
    result = _run(db, "run-6", _Client([_manifest(tmp_path / "never.md")]), scope)
    assert result.faults == [] and result.advisory_faults == []
    assert result.status == "complete"


def test_measuring_less_than_the_spec_declared_is_a_fault_not_an_advisory(tmp_path):
    """O estouro do cap não fala de alegação: diz que o harness olhou MENOS do
    que a célula declarou. Segue em `notes`, e degrada como sempre degradou."""
    scope = ArtifactScope.of(tmp_path, None)
    claims = []
    for index in range(artifacts.MAX_ENTRIES + 2):
        made = tmp_path / f"f{index}.md"
        made.write_text(str(index), encoding="utf-8")
        claims.append({"path": str(made)})

    record = artifacts.verify_output(claims, scope)
    assert record is not None
    assert record.divergences == ()
    assert any("only the first" in note for note in record.notes)


def test_the_engine_records_the_cap_note_as_an_ordinary_fault(db, tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts, "MAX_ENTRIES", 1)
    first, second = tmp_path / "a.md", tmp_path / "b.md"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    spec = {
        "meta": {"name": "artifacts", "version": 1},
        "nodes": [_manifest_node(schema_ref="artifact_manifests")],
    }
    answer = json.dumps([{"path": str(first)}, {"path": str(second)}])
    result = _run(
        db, "run-7", _Client([answer]), ArtifactScope.of(tmp_path, None), spec=spec
    )
    assert len(result.faults) == 1 and "only the first" in result.faults[0]
    assert result.advisory_faults == []
    assert result.status == "degraded"


# --- (iii) o aviso sobrevive ao processo que o escreveu ---------------------


_GATED_MANIFEST = {
    "meta": {"name": "advised_gate", "version": 1},
    "nodes": [
        {
            "id": "writer",
            "type": "agent",
            "prompt": "write the report",
            "schema_ref": "artifact_manifest",
        },
        {"id": "ask", "type": "checkpoint", "prompt": "Ship it?", "depends_on": ["writer"]},
    ],
}


def _service(db, home: Path, client: _Client, root: Path) -> WorkflowService:
    return WorkflowService(
        base_child_factory=lambda: Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=client,
        ),
        db=db,
        home=home,
        policy=WorkflowPolicy(fs_allow=(str(root),)),
    )


def test_an_advisory_written_by_a_dead_process_does_not_degrade_the_resume(tmp_path):
    """A metade cross-processo. Estirão 1 diverge e para num portão humano; o
    processo morre; estirão 2 responde o portão num serviço NOVO sobre o mesmo
    banco. O veredito do estirão 2 é calculado num `RunResult` que nunca viu a
    divergência — sem a lista DURÁVEL o run herda o fault por `prior_faults` e
    sela `degraded`, sem certificar nada, por causa de um aviso."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / "report.md"
    target.write_text("the first draft\n", encoding="utf-8")
    db = SessionDB(str(tmp_path / "state.db"))
    try:
        svc = _service(db, tmp_path, _Client([_manifest(target, sha256=_WRONG_SHA)]), root)
        try:
            run_id = svc.start(_GATED_MANIFEST, {})["run_id"]
            paused = svc.status(run_id, wait=True, timeout=10)
            assert paused["status"] == "paused"
            assert len(paused["advisory_faults"]) == 1
        finally:
            svc.shutdown()  # o "kill": o registro morre, o SQLite não

        line = svc._store.load(run_id)
        assert line.prior_degraded is False
        # O aviso é o PRIMEIRO fault da linha; o segundo é o do próprio portão.
        assert line.prior_advisory == line.prior_faults[:1]

        svc2 = _service(db, tmp_path, _Client([_manifest(target, sha256=_WRONG_SHA)]), root)
        try:
            durable = svc2.status(run_id)
            assert durable["status"] == "paused"
            assert durable["advisory_faults"] == line.prior_advisory

            out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
            assert "error" not in out, out
            final = svc2.status(run_id, wait=True, timeout=10)
            assert final["status"] == "complete"
            # Reportado verbatim na lista cumulativa, e reconciliável nela.
            assert "advisory" in "\n".join(final["faults_total"])
            assert final["advisory_faults"] == line.prior_advisory
            assert svc2._store.load(run_id).prior_degraded is False
            # ...e a única prova de que a contagem CUMULATIVA chega ao
            # `record_outcome` por um run de verdade.
            template = json.loads(
                (tmp_path / "workflows" / "templates" / "advised_gate.json").read_text()
            )
            assert template["meta"]["artifact_divergences"] == 1
        finally:
            svc2.shutdown()
    finally:
        db.close()
