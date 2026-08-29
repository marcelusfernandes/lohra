"""Custo acumulado POR SESSÃO — liga as colunas fantasma da `sessions`.

Semântica travada: acumula NO MOMENTO DO GASTO (preço vigente do turno),
nunca recalcula na leitura; sessão = os próprios turnos (filhos têm ledger
próprio — dupla contagem é pior que visão parcial); `actual_cost_usd` = real,
`estimated_cost_usd` = bruto como-se-sem-cache (documentado no schema).
"""

from __future__ import annotations

import json

import pytest

from lohra.agent.session_cost import record_turn
from lohra.agent.types import Usage
from lohra.state import SessionDB


@pytest.fixture
def db():
    database = SessionDB(":memory:")
    yield database
    database.close()


def _usage(i=100, o=10, cr=0, cw=0, r=0):
    return Usage(
        input_tokens=i, output_tokens=o,
        cache_read_tokens=cr, cache_write_tokens=cw, reasoning_tokens=r,
    )


# --- camada DB ---------------------------------------------------------------


def test_session_add_usage_accumulates_across_turns(db):
    db.create_session("s1", model="m")
    db.session_add_usage("s1", _usage(100, 10, 5, 3, 2), real_usd=0.01, gross_usd=0.02)
    db.session_add_usage("s1", _usage(50, 5, 1, 1, 1), real_usd=0.005, gross_usd=0.01)
    row = db.session_usage("s1")
    assert row["input_tokens"] == 150 and row["output_tokens"] == 15
    assert row["cache_read_tokens"] == 6 and row["cache_write_tokens"] == 4
    assert row["reasoning_tokens"] == 3
    assert row["actual_cost_usd"] == pytest.approx(0.015)
    assert row["estimated_cost_usd"] == pytest.approx(0.03)


def test_session_add_usage_tolerates_legacy_null_columns(db):
    # Linha antiga (colunas fantasma NULL) precisa acumular via COALESCE.
    db.create_session("s1", model="m")
    db._connection.execute(
        "UPDATE sessions SET cache_read_tokens=NULL, actual_cost_usd=NULL WHERE id='s1'"
    )
    db.session_add_usage("s1", _usage(10, 1, 7), real_usd=0.001)
    row = db.session_usage("s1")
    assert row["cache_read_tokens"] == 7
    assert row["actual_cost_usd"] == pytest.approx(0.001)


def test_session_add_usage_without_price_adds_tokens_only(db):
    db.create_session("s1", model="m")
    db.session_add_usage("s1", _usage(10, 1))
    row = db.session_usage("s1")
    assert row["input_tokens"] == 10
    assert row["actual_cost_usd"] in (None, 0)  # nunca inventa dinheiro


# --- helper: preço do momento (snapshot/override), fail-closed ---------------


def test_record_turn_prices_with_the_operator_override(db, tmp_path):
    (tmp_path / "pricing.json").write_text(json.dumps(
        {"prov": {"mod": {"input_usd": 1.0, "output_usd": 2.0, "cached_input_usd": 0.1}}}
    ))
    db.create_session("s1", model="mod")
    summary = record_turn(
        db, "s1", _usage(1_000_000, 1_000_000, 1_000_000),
        provider="prov", model="mod", home=tmp_path,
    )
    # real: 1.0 (in) + 2.0 (out) + 0.1 (cached) ; bruto: 2*1.0 + 2.0
    assert summary["cost"]["usd"] == pytest.approx(3.1)
    assert summary["cost"]["gross_usd"] == pytest.approx(4.0)
    row = db.session_usage("s1")
    assert row["actual_cost_usd"] == pytest.approx(3.1)


def test_record_turn_unknown_model_accumulates_tokens_without_money(db, tmp_path):
    db.create_session("s1", model="???")
    summary = record_turn(
        db, "s1", _usage(10, 1), provider="prov", model="???", home=tmp_path
    )
    assert summary["input_tokens"] == 10
    assert "cost" not in summary  # fail-closed: sem preço, sem dinheiro
    assert db.session_usage("s1")["actual_cost_usd"] in (None, 0)


def test_record_turn_none_usage_is_a_noop(db, tmp_path):
    db.create_session("s1", model="m")
    assert record_turn(db, "s1", None, provider="p", model="m", home=tmp_path) is None
    assert db.session_usage("s1")["input_tokens"] == 0


def test_record_turn_cumulative_summary_grows(db, tmp_path):
    db.create_session("s1", model="m")
    record_turn(db, "s1", _usage(10, 1), provider="p", model="m", home=tmp_path)
    summary = record_turn(db, "s1", _usage(5, 2), provider="p", model="m", home=tmp_path)
    assert summary["input_tokens"] == 15 and summary["output_tokens"] == 3
