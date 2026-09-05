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

import json
from typing import Any

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import library
from lohra.workflow.accounting import unrecovered
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.cell_stamp import (
    REASON_HARNESS_VERSION_CHANGED,
    REASON_POLICY_AND_HARNESS_VERSION_CHANGED,
    REASON_POLICY_CHANGED,
    CellStamp,
    divergence,
    policy_fingerprint,
)
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.gates import CHECKPOINT
from lohra.workflow.sandbox import WorkflowPolicy
from lohra.workflow.schema import validate_spec
from lohra.workflow.service import WorkflowService
from tests.test_workflow_artifact_manifest import _manifest
from tests.test_workflow_pipeline import ScriptedClient
from tests.test_workflow_token_budget import _core

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


# --- o mesmo experimento sem provider: engine + NodeCache, dois estirões -----


_TWO_NODE: dict[str, Any] = {
    "meta": {"name": "stamped", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _stretch(db, run_id: str, *, policy: WorkflowPolicy | None, spec=None):
    """Um estirão do run sob UMA política: (result, spawns)."""
    responder, calls = _counting()
    core = _core(db, responder)
    cache = NodeCache(
        db, run_id, stamp=None if policy is None else CellStamp.current(policy)
    )
    engine = WorkflowEngine(core, budget=Budget(), cache=cache, run_id=run_id)
    try:
        return engine.run(validate_spec(spec or _TWO_NODE), {}), calls[0]
    finally:
        core.shutdown()


def _advisories(result) -> list[str]:
    return [f for f in result.faults if "replayed under a different" in f]


def test_nothing_is_invalidated_when_the_policy_moves(db):
    """A garantia central da decisão B: a célula divergente REPLAIA — o run não
    paga de novo por trabalho que já pagou."""
    first, spawns = _stretch(db, "run-A", policy=WorkflowPolicy(allow_terminal=True))
    assert first.status == "complete" and spawns == 2

    second, respawns = _stretch(db, "run-A", policy=WorkflowPolicy(allow_terminal=False))
    assert respawns == 0  # tudo replaiado, nada recomputado
    assert second.status == "complete"  # o aviso não degrada um nó que concluiu
    assert len(_advisories(second)) == 2  # um por célula replaiada
    assert second.advisory_faults == second.faults
    assert second.replay_divergences == 2
    assert unrecovered(second) is False


def test_the_same_policy_written_in_another_order_is_the_same_policy(db):
    """Canonicidade: ordenar diferente ``workflow_policy.json`` não é mudar de
    política, e um fingerprint que dissesse o contrário avisaria sobre todo run
    de um operador que só arrumou o arquivo."""
    wide = WorkflowPolicy(
        mcp_allow=("git", "notion"), egress_allow=("API.test", "b.test"),
        fs_allow=("/x", {"path": "/y", "mode": "ro"}),
    )
    shuffled = WorkflowPolicy(
        mcp_allow=("notion", "git"), egress_allow=("b.test", "api.TEST"),
        fs_allow=({"path": "/y", "mode": "ro"}, "/x"),
    )
    assert policy_fingerprint(wide) == policy_fingerprint(shuffled)

    _stretch(db, "run-B", policy=wide)
    second, _ = _stretch(db, "run-B", policy=shuffled)
    assert _advisories(second) == []
    assert second.replay_divergences == 0


@pytest.mark.parametrize(
    "narrowed",
    [
        WorkflowPolicy(mcp_allow=("git",), egress_allow=("api.test",), fs_allow=("/x",)),
        WorkflowPolicy(
            mcp_allow=("git", "notion"), egress_allow=("api.test",), fs_allow=("/x",)
        ),
        WorkflowPolicy(
            mcp_allow=("git", "notion"), egress_allow=("api.test", "b.test"), fs_allow=()
        ),
        WorkflowPolicy(
            mcp_allow=("git", "notion"), egress_allow=("api.test", "b.test"),
            fs_allow=({"path": "/x", "mode": "ro"},),
        ),
    ],
    ids=["mcp", "egress", "fs_root", "fs_mode"],
)
def test_every_gate_the_sandbox_applies_is_in_the_fingerprint(narrowed):
    """As quatro classes de capacidade que ``sandbox_dispatch`` guarda — não só
    as três nomeadas na issue. Deixar ``egress_allow`` de fora faria "mesma
    política" uma afirmação que o harness não sustenta.

    O MODO de uma root conta: ``ro`` e ``rw`` são políticas diferentes."""
    wide = WorkflowPolicy(
        mcp_allow=("git", "notion"), egress_allow=("api.test", "b.test"), fs_allow=("/x",)
    )
    assert policy_fingerprint(wide) != policy_fingerprint(narrowed)


def test_an_upgrade_between_stretches_is_named_with_both_versions(db, monkeypatch):
    """O outro fato: o binário mudou no meio de um run longo."""
    import lohra

    monkeypatch.setattr(lohra, "__version__", "0.0.24")
    _stretch(db, "run-C", policy=WorkflowPolicy())
    monkeypatch.setattr(lohra, "__version__", "0.0.25")
    second, respawns = _stretch(db, "run-C", policy=WorkflowPolicy())

    assert respawns == 0
    advisories = _advisories(second)
    assert len(advisories) == 2
    assert "0.0.24 → 0.0.25" in advisories[0]
    assert "harness version" in advisories[0]


def test_a_cell_that_moved_on_both_axes_is_one_advisory(db, monkeypatch):
    """UM aviso por replay divergente, mesmo quando os dois campos andaram: a
    contagem é o que o template certificado carimba, e dois registros para uma
    célula publicariam o run como o dobro de avisado do que foi."""
    import lohra

    monkeypatch.setattr(lohra, "__version__", "0.0.24")
    _stretch(db, "run-D", policy=WorkflowPolicy(allow_terminal=True))
    monkeypatch.setattr(lohra, "__version__", "0.0.25")
    second, _ = _stretch(db, "run-D", policy=WorkflowPolicy(allow_terminal=False))

    assert len(_advisories(second)) == 2  # duas células, um aviso cada
    assert second.replay_divergences == 2
    assert "sandbox policy and a different harness version" in _advisories(second)[0]


# --- o invariante do dono: NULL é DESCONHECIDO, nunca "diferente" -----------


def test_a_cell_stored_before_the_stamp_existed_replays_in_silence(db):
    """Toda célula gravada antes desta feature lê NULL — e replaia exatamente
    como sempre replaiou. Inventar divergência onde não há registro seria avisar
    sobre todo run que existe hoje."""
    first, _ = _stretch(db, "run-E", policy=None)  # o mundo pré-#75
    assert first.status == "complete"

    second, respawns = _stretch(db, "run-E", policy=WorkflowPolicy(allow_terminal=True))
    assert respawns == 0
    assert _advisories(second) == [] and second.replay_divergences == 0


def test_a_reader_with_no_policy_of_its_own_compares_nothing(db):
    """O outro lado do mesmo invariante: quem replaia sem política em mãos
    (``cache_preview``, ``spend``) não tem com o que comparar."""
    _stretch(db, "run-F", policy=WorkflowPolicy(allow_terminal=True))
    second, _ = _stretch(db, "run-F", policy=None)
    assert _advisories(second) == []


def test_the_divergence_helper_never_speaks_where_a_side_is_unknown():
    """A unidade da regra, sem banco no meio."""
    known = CellStamp("hash-a", "0.0.24")
    assert divergence(known, CellStamp(None, None)) is None
    assert divergence(CellStamp(None, None), known) is None
    assert divergence(known, known) is None
    # ...e um lado conhecido só num campo responde só por aquele campo.
    assert divergence(CellStamp("hash-a", None), CellStamp("hash-b", "0.0.25")) == (
        REASON_POLICY_CHANGED,
        divergence(known, CellStamp("hash-b", "0.0.24"))[1],
    )


def test_a_human_checkpoint_answer_carries_no_stamp(db):
    """Nenhuma política de sandbox governou uma PESSOA respondendo. Carimbar a
    célula dela faria uma mudança de política avisar sobre o único tipo de
    célula que knob nenhum poderia ter mudado."""
    gated = {
        "meta": {"name": "gated", "version": 1},
        "nodes": [{"id": "ask", "type": "checkpoint", "prompt": "go?"}],
    }
    responder, _calls = _counting()
    core = _core(db, responder)
    engine = WorkflowEngine(
        core,
        budget=Budget(),
        cache=NodeCache(db, "run-G", stamp=CellStamp.current(WorkflowPolicy())),
        run_id="run-G",
        checkpoint_answers={"ask": "yes"},
    )
    try:
        assert engine.run(validate_spec(gated), {}).status == "complete"
    finally:
        core.shutdown()

    second, _ = _stretch(
        db, "run-G", policy=WorkflowPolicy(allow_terminal=True), spec=gated
    )
    assert _advisories(second) == []


# --- a linha durável, o template certificado e as colunas ------------------


_TWICE_GATED: dict[str, Any] = {
    "meta": {"name": "twice-gated", "version": 1},
    "nodes": [
        {"id": "draft", "type": "agent", "prompt": "Draft ${args.topic}"},
        {"id": "ask1", "type": "checkpoint", "prompt": "Once?", "depends_on": ["draft"]},
        {"id": "ask2", "type": "checkpoint", "prompt": "Twice?", "depends_on": ["ask1"]},
    ],
}


def test_the_count_survives_the_process_and_reaches_the_template(db, tmp_path):
    """Três estirões: a divergência acontece no segundo e o terceiro certifica.

    Sem a contagem DURÁVEL, o template carimbaria só o que o último estirão viu.
    E o carimbo tem de ser o CERTO: os dois avisos são de replay, então
    ``artifact_divergences`` — a outra fonte de advisory — segue 0."""
    svc = _service(db, tmp_path, _counting()[0], policy=WorkflowPolicy(allow_terminal=True))
    try:
        run_id = svc.start(_TWICE_GATED, {"topic": "kites"})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc.shutdown()

    narrow = WorkflowPolicy(allow_terminal=False)
    svc2 = _service(db, tmp_path, _counting()[0], policy=narrow)
    try:
        out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask1": "yes"})
        assert "error" not in out, out
        assert svc2.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc2.shutdown()

    # O que o processo morto deixou na linha é o que o próximo lê.
    assert svc2._store.load(run_id).prior_replay_divergences == 1

    svc3 = _service(db, tmp_path, _counting()[0], policy=narrow)
    try:
        out = svc3.start(resume_run_id=run_id, checkpoint_answers={"ask2": "yes"})
        assert "error" not in out, out
        final = svc3.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
    finally:
        svc3.shutdown()

    template = json.loads(
        (tmp_path / "workflows" / "templates" / "twice-gated.json").read_text()
    )
    # 1 (o estirão que a linha guardou) + 1 (o deste): a célula do draft segue
    # gravada sob a política antiga, então TODO replay dela é divergente.
    assert template["meta"]["replay_divergences"] == 2
    # ...e nenhum deles é uma alegação de artefato que o harness corrigiu.
    assert template["meta"]["artifact_divergences"] == 0
    assert library.list_templates(tmp_path)[0]["replay_divergences"] == 2


def test_the_stamp_rides_in_the_cell_own_guarded_transaction(db):
    """Escrita cercada (issue #12): o carimbo vai no MESMO INSERT da célula, sob
    a MESMA guarda. Uma célula gravada com o carimbo recusado à parte replaiaria
    "desconhecida" pelo resto da vida do run — o argumento do manifesto (#45),
    inteiro."""
    fence = db.acquire_run_lease("run-H", "owner-2", ttl_seconds=60.0, now=1000.0)
    assert db.cache_put_with_cost(
        "run-H", "h1", "a", '"kept"', "complete",
        cost=(5, 3, 0, 0, 0), fence=fence, stamp=("policy-abc", "0.0.24"),
    )
    row = db.cache_get("run-H", "h1")
    assert (row["policy_hash"], row["harness_version"]) == ("policy-abc", "0.0.24")

    # ...e o straggler do dono anterior não grava NADA: nem célula, nem carimbo.
    assert not db.cache_put_with_cost(
        "run-H", "h2", "b", '"stale"', "complete", fence=fence - 1,
        stamp=("policy-xyz", "0.0.9"),
    )
    assert db.cache_get("run-H", "h2") is None


def test_a_row_written_by_the_old_path_reads_unknown(db):
    """``cache_put`` (o caminho legado, sem carimbo) e toda linha gravada antes
    da migração leem NULL nas duas colunas — que é o que ``CellStamp.stored``
    tem de ler como DESCONHECIDO."""
    assert db.cache_put("run-I", "h1", "a", '"kept"', "complete")
    row = db.cache_get("run-I", "h1")
    assert row["policy_hash"] is None and row["harness_version"] is None
    assert CellStamp.stored(row) == CellStamp(None, None)


def test_the_three_reasons_are_words_the_audit_can_keep():
    """Um ``reason`` fora do allow-list volta como ``excluded_by_policy`` e conta
    como REDAÇÃO — o motivo honesto leria como conteúdo que a auditoria recusou."""
    from lohra.workflow.audit import _SAFE_STRING_VALUES

    for reason in (
        REASON_POLICY_CHANGED,
        REASON_HARNESS_VERSION_CHANGED,
        REASON_POLICY_AND_HARNESS_VERSION_CHANGED,
    ):
        assert reason in _SAFE_STRING_VALUES["reason"]


# --- as duas fontes de advisory são contadas na PORTA de cada uma -----------


_WRONG_SHA = "0" * 64


def _project(tmp_path):
    """Uma root do operador com um artefato real dentro."""
    root = tmp_path / "project"
    root.mkdir()
    target = root / "report.md"
    target.write_text("the first draft\n", encoding="utf-8")
    return root, target


_MANIFEST_AND_MORE: dict[str, Any] = {
    "meta": {"name": "advised-and-replayed", "version": 1},
    "nodes": [
        {"id": "writer", "type": "agent", "prompt": "write it", "schema_ref": "artifact_manifest"},
        # Fan-out de propósito: com a agregação (uma ENTRADA por nó) o número de
        # entradas deixou de ser o número de células, então derivar uma contagem
        # da outra por subtração dá o número errado — é o que este teste prende.
        {
            "id": "p", "type": "pipeline", "items": ["a", "b"],
            "stages": [{"type": "agent", "prompt": "stage ${item}"}],
        },
        {
            "id": "ask", "type": "checkpoint", "prompt": "Ship it?",
            "depends_on": ["writer", "p"],
        },
    ],
}


def test_the_two_advisory_sources_are_counted_apart(db, tmp_path):
    """Um run com UMA alegação de artefato corrigida (estirão 1) e TRÊS replays
    divergentes em DUAS entradas (estirão 2) certifica 1 e 3 — cada contador
    incrementado na porta por onde o seu aviso entrou.

    Derivar um do outro por subtração (ou pior, pela PROSA) quebraria calado no
    dia em que uma terceira fonte de advisory pousasse na mesma lista."""
    root, target = _project(tmp_path)
    manifest = _manifest(target, sha256=_WRONG_SHA)

    def responder(prompt: str) -> str:
        return manifest if "write it" in prompt else "R"

    wide = WorkflowPolicy(fs_allow=(str(root),), allow_terminal=True)
    narrow = WorkflowPolicy(fs_allow=(str(root),), allow_terminal=False)

    svc = _service(db, tmp_path, responder, policy=wide)
    try:
        run_id = svc.start(_MANIFEST_AND_MORE, {})["run_id"]
        paused = svc.status(run_id, wait=True, timeout=10)
        assert paused["status"] == "paused"
        # A alegação errada já virou aviso AQUI, e é um aviso de ARTEFATO.
        assert len(paused["advisory_faults"]) == 1
    finally:
        svc.shutdown()

    assert svc._store.load(run_id).prior_artifact_advisories == 1

    svc2 = _service(db, tmp_path, responder, policy=narrow)
    try:
        out = svc2.start(resume_run_id=run_id, checkpoint_answers={"ask": "yes"})
        assert "error" not in out, out
        final = svc2.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
    finally:
        svc2.shutdown()

    template = json.loads(
        (tmp_path / "workflows" / "templates" / "advised-and-replayed.json").read_text()
    )
    assert template["meta"]["artifact_divergences"] == 1
    assert template["meta"]["replay_divergences"] == 3
    # 2 entradas (writer + p) para 3 células: a aritmética antiga
    # (total de advisories − replays) devolveria 0 alegações de artefato.
    assert len([f for f in final["advisory_faults"] if "replayed under" in f]) == 2


def test_a_run_advised_only_about_replays_certifies_zero_artifact_divergences(db, tmp_path):
    """O controle negativo do teste acima, no caminho real: sem manifesto
    nenhum, os avisos de replay não podem virar "alegações que o harness
    corrigiu"."""
    svc = _service(db, tmp_path, _counting()[0], policy=WorkflowPolicy(allow_terminal=True))
    try:
        run_id = _pause_at_gate(svc)
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, _counting()[0], policy=WorkflowPolicy())
    try:
        assert "error" not in svc2.start(
            resume_run_id=run_id, checkpoint_answers={"ask": "yes"}
        )
        assert svc2.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc2.shutdown()

    template = json.loads(
        (tmp_path / "workflows" / "templates" / "policy-gated.json").read_text()
    )
    assert template["meta"]["artifact_divergences"] == 0
    assert template["meta"]["replay_divergences"] == 1


# --- fan-out: UMA linha por (nó, motivo), N células no número --------------


_PIPELINE_GATED: dict[str, Any] = {
    "meta": {"name": "fanout-gated", "version": 1},
    "nodes": [
        {
            "id": "p", "type": "pipeline", "items": ["a", "b", "c"],
            "stages": [{"type": "agent", "prompt": "stage ${item}"}],
        },
        {"id": "ask", "type": "checkpoint", "prompt": "Ship it?", "depends_on": ["p"]},
    ],
}


def test_a_fan_out_node_writes_one_advisory_and_counts_every_cell(db, tmp_path):
    """500 faults idênticos afogariam o ledger que o aviso existe para informar.
    UMA linha por (nó, motivo), com a contagem no texto; o número por CÉLULA
    sobrevive no contador durável, e o LEDGER guarda o `reason` de cada uma."""
    svc = _service(db, tmp_path, _counting()[0], policy=WorkflowPolicy(allow_terminal=True))
    try:
        run_id = svc.start(_PIPELINE_GATED, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        svc.shutdown()

    svc2 = _service(db, tmp_path, _counting()[0], policy=WorkflowPolicy())
    try:
        assert "error" not in svc2.start(
            resume_run_id=run_id, checkpoint_answers={"ask": "yes"}
        )
        final = svc2.status(run_id, wait=True, timeout=10)
        assert final["status"] == "complete"
    finally:
        svc2.shutdown()

    advisories = [f for f in final["faults_total"] if "replayed under a different" in f]
    assert len(advisories) == 1
    assert advisories[0].startswith("p: 3 cells replayed under a different sandbox policy")
    # ...e o desconto do veredito continua casando texto por texto.
    assert advisories[0] in final["advisory_faults"]
    # O ledger não agrega: cada célula tem o seu evento com o seu motivo.
    reasons = [row["data"].get("reason") for row in _replays(db, run_id)]
    assert reasons == ["policy_changed"] * 3
    template = json.loads(
        (tmp_path / "workflows" / "templates" / "fanout-gated.json").read_text()
    )
    assert template["meta"]["replay_divergences"] == 3


def test_a_duplicated_root_the_operator_removed_is_not_a_policy_change(db):
    """Listar a mesma root (ou o mesmo host) duas vezes concede exatamente a
    mesma capacidade que listá-la uma vez: apagar a duplicata não é mudar de
    política, e avisar ali seria avisar sobre o que não mudou."""
    doubled = WorkflowPolicy(
        fs_allow=("/x", "/x"), egress_allow=("api.test", "api.test"),
        mcp_allow=("git", "git"),
    )
    single = WorkflowPolicy(fs_allow=("/x",), egress_allow=("api.test",), mcp_allow=("git",))
    assert policy_fingerprint(doubled) == policy_fingerprint(single)

    _stretch(db, "run-J", policy=doubled)
    second, _ = _stretch(db, "run-J", policy=single)
    assert _advisories(second) == [] and second.replay_divergences == 0
