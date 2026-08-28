"""Cost estimation — token usage × a static price table → USD.

Fail-closed by design: an unknown (provider, model) returns None, never zero or
a guess. Zero is reserved for providers that are actually free (local ollama).
Two cache-accounting conventions exist and the math must match the meter:

- openai-compat (openai, deepseek, groq, ...): ``input_tokens`` INCLUDES the
  cached tokens; ``cache_read_tokens`` is a breakdown of that total.
- anthropic: ``input_tokens`` EXCLUDES cache reads/writes; each is its own
  meter billed at its own rate.

Subscription (openai-codex) has no per-token bill — the estimate is computed
from a mapped API-equivalent model and labeled ``basis="api_equivalent"`` so it
never masquerades as an actual charge.
"""

from __future__ import annotations

from dataclasses import dataclass

from lohra.agent.types import Usage

# Providers whose usage meters count cache reads/writes OUTSIDE input_tokens.
_CACHE_EXCLUSIVE_PROVIDERS = frozenset({"anthropic"})

# Providers with no static list price by nature (never an error, never a guess).
_DYNAMIC_PROVIDERS = frozenset({"openrouter"})

_FREE_PROVIDERS = frozenset({"ollama"})

_USD_DECIMALS = 6  # sub-microdollar noise is float dust, not price signal


@dataclass(frozen=True)
class ModelPrice:
    """List price of one model, in USD per 1M tokens."""

    input_usd: float
    output_usd: float
    cached_input_usd: float | None = None  # None → cache reads bill as input
    cache_write_usd: float | None = None  # anthropic cache_creation meter


@dataclass(frozen=True)
class CostEstimate:
    """An estimated cost with its provenance — never presented as a bill."""

    usd: float
    basis: str  # "api_list_price" | "api_equivalent" | "local"
    note: str | None = None

    def as_dict(self) -> dict:
        out: dict = {"usd": round(self.usd, _USD_DECIMALS), "basis": self.basis}
        if self.note:
            out["note"] = self.note
        return out


def _lookup(table: dict, provider: str, model: str) -> ModelPrice | None:
    """Exact match first, then the LONGEST price-table prefix of the model id —
    so a dated snapshot (gpt-4o-mini-2024-07-18) resolves to its family price
    (gpt-4o-mini) and never to a shorter cousin (gpt-4o)."""
    exact = table.get((provider, model))
    if exact is not None:
        return exact
    best: tuple[int, ModelPrice] | None = None
    for (entry_provider, entry_model), price in table.items():
        if entry_provider != provider or not model.startswith(entry_model):
            continue
        if best is None or len(entry_model) > best[0]:
            best = (len(entry_model), price)
    return best[1] if best else None


def _cost_usd(usage: Usage, price: ModelPrice, *, cache_exclusive: bool) -> float:
    input_tokens = usage.input_tokens or 0
    output_tokens = usage.output_tokens or 0
    cache_read = usage.cache_read_tokens or 0
    cache_write = usage.cache_write_tokens or 0
    cached_rate = price.cached_input_usd if price.cached_input_usd is not None else price.input_usd

    if cache_exclusive:  # anthropic: three independent input meters
        input_cost = (
            input_tokens * price.input_usd
            + cache_read * cached_rate
            + cache_write * (price.cache_write_usd or 0.0)
        )
    else:  # openai-compat: cached tokens are a slice of input_tokens
        cached = min(cache_read, input_tokens)
        input_cost = (input_tokens - cached) * price.input_usd + cached * cached_rate
    return (input_cost + output_tokens * price.output_usd) / 1_000_000


def estimate_cost(
    usage: Usage | None,
    *,
    provider: str,
    model: str,
    table: dict[tuple[str, str], ModelPrice] | None = None,
    equivalents: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> CostEstimate | None:
    """Estimate the USD cost of ``usage`` on (provider, model), or None.

    ``table``/``equivalents`` default to the shipped price data; tests inject
    their own so the logic never depends on the data snapshot.
    """
    if usage is None:
        return None
    if provider in _FREE_PROVIDERS:
        return CostEstimate(usd=0.0, basis="local")
    if provider in _DYNAMIC_PROVIDERS:
        return None

    if table is None or equivalents is None:
        from lohra.pricing.table import EQUIVALENTS, PRICES

        table = PRICES if table is None else table
        equivalents = EQUIVALENTS if equivalents is None else equivalents

    basis = "api_list_price"
    mapped = equivalents.get((provider, model))
    if mapped is not None:
        provider, model = mapped
        basis = "api_equivalent"

    price = _lookup(table, provider, model)
    if price is None:
        return None
    usd = _cost_usd(usage, price, cache_exclusive=provider in _CACHE_EXCLUSIVE_PROVIDERS)
    return CostEstimate(usd=usd, basis=basis)
