"""What a run SPENT and how it ENDED — the engine's result types, on their own.

Pulled out of ``engine`` when Fatia C widened the accounting from two axes to
five: the interpreter is control flow, and these are pure data plus one verdict
function, read by ``rollup``, ``library``, ``service`` and ``costs`` without any
of them needing the interpreter.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from lohra.agent.types import Usage, combine_usage
from lohra.orchestration.core import TERMINAL_STATUSES

# What a run says about a leaf whose bill it could not read when the rollup
# closed (issue #42). Neither is an estimate and, above all, neither is a zero:
# the bill exists, nobody can read it, and the counter beside these faults says
# so. TWO causes, never merged — a leaf still inside a provider call and a leaf
# the registry no longer knows are different facts with different remedies, and
# writing "still running" over a leaf that finished long ago (evicted under
# ``DEFAULT_MAX_CHILDREN``) is a fault with a FALSE cause, which is exactly what
# a fail-closed report must not manufacture.
UNSETTLED_AT_SEAL = "leaf still running at seal; provider usage unknown"
UNKNOWN_AT_SEAL = (
    "leaf unknown at seal (evicted from the registry); provider usage unknown"
)


@dataclass(frozen=True)
class NodeCost:
    """What ONE node of the DAG spent, and WHICH agent spent it (Fatia C).

    ``provider``/``model`` are what makes a node priceable — a leaf may run on a
    different model than the run's default (tiers, cross-provider delegation).
    When a node's leaves disagree, both go None: the tokens stay reported and the
    money is withheld, because a price for the wrong model is worse than none."""

    usage: Usage = field(default_factory=Usage)
    provider: str | None = None
    model: str | None = None

    def merge(self, usage: Usage, provider: str | None, model: str | None) -> "NodeCost":
        """This node plus one more leaf, as a NEW NodeCost (never mutated)."""
        agreed = (self.provider, self.model) == (provider, model)
        first = self.usage == Usage()
        return NodeCost(
            usage=combine_usage(self.usage, usage) or self.usage,
            provider=provider if (first or agreed) else None,
            model=model if (first or agreed) else None,
        )


@dataclass
class RunResult:
    outputs: dict[str, Any] = field(default_factory=dict)
    faults: list[str] = field(default_factory=list)
    null_count: int = 0
    validation_retries: int = 0
    cap_trips: int = 0  # fan-out rejections (budget)
    engine_faults: int = 0  # node-level engine faults (distinct from leaf null)
    nodes_total: int = 0
    tokens_in: int = 0  # aggregate leaf token cost (§10) — UNCACHED prompt
    tokens_out: int = 0
    # The other two prompt meters + reasoning (Fatia C). Disjoint from
    # ``tokens_in`` by the transports' normalization, so they can be summed.
    # Report only: the token BUDGET still charges in+out.
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    # Per-node attribution of all of the above — "custo por no/agente".
    node_costs: dict[str, NodeCost] = field(default_factory=dict)
    forcing_fallbacks: int = 0  # forced tool_choice ignored by provider (§5.3)
    # How many leaves had their provider stream CLOSED mid-flight by a cancel
    # (issue #42, épico E3). Counted APART from the tokens on purpose: usage
    # only arrives at the END of a stream, so an aborted leaf's contribution to
    # ``tokens_in``/``tokens_out`` above is a FLOOR, not the bill — the provider
    # may have charged everything it generated before the socket dropped. This
    # counter is the only thing that says so: no estimate is ever added to the
    # meters, and nothing approximate is charged to the exact per-cell ledger.
    usage_uncertain_leaves: int = 0
    # How much of this stretch came out of the node cache instead of a provider
    # (#61). Counted per CELL, not per node: a fully cached pipeline of 3 items
    # through 2 stages replayed SIX leaves, and reporting "1 node" would hide the
    # only number anyone resumes for. ``tokens_saved`` is what those cells cost
    # the FIRST time, all five meters — a floor, never an estimate: a cell whose
    # price was never recorded (cached before the sidecar existed, or a human's
    # checkpoint answer) adds 0, exactly like every other unpriced cell in the
    # harness. Folded up from a nested template by ``fold_nested``.
    cells_replayed: int = 0
    tokens_saved: int = 0
    status: str = "complete"  # complete | degraded | failed | cancelled | paused
    # quota | token_budget | user_requested | checkpoint | route_fault
    pause_reason: str | None = None
    # The ONE fault the pause itself wrote (WF-26). A pause is not a lesson
    # about the SPEC — it is what stopped this stretch — so whoever judges the
    # run across its stretches needs to tell that fault from the real ones.
    pause_fault: str | None = None
    # ...and the faults the pause CAUSED (WF/sol #5). A pause stops the leaves
    # still in flight on purpose — they would all 429 too — and each one lands
    # here as "leaf cancelled/interrupted". They are administrative, not a lesson
    # about the SPEC: the remedy is the wait the run is already doing. Still
    # reported in ``faults`` (fail-closed reporting is untouched); they are only
    # discounted from the "did an earlier stretch really fail" verdict.
    pause_faults: list[str] = field(default_factory=list)
    # ...and the faults a same-route re-spawn series RECOVERED FROM (Q2, #43).
    # A leaf that died on attempt 1 and answered on attempt 2 left a real fault
    # behind — the provider really did refuse — but the node produced its output
    # and the DAG carried on. Sealing the run ``degraded`` on that fault would
    # make ``retries`` self-defeating: the very knob bought to survive a provider
    # blink would guarantee ``library`` never certifies the spec that survived
    # it. Reported in ``faults`` like every other fault (fail-closed reporting is
    # untouched); discounted only from the VERDICT, and only where the series
    # really ended with a winner. A series that never recovers records nothing
    # here, so its faults — and its ``exhausted``/``stopped`` verdicts — count.
    recovered_faults: list[str] = field(default_factory=list)
    # ...and the faults that are an ADVICE about a leaf, not a verdict about the
    # run (#45). Today exactly one thing lands here: a leaf whose artifact
    # manifest claimed a ``sha256``/``bytes`` the harness measured differently.
    # The node CONCLUDED — the file was written, and the cell stores the
    # measurement, not the claim — so the only thing wrong is a number the leaf
    # counted badly, which is not a defect of the spec's SHAPE. Sealing the run
    # ``degraded`` on it would make ``library`` refuse to certify a spec that
    # worked, on the strength of a hint the harness had already corrected.
    # Reported in ``faults`` like every other fault (fail-closed reporting is
    # untouched); discounted only from the VERDICT. A node that fails to
    # conclude still degrades — by its null, never by the advice beside it.
    advisory_faults: list[str] = field(default_factory=list)
    # How many EXTRA leaves the run paid for beyond the one each cell authored
    # (Q2, #43). Both re-spawn classes count: an empty answer and a provider
    # death each cost a whole leaf, and a template that says "works, cost 3
    # re-spawns" must not quietly omit half of them. 0 on a run that never
    # re-spawned, exactly like ``validation_retries``.
    leaf_respawns: int = 0
    # WHICH ``required: true`` node stopped this run (issue #15). Namespaced
    # ``sub[ref]:node`` when it failed inside a nested workflow, exactly like the
    # faults and node costs ``fold_nested`` carries up. Set => the verdict is
    # ``failed``: a run that lost a node its author declared indispensable has
    # no partial result worth calling ``degraded``.
    required_failure: str | None = None
    retry_after: float | None = None  # provider hint for when to resume, if any
    checkpoint: dict | None = None  # what a checkpoint pause is waiting for (WF-10)
    # ...and what a ``route_fault`` pause stopped ON (#43): {node_id, provider,
    # model, error_kind, cause}. A separate field rather than a second meaning
    # for ``checkpoint``: one waits for a human ANSWER, the other names a dead
    # ROUTE, and a reader that confuses them acts on the wrong remedy.
    route_fault: dict | None = None

    @property
    def null_rate(self) -> float:
        return self.null_count / self.nodes_total if self.nodes_total else 0.0


def leaf_settled(collected: dict) -> bool:
    """Has this leaf's LAST TURN landed — i.e. is what ``collect`` just reported
    a total, or a number still moving?

    Precisely what the core guarantees, and no more: a terminal status means the
    turn that was running has finished and its usage is in. It does NOT mean the
    sub-session can never run again — a steered sub-session starts a new turn
    without leaving this set (the core never resets the status to ``running``),
    and its meters ACCUMULATE, so a read taken during that second turn is a
    total of the first one plus however much of the second has landed. No
    workflow path accounts a leaf mid-steer today (the schema correction
    collects blocking first), which is what keeps that from being a live bug.

    Anything else — ``running``, or a dict with no status at all (an unknown
    sub-session: evicted from the registry, or never there) — is work whose bill
    has not been written yet. Accounting it would freeze a zero into the rollup
    and, worse, spend the sub_id's one trip through the dedup (issue #42)."""
    return collected.get("status") in TERMINAL_STATUSES


def leaf_unknown(collected: dict) -> bool:
    """Does the core no longer know this sub-session at all? (Distinct from "not
    settled yet": ``collect`` answers an unknown id with an ``error`` key and no
    ``status``.) It is what tells the two seal faults apart."""
    return "status" not in collected


def leaf_usage(collected: dict) -> Usage:
    """A collected sub-session's four disjoint meters as a ``Usage``.

    Reads the dict the core returns (not the Usage object) because that is the
    contract every consumer of ``collect`` shares — and a fake core in a test
    may report only the two axes it knows about."""
    return Usage(
        input_tokens=collected.get("tokens_in") or 0,
        output_tokens=collected.get("tokens_out") or 0,
        cache_read_tokens=collected.get("cache_read_tokens") or 0,
        cache_write_tokens=collected.get("cache_write_tokens") or 0,
        reasoning_tokens=collected.get("reasoning_tokens") or 0,
    )


def derive_status(result: RunResult) -> str:
    """The run's honest verdict (fail-closed, §7.5).

    A null node is never a clean run — reporting "complete" over nulls is what
    let ``library`` certify a broken spec as a reusable template. Everything
    nulled means the run produced nothing at all: "failed".

    A ``required`` node that resolved to null outranks the arithmetic (§7.4):
    the author declared that node indispensable, so what the other nodes managed
    to produce is not a partial success — it is a failed run with leftovers.
    """
    if result.required_failure is not None:
        return "failed"
    if result.nodes_total and result.null_count >= result.nodes_total:
        return "failed"
    if result.null_count or unrecovered(result):
        return "degraded"
    return "complete"


def unrecovered(result: RunResult) -> bool:
    """Did this run fault on anything a re-spawn did NOT go on to fix?

    The discount is by IDENTITY against the list the retry loop built, never by
    pattern-matching the fault's prose (``providers/errors.py`` forbids regex
    over provider text, and the same rule protects a verdict): only the exact
    message a series that ended with a winner left behind is discounted.

    The advisory list (#45) is discounted on the same terms and by the same
    rule: it is an advice about a node that CONCLUDED, so it says nothing about
    the shape either.

    A MULTISET, not a set: two leaves of the same node can die with byte-identical
    text, and one recovery must retire exactly one of them. Discounting by
    membership would let a node that recovered launder a second, real death that
    happened to read the same."""
    discounted = Counter(result.recovered_faults) + Counter(result.advisory_faults)
    return bool(Counter(result.faults) - discounted)

