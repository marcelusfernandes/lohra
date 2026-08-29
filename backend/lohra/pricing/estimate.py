"""Cost estimation — token usage × a price table → USD, gross AND real.

Fail-closed by design: an unknown (provider, model) returns None, never zero or
a guess. Zero is reserved for providers that are actually free (local ollama).

ONE cache convention (Fatia C). Every transport normalizes at its boundary so
``input_tokens`` means "prompt tokens NOT served from cache" in every provider —
the four token meters (uncached input, cache read, cache write, output) are
therefore DISJOINT, and the money math is a single formula instead of one per
provider. See ``providers/transports/*._normalize_usage``.

Two numbers come out of it, because a cached turn has two honest prices:

- ``usd`` (REAL): what the turn actually costs, each meter at its own rate.
- ``gross_usd``: what the SAME turn would have cost with no cache at all —
  every prompt token at the full input rate.

``saved_usd = gross - real`` is the cache's contribution. It can be NEGATIVE on
a first pass: a cache write costs more than a plain read of the same tokens, and
pretending otherwise would sell a premium as a discount.

A missing cache rate is never an invented discount: the meter bills at the full
input rate and the estimate carries a ``note`` saying so.

``reasoning_tokens`` is deliberately NOT priced — on OpenAI it is a breakdown of
``output_tokens`` and on Anthropic thinking IS output, so charging it again
would double-count. It stays informational.

Provenance is never silent: every estimate carries ``source`` — the dated
snapshot it came from, or the operator's ``~/.lohra/pricing.json`` (which wins,
per model, and may price models the snapshot has never heard of).

Subscription (openai-codex) has no per-token bill — the estimate is computed
from a mapped API-equivalent model and labeled ``basis="api_equivalent"`` so it
never masquerades as an actual charge.
"""

from __future__ import annotations

from dataclasses import dataclass

from lohra.agent.types import Usage

# What an estimate reports as the provenance of an operator-overridden price.
# Defined HERE, not in ``overrides``, because that module needs ``ModelPrice``
# from this one — the constant travels down, not back up.
OVERRIDE_SOURCE = "pricing.json"

# Providers with no static list price by nature (never an error, never a guess).
_DYNAMIC_PROVIDERS = frozenset({"openrouter"})

_FREE_PROVIDERS = frozenset({"ollama"})

# Providers billed by a plan, not per token — the estimate is notional there
# whatever the price came from (see ``_basis``).
_SUBSCRIPTION_PROVIDERS = frozenset({"openai-codex"})

_USD_DECIMALS = 6  # sub-microdollar noise is float dust, not price signal


@dataclass(frozen=True)
class ModelPrice:
    """List price of one model, in USD per 1M tokens."""

    input_usd: float
    output_usd: float
    cached_input_usd: float | None = None  # None → cache reads bill as input
    cache_write_usd: float | None = None  # None → cache writes bill as input


@dataclass(frozen=True)
class CostEstimate:
    """An estimated cost with its provenance — never presented as a bill."""

    usd: float  # REAL: what the cache actually made it cost
    gross_usd: float = 0.0  # as-if-no-cache, for the saving
    basis: str = "api_list_price"  # "api_list_price" | "api_equivalent" | "local"
    source: str | None = None  # "snapshot <date>" | "pricing.json"
    note: str | None = None

    @property
    def saved_usd(self) -> float:
        """What the cache saved. Negative when a write premium dominated."""
        return self.gross_usd - self.usd

    def as_dict(self) -> dict:
        out: dict = {
            "usd": round(self.usd, _USD_DECIMALS),
            "gross_usd": round(self.gross_usd, _USD_DECIMALS),
            "saved_usd": round(self.saved_usd, _USD_DECIMALS),
            "basis": self.basis,
        }
        if self.source:
            out["source"] = self.source
        if self.note:
            out["note"] = self.note
        return out


def _lookup(table: dict, provider: str, model: str) -> tuple[tuple[str, str], ModelPrice] | None:
    """(matched key, price): exact match first, then the LONGEST price-table
    prefix of the model id — so a dated snapshot (gpt-4o-mini-2024-07-18)
    resolves to its family price (gpt-4o-mini) and never to a shorter cousin
    (gpt-4o). The KEY comes back so the caller can say where the price is from."""
    exact = table.get((provider, model))
    if exact is not None:
        return ((provider, model), exact)
    best: tuple[tuple[str, str], ModelPrice] | None = None
    for key, price in table.items():
        entry_provider, entry_model = key
        if entry_provider != provider or not model.startswith(entry_model):
            continue
        # Fronteira obrigatória: "gpt-50" NÃO é "gpt-5" com sufixo — sem isto o
        # prefixo inventa preço autoritativo para modelo desconhecido (fail-
        # closed violado; repro do review: gpt-5.6-solar precificado como sol).
        rest = model[len(entry_model):]
        if rest and rest[0] not in "-.:/@":
            continue
        if best is None or len(entry_model) > len(best[0][1]):
            best = (key, price)
    return best


def _priced(
    table: dict, overrides: dict, provider: str, model: str
) -> tuple[ModelPrice, str] | None:
    """(price, source) — the most SPECIFIC price for this id across BOTH tables.

    ONE lookup over the union, not two lookups in priority order: consulting the
    operator's file first made specificity local to each table, so a three-letter
    override prefix outranked the snapshot's exact entry for the very model being
    priced (an 800x error, presented as a dollar figure). Merged, the operator
    still wins every key they actually wrote — a same-key override shadows the
    snapshot — and only loses to an entry that is genuinely more specific.
    """
    merged = {**table, **overrides}
    entry = _lookup(merged, provider, model)
    if entry is None:
        return None
    key, price = entry
    return (price, OVERRIDE_SOURCE if key in overrides else _snapshot_source())


def _basis(provider: str, model: str, equivalents: dict) -> str:
    """What KIND of bill this is — never what the price came FROM (``source``).

    An override changes the number, never the nature: a subscription still has no
    per-token bill after the operator pins a price to it, and ``api_equivalent``
    is the only label keeping that notional cost from reading as a charge.
    """
    if provider in _SUBSCRIPTION_PROVIDERS or (provider, model) in equivalents:
        return "api_equivalent"
    if provider in _FREE_PROVIDERS:
        return "local"
    return "api_list_price"


def _rates(usage: Usage, price: ModelPrice) -> tuple[float, float, str | None]:
    """(cache_read_rate, cache_write_rate, note) — an unpriced meter that this
    usage actually used bills at the FULL input rate and says so."""
    missing: list[str] = []
    read_rate = price.cached_input_usd
    if read_rate is None:
        read_rate = price.input_usd
        if usage.cache_read_tokens:
            missing.append("cache read")
    write_rate = price.cache_write_usd
    if write_rate is None:
        write_rate = price.input_usd
        if usage.cache_write_tokens:
            missing.append("cache write")
    note = (
        f"no {' and '.join(missing)} price for this model — billed at the full input rate"
        if missing
        else None
    )
    return read_rate, write_rate, note


def _costs(usage: Usage, price: ModelPrice) -> tuple[float, float, str | None]:
    """(real_usd, gross_usd, note) over the four DISJOINT meters."""
    uncached = max(0, usage.input_tokens or 0)
    cache_read = max(0, usage.cache_read_tokens or 0)
    cache_write = max(0, usage.cache_write_tokens or 0)
    output = max(0, usage.output_tokens or 0)
    read_rate, write_rate, note = _rates(usage, price)

    output_cost = output * price.output_usd
    real = (
        uncached * price.input_usd
        + cache_read * read_rate
        + cache_write * write_rate
        + output_cost
    )
    gross = (uncached + cache_read + cache_write) * price.input_usd + output_cost
    return (real / 1_000_000, gross / 1_000_000, note)


def _default_tables(
    table: dict | None, equivalents: dict | None, overrides: dict | None
) -> tuple[dict, dict, dict]:
    """Resolve the three price sources. An INJECTED ``table`` is a pure-logic
    call (tests): it never silently reads the operator's disk, so a machine with
    a ``pricing.json`` cannot change what a unit test measures."""
    if table is not None:
        return (table, equivalents or {}, overrides or {})
    from lohra.pricing.table import EQUIVALENTS, PRICES

    if overrides is None:
        from lohra.memory.paths import lohra_home
        from lohra.pricing.overrides import load_price_overrides, price_overrides_path

        overrides = load_price_overrides(price_overrides_path(lohra_home()))
    return (PRICES, EQUIVALENTS if equivalents is None else equivalents, overrides)


def _snapshot_source() -> str:
    from lohra.pricing.table import PRICES_AS_OF

    return f"snapshot {PRICES_AS_OF}"


def estimate_cost(
    usage: Usage | None,
    *,
    provider: str,
    model: str,
    table: dict[tuple[str, str], ModelPrice] | None = None,
    equivalents: dict[tuple[str, str], tuple[str, str]] | None = None,
    overrides: dict[tuple[str, str], ModelPrice] | None = None,
) -> CostEstimate | None:
    """Estimate the USD cost of ``usage`` on (provider, model), or None.

    ``table``/``equivalents``/``overrides`` default to the shipped price data
    plus the operator's file; tests inject their own so the logic never depends
    on the data snapshot or on the machine it runs on.
    """
    if usage is None:
        return None
    table, equivalents, overrides = _default_tables(table, equivalents, overrides)
    basis = _basis(provider, model, equivalents)

    found = _priced(table, overrides, provider, model)
    # An operator price on the id AS ASKED outranks the free/dynamic
    # short-circuits: someone who knows their openrouter (or local) rate gets a
    # real number instead of "unknown" or a hardcoded zero.
    if found is not None and found[1] == OVERRIDE_SOURCE:
        return _estimate(usage, found, basis)

    if provider in _FREE_PROVIDERS:
        return CostEstimate(usd=0.0, gross_usd=0.0, basis="local")
    if provider in _DYNAMIC_PROVIDERS:
        return None

    mapped = equivalents.get((provider, model))
    if mapped is not None:
        # Priced BY the API twin — including any override the operator wrote for
        # it, or the same model would cost two different things depending on
        # which surface asked.
        provider, model = mapped
        basis = "api_equivalent"
        found = _priced(table, overrides, provider, model)

    return _estimate(usage, found, basis) if found is not None else None


def _estimate(usage: Usage, found: tuple[ModelPrice, str], basis: str) -> CostEstimate:
    price, source = found
    real, gross, note = _costs(usage, price)
    return CostEstimate(usd=real, gross_usd=gross, basis=basis, source=source, note=note)
