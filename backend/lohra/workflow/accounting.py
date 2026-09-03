"""What a run SPENT and how it ENDED — the engine's result types, on their own.

Pulled out of ``engine`` when Fatia C widened the accounting from two axes to
five: the interpreter is control flow, and these are pure data plus one verdict
function, read by ``rollup``, ``library``, ``service`` and ``costs`` without any
of them needing the interpreter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lohra.agent.types import Usage, combine_usage


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
    status: str = "complete"  # complete | degraded | failed | cancelled | paused
    pause_reason: str | None = None  # quota | token_budget | user_requested | checkpoint
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

    @property
    def null_rate(self) -> float:
        return self.null_count / self.nodes_total if self.nodes_total else 0.0


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
    message a series that ended with a winner left behind is discounted."""
    if not result.recovered_faults:
        return bool(result.faults)
    recovered = set(result.recovered_faults)
    return any(fault not in recovered for fault in result.faults)

