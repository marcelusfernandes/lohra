"""Fatia C — o split de cache/reasoning atravessa o workflow ate o rollup.

O que estes testes fixam:

- o engine soma os QUATRO medidores disjuntos (nao so in/out) e sabe qual NO
  gastou cada um, com o (provider, model) que os gastou;
- o cache de celula grava e devolve o split (uma resume nao esquece o cache);
- as tabelas sidecar ganham colunas ADITIVAS: um banco antigo abre e le 0;
- o rollup mostra por no "X in (Y cached) + Z out" e o custo real x bruto;
- o BUDGET nao muda: continua cobrando input+output (agora uniformemente
  nao-cacheado). Cache e coluna de relatorio, nao eixo de orcamento.
"""

import sqlite3

import pytest

from lohra.agent.agent import Agent
from lohra.agent.types import Usage
from lohra.orchestration.core import OrchestrationCore
from lohra.providers import get_provider_profile
from lohra.state import SessionDB
from lohra.workflow import rollup
from lohra.workflow.budget import Budget
from lohra.workflow.cache import NodeCache
from lohra.workflow.engine import WorkflowEngine
from lohra.workflow.schema import validate_spec
from tests.test_loop import FakeClient


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _cached_response(text="R"):
    """Uma resposta com os quatro medidores populados (shape real da Anthropic)."""
    return {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 60,
            "cache_creation_input_tokens": 40,
        },
    }


def _core(db, *, model="claude-opus-4-8", max_concurrent=4):
    def factory():
        return Agent(
            model=model,
            provider=get_provider_profile("anthropic"),
            client=FakeClient([_cached_response()]),
        )

    return OrchestrationCore(db, factory, max_concurrent=max_concurrent)


def _engine(core, **budget_kw):
    return WorkflowEngine(core, budget=Budget(**budget_kw))


def _spec(*node_ids):
    return validate_spec(
        {
            "meta": {"name": "t"},
            "nodes": [{"id": nid, "type": "agent", "prompt": "x"} for nid in node_ids],
        }
    )


# --- o engine soma os quatro medidores ---


def test_run_result_aggregates_the_cache_split(db):
    core = _core(db)
    try:
        result = _engine(core).run(_spec("a", "b"), {})
        assert result.tokens_in == 200 and result.tokens_out == 40
        assert result.cache_read_tokens == 120
        assert result.cache_write_tokens == 80
    finally:
        core.shutdown()


def test_budget_still_charges_only_input_plus_output(db):
    """Decisao travada: o cache NAO e eixo de orcamento."""
    core = _core(db)
    engine = _engine(core)
    try:
        engine.run(_spec("a"), {})
        assert engine.budget.tokens_spent == 120  # 100 in + 20 out, sem cache
    finally:
        core.shutdown()


def test_node_costs_attribute_the_split_to_the_node_and_the_agent(db):
    core = _core(db)
    engine = _engine(core)
    try:
        engine.run(_spec("a", "b"), {})
        costs = engine.node_costs()
        assert set(costs) == {"a", "b"}
        assert costs["a"].usage.cache_read_tokens == 60
        assert costs["a"].provider == "anthropic"
        assert costs["a"].model == "claude-opus-4-8"
    finally:
        core.shutdown()


def test_leaf_cost_is_a_usage_with_every_meter(db):
    core = _core(db)
    engine = _engine(core)
    try:
        engine.run(_spec("a"), {})
        (sub_id,) = list(engine.spawned)
        cost = engine.leaf_cost(sub_id)
        assert isinstance(cost, Usage)
        assert (cost.input_tokens, cost.cache_read_tokens, cost.cache_write_tokens) == (100, 60, 40)
    finally:
        core.shutdown()


# --- o cache de celula guarda o split (uma resume nao esquece o cache) ---


def test_node_cache_round_trips_the_split(db):
    cache = NodeCache(db, "run-1")
    cache.put_complete(
        "hash-1",
        "node-a",
        {"ok": True},
        Usage(input_tokens=100, output_tokens=20, cache_read_tokens=60, cache_write_tokens=40),
    )
    assert cache.total_cost() == (100, 20)  # o eixo do budget, inalterado
    assert cache.total_split() == Usage(
        input_tokens=100, output_tokens=20, cache_read_tokens=60, cache_write_tokens=40
    )


def test_node_cache_accepts_a_costless_answer(db):
    """Um checkpoint humano nao gasta leaf nenhum — e continua cacheavel."""
    cache = NodeCache(db, "run-1")
    cache.put_complete("h", "n", "resposta humana")
    assert cache.total_split() == Usage()


# --- migracao aditiva: um banco pre-Fatia-C abre e le zeros ---


def test_old_sidecar_tables_are_migrated_additively(tmp_path):
    """As colunas novas sao ADITIVAS e NULLABLE: uma linha antiga le 0/None."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE workflow_node_cost (
            run_id TEXT NOT NULL, content_hash TEXT NOT NULL,
            tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, content_hash));
        CREATE TABLE workflow_run_spend (
            run_id TEXT PRIMARY KEY, token_budget INTEGER,
            tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL);
        INSERT INTO workflow_node_cost VALUES ('r', 'h', 7, 3);
        INSERT INTO workflow_run_spend VALUES ('r', NULL, 7, 3, 0.0);
        """
    )
    con.commit()
    con.close()

    database = SessionDB(str(path))
    try:
        assert database.cache_cost_total("r") == (7, 3)
        assert database.cache_cost_split("r") == (0, 0, 0)  # linha antiga: nunca escrita
        row = database.run_spend_get("r")
        assert row["tokens_in"] == 7
        assert (row["cache_read_tokens"] or 0) == 0
    finally:
        database.close()


def test_run_spend_round_trips_the_split(db):
    db.run_spend_put("r", 500, 10, 5, cache_read=4, cache_write=2, reasoning=1)
    row = db.run_spend_get("r")
    assert (row["tokens_in"], row["tokens_out"]) == (10, 5)
    assert (row["cache_read_tokens"], row["cache_write_tokens"], row["reasoning_tokens"]) == (
        4, 2, 1,
    )


# --- rollup: por no, e em dinheiro quando ha preco ---


def test_rollup_reports_the_split_and_the_per_node_line(db):
    core = _core(db)
    engine = _engine(core)
    try:
        result = engine.run(_spec("a"), {})
        summary = rollup.summarize("run-1", "complete", result, nodes=engine.node_costs())
    finally:
        core.shutdown()
    assert summary["tokens_cache_read"] == 60
    assert summary["tokens_cache_write"] == 40
    node = summary["node_costs"][0]
    assert node["node_id"] == "a"
    assert node["tokens"] == "100 in (60 cached, 40 written) + 20 out"
    assert node["cost"]["usd"] > 0
    assert node["cost"]["gross_usd"] > node["cost"]["usd"]  # o cache barateou
    assert node["cost"]["source"]


def test_rollup_totals_the_money_across_priced_nodes(db):
    core = _core(db)
    engine = _engine(core)
    try:
        result = engine.run(_spec("a", "b"), {})
        summary = rollup.summarize("run-1", "complete", result, nodes=engine.node_costs())
    finally:
        core.shutdown()
    per_node = sum(n["cost"]["usd"] for n in summary["node_costs"])
    assert summary["cost"]["usd"] == pytest.approx(per_node, rel=1e-6)
    assert summary["cost"]["nodes_priced"] == 2


def test_rollup_without_nodes_is_unchanged(db):
    """Compat: quem nao passa ``nodes`` nao ganha chave nova nenhuma."""
    core = _core(db)
    try:
        result = _engine(core).run(_spec("a"), {})
    finally:
        core.shutdown()
    summary = rollup.summarize("run-1", "complete", result)
    assert "node_costs" not in summary and "cost" not in summary


def test_rollup_leaves_an_unpriced_node_with_tokens_only(db):
    """Fail-closed: um modelo sem preco mostra tokens, nunca um dolar inventado."""
    core = _core(db, model="modelo-que-nao-existe")
    engine = _engine(core)
    try:
        result = engine.run(_spec("a"), {})
        summary = rollup.summarize("r", "complete", result, nodes=engine.node_costs())
    finally:
        core.shutdown()
    node = summary["node_costs"][0]
    assert "cost" not in node
    assert node["tokens_in"] == 100
    assert "cost" not in summary  # nada priceado -> nenhum total em dinheiro


# --- servico: status ao vivo + ledger durável ---


def _service(db, home, reply="ok"):
    from lohra.workflow.service import WorkflowService
    from tests.test_workflow_pipeline import ScriptedClient

    def factory():
        return Agent(
            model="claude-opus-4-8",
            provider=get_provider_profile("anthropic"),
            client=ScriptedClient(lambda _prompt: reply),
        )

    return WorkflowService(base_child_factory=factory, db=db, home=home)


_SPEC = {"meta": {"name": "demo", "version": 1}, "nodes": [{"id": "a", "type": "agent", "prompt": "go"}]}


def test_status_reports_the_per_node_cost(db, tmp_path):
    svc = _service(db, tmp_path)
    try:
        started = svc.start(_SPEC, {})
        svc.status(started["run_id"], wait=True, timeout=10)
        status = svc.status(started["run_id"])
        node = status["node_costs"][0]
        assert node["node_id"] == "a"
        assert node["tokens"] == "5 in + 3 out"
        assert node["cost"]["usd"] > 0
        assert status["cost"]["nodes_priced"] == 1
    finally:
        svc.shutdown()


def test_run_ledger_persists_the_split(db, tmp_path):
    """As colunas novas do workflow_run_spend sao ESCRITAS e LIDAS, nunca fantasmas."""
    svc = _service(db, tmp_path)
    try:
        started = svc.start(_SPEC, {})
        svc.status(started["run_id"], wait=True, timeout=10)
        row = db.run_spend_get(started["run_id"])
    finally:
        svc.shutdown()
    assert row is not None
    assert row["tokens_in"] == 5 and row["tokens_out"] == 3
    assert (row["cache_read_tokens"] or 0) == 0  # o fake nao cacheia — mas a coluna existe


def test_split_total_prefers_the_larger_honest_count(db):
    from lohra.workflow.spend import split_total

    db.run_spend_put("r", None, 10, 5, cache_read=40, cache_write=0, reasoning=0)
    live = Usage(input_tokens=10, output_tokens=5, cache_read_tokens=4)
    assert split_total(db, "r", live).cache_read_tokens == 40  # a linha persistida ganha
    bigger = Usage(input_tokens=100, output_tokens=50, cache_read_tokens=400)
    assert split_total(db, "r", bigger).cache_read_tokens == 400


# --- proveniencia e escopo do TOTAL (o numero que o operador realmente le) ---


def test_money_total_carries_the_sources_and_the_bases():
    """Decisao 2 do design: TODO custo exibido carrega a fonte.

    O total era o unico custo do sistema sem procedencia — e num run
    cross-provider ele soma dinheiro `api_equivalent` (uma assinatura, que NAO e
    cobrada por token) com list price real dentro do mesmo ``usd``."""
    from lohra.workflow.costs import money_total

    entries = [
        {"node_id": "a", "cost": {"usd": 1.0, "gross_usd": 2.0, "basis": "api_list_price",
                                  "source": "snapshot 2026-08-28"}},
        {"node_id": "b", "cost": {"usd": 3.0, "gross_usd": 4.0, "basis": "api_equivalent",
                                  "source": "pricing.json"}},
    ]
    total = money_total(entries)
    assert total["usd"] == pytest.approx(4.0)
    assert total["sources"] == ["pricing.json", "snapshot 2026-08-28"]
    assert total["bases"] == ["api_equivalent", "api_list_price"]


def test_money_total_says_which_slice_of_the_run_it_covers():
    """``cost`` e de ESCOPO-STRETCH: uma celula replayada do cache retorna antes
    do ``account_leaf`` e nunca entra em ``node_costs``. Ao lado de um
    ``tokens_spent_total`` cumulativo, um total sem rotulo le como "o run
    inteiro custou isto"."""
    from lohra.workflow.costs import COST_SCOPE, money_total

    total = money_total([{"cost": {"usd": 1.0, "gross_usd": 1.0, "basis": "api_list_price"}}])
    assert total["scope"] == COST_SCOPE


def test_node_cost_entries_without_nodes_reads_no_price_file(monkeypatch):
    """``status()`` e polled: a listagem vazia (todo chamador antigo) nao pode
    pagar a I/O do pricing.json do operador para devolver []."""
    from lohra.workflow import costs

    def refuse(_path):
        raise AssertionError("read the operator's price file to build an empty listing")

    monkeypatch.setattr(costs, "load_price_overrides", refuse)
    assert costs.node_cost_entries(None) == []
    assert costs.node_cost_entries({}) == []
