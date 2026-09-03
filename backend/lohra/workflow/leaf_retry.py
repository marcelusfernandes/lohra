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
  This class is **opt-in**: it is bought only where the author actually WROTE
  ``retries``. The default of 1 was written for the empty answer and predates
  E1 by a whole campaign, so spending it on provider deaths as well would
  double the bill of every spec already in the library without its author ever
  asking. The predicate is the one ``max_iterations`` already uses for its cell
  identity — the field is in ``node.fields``, or the knob was never asked for.

Every other death is deliberately OUT, and each is out for its own reason:

- ``quota_exhausted`` — the run PAUSES and auto-resumes. Re-spawning would buy
  the same 429 N times before the pause it was always going to reach;
- ``timeout`` — both of them. The HTTP read window and the leaf's own deadline
  each already name their knob in the fault, and a leaf cancelled at its
  deadline is only cooperatively dead: a re-spawn can race the leaf it stranded;
- ``auth_failed`` — the client is cached per route for the life of the pool, so
  the credential just refused is the one every later attempt would present. The
  refusal is deterministic within the run, and the remedy is the operator's;
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
from lohra.providers.errors import AUTH_FAILED, QUOTA_EXHAUSTED, TIMEOUT
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
NO_RESPAWN_KINDS = frozenset(
    {QUOTA_EXHAUSTED, AUTH_FAILED, TIMEOUT, TOKEN_BUDGET_EXHAUSTED}
)

def terminal_respawns_allowed(fields: dict) -> bool:
    """Did the author ASK for same-route re-spawns on a dead leaf?

    ``retries: 1`` and an unset field resolve to the same number; what differs is
    that one of them is a request. Only the request buys the terminal class."""
    return "retries" in fields


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


def empty_fault(node_id: str, attempts: int, *, saw_terminal: bool) -> str:
    """The verdict after the last attempt answered nothing.

    A series can mix the two classes. Sealing a mixed one on the empty verdict
    alone hides the provider death that cost an attempt — and of the two, the
    death is the half the author can actually act on (a route, a key, a budget),
    while "it said nothing" only ever points back at the prompt."""
    message = f"{node_id}: empty output after retry ({attempts} attempt(s))"
    if saw_terminal:
        message += "; a provider death also occurred in this series"
    return message


def stopped_fault(node_id: str, attempt: int, attempts: int) -> str:
    """The line that keeps a numbered attempt from lying.

    A fault reading "attempt 1/3" promises two more; when a pause or a cancel
    lands while that leaf was in flight, they never come. Say so once, instead of
    leaving the author to wonder which two attempts they were never shown."""
    return (
        f"{node_id}: run stopped after attempt {attempt}/{attempts}; "
        "no further same-route re-spawn"
    )


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
    terminal_ok = terminal_respawns_allowed(node.fields)
    last_failure: str | None = None
    saw_terminal = False  # ...anywhere in the series, not just on the last attempt
    # The leaves that died on the way to a winner, if one comes — and, if none
    # does, the series whose last entry names the route that has to be reported
    # (#43). Bound where each one dies rather than read off the loop variable
    # afterwards: a leaked loop variable is exactly the shape the E1 review
    # already caught once.
    dead: list[str] = []
    for attempt in range(attempts):
        if attempt:
            # ONE extra leaf, bought for a cell the author wrote once. Counted
            # before the spawn and for BOTH classes: an empty answer costs a
            # whole leaf exactly like a provider death, and a template that
            # advertises "works, cost N re-spawns" must count what it cost.
            engine.count_leaf_respawn()
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
        # Number the attempt ONLY where a series is really on offer: an author
        # who never wrote ``retries`` gets one shot at a dead leaf, and stamping
        # "1/2" on it would promise a second that is not coming.
        output = engine.collect_validated(
            node, sub_id, attempt=(attempt + 1, attempts) if terminal_ok else None
        )
        if output is None:
            if not terminal_ok or not engine.leaf_retryable(sub_id):
                # A death no re-spawn can fix — or one the author never asked to
                # pay for. Either way it already carries its own cause (the only
                # outcome this file had before E1): null it here.
                #
                # If a route_fault pause is what just ended this cell — an auth
                # refusal landing on a later attempt of a series that started
                # with an ordinary death — the numbered faults the earlier
                # attempts wrote belong to that pause and not to the spec. The
                # engine checks the reason itself; every other stop leaves them
                # counting, unchanged.
                engine.mark_route_fault_caused(node.id, dead)
                return None, engine.leaf_cost(sub_id)
            last_failure = _TERMINAL
            saw_terminal = True
            dead.append(sub_id)
            if engine.stopped:
                # A pause or a cancel landed while this leaf was in flight. It
                # owns the story, and starting one more leaf would contradict it
                # — but the fault just recorded is numbered, so say why the rest
                # of the series never ran.
                if attempts > 1 and attempt + 1 < attempts:
                    engine.record_fault(stopped_fault(node.id, attempt + 1, attempts))
                return None, engine.leaf_cost(sub_id)
        elif is_empty_output(output):
            last_failure = _EMPTY
        else:
            # A WINNER. Whatever the dead attempts faulted on, this cell is not
            # the reason the run is unhealthy: the node produced its output and
            # the DAG carried on. Retire those faults from the VERDICT (they
            # stay in ``faults``, verbatim) so ``retries`` stops being the knob
            # that guarantees the run it rescued is never certified (Q2, #43).
            if dead:
                engine.mark_recovered(dead)
            return output, engine.leaf_cost(sub_id)
    if last_failure == _TERMINAL:
        if attempts > 1:
            # With ``retries: 0`` there were no re-spawns to exhaust: the leaf's
            # own cause is the whole story, and a second fault would only pad it.
            verdict = exhausted_fault(node.id, attempts)
            # A DECLARED series that spent every attempt on ONE route and died on
            # all of them is evidence about the route, not about this call's luck
            # (#43, opção C): the run PAUSES on it rather than scheduling the next
            # node onto a route already known to be dead. The pause records the
            # verdict itself, once and discounted like every pause's own fault; if
            # it declines — the death is not one of the two narrow shapes, or
            # another pause already owns this run — the verdict is still the
            # author's to read, so it lands as an ordinary fault exactly as before.
            if engine.note_route_fault(
                node.id,
                # The LAST attempt's leaf: the route the series really died on.
                engine.leaf_result(dead[-1]),
                verdict,
                node=node,
                attempts_declared=terminal_ok,
                exhausted=True,
            ):
                # The pause is now this cell's verdict, so the numbered faults
                # that built it are the pause's evidence, not a lesson about the
                # spec (Q2's discount, reached by the other door: there was no
                # winner, but there is no spec edit either — the remedy is a
                # route). They stay in ``faults`` and in ``leaf_respawns``.
                engine.mark_route_fault_caused(node.id, dead)
            else:
                engine.record_fault(verdict)
    else:
        engine.record_fault(empty_fault(node.id, attempts, saw_terminal=saw_terminal))
    return None, Usage()  # nothing to cache, and no price to carry
