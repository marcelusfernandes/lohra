"""Bounded fresh re-spawns of a DEAD `parallel` branch (H7, issue #77).

`run_parallel`'s barrier collects every branch exactly ONCE: a death holds as
a hole in the panel, and the #72 fail-closed guard then refuses to let a
reduce node read it (correctly — the author never gets a wrong answer, only a
refused one). What #72 left open is the remedy: the only way to recover a
transient blip was a full resume that re-spawns the ENTIRE fan-out, because
`retries` did not exist on `parallel` at all.

`parallel.retries` (opt-in, default 0, nodes.MAX_NODE_RETRIES ceiling) buys a
bounded number of FRESH re-spawns per dead branch — same doctrine as
`agent.retries`'s TERMINAL class (`leaf_retry.py`): the same prompt, the same
route, no correction text. A dead branch's prompt is not what failed, so
"correcting" it would only change the cell the author authored. There is no
EMPTY-answer class here (unlike `agent.retries`): branches are collected
schema-less, so an empty string is that branch's own data, never an upstream
hole — only `None` (the leaf genuinely died) is death.

The retryable/not-retryable line is the SAME predicate the agent's terminal
class uses (`leaf_retry.is_retryable_failure`, read here via
`engine.leaf_retryable`): quota/auth/timeout/token-budget deaths already own
their own remedy and a re-spawn would only ask the same doomed thing again.

A re-spawn's own SPAWN can itself be refused by the unified budget
(`LifetimeExhausted`/`TokenBudgetExhausted`, `budget.py`) — the barrier's
first round is width-gated up front (`gate_fanout`), but a re-spawn is one
MORE leaf beyond that gated width, going through the same per-spawn funnel
every `spawn_leaf` call does. Both are caught HERE, at the re-spawn site,
never left to escape: `run_parallel`'s caller loop still has OTHER branches
already spawned and running that must still be collected and charged even
when THIS branch's re-spawn is refused — an uncaught raise here would abort
that loop and orphan them (HIGH-1, #77 adversarial review), exactly the
`pipeline`'s own `on_done` worker already guards against
(`strategies.py`'s `_dispatch`, `LifetimeExhausted`/`TokenBudgetExhausted`).
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.budget import LifetimeExhausted, TokenBudgetExhausted
from lohra.workflow.leaf_retry import stopped_fault


def respawn_dead_branch(
    engine: Any,
    node: Any,
    chash: str,
    prompt: Any,
    index: int,
    first_sub_id: str,
    *,
    attempts_total: int,
) -> tuple[Any, list[str], list[str]]:
    """One DEAD branch, re-spawned fresh while it stays retryable.

    Called ONLY once the caller already knows the branch's first attempt
    (``first_sub_id``, already spawned and collected — every branch in the
    fan-out dispatches together, for the same concurrency the barrier always
    had) died: ``attempts_total > 1`` (retries were declared) is the caller's
    own gate, so there is nothing left to re-check about the first attempt's
    outcome here.

    Returns ``(output, dead_sub_ids, spawned_sub_ids)``:
    - ``output`` is the winning answer, or ``None`` if the branch never
      produced one (today's #72-guarded hole, unchanged);
    - ``dead_sub_ids`` are every attempt that died in this branch's series —
      fed to ``engine.mark_recovered`` on a winner (retiring their numbered
      faults from the verdict, Q2) or to ``engine.mark_route_fault_caused`` on
      a pause that latched mid-series and now owns them instead;
    - ``spawned_sub_ids`` are the RE-spawns that actually happened (never the
      first attempt, and never a re-spawn the budget refused before a leaf
      ever existed) — fed into the cell's total cost alongside every other
      branch's leaves.
    """
    dead: list[str] = [first_sub_id]
    spawned: list[str] = []
    sub_id = first_sub_id
    for attempt in range(1, attempts_total):
        if not engine.leaf_retryable(sub_id):
            # A death no re-spawn can fix. If this is what just latched a
            # route_fault pause on THIS node (an auth refusal, most likely —
            # `note_leaf_failure` already raised it), the numbered faults this
            # series wrote so far are the pause's own evidence, not a second
            # verdict (Q2 x #43's second door); a no-op everywhere else.
            engine.mark_route_fault_caused(node.id, dead)
            return None, dead, spawned
        if engine.stopped:
            # A pause or a cancel landed while this branch was in flight —
            # spawning another leaf would contradict it. The numbered fault
            # already recorded says why the rest of the series never ran.
            engine.record_fault(stopped_fault(node.id, attempt, attempts_total))
            return None, dead, spawned
        try:
            sub_id = engine.spawn_leaf(
                prompt,
                causal_context=engine.causal_context(
                    cell_id=chash, role="parallel.branch", branch_path=(index,), attempt=attempt
                ),
            )
        except LifetimeExhausted as exc:
            # Fail CLOSED exactly like the ``FanoutRejected`` the engine's node
            # handler records for every other node type that lets one escape:
            # a fault naming the cell and a cap trip — but recorded HERE,
            # before it can reach the node thread, the same reason the
            # pipeline's own `on_done` worker catches its own (`strategies.py`
            # `_dispatch`). ``str(exc)`` alone, never re-prefixed with
            # ``node.id``: the message already opens with the node that
            # raised it (``engine._reserve_lifetime``), which for a re-spawn
            # IS this same node — prefixing again would print it twice.
            engine.record_fault(str(exc))
            engine.count_cap_trip()
            return None, dead, spawned
        except TokenBudgetExhausted:
            # The engine already latched the pause and recorded its own fault
            # (`_gate_tokens` calls `note_budget_exhausted` before raising).
            # Nothing about THIS branch's death caused it — settle it as dead
            # and let the caller's loop keep collecting whatever branches were
            # already spawned and are still running: they finish and are
            # charged, only the next spawn is refused (`note_budget_exhausted`'s
            # own doctrine, unchanged by this feature).
            return None, dead, spawned
        # ONE extra leaf, bought for a cell the author wrote once — counted
        # for the same reason a same-route agent re-spawn is (Q2). Counted
        # ONLY after a spawn that actually happened: a re-spawn the budget
        # refused never produced a leaf, so it must never inflate
        # `leaf_respawns` with one that does not exist.
        engine.count_leaf_respawn()
        spawned.append(sub_id)
        output = engine.collect_with_schema(
            sub_id, None, attempt=(attempt + 1, attempts_total)
        )
        if output is not None:
            return output, dead, spawned
        dead.append(sub_id)
    # Every attempt died: today's behaviour (#72's guard refuses the reduce).
    # Deliberately NO route-fault escalation here (unlike the agent's terminal
    # class) — a parallel branch exhausting its retries is not, on its own,
    # evidence the ROUTE is gone; it is one branch of many, and #72 already
    # gives the author the loudest signal a hole ever needs.
    #
    # The LAST attempt's own death is never examined inside the loop above
    # (there is no next iteration left to check it in) — a generic exhaustion
    # must fall through exactly as the comment says, but if THIS death is what
    # just latched a route_fault pause on this node (auth_failed, most often —
    # `note_leaf_failure` already raised it before returning here), the earlier
    # numbered faults are that pause's evidence, not a second verdict, same as
    # the mid-loop door above.
    if not engine.leaf_retryable(dead[-1]):
        engine.mark_route_fault_caused(node.id, dead)
    return None, dead, spawned
