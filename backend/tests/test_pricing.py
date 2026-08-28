"""Tests for lohra.pricing — $ cost estimation from token usage.

The logic is tested against an injected table so the tests never depend on the
real (data-only) price list. Semantics pinned here:

- unknown (provider, model) → None (fail-closed: never 0, never a guess)
- openrouter → None (dynamic pass-through pricing)
- ollama → $0 (local, basis "local")
- openai-compat cache math: input_tokens INCLUDES cached tokens (details are a
  breakdown) → billed = (input - cached)·in + cached·cached_rate + out·out_rate
- anthropic cache math: input_tokens EXCLUDES cache read/write (separate meters)
- subscription (openai-codex) → priced by the mapped API equivalent, labeled
  basis "api_equivalent" (the real bill is the plan, not per-token)
"""

import pytest

from lohra.agent.types import Usage
from lohra.pricing import CostEstimate, ModelPrice, estimate_cost

TABLE = {
    ("openai", "gpt-4o"): ModelPrice(input_usd=2.50, cached_input_usd=1.25, output_usd=10.00),
    ("openai", "gpt-4o-mini"): ModelPrice(input_usd=0.15, cached_input_usd=0.075, output_usd=0.60),
    ("anthropic", "claude-sonnet-4-6"): ModelPrice(
        input_usd=3.00, cached_input_usd=0.30, cache_write_usd=3.75, output_usd=15.00
    ),
    ("openai", "gpt-5.6"): ModelPrice(input_usd=1.00, cached_input_usd=0.10, output_usd=8.00),
}

EQUIVALENTS = {("openai-codex", "gpt-5.6-sol"): ("openai", "gpt-5.6")}


def _estimate(usage, provider, model):
    return estimate_cost(usage, provider=provider, model=model, table=TABLE, equivalents=EQUIVALENTS)


# --- fail-closed lookups ---


def test_unknown_model_is_none():
    assert _estimate(Usage(input_tokens=1000), "openai", "gpt-nonexistent") is None


def test_unknown_provider_is_none():
    assert _estimate(Usage(input_tokens=1000), "mystery", "gpt-4o") is None


def test_openrouter_is_none_dynamic_pricing():
    est = _estimate(Usage(input_tokens=1000), "openrouter", "openai/gpt-4o")
    assert est is None


def test_ollama_is_free():
    est = _estimate(Usage(input_tokens=1_000_000, output_tokens=500), "ollama", "llama3")
    assert est == CostEstimate(usd=0.0, basis="local")


def test_none_usage_is_none():
    assert estimate_cost(None, provider="openai", model="gpt-4o", table=TABLE) is None


# --- openai-compat math (input INCLUDES cached) ---


def test_openai_simple_no_cache():
    est = _estimate(Usage(input_tokens=1_000_000, output_tokens=1_000_000), "openai", "gpt-4o")
    assert est.usd == pytest.approx(2.50 + 10.00)
    assert est.basis == "api_list_price"


def test_openai_cached_split_billed_at_cached_rate():
    usage = Usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=400_000)
    est = _estimate(usage, "openai", "gpt-4o")
    # 600k at 2.50 + 400k at 1.25
    assert est.usd == pytest.approx(0.6 * 2.50 + 0.4 * 1.25)


def test_openai_cached_capped_at_input():
    # defensive: a cached count larger than input never bills negative tokens
    usage = Usage(input_tokens=100, cache_read_tokens=200)
    est = _estimate(usage, "openai", "gpt-4o")
    assert est.usd == pytest.approx(100 * 1.25 / 1e6)  # all 100 billed cached, none negative


def test_missing_cached_rate_bills_cached_as_input():
    table = {("openai", "m"): ModelPrice(input_usd=2.0, output_usd=4.0)}
    usage = Usage(input_tokens=1_000_000, cache_read_tokens=500_000)
    est = estimate_cost(usage, provider="openai", model="m", table=table)
    assert est.usd == pytest.approx(2.0)


# --- anthropic math (input EXCLUDES cache read/write) ---


def test_anthropic_cache_meters_are_additive():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    est = _estimate(usage, "anthropic", "claude-sonnet-4-6")
    assert est.usd == pytest.approx(3.00 + 15.00 + 0.30 + 3.75)


# --- prefix match (dated snapshots resolve to the family price) ---


def test_longest_prefix_match():
    est = _estimate(Usage(input_tokens=1_000_000), "openai", "gpt-4o-mini-2024-07-18")
    assert est.usd == pytest.approx(0.15)  # gpt-4o-mini, not gpt-4o


# --- subscription equivalents ---


def test_subscription_is_labeled_api_equivalent():
    est = _estimate(Usage(input_tokens=1_000_000, output_tokens=0), "openai-codex", "gpt-5.6-sol")
    assert est.basis == "api_equivalent"
    assert est.usd == pytest.approx(1.00)


def test_subscription_without_equivalent_is_none():
    assert _estimate(Usage(input_tokens=1000), "openai-codex", "gpt-unknown") is None


# --- serialization for the JSON envelope ---


def test_cost_estimate_as_dict():
    est = CostEstimate(usd=0.123456789, basis="api_list_price")
    d = est.as_dict()
    assert d == {"usd": 0.123457, "basis": "api_list_price"}


def test_cost_estimate_as_dict_with_note():
    d = CostEstimate(usd=0.0, basis="local", note="ollama runs locally").as_dict()
    assert d["note"] == "ollama runs locally"


# --- the real table is well-formed (data sanity, not price assertions) ---


def test_real_table_entries_are_positive():
    from lohra.pricing import PRICES, PRICES_AS_OF

    assert PRICES_AS_OF  # dated snapshot
    for (provider, model), price in PRICES.items():
        assert provider and model
        assert price.input_usd >= 0 and price.output_usd >= 0
        if price.cached_input_usd is not None:
            assert 0 <= price.cached_input_usd <= price.input_usd
