"""Bounded re-spawns of a leaf that came back with nothing usable (WF-7 + E1).

TWO different failures share the one knob the author already writes, ``retries``:

- an **empty answer** (WF-7). The leaf said ``complete`` and said nothing: a
  recoverable failure, invisible downstream (it passes every schema-less path
  and counts as no null at all). The re-spawn carries a correction saying why it
  is being asked again — the answer is what failed.
- a **terminal provider failure on the same route** (E1, issue #43). The turn
  raised and the leaf died with it. The re-spawn carries the SAME prompt, the
  same model, the same provider and no correction — the prompt is not what
  failed, so "correcting" it would only change the cell the author authored.

Every other death is deliberately OUT, and each is out for its own reason:

- ``quota_exhausted`` — the run PAUSES and auto-resumes. Re-spawning would buy
  the same 429 N times before the pause it was always going to reach;
- ``timeout`` — both of them. The HTTP read window and the leaf's own deadline
  each already name their knob in the fault, and a leaf cancelled at its
  deadline is only cooperatively dead: a re-spawn can race the leaf it stranded;
- ``token_budget_exhausted`` — a human raises a budget, never a retry;
- an administrative stop (``cancelled`` / ``interrupted``) — somebody stopped
  this run on purpose. Starting more work is the opposite of the request.

The rule is FAIL-CLOSED: exactly one status buys a re-spawn, and an unrecognised
status never does. Re-routing is explicitly NOT here — every attempt runs on the
route the spec authored, so the cell's content hash never moves and a resume
still recognises the work this run already paid for.
"""

from __future__ import annotations

from typing import Any

from lohra.agent.types import Usage
from lohra.providers.errors import QUOTA_EXHAUSTED, TIMEOUT
from lohra.workflow.budget import TOKEN_BUDGET_EXHAUSTED
from lohra.workflow.nodes import node_retries
from lohra.workflow.prompts import with_schema_hint
from lohra.workflow.validation import is_empty_output

# What a leaf that answered nothing is told on its re-spawn (WF-7).
EMPTY_OUTPUT_CORRECTION = (
    "Your previous answer was empty. Produce the actual answer as text — "
    "if you truly cannot, say explicitly what blocked you."
)

# The ONE sub-session status a same-route re-spawn could fix: the turn raised
# and the leaf died carrying the exception (``orchestration/core.py``).
LEAF_ERROR = "error"

# Classified failures a re-spawn must never touch — each already owns a remedy,
# and none of those remedies is "ask the same provider again, right now".
NO_RESPAWN_KINDS = frozenset({QUOTA_EXHAUSTED, TIMEOUT, TOKEN_BUDGET_EXHAUSTED})

# The two classes of non-answer, as the retry loop remembers the last one.
_EMPTY = "empty"
_TERMINAL = "terminal"


def is_retryable_failure(status: str | None, error_kind: str | None) -> bool:
    """Could a SAME-ROUTE re-spawn plausibly fix how this leaf died?

    Structural only — a status and a classified kind, never a regex over the
    provider's prose (``providers/errors.py``): a tool result quoting "429" back
    at us must not steer this decision any more than it steers the pause.
    """
    return status == LEAF_ERROR and error_kind not in NO_RESPAWN_KINDS


def exhausted_fault(node_id: str, attempts: int) -> str:
    """The verdict after the last same-route attempt died.

    Distinct from the empty-output verdict on purpose: the author who reads this
    has a provider problem to take somewhere else (a route, a key, a budget),
    not a prompt to rewrite."""
    return (
        f"{node_id}: leaf failed on the same route after {attempts} attempt(s); "
        "re-spawns exhausted"
    )


def run_leaf_with_retries(
    engine: Any, node: Any, prompt: Any, schema: dict | None, configure: Any,
    *, cell_id: str,
):
    """Spawn the leaf, re-spawning it while ``retries`` and the failure class allow.

    Returns ``(output, cost)`` — the cost of the leaf that actually answered, for
    the cache row. EVERY attempt is charged to the run's budget by
    ``account_leaf`` and by the spawn funnel's lifetime reservation; only the
    winner's price is what this cell replays as. That asymmetry is what keeps an
    always-failing shape bounded: ``retries`` is a ceiling on attempts, not a
    discount on them.
    """
    attempts = node_retries(node.fields) + 1
    last_failure: str | None = None
    for attempt in range(attempts):
        # An EMPTY answer is asked again with a correction; a dead leaf is asked
        # again verbatim. Either way the cache cell identity stays the AUTHORED
        # prompt, so a resume still recognises this same cell.
        text = f"{prompt}\n\n{EMPTY_OUTPUT_CORRECTION}" if last_failure == _EMPTY else prompt
        sub_id = engine.spawn_leaf(
            with_schema_hint(text, schema), configure=configure,
            causal_context=engine.causal_context(
                cell_id=cell_id, role="agent", attempt=attempt
            ),
        )
        output = engine.collect_validated(node, sub_id, attempt=(attempt + 1, attempts))
        if output is None:
            if not engine.leaf_retryable(sub_id):
                # A death no re-spawn can fix (and the only outcome this file had
                # before E1): it already carries its own cause. Null it here.
                return None, engine.leaf_cost(sub_id)
            last_failure = _TERMINAL
            if engine.stopped:
                # A pause or a cancel landed while this leaf was in flight. It
                # owns the story, and starting one more leaf would contradict it.
                return None, engine.leaf_cost(sub_id)
        elif is_empty_output(output):
            last_failure = _EMPTY
        else:
            return output, engine.leaf_cost(sub_id)
    if last_failure == _TERMINAL:
        if attempts > 1:
            # With ``retries: 0`` there were no re-spawns to exhaust: the leaf's
            # own cause is the whole story, and a second fault would only pad it.
            engine.record_fault(exhausted_fault(node.id, attempts))
    else:
        engine.record_fault(f"{node.id}: empty output after retry ({attempts} attempt(s))")
    return None, Usage()  # nothing to cache, and no price to carry
