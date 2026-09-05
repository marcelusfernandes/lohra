"""O teto de tokens PRÉ-AUTORIZADO pelo operador (issue #47, parte 2).

O `token_budget` é opcional e escolhido pelo próprio agente: sem ele, um run é
ilimitado e, em headless (`lohra chat --json`, one-shot), ninguém está lá para
segurar a conta. Este é o freio do OPERADOR — flag `--token-budget-cap` ou env
`LOHRA_TOKEN_BUDGET_CAP` — e ele obedece à doutrina de sempre:

- o valor vem do HUMANO, nunca da spec autorada pelo agente (aqui o humano é o
  operador do processo, e a autorização é PRÉVIA);
- o agente nunca ELEVA o teto do operador — nem no `run_workflow`, nem no
  `resume_run_id`: o pedido maior é clampado;
- sem teto, tudo é byte-idêntico ao de hoje (nenhum campo novo em lugar nenhum);
- a pausa por budget sob teto do operador aponta o remédio CERTO (o operador),
  nunca `resume_run_id` com um número maior — que seria clampado e pausaria de
  novo, o loop silencioso que a doutrina raise-only existe para matar.
"""

import pytest

from lohra.agent.agent import Agent
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.operator_budget import (
    ENV_TOKEN_BUDGET_CAP,
    ORIGIN_INHERITED,
    SOURCE_INHERITED,
    SOURCE_CLAMPED,
    SOURCE_OPERATOR,
    SOURCE_SPEC,
    apply_operator_cap,
    resolve_operator_token_cap,
)
from lohra.workflow.service import WorkflowService
from lohra.workflow.spend import refuse_spent_budget
from tests.test_workflow_pipeline import ScriptedClient

LEAF_COST = 8  # um turno falso: 5 input + 3 output


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


_TWO_NODE = {
    "meta": {"name": "demo", "version": 1},
    "nodes": [
        {"id": "a", "type": "agent", "prompt": "go"},
        {"id": "b", "type": "agent", "prompt": "then ${a}"},
    ],
}


def _ok(_prompt):
    return "R"


def _service(db, home, responder=_ok, *, operator_cap=None):
    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(responder),
        )

    return WorkflowService(
        base_child_factory=factory, db=db, home=home, operator_cap=operator_cap
    )


# --- 1. resolução: flag > env > sem teto --------------------------------


def test_no_flag_and_no_env_means_no_ceiling():
    # O default É o comportamento de hoje: nada imposto, nada reportado.
    assert resolve_operator_token_cap(env={}) is None


def test_the_env_var_sets_the_ceiling():
    assert resolve_operator_token_cap(env={ENV_TOKEN_BUDGET_CAP: "200000"}) == 200000


def test_the_flag_beats_the_env():
    # Mesma precedência de resolve_limits: flag > env > default.
    assert resolve_operator_token_cap(50, env={ENV_TOKEN_BUDGET_CAP: "200000"}) == 50


def test_the_flag_alone_sets_the_ceiling():
    assert resolve_operator_token_cap(50, env={}) == 50


@pytest.mark.parametrize("bad", ["", "lots", "1.5", "0", "-1", "  "])
def test_a_bad_env_value_warns_and_falls_back_to_no_ceiling(bad, caplog):
    # Fail-OPEN de propósito (o padrão de LOHRA_AUDIT/resolve_limits): um env
    # ilegível não pode transformar todo run do operador num erro — mas também
    # não pode ser lido como um teto inventado. Avisa e some.
    with caplog.at_level("WARNING"):
        assert resolve_operator_token_cap(env={ENV_TOKEN_BUDGET_CAP: bad}) is None
    if bad.strip():
        assert ENV_TOKEN_BUDGET_CAP in caplog.text


@pytest.mark.parametrize("bad", [0, -1])
def test_a_bad_flag_warns_and_falls_back_to_no_ceiling(bad, caplog):
    with caplog.at_level("WARNING"):
        assert resolve_operator_token_cap(bad, env={}) is None
    assert "token-budget-cap" in caplog.text


@pytest.mark.parametrize("bad", [0, -1])
def test_a_bad_flag_falls_back_to_the_env_not_to_no_ceiling(bad, caplog):
    """Das duas maneiras de errar, só uma deixa um run gastar MAIS do que o
    humano autorizou: uma flag com typo não pode apagar o teto que o operador
    pôs no ambiente de propósito."""
    with caplog.at_level("WARNING"):
        assert resolve_operator_token_cap(bad, env={ENV_TOKEN_BUDGET_CAP: "9000"}) == 9000


# --- 2. a aplicação pura (min + proveniência) ---------------------------


def test_without_a_cap_nothing_changes_and_nothing_is_reported():
    for asked in (None, 900):
        applied = apply_operator_cap(asked, None)
        assert applied.total == asked
        assert applied.as_dict() is None  # nenhum campo novo no resultado


def test_the_cap_alone_becomes_the_ceiling():
    applied = apply_operator_cap(None, 1000)
    assert applied.total == 1000
    assert applied.as_dict() == {
        "total": 1000,
        "source": SOURCE_OPERATOR,
        "operator_cap": 1000,
    }


def test_a_larger_spec_budget_is_clamped_to_the_cap():
    applied = apply_operator_cap(5000, 1000)
    assert applied.total == 1000
    assert applied.as_dict() == {
        "total": 1000,
        "source": SOURCE_CLAMPED,
        "operator_cap": 1000,
    }


def test_a_smaller_spec_budget_still_wins_but_the_cap_is_shown():
    # O agente pode pedir MENOS que o operador — teto é teto, não uma cota.
    applied = apply_operator_cap(300, 1000)
    assert applied.total == 300
    assert applied.as_dict() == {
        "total": 300,
        "source": SOURCE_SPEC,
        "operator_cap": 1000,
    }


# --- 3. o serviço: launch, resume, e o resultado do run_workflow --------


def test_without_a_cap_the_start_result_is_byte_identical(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        out = svc.start(_TWO_NODE, {}, token_budget=900)
        assert set(out) == {"run_id", "status"}
    finally:
        svc.shutdown()


def test_the_start_result_reports_the_operator_ceiling(db, tmp_path):
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        out = svc.start(_TWO_NODE, {})
        assert out["status"] == "started"
        assert out["token_budget"] == {
            "total": 1000,
            "source": SOURCE_OPERATOR,
            "operator_cap": 1000,
        }
        run_id = out["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["token_budget"]["total"] == 1000
    finally:
        svc.shutdown()


def test_an_agent_budget_over_the_cap_is_clamped_at_launch(db, tmp_path):
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        out = svc.start(_TWO_NODE, {}, token_budget=10_000_000)
        assert out["token_budget"]["total"] == 1000
        assert out["token_budget"]["source"] == SOURCE_CLAMPED
        # E o teto que o motor realmente roda é o do operador, não o pedido.
        final = svc.status(out["run_id"], wait=True, timeout=10)
        assert final["token_budget"]["total"] == 1000
    finally:
        svc.shutdown()


def test_a_resume_cannot_raise_the_ceiling_past_the_cap(db, tmp_path):
    """O humano do resume é o AGENTE; o operador continua acima dele."""
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=500)
        assert "error" not in out
        assert out["token_budget"]["total"] == 500  # abaixo do teto: passa
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        svc.shutdown()


def test_a_resume_asking_for_more_than_the_cap_is_clamped(db, tmp_path):
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=10_000_000)
        assert out["token_budget"] == {
            "total": 1000,
            "source": SOURCE_CLAMPED,
            "operator_cap": 1000,
        }
        assert svc.status(run_id, wait=True, timeout=10)["token_budget"]["total"] == 1000
    finally:
        svc.shutdown()


def test_the_real_path_pauses_on_the_operator_ceiling_with_no_spec_budget(db, tmp_path):
    """Caminho REAL service→engine: spec SEM token_budget (o furo da issue) e o
    teto do operador pausa o run mesmo assim."""
    svc = _service(db, tmp_path, operator_cap=5)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == TOKEN_BUDGET_EXHAUSTED
        assert out["token_budget"] == {
            "total": 5, "spent": LEAF_COST, "remaining": 0, "overrun": LEAF_COST - 5
        }
        # O remédio é do OPERADOR — não `resume_run_id` com um número maior.
        assert "LOHRA_TOKEN_BUDGET_CAP" in out["hint"]
        assert "--token-budget-cap" in out["hint"]
    finally:
        svc.shutdown()


def test_a_resume_past_a_spent_operator_ceiling_is_refused_naming_the_operator(db, tmp_path):
    """O loop que precisa NÃO existir: pedir mais, ser clampado, pausar de novo."""
    svc = _service(db, tmp_path, operator_cap=5)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=10_000_000)
        assert "run_id" not in out
        assert "LOHRA_TOKEN_BUDGET_CAP" in out["error"]
    finally:
        svc.shutdown()


def test_a_pause_under_a_cap_that_does_not_bind_keeps_the_human_hint(db, tmp_path):
    """Teto de 1000, o agente pediu 5: um humano AINDA pode autorizar até 1000,
    então o remédio de sempre continua certo. O teto do operador só rouba a cena
    quando ele é que está barrando."""
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert "resume_run_id" in out["hint"]
        assert "LOHRA_TOKEN_BUDGET_CAP" not in out["hint"]
    finally:
        svc.shutdown()


def test_the_spent_budget_refusal_names_the_operator_only_when_it_binds():
    # Sem teto: a recusa raise-only de sempre, palavra por palavra.
    plain = refuse_spent_budget("r1", 100, 100)
    assert "Only a human may authorize" in plain["error"]
    assert refuse_spent_budget("r1", 100, 100, operator_cap=None) == plain
    # Teto do operador não vinculante (200 > 100): o remédio humano ainda serve.
    assert refuse_spent_budget("r1", 100, 100, operator_cap=200) == plain
    # Teto vinculante: quem pode mexer é o operador do processo.
    bound = refuse_spent_budget("r1", 100, 100, operator_cap=100)
    assert "LOHRA_TOKEN_BUDGET_CAP" in bound["error"]
    assert "--token-budget-cap" in bound["error"]


def test_a_budget_pause_under_an_operator_cap_still_never_auto_resumes(db, tmp_path):
    """A allowlist de um único motivo (quota) não muda: esperar não recarrega um
    budget, e um auto-resume queimaria as 5 tentativas re-pausando."""
    svc = _service(db, tmp_path, operator_cap=5)
    try:
        run_id = svc.start(_TWO_NODE, {})["run_id"]
        out = svc.status(run_id, wait=True, timeout=10)
        assert out["status"] == "paused"
        assert out["reason"] == TOKEN_BUDGET_EXHAUSTED
        assert out["resume_at"] is None
    finally:
        svc.shutdown()


# --- 4. o CLI: a flag do operador chega mesmo ao serviço ----------------


def test_the_chat_parser_accepts_the_cap_flag():
    from lohra.cli import build_parser

    args = build_parser().parse_args(["chat", "oi", "--token-budget-cap", "200000"])
    assert args.token_budget_cap == 200000
    # ausente -> None, e a resolução cai no env/sem teto
    assert build_parser().parse_args(["chat", "oi"]).token_budget_cap is None


@pytest.mark.parametrize(
    "flag,env,expected",
    [
        (7000, None, 7000),  # a flag
        (None, "9000", 9000),  # o env
        (7000, "9000", 7000),  # flag > env
        (0, "9000", 9000),  # flag inválida NÃO apaga o teto do ambiente
        (None, None, None),  # sem teto: WorkflowService igual ao de sempre
    ],
)
def test_the_chat_flag_and_env_reach_the_workflow_service(
    flag, env, expected, monkeypatch, tmp_path
):
    """O fio inteiro: `lohra chat --token-budget-cap` → WorkflowService."""
    from lohra import cli
    from lohra.workflow import service as service_module
    from tests.test_cli import _patch_fake_client

    seen: dict = {}
    real_init = service_module.WorkflowService.__init__

    def spy(self, **kwargs):
        seen.update(kwargs)
        real_init(self, **kwargs)

    monkeypatch.setattr(service_module.WorkflowService, "__init__", spy)
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv(ENV_TOKEN_BUDGET_CAP, raising=False)
    if env is not None:
        monkeypatch.setenv(ENV_TOKEN_BUDGET_CAP, env)
    _patch_fake_client(monkeypatch)

    assert cli.run_chat("oi", provider="anthropic", token_budget_cap=flag) == 0
    assert seen["operator_cap"] == expected


# --- 5. review adversarial: dois números, e o remédio que destrava ------


def test_the_pause_hint_never_claims_the_run_spent_the_operators_number():
    """O teto DO RUN (persistido, talvez escrito por outro processo sob outro
    teto) e o teto DESTE processo são dois números. Fundi-los afirmava um fato
    que ninguém observou: com total=5 e cap=3, o run gastou 5, não 3."""
    from lohra.workflow.runstate_store import pause_fields

    fields = pause_fields(
        "paused",
        TOKEN_BUDGET_EXHAUSTED,
        None,
        0,
        None,
        token_budget=5,
        operator_cap=3,
        spent=None,
    )
    assert "5" in fields["hint"] and "3" in fields["hint"]
    assert "spent the token ceiling the OPERATOR pre-authorized" not in fields["hint"]
    assert "--token-budget-cap" in fields["hint"]


def test_the_pause_hint_also_prescribes_the_explicit_budget_on_the_resume():
    """Relançar com um teto maior NÃO destrava sozinho: o ledger guardou o valor
    já clampado e o resume o herda. O hint tem que dizer as duas metades."""
    from lohra.workflow.runstate_store import pause_fields

    fields = pause_fields(
        "paused", TOKEN_BUDGET_EXHAUSTED, None, 0, None, token_budget=5, operator_cap=5
    )
    assert "resume_run_id" in fields["hint"] and "token_budget=" in fields["hint"]


def test_raising_the_cap_and_resuming_with_an_explicit_budget_unsticks_the_run(
    db, tmp_path
):
    """O remédio prescrito, ponta a ponta: o operador sobe o teto, relança — e o
    resume carrega um token_budget acima do já gasto (que o teto novo clampa)."""
    stuck = _service(db, tmp_path, operator_cap=5)
    try:
        run_id = stuck.start(_TWO_NODE, {})["run_id"]
        assert stuck.status(run_id, wait=True, timeout=10)["status"] == "paused"
    finally:
        stuck.shutdown()

    # Novo processo do operador, teto maior. Só relançar não basta: o ceiling
    # herdado do ledger é o gasto, e o resume seria recusado.
    raised = _service(db, tmp_path, operator_cap=100_000)
    try:
        assert "error" in raised.start(_TWO_NODE, {}, resume_run_id=run_id)
        out = raised.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=50_000)
        assert "error" not in out
        assert raised.status(run_id, wait=True, timeout=10)["status"] == "complete"
    finally:
        raised.shutdown()


def test_a_quota_pause_over_the_operator_ceiling_promises_no_retry():
    """O zumbi: o auto-resume re-arma, dispara, é clampado e RECUSADO antes de
    spawnar — sem incrementar attempts. Reportar `resume_at` seria prometer uma
    retomada que este processo não pode entregar."""
    from lohra.workflow.runstate_store import pause_fields

    fields = pause_fields(
        "paused",
        "quota_exhausted",
        resume_at=1000.0,
        attempts=1,
        checkpoint=None,
        token_budget=5,
        operator_cap=5,
        spent=8,
    )
    assert fields["reason"] == "quota_exhausted"  # o motivo real fica de pé
    assert fields["resume_at"] is None
    assert "--token-budget-cap" in fields["hint"]
    assert "8" in fields["hint"]


def test_the_durable_line_of_a_stalled_quota_run_says_the_same():
    from lohra.workflow.runstate_store import DurableRun, durable_rollup

    row = DurableRun(
        run_id="r1",
        status="paused",
        pause_reason="quota_exhausted",
        resume_at=1000.0,
        attempts=1,
        token_budget=5,
    )
    line = durable_rollup(row, spent_total=8, stale=False, operator_cap=5)
    assert line["resume_at"] is None
    assert "--token-budget-cap" in line["hint"]
    # Sem teto neste processo, a linha é a de sempre: a promessa vale.
    plain = durable_rollup(row, spent_total=8, stale=False)
    assert plain["resume_at"] == 1000.0


def test_a_checkpoint_over_the_ceiling_keeps_its_question_visible():
    from lohra.workflow.runstate_store import pause_fields

    fields = pause_fields(
        "paused",
        "checkpoint",
        None,
        0,
        {"node_id": "approve", "prompt": "ok?"},
        token_budget=5,
        operator_cap=5,
        spent=8,
    )
    assert fields["checkpoint"]["node_id"] == "approve"
    assert "--token-budget-cap" in fields["hint"]  # a resposta sozinha não destrava


def test_an_inherited_ceiling_is_labelled_inherited_not_spec(db, tmp_path):
    """O que o resume herda do LEDGER não é autoria desta chamada — e pode já
    estar clampado por um stretch anterior."""
    svc = _service(db, tmp_path, operator_cap=1000)
    try:
        run_id = svc.start(_TWO_NODE, {}, token_budget=5)["run_id"]
        assert svc.status(run_id, wait=True, timeout=10)["status"] == "paused"
        out = svc.start(_TWO_NODE, {}, resume_run_id=run_id, token_budget=500)
        assert out["token_budget"]["source"] == SOURCE_SPEC
    finally:
        svc.shutdown()

    fresh = _service(db, tmp_path, operator_cap=1000)
    try:
        run_id = fresh.start(_TWO_NODE, {}, token_budget=900)["run_id"]
        fresh.status(run_id, wait=True, timeout=10)
        out = fresh.start(_TWO_NODE, {}, resume_run_id=run_id)  # sem pedir nada
        assert out["token_budget"]["source"] == SOURCE_INHERITED
    finally:
        fresh.shutdown()


def test_an_inherited_ceiling_over_the_cap_is_labelled_as_the_clamp_of_a_ledger():
    assert apply_operator_cap(5000, 1000, origin=ORIGIN_INHERITED).source == (
        "min(inherited,operator_cap)"
    )


@pytest.mark.parametrize("bad", [0, -1, True, "lots"])
def test_a_bogus_cap_at_the_service_boundary_runs_uncapped(bad, db, tmp_path, caplog):
    """Fronteira própria: 0 leria como "teto de nada" e pausaria todo run no
    primeiro spawn. Fail-open com aviso, como toda resolução aqui."""
    with caplog.at_level("WARNING"):
        svc = _service(db, tmp_path, operator_cap=bad)
    try:
        assert set(svc.start(_TWO_NODE, {})) == {"run_id", "status"}  # nada imposto
    finally:
        svc.shutdown()
    assert "operator token cap" in caplog.text
