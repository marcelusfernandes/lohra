"""Choosing a real model when the authored one does not exist (W9-E8, #85).

The owner's decision on #54: a nonexistent model named by the AUTHOR is
``agency`` — the harness has a catalog and should not make that mistake — and a
nonexistent model named by a HUMAN instruction must not kill the leaf or stop
the run for a person to answer. The harness picks an existing adequate model and
says so, out loud, every time.

That is a NEW authority, and the two mechanisms next door refuse it on purpose:
``leaf_retry`` re-spawns only on the route the spec authored ("re-routing is
explicitly NOT here"), and ``routes``/``route_fault`` move a node only inside the
envelope an operator wrote before the run ("zero new authority"). Neither is
wrong; neither covers a typo. So this module exists, and it is deliberately the
narrowest thing that answers the owner: **one** substitution, on the **same
provider**, to a model the **operator already sanctioned**, whose **bill is
readable offline**, or nothing at all.

**Offline by construction.** ``catalog/catalog.py`` builds the real catalog by
talking to every provider — a live fetch, several seconds, and a failure mode of
its own. It must never be reached from a leaf's dispatch path: a run whose model
slug was wrong would then also depend on the provider's ``/models`` endpoint
being up, and network availability is ``environment``, the exact thing the
taxonomy asks never to be confused with authoring. The offline stand-in is the
PRICE SNAPSHOT (``pricing/table.py`` + the operator's ``pricing.json``): every id
in it is a model somebody published, and every id in it has a readable bill —
which is the pair the choice needs anyway.

**The pool is the operator's, never the catalog's.** The candidate is read from
``~/.lohra/workflow_tiers.json`` (``tiers.TierMap``), the same file that already
decides what ``tier: big`` means, and the catalog only CONFIRMS that the model
the operator named is real. Picking freely off a price table would be the
harness choosing a route nobody sanctioned — which is precisely the authority
``routes.py`` withholds from itself, and this slice has no better claim to it.

**Never costlier than what was asked for, and never unpriced.** A candidate
whose price neither the snapshot nor the operator knows is refused (doctrine 2
of #63: the harness never acts on a bill it cannot read), so a dynamic provider
substitutes nothing without an operator override, and a subscription plan is
never a candidate at all. Note that ``cheaper_or_equal`` against the DEAD route
is not available here and never can be: a model that does not exist has no
price, so the comparison would be ``None`` — unknown — for every substitution
this module could ever make. What bounds the choice instead is the tier the
author declared plus an optional explicit ``price_cap``.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from lohra.pricing.estimate import ModelPrice, list_price
from lohra.workflow.tiers import MODEL_TIERS

# The subscription provider is billed by a PLAN, so "cheaper per token" is not a
# comparison that exists for it — the same reason ``routes.py`` refuses it as a
# fallback and ``list_price`` returns None for it. Never a source of candidates,
# never a candidate.
SUBSCRIPTION_PROVIDER = "openai-codex"


def offline_catalog(
    *, table: dict[tuple[str, str], ModelPrice] | None = None
) -> dict[str, tuple[str, ...]]:
    """``provider -> the model ids known offline``, from the price snapshot.

    Not the live catalog and never claiming to be: it is the set of models the
    shipped snapshot (or the operator's ``pricing.json``) has a rate for. That
    makes it both narrower and safer than a ``/models`` listing — narrower
    because a brand-new model nobody priced is absent, safer because everything
    in it can be compared on cost without a second lookup.

    ``table`` is injected by tests so a verdict never depends on the shipped
    snapshot or on the machine's own ``pricing.json``.
    """
    if table is None:
        from lohra.memory.paths import lohra_home
        from lohra.pricing.overrides import load_price_overrides, price_overrides_path
        from lohra.pricing.table import PRICES

        table = {**PRICES, **load_price_overrides(price_overrides_path(lohra_home()))}
    catalog: dict[str, list[str]] = {}
    for key in table:
        if not (isinstance(key, tuple) and len(key) == 2):
            continue
        provider, model = key
        if not (isinstance(provider, str) and isinstance(model, str)):
            continue
        if provider == SUBSCRIPTION_PROVIDER:
            continue  # a plan is not a per-token route (#63)
        catalog.setdefault(provider, []).append(model)
    return {provider: tuple(models) for provider, models in catalog.items()}


def tier_order(tier: Any) -> tuple[str, ...]:
    """Which tiers to try, in order: the one declared, then the ones ABOVE it.

    "The nearest above" is the direction the issue asks for, and it only ever
    applies to a tier the operator left UNMAPPED — a mapped one answers on the
    first step. A node that declared no tier at all has no anchor, so the walk
    starts at the cheapest: that is the only start that cannot spend more of the
    operator's money than the author asked for, and the alternative (guessing
    the author meant ``big``) is the harness inventing an intention.
    """
    if isinstance(tier, str) and tier in MODEL_TIERS:
        return MODEL_TIERS[MODEL_TIERS.index(tier):]
    return MODEL_TIERS


def _priced_within(
    provider: str,
    model: str,
    cap: ModelPrice | None,
    table: dict[tuple[str, str], ModelPrice] | None,
) -> bool:
    """Is this route's bill READABLE, and inside the cap when there is one?

    Fail-closed on both halves, and on both meters when a cap exists — the same
    rule ``routes.cheaper_or_equal`` applies, for the same reason: a candidate
    that halves the input rate and triples the output rate is not cheaper, it is
    a different bet on somebody else's money.
    """
    price = list_price(provider, model, table=table)
    if price is None:
        return False
    if cap is None:
        return True
    return price.input_usd <= cap.input_usd and price.output_usd <= cap.output_usd


def choose(
    catalog: Mapping[str, Sequence[str]],
    provider: Any,
    model: Any,
    tier: Any,
    tiers: Any,
    *,
    price_cap: ModelPrice | None = None,
    table: dict[tuple[str, str], ModelPrice] | None = None,
) -> str | None:
    """The ONE model this dead slug may be replaced with, or None.

    Every refusal returns None and the caller falls back to exactly today's
    behaviour, so the failure mode of this whole feature is "the harness did not
    help", never "the harness ran something nobody authorized".

    The refusals, each load-bearing:

    - no provider, or the SUBSCRIPTION provider: a plan has no per-token bill;
    - no operator tier map: the pool is the operator's config and nothing else;
    - a tier mapped onto a DIFFERENT provider: cross-provider substitution is
      out of scope (#85) and out of authority (#63) — another provider is
      another credential and another bill;
    - a candidate the offline catalog does not know: substituting one
      nonexistent slug for another buys a second 404;
    - a candidate nobody prices, or one past ``price_cap``;
    - the dead slug itself, however the operator mapped it.
    """
    if not (isinstance(provider, str) and provider.strip()):
        return None
    provider = provider.strip()
    if provider == SUBSCRIPTION_PROVIDER:
        return None
    get = getattr(tiers, "get", None)
    if not callable(get):
        return None
    known = catalog.get(provider) or ()
    dead = model.strip() if isinstance(model, str) else ""
    for name in tier_order(tier):
        entry = get(name)
        candidate = getattr(entry, "model", None) if entry is not None else None
        if not (isinstance(candidate, str) and candidate.strip()):
            continue
        candidate = candidate.strip()
        mapped_provider = getattr(entry, "provider", None)
        if isinstance(mapped_provider, str) and mapped_provider.strip() != provider:
            continue
        if candidate == dead or candidate not in known:
            continue
        if not _priced_within(provider, candidate, price_cap, table):
            continue
        return candidate
    return None


def substitution_fault(node_id: str, provider: str, dead: str, chosen: str) -> str:
    """The line that keeps the substitution from ever being silent.

    Says the three things a reader has to act on — which node, which slug does
    not exist, and what ran instead — and says where the replacement came from,
    because the harness picking a model on its own would be exactly the
    authority ``route_fault`` withholds. It came from the operator's tier map.
    """
    return (
        f"{node_id}: model {dead!r} does not exist on {provider!r}; substituted "
        f"by {chosen!r} from the operator's tier map — fix the spec, or map the "
        "tier you meant in ~/.lohra/workflow_tiers.json"
    )
