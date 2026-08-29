"""Tests for lohra.pricing — $ cost estimation from token usage.

The logic is tested against an injected table so the tests never depend on the
real (data-only) price list. Semantics pinned here:

- unknown (provider, model) → None (fail-closed: never 0, never a guess)
- openrouter → None (dynamic pass-through pricing) unless the operator priced it
- ollama → $0 (local, basis "local")
- ONE cache convention (Fatia C): ``input_tokens`` is what was NOT cached, in
  every provider, so the math is a single formula over four disjoint meters —
  uncached input, cache read, cache write, output.
- every estimate is BOTH real (what the cache made it cost) and gross (what it
  would have cost with no cache at all); the difference is the saving.
- every estimate carries its SOURCE (the dated snapshot, or the operator's
  ~/.lohra/pricing.json), so a stale price is never silent.
- subscription (openai-codex) → priced by the mapped API equivalent, labeled
  basis "api_equivalent" (the real bill is the plan, not per-token)
"""

import json

import pytest

from lohra.agent.types import Usage
from lohra.pricing import CostEstimate, ModelPrice, estimate_cost, load_price_overrides
from lohra.pricing.table import PRICES_AS_OF

TABLE = {
    ("openai", "gpt-4o"): ModelPrice(input_usd=2.50, cached_input_usd=1.25, output_usd=10.00),
    ("openai", "gpt-4o-mini"): ModelPrice(input_usd=0.15, cached_input_usd=0.075, output_usd=0.60),
    ("anthropic", "claude-sonnet-4-6"): ModelPrice(
        input_usd=3.00, cached_input_usd=0.30, cache_write_usd=3.75, output_usd=15.00
    ),
    ("openai", "gpt-5.6"): ModelPrice(input_usd=1.00, cached_input_usd=0.10, output_usd=8.00),
}

EQUIVALENTS = {("openai-codex", "gpt-5.6-sol"): ("openai", "gpt-5.6")}

SNAPSHOT = f"snapshot {PRICES_AS_OF}"


def _estimate(usage, provider, model, overrides=None):
    return estimate_cost(
        usage,
        provider=provider,
        model=model,
        table=TABLE,
        equivalents=EQUIVALENTS,
        overrides=overrides,
    )


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
    assert est.usd == 0.0 and est.gross_usd == 0.0 and est.basis == "local"


def test_none_usage_is_none():
    assert estimate_cost(None, provider="openai", model="gpt-4o", table=TABLE) is None


# --- ONE formula: input_tokens is always the UNCACHED part ---


def test_simple_no_cache():
    est = _estimate(Usage(input_tokens=1_000_000, output_tokens=1_000_000), "openai", "gpt-4o")
    assert est.usd == pytest.approx(2.50 + 10.00)
    assert est.gross_usd == pytest.approx(2.50 + 10.00)  # no cache → gross == real
    assert est.saved_usd == pytest.approx(0.0)
    assert est.basis == "api_list_price"


def test_openai_cache_read_billed_at_cached_rate_disjoint():
    # 600k uncached + 400k read from cache — DISJOINT (the transport subtracted).
    usage = Usage(input_tokens=600_000, output_tokens=0, cache_read_tokens=400_000)
    est = _estimate(usage, "openai", "gpt-4o")
    assert est.usd == pytest.approx(0.6 * 2.50 + 0.4 * 1.25)


def test_anthropic_uses_the_same_formula():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    est = _estimate(usage, "anthropic", "claude-sonnet-4-6")
    assert est.usd == pytest.approx(3.00 + 15.00 + 0.30 + 3.75)


# --- gross vs real (the cache made it cheaper — show by how much) ---


def test_gross_is_the_cost_as_if_nothing_were_cached():
    usage = Usage(
        input_tokens=200_000,
        output_tokens=100_000,
        cache_read_tokens=700_000,
        cache_write_tokens=100_000,
    )
    est = _estimate(usage, "anthropic", "claude-sonnet-4-6")
    # gross: all 1M prompt tokens at the full input rate
    assert est.gross_usd == pytest.approx(3.00 + 0.1 * 15.00)
    assert est.usd == pytest.approx(
        0.2 * 3.00 + 0.7 * 0.30 + 0.1 * 3.75 + 0.1 * 15.00
    )
    assert est.saved_usd == pytest.approx(est.gross_usd - est.usd)
    assert est.saved_usd > 0


def test_cache_write_can_cost_more_than_gross():
    """A write premium is a REAL loss on a first pass — never hidden as a saving."""
    usage = Usage(input_tokens=0, output_tokens=0, cache_write_tokens=1_000_000)
    est = _estimate(usage, "anthropic", "claude-sonnet-4-6")
    assert est.usd == pytest.approx(3.75)
    assert est.gross_usd == pytest.approx(3.00)
    assert est.saved_usd == pytest.approx(-0.75)


# --- missing cache prices: full input rate + a NOTE, never an invented discount ---


def test_missing_cached_read_price_bills_as_input_with_note():
    table = {("openai", "m"): ModelPrice(input_usd=2.0, output_usd=4.0)}
    usage = Usage(input_tokens=1_000_000, cache_read_tokens=500_000)
    est = estimate_cost(usage, provider="openai", model="m", table=table, overrides={})
    assert est.usd == pytest.approx(3.0)  # 1.5M tokens at the full input rate
    assert est.usd == pytest.approx(est.gross_usd)  # no discount was invented
    assert "cache read" in (est.note or "")


def test_missing_cache_write_price_bills_as_input_with_note():
    table = {("openai", "m"): ModelPrice(input_usd=2.0, cached_input_usd=0.2, output_usd=4.0)}
    usage = Usage(input_tokens=0, cache_write_tokens=1_000_000)
    est = estimate_cost(usage, provider="openai", model="m", table=table, overrides={})
    assert est.usd == pytest.approx(2.0)
    assert "cache write" in (est.note or "")


def test_no_note_when_the_unpriced_meter_is_unused():
    table = {("openai", "m"): ModelPrice(input_usd=2.0, output_usd=4.0)}
    est = estimate_cost(
        Usage(input_tokens=1000), provider="openai", model="m", table=table, overrides={}
    )
    assert est.note is None


def test_reasoning_tokens_are_never_priced_separately():
    """Reasoning is a BREAKDOWN of output (openai) or is output (anthropic) —
    pricing it again would double-count it."""
    plain = _estimate(Usage(input_tokens=1000, output_tokens=1000), "openai", "gpt-4o")
    reasoned = _estimate(
        Usage(input_tokens=1000, output_tokens=1000, reasoning_tokens=900), "openai", "gpt-4o"
    )
    assert reasoned.usd == pytest.approx(plain.usd)


# --- prefix match (dated snapshots resolve to the family price) ---


def test_longest_prefix_match():
    est = _estimate(Usage(input_tokens=1_000_000), "openai", "gpt-4o-mini-2024-07-18")
    assert est.usd == pytest.approx(0.15)  # gpt-4o-mini, not gpt-4o


# --- subscription equivalents ---


def test_subscription_is_labeled_api_equivalent():
    est = _estimate(Usage(input_tokens=1_000_000, output_tokens=0), "openai-codex", "gpt-5.6-sol")
    assert est.basis == "api_equivalent"
    assert est.usd == pytest.approx(1.00)
    assert est.source == SNAPSHOT


def test_subscription_without_equivalent_is_none():
    assert _estimate(Usage(input_tokens=1000), "openai-codex", "gpt-unknown") is None


# --- source: every price says where it came from ---


def test_source_is_the_dated_snapshot_by_default():
    est = _estimate(Usage(input_tokens=1000), "openai", "gpt-4o")
    assert est.source == SNAPSHOT
    assert PRICES_AS_OF in est.source


def test_operator_override_wins_and_says_so():
    overrides = {("openai", "gpt-4o"): ModelPrice(input_usd=10.0, output_usd=0.0)}
    est = _estimate(Usage(input_tokens=1_000_000), "openai", "gpt-4o", overrides=overrides)
    assert est.usd == pytest.approx(10.0)
    assert est.source == "pricing.json"


def test_operator_override_prices_a_model_outside_the_snapshot():
    overrides = {("openai", "gpt-9-unreleased"): ModelPrice(input_usd=1.0, output_usd=2.0)}
    est = _estimate(
        Usage(input_tokens=1_000_000, output_tokens=1_000_000),
        "openai",
        "gpt-9-unreleased",
        overrides=overrides,
    )
    assert est.usd == pytest.approx(3.0)
    assert est.source == "pricing.json"


def test_operator_override_prices_a_dynamic_provider():
    """openrouter has no list price by nature — an operator who knows theirs
    may pin it, and that answer beats "unknown"."""
    overrides = {("openrouter", "openai/gpt-4o"): ModelPrice(input_usd=2.5, output_usd=10.0)}
    est = _estimate(Usage(input_tokens=1_000_000), "openrouter", "openai/gpt-4o", overrides)
    assert est.usd == pytest.approx(2.5)
    assert est.source == "pricing.json"


def test_a_short_override_prefix_never_beats_an_exact_snapshot_match():
    """Specificity wins ACROSS the two tables, not just inside each one.

    An operator who pins ``gpt-5.6`` must not silently reprice every model whose
    id merely starts with it — the snapshot's EXACT entry for that id is the more
    specific answer, and letting a 3-character-shorter override win produced an
    800x error presented as a dollar figure."""
    overrides = {("openai", "gpt-4"): ModelPrice(input_usd=999.0, output_usd=999.0)}
    est = _estimate(Usage(input_tokens=1_000_000), "openai", "gpt-4o", overrides=overrides)
    assert est.usd == pytest.approx(2.50)  # the snapshot's exact gpt-4o
    assert est.source == SNAPSHOT
    # ...and the override still owns every id the snapshot does NOT price exactly
    other = _estimate(Usage(input_tokens=1_000_000), "openai", "gpt-4-turbo", overrides=overrides)
    assert other.usd == pytest.approx(999.0)
    assert other.source == "pricing.json"


def test_an_override_changes_the_price_not_the_nature_of_the_charge():
    """``basis`` says what KIND of bill this is; ``source`` says where the price
    came from. A subscription has no per-token bill no matter who priced it, so
    an override must not turn ``api_equivalent`` into ``api_list_price`` — that
    label is the only thing keeping a notional cost from reading as a charge."""
    overrides = {("openai-codex", "gpt-5.6-sol"): ModelPrice(input_usd=7.0, output_usd=0.0)}
    est = _estimate(Usage(input_tokens=1_000_000), "openai-codex", "gpt-5.6-sol", overrides)
    assert est.usd == pytest.approx(7.0)
    assert est.source == "pricing.json"
    assert est.basis == "api_equivalent"


def test_an_override_on_the_api_twin_reaches_the_subscription_that_maps_to_it():
    """The operator corrected the OpenAI price; the run under the Codex
    subscription is priced BY that OpenAI model, so it must see the correction
    too — otherwise the same (provider, model) has two prices depending on which
    surface asked."""
    overrides = {("openai", "gpt-5.6"): ModelPrice(input_usd=100.0, output_usd=100.0)}
    est = _estimate(Usage(input_tokens=1_000_000), "openai-codex", "gpt-5.6-sol", overrides)
    assert est.usd == pytest.approx(100.0)
    assert est.source == "pricing.json"
    assert est.basis == "api_equivalent"


def test_an_override_on_a_local_model_prices_it_without_calling_it_an_api_bill():
    """ollama is ``basis="local"`` because there is no API bill at all. An
    operator who pins what their own hardware costs gets that number — and the
    label stays ``local``: the price came from them, the nature of the charge
    did not change."""
    overrides = {("ollama", "llama3"): ModelPrice(input_usd=0.5, output_usd=0.5)}
    est = _estimate(Usage(input_tokens=1_000_000), "ollama", "llama3", overrides=overrides)
    assert est.usd == pytest.approx(0.5)
    assert est.source == "pricing.json"
    assert est.basis == "local"


# --- the override loader: fail-safe, exactly like load_tiers ---


def test_load_overrides_missing_file_is_empty(tmp_path):
    assert load_price_overrides(tmp_path / "pricing.json") == {}


def test_load_overrides_garbage_is_empty(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text("{not json at all", encoding="utf-8")
    assert load_price_overrides(path) == {}


def test_load_overrides_non_dict_is_empty(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_price_overrides(path) == {}


def test_load_overrides_reads_nested_provider_model_shape(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "openai": {
                    "gpt-9": {
                        "input_usd": 1.0,
                        "output_usd": 2.0,
                        "cached_input_usd": 0.1,
                        "cache_write_usd": 1.25,
                    }
                },
                "openrouter": {"openai/gpt-4o": {"input_usd": 2.5, "output_usd": 10.0}},
            }
        ),
        encoding="utf-8",
    )
    loaded = load_price_overrides(path)
    assert loaded[("openai", "gpt-9")] == ModelPrice(
        input_usd=1.0, output_usd=2.0, cached_input_usd=0.1, cache_write_usd=1.25
    )
    assert loaded[("openrouter", "openai/gpt-4o")].input_usd == 2.5


def test_load_overrides_drops_malformed_entries_keeps_the_rest(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(
        json.dumps(
            {
                "openai": {
                    "good": {"input_usd": 1.0, "output_usd": 2.0},
                    "no_output": {"input_usd": 1.0},
                    "negative": {"input_usd": -1.0, "output_usd": 2.0},
                    "not_a_number": {"input_usd": "cheap", "output_usd": 2.0},
                    "not_a_dict": 5,
                },
                "bad_provider": "nope",
            }
        ),
        encoding="utf-8",
    )
    loaded = load_price_overrides(path)
    assert list(loaded) == [("openai", "good")]


def test_load_overrides_ignores_a_bool_price(tmp_path):
    # json true is an int in Python — a price of True is not a price
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps({"openai": {"m": {"input_usd": True, "output_usd": 2.0}}}), "utf-8")
    assert load_price_overrides(path) == {}


# --- serialization for the JSON envelope ---


def test_cost_estimate_as_dict():
    est = CostEstimate(
        usd=0.123456789, gross_usd=0.2, basis="api_list_price", source="snapshot 2026-08-28"
    )
    d = est.as_dict()
    assert d == {
        "usd": 0.123457,
        "gross_usd": 0.2,
        "saved_usd": 0.076543,
        "basis": "api_list_price",
        "source": "snapshot 2026-08-28",
    }


def test_cost_estimate_as_dict_with_note():
    d = CostEstimate(usd=0.0, gross_usd=0.0, basis="local", note="ollama runs locally").as_dict()
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


def test_real_estimate_reads_the_operator_file_from_the_profile_home(tmp_path, monkeypatch):
    """No injection at all: the production path resolves ~/.lohra/pricing.json
    through lohra_home(), so a profile stays isolated by construction."""
    monkeypatch.setenv("LOHRA_HOME", str(tmp_path))
    monkeypatch.delenv("LOHRA_PROFILE", raising=False)
    (tmp_path / "pricing.json").write_text(
        json.dumps({"anthropic": {"claude-made-up": {"input_usd": 1.0, "output_usd": 1.0}}}),
        encoding="utf-8",
    )
    est = estimate_cost(
        Usage(input_tokens=1_000_000), provider="anthropic", model="claude-made-up"
    )
    assert est is not None and est.usd == pytest.approx(1.0)
    assert est.source == "pricing.json"


def test_prefix_lookup_requires_a_version_boundary():
    # Review sol #8: gpt-50 NÃO é gpt-5; gpt-5.6-solar NÃO é gpt-5.6-sol.
    from lohra.agent.types import Usage
    from lohra.pricing import estimate_cost
    from lohra.pricing.estimate import ModelPrice

    table = {("openai", "gpt-5"): ModelPrice(input_usd=1.25, output_usd=10.0)}
    u = Usage(input_tokens=1000, output_tokens=0)
    assert estimate_cost(u, provider="openai", model="gpt-50", table=table) is None
    assert estimate_cost(u, provider="openai", model="gpt-5-mini", table=table) is not None
    assert estimate_cost(u, provider="openai", model="gpt-5", table=table) is not None
