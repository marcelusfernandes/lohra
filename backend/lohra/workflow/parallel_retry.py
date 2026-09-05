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
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.leaf_retry import stopped_fault


def respawn_dead_branch(
    engine: Any,
    node: Any,
    chash: str,
    prompt: Any,
    index: int,
    first_sub_id: str,
    first_output: Any,
    *,
    retries: int,
    attempts_total: int,
) -> tuple[Any, list[str], list[str]]:
    """One branch's outcome, re-spawning it fresh while it stays retryable.

    ``first_sub_id``/``first_output`` are the branch's FIRST attempt, already
    spawned and collected by the caller (every branch in the fan-out dispatches
    together, for the same concurrency the barrier always had) — this only
    takes over once that attempt is known to be a live answer or a death.

    Returns ``(output, dead_sub_ids, spawned_sub_ids)``:
    - ``output`` is the winning answer, or ``None`` if the branch never
      produced one (today's #72-guarded hole, unchanged);
    - ``dead_sub_ids`` are every attempt that died in this branch's series —
      fed to ``engine.mark_recovered`` on a winner (retiring their numbered
      faults from the verdict, Q2) or to ``engine.mark_route_fault_caused`` on
      a pause that latched mid-series and now owns them instead;
    - ``spawned_sub_ids`` are the RE-spawns only (never the first attempt) —
      fed into the cell's total cost alongside every other branch's leaves.
    """
    if first_output is not None or retries == 0:
        return first_output, [], []
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
        # ONE extra leaf, bought for a cell the author wrote once — counted
        # for the same reason a same-route agent re-spawn is (Q2).
        engine.count_leaf_respawn()
        sub_id = engine.spawn_leaf(
            prompt,
            causal_context=engine.causal_context(
                cell_id=chash, role="parallel.branch", branch_path=(index,), attempt=attempt
            ),
        )
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
