"""The operator's ROUTE ENVELOPE (#63) — a finite, pre-authorized fallback list.

The 0.0.21 pause (``route_fault.py``) buys a real thing: a run whose route died
stops instead of scheduling four more nodes onto a credential the provider has
already refused. What it costs is a trip to a human for every dead route — and
in a headless overnight run that is the whole night, spent on a decision the
operator would have made in advance and in one line ("if anthropic/opus dies,
haiku is fine").

This module is that line. The operator writes ``~/.lohra/workflow_routes.json``
BEFORE the run; when a route dies in exactly the way that pauses today, the
harness may move that node to the next route the operator listed — and to
nothing else, ever.

Shape::

    {
      "routes": {
        "anthropic/claude-opus-4-8": {
          "fallback": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"]
        },
        "openai/gpt-5.5": ["openai/gpt-4o-mini"]
      },
      "max_fallbacks_per_run": 2
    }

A key is ``<provider>/<model>`` split on the FIRST slash, because model ids
already contain slashes (``openrouter/openai/gpt-4o`` is the openrouter provider
serving ``openai/gpt-4o``). The bare-list form is shorthand for ``{"fallback":
[...]}``, the same courtesy ``load_tiers`` gives a bare model string.

**Five doctrines, each load-bearing:**

1. **File only, never the spec.** An authored (or injected) spec must not be
   able to point itself at a route the operator did not sanction — the rule
   ``workflow_tiers.json`` and ``workflow_policy.json`` already follow. A
   top-level ``routes:`` key in a spec is read by nothing (the validator reads
   ``nodes``/``schemas``), and a node-level ``fallback:`` is refused by the
   closed field set before anything spawns.

2. **Never more expensive, decided on the PRICE TABLE.** ``cheaper_or_equal``
   compares list prices per token (input AND output) between the dead route and
   the candidate. A price unknown on EITHER side — a dynamic provider with no
   operator override, a subscription plan, a model nobody priced — returns
   ``None``, and ``None`` means the run pauses as it does today. That is what
   makes the re-route "orçada" in the sense SUP-01 §6.3 asks for without waiting
   for a USD budget (#46): the harness never guesses a bill, so it can never
   authorize one it could not read.

3. **Fail-soft on the file, fail-closed on the entry.** An absent, unreadable or
   malformed file means "no envelope", never an exception — the run pauses
   exactly as it does with no file at all. But an entry is read against an
   ALLOW-list of one key (``fallback``): anything else at all — the
   ``max_usd_per_cell`` and ``on`` sketched in the issue, a ``budget_usd`` a
   future version might add, a typo — DROPS the whole entry. Deny-listing the
   two names we happen to know would be fail-OPEN on the very axis this file
   exists to close: every other limit an operator wrote would be ignored while
   their fallback list was honoured, which is the harness deciding it understood
   a limit it never read. Refusing to act on a half-understood envelope is the
   only reading that cannot exceed the operator's intent.

4. **One judgment per death, in the operator's order.** ``next_route`` returns
   the FIRST fallback the node has not already tried; if that one is unpriced,
   costlier, ungated or over the counter, the run pauses. The harness does not
   walk further down the list looking for one that passes — choosing among the
   operator's options on cost grounds is precisely the billing authority the
   doctrine withholds from it. The list is a chain across DEATHS (X dies → A; A
   dies → B), not a search space within one.

5. **A durable brake, always.** The count lives in
   ``workflow_route_fallbacks`` (``state/db.py``), not in this process: one
   fallback per ``(run, dead route)`` and ``max_fallbacks_per_run`` for the whole
   run, so a resume cannot re-buy the allowance a previous stretch spent.

None of this is the #36 decision (whether the supervision keys enforce by
default). This envelope is CONFIGURATION the operator wrote before the run —
§6.3 governs decisions the AGENT makes mid-flight, and the agent has no say here
at all: it cannot write the file, cannot read it into a spec, and cannot widen
it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lohra.pricing.estimate import ModelPrice, list_price

# Where the operator's envelope lives, under the (already resolved) profile home.
ROUTES_FILE = "workflow_routes.json"

# How many re-routes ONE run may buy in total when the file names no ceiling.
# Deliberately small: the envelope exists to survive a dead route overnight, not
# to walk a run through a provider outage one node at a time.
DEFAULT_MAX_FALLBACKS_PER_RUN = 2

_FALLBACK_KEY = "fallback"

# The ONLY key an entry may carry. An ALLOW-list, not a deny-list of the two
# knobs the issue sketched (``max_usd_per_cell``, ``on``): a deny-list is
# fail-OPEN on exactly the axis this slice exists to close — ``max_usd``,
# ``budget_usd``, ``only_on_weekends``, anything at all a future version or a
# hopeful operator writes would be silently ignored and the fallback list
# honoured anyway, which is the harness deciding it understood a limit it did
# not read. Unknown key ⇒ the entry is dropped ⇒ the run pauses, which is the
# one outcome that can never exceed what the operator meant.

# What the envelope decided, as one word. Carried in the ``route_fault`` payload
# so a cross-process ``status``/``watch`` can say the same thing the pause said,
# and mapped to a remedy tail by ``route_fault.route_fault_hint``.
NO_ENVELOPE = "no_envelope"  # no file, or nothing listed for the dead route
UNPRICED = "unpriced"  # a price unknown on either side -> fail-closed
COSTLIER = "costlier"  # the candidate bills more per token than the dead route
GATED = "gated"  # the candidate's provider refused to build (credential/opt-in)
EXHAUSTED = "exhausted"  # the durable allowance for this run/route is spent
# Three refusals that are NOT about the envelope's contents at all, kept apart
# because their remedies have nothing in common: a node type v1 does not move, a
# route one level down that no resume could carry, and a run that was already
# stopping. One word for all three used to buy one tail — which then had to
# explain three different things and got two of them wrong.
INELIGIBLE = "ineligible"  # this node TYPE is not one v1 re-routes
NESTED = "nested"  # the dead route lives inside a `workflow` template
RUN_STOPPED = "run_stopped"  # a pause or a cancel had already stopped this run
REROUTED = "rerouted"  # ...and the one outcome that does not pause

OUTCOMES = (
    NO_ENVELOPE, UNPRICED, COSTLIER, GATED, EXHAUSTED,
    INELIGIBLE, NESTED, RUN_STOPPED, REROUTED,
)


@dataclass(frozen=True)
class RouteEnvelope:
    """The operator's dead-route -> ordered-alternatives map, copied on build."""

    routes: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_fallbacks_per_run: int = DEFAULT_MAX_FALLBACKS_PER_RUN

    def __post_init__(self) -> None:
        kept = {
            key: tuple(value)
            for key, value in self.routes.items()
            if isinstance(key, str) and key and isinstance(value, (list, tuple)) and value
        }
        object.__setattr__(self, "routes", kept)
        object.__setattr__(
            self,
            "max_fallbacks_per_run",
            max(0, int(self.max_fallbacks_per_run)),
        )

    @property
    def empty(self) -> bool:
        """True when nothing is authorized — the "no file" state, by value."""
        return not self.routes or self.max_fallbacks_per_run <= 0

    def fallbacks(self, dead_route: Any) -> tuple[str, ...]:
        """What the operator authorized for this dead route (``()`` for none)."""
        return self.routes.get(dead_route, ()) if isinstance(dead_route, str) else ()


# The value every caller gets when there is no envelope at all. A VALUE, not
# ``None``, so the engine has one shape to reason about.
EMPTY_ENVELOPE = RouteEnvelope()


def route_key(provider: Any, model: Any) -> str | None:
    """``provider/model``, or None when either half is missing.

    A leaf that ran on the run's own default may report no provider or no model
    (``route_label`` says so out loud). The envelope is keyed on a route the
    operator NAMED, so half a route matches nothing — and answering "anthropic"
    for a node whose model nobody knows would let one entry authorize a fallback
    for every model on that provider. Fail-closed: no key, no envelope.
    """
    if not (isinstance(provider, str) and provider.strip()):
        return None
    if not (isinstance(model, str) and model.strip()):
        return None
    return f"{provider.strip()}/{model.strip()}"


def split_route_key(key: Any) -> tuple[str, str] | None:
    """``provider/model`` -> ``(provider, model)``, split on the FIRST slash.

    Model ids contain slashes (``openrouter`` serves ``openai/gpt-4o``), so the
    LAST slash is the wrong boundary and a naive ``split("/")`` is worse. None
    for anything that is not a two-sided key."""
    if not isinstance(key, str) or "/" not in key:
        return None
    provider, _, model = key.partition("/")
    if not provider.strip() or not model.strip():
        return None
    return (provider.strip(), model.strip())


def _fallback_list(entry: Any) -> tuple[str, ...] | None:
    """One authored entry -> its ordered fallbacks, or None to DROP it.

    Dropped, never partially honoured, and dropped by an ALLOW-list: an entry
    naming ``max_usd_per_cell``, ``on``, or any other key at all is asking for
    something this version cannot enforce, and honouring the list without it
    would authorize MORE than the operator wrote (doctrine 3). The two knobs the
    issue sketched are only the cases we can name — the ones we cannot are
    exactly why the test is "did I understand every word of this entry", not
    "did I recognise a word I know to refuse"."""
    if isinstance(entry, (list, tuple)):
        routes = entry
    elif isinstance(entry, dict):
        if set(entry) - {_FALLBACK_KEY}:
            return None
        routes = entry.get(_FALLBACK_KEY)
        if not isinstance(routes, (list, tuple)):
            return None
    else:
        return None
    kept = tuple(
        item.strip()
        for item in routes
        if isinstance(item, str) and split_route_key(item) is not None
    )
    return kept or None


def load_routes(path: Path) -> RouteEnvelope:
    """Load ``~/.lohra/workflow_routes.json``; an EMPTY envelope when the file is
    absent, unreadable or malformed.

    Fail-soft on the whole file for the reason ``load_tiers`` is: a typo in
    operator config must not turn every run into an exception. The consequence
    of an empty envelope is exactly today's behaviour — a dead route pauses the
    run and a human answers it — so the fail-soft direction is also the
    fail-CLOSED one, which is why this is safe to swallow.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return EMPTY_ENVELOPE
    if not isinstance(data, dict):
        return EMPTY_ENVELOPE
    raw = data.get("routes")
    if not isinstance(raw, dict):
        return EMPTY_ENVELOPE
    resolved: dict[str, tuple[str, ...]] = {}
    for key, entry in raw.items():
        if split_route_key(key) is None:
            continue  # a key that names no route can never match a dead one
        routes = _fallback_list(entry)
        if routes is not None:
            resolved[key.strip()] = routes
    ceiling = data.get("max_fallbacks_per_run")
    if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 0:
        # ``True`` is an int in Python and is not a count; a negative ceiling is
        # a typo, not "unlimited". Either way: the default, never a guess.
        ceiling = DEFAULT_MAX_FALLBACKS_PER_RUN
    return RouteEnvelope(resolved, ceiling)


def next_route(
    envelope: RouteEnvelope, dead_route: Any, tried: Iterable[str] = ()
) -> str | None:
    """The ONE candidate the operator's order offers for this dead route.

    The first entry the node has not already been on — never a search for one
    that would pass the price or the gate (doctrine 4). ``tried`` is how a CHAIN
    stays a chain: a node moved X -> A whose A then dies looks up A's own entry
    and must not be sent back to X."""
    seen = {item for item in tried if isinstance(item, str)}
    seen.add(dead_route if isinstance(dead_route, str) else "")
    for candidate in envelope.fallbacks(dead_route):
        if candidate not in seen:
            return candidate
    return None


def cheaper_or_equal(
    dead_route: Any,
    candidate: Any,
    *,
    table: dict[tuple[str, str], ModelPrice] | None = None,
    overrides: dict[tuple[str, str], ModelPrice] | None = None,
) -> bool | None:
    """Does ``candidate`` bill no more per token than ``dead_route``?

    ``None`` means UNKNOWN — a price missing on either side — and every caller
    must read it as "do not re-route", never as "probably fine". That is the
    whole of doctrine 2: an unreadable bill is the one thing a harness with no
    billing authority may not act on.

    BOTH meters must hold. A candidate that halves the input rate and triples
    the output rate is not cheaper; it is a different bet, and taking it would
    be the harness choosing on the operator's money. Equality passes — the
    common case is a second key on the same model.

    ``table``/``overrides`` are injected by tests so the verdict never depends on
    the shipped price snapshot or on the machine's ``pricing.json``.
    """
    dead_pair = split_route_key(dead_route)
    candidate_pair = split_route_key(candidate)
    if dead_pair is None or candidate_pair is None:
        return None
    dead_price = list_price(*dead_pair, table=table, overrides=overrides)
    candidate_price = list_price(*candidate_pair, table=table, overrides=overrides)
    if dead_price is None or candidate_price is None:
        return None
    return (
        candidate_price.input_usd <= dead_price.input_usd
        and candidate_price.output_usd <= dead_price.output_usd
    )


def route_override(candidate: str) -> dict[str, str] | None:
    """A candidate key as the two ROUTING FIELDS a node carries.

    Both halves, always: a re-route that moved only the model would leave the
    node on the dead route's provider (or on a tier's), which is the exact
    footgun ``ROUTE_FAULT_HINT`` warns a human about. ``effort`` is untouched —
    the operator authorized a route, not a knob."""
    pair = split_route_key(candidate)
    if pair is None:
        return None
    return {"provider": pair[0], "model": pair[1]}


def rerouted_fault(node_id: str, dead_route: str, candidate: str) -> str:
    """The run's record that the OPERATOR's envelope moved this node.

    Says where the authority came from, in the same breath as the move: the
    harness picked this route out of a list the operator wrote before the run,
    and it could not have picked anything else. ``reroute_fault`` (the command
    channel) makes the mirror-image claim for the mirror-image reason."""
    return (
        f"{node_id}: re-routed by operator envelope: {dead_route} -> {candidate} "
        "(never chosen by the harness beyond the operator's list)"
    )
