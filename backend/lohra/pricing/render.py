"""Human rendering of token usage and cost — one phrasing, everywhere.

The same sentence has to read the same in the CLI's closing line, in a workflow
rollup and in ``workflow_status``, or the operator has to learn three formats for
one number. Pure formatting: no lookups, no I/O beyond the price table the
estimator itself consults.

Two things are always said together, because either alone misleads:

- the TOKENS, split into what was paid for and what the cache served
  (``12,300 in (9,800 cached) + 1,200 out``);
- the MONEY, real against gross, with the saving and the price's SOURCE
  (``$0.012345 (gross $0.041000, saved $0.028655) · snapshot 2026-08-28``).
"""

from __future__ import annotations

from lohra.agent.types import Usage
from lohra.pricing.estimate import CostEstimate, estimate_cost


def format_tokens(usage: Usage | None) -> str:
    """``X in (Y cached[, Z written]) + W out`` — the cache made visible.

    The cached/written parenthetical only appears when there is something to
    report: a provider with no caching must not read as "0 cached" forever."""
    if usage is None:
        return "no usage reported"
    parts = []
    if usage.cache_read_tokens:
        parts.append(f"{usage.cache_read_tokens:,} cached")
    if usage.cache_write_tokens:
        parts.append(f"{usage.cache_write_tokens:,} written")
    cached = f" ({', '.join(parts)})" if parts else ""
    return f"{usage.input_tokens:,} in{cached} + {usage.output_tokens:,} out"


def format_cost(estimate: CostEstimate | None) -> str | None:
    """``$real (gross $g, saved $s) · <source>`` — None when nothing is priced.

    The saving is omitted when it is zero (nothing was cached): "saved $0.00"
    is noise, and a negative saving (a cache WRITE premium) is reported as a
    cost instead of dressed up as a discount."""
    if estimate is None:
        return None
    line = f"${estimate.usd:.6f}"
    saved = estimate.saved_usd
    if saved > 0:
        line += f" (gross ${estimate.gross_usd:.6f}, saved ${saved:.6f})"
    elif saved < 0:
        line += f" (gross ${estimate.gross_usd:.6f}, cache write premium ${-saved:.6f})"
    if estimate.basis != "api_list_price":
        line += f" [{estimate.basis}]"
    if estimate.source:
        line += f" · {estimate.source}"
    if estimate.note:
        line += f" · {estimate.note}"
    return line


def cost_line(usage: Usage | None, *, provider: str | None, model: str | None) -> str | None:
    """The one-line summary of a turn: money, then tokens. None when the turn
    reported no usage at all, or when this (provider, model) has no price —
    a fabricated dollar figure is worse than none (see lohra.pricing)."""
    if usage is None or not provider or not model:
        return None
    estimate = estimate_cost(usage, provider=provider, model=model)
    money = format_cost(estimate)
    if money is None:
        return None
    return f"cost: {money} · {format_tokens(usage)}"
