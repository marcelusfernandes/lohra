"""Per-node cost reporting for a run (Fatia C) — tokens, then money.

The rollup already said what a run cost IN TOTAL. The question an author asks is
narrower: which node is the expensive one, and how much of its prompt the cache
actually served. That needs the split attributed per node — which is what
``engine.node_costs()`` keeps — plus a price for the (provider, model) that node
really ran on.

Fail-closed on money, never on tokens: a node whose model has no list price
reports its tokens and simply carries no ``cost`` key. The run-level total sums
only the nodes that WERE priced and says how many those were, so a partial total
can never read as the whole bill.

The total carries its own provenance (``sources``/``bases``) and its own SCOPE
for the same reason the per-node entries do: it is the number the operator
actually reads, it can mix a stale snapshot with the operator's file, it can mix
real list price with subscription money that is never billed per token, and on a
resumed run it covers only the cells this stretch re-ran.
"""

from __future__ import annotations

from typing import Any

from lohra.memory.paths import lohra_home
from lohra.pricing.estimate import estimate_cost
from lohra.pricing.overrides import load_price_overrides, price_overrides_path
from lohra.pricing.render import format_tokens

_USD_DECIMALS = 6

# What ``money_total`` covers. A cache-replayed cell returns before
# ``account_leaf``, so it never reaches ``node_costs`` — a resumed run's money
# is the money of THIS stretch, sitting next to a CUMULATIVE
# ``tokens_spent_total``. Saying so is the difference between a partial number
# and a wrong one.
COST_SCOPE = "nodes_executed_in_this_stretch"


def node_cost_entries(nodes: dict[str, Any] | None) -> list[dict]:
    """One entry per node that actually spent something, in run order."""
    if not nodes:
        return []  # no node, no price file: ``status()`` is polled
    entries: list[dict] = []
    # Read the operator's price file ONCE for the whole listing, not once per
    # node: a status poll on a 20-node run would otherwise re-open it 20 times,
    # and half a listing priced from an edited file is worse than either half.
    overrides = _overrides()
    for node_id, cost in nodes.items():
        usage = cost.usage
        if not any(
            (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
             usage.cache_write_tokens, usage.reasoning_tokens)
        ):
            continue  # a node that spawned no leaf (a checkpoint, a cache hit)
        entry: dict[str, Any] = {
            "node_id": node_id,
            "tokens": format_tokens(usage),  # "X in (Y cached) + Z out"
            "tokens_in": usage.input_tokens,
            "tokens_out": usage.output_tokens,
        }
        for name in ("cache_read_tokens", "cache_write_tokens", "reasoning_tokens"):
            value = getattr(usage, name)
            if value:
                entry[name] = value
        if cost.provider:
            entry["provider"] = cost.provider
        if cost.model:
            entry["model"] = cost.model
        estimate = estimate_cost(
            usage, provider=cost.provider or "", model=cost.model or "", overrides=overrides
        )
        if estimate is not None:
            entry["cost"] = estimate.as_dict()
        entries.append(entry)
    return entries


def _overrides() -> dict:
    """The operator's price overrides, or {} — never a reason a rollup fails."""
    try:
        return load_price_overrides(price_overrides_path(lohra_home()))
    except (OSError, ValueError):  # a profile name that cannot resolve to a path
        return {}


def money_total(entries: list[dict]) -> dict | None:
    """The run's money, summed over the PRICED nodes only — or None when nothing
    could be priced. ``nodes_priced``/``nodes_total`` are part of the answer: a
    total over half the nodes is not the run's bill and must not look like it.

    So are ``sources``/``bases`` (which price data, and what KIND of charge each
    summed node was) and ``scope`` (which slice of a resumed run this covers)."""
    priced = [entry["cost"] for entry in entries if "cost" in entry]
    if not priced:
        return None
    usd = sum(cost["usd"] for cost in priced)
    gross = sum(cost["gross_usd"] for cost in priced)
    return {
        "usd": round(usd, _USD_DECIMALS),
        "gross_usd": round(gross, _USD_DECIMALS),
        "saved_usd": round(gross - usd, _USD_DECIMALS),
        "nodes_priced": len(priced),
        "nodes_total": len(entries),
        "scope": COST_SCOPE,
        # Provenance travels with the SUM too, not just with each node. Two
        # things would otherwise go silent exactly where the operator reads:
        # a stale snapshot, and — in a cross-provider run — subscription money
        # (``api_equivalent``: no per-token bill at all) added into the same
        # ``usd`` as real list price.
        "sources": sorted({cost["source"] for cost in priced if cost.get("source")}),
        "bases": sorted({cost["basis"] for cost in priced if cost.get("basis")}),
    }
