"""Run-level rollup (spec §10) — the honest health summary of a workflow run.

``null_rate`` is a first-class metric so a run whose leaves mostly died is
visibly degraded, not silently synthesized. Used by ``workflow_status`` (what the
agent polls) and by ``library`` (the self-improvement feedback, §12.2).
"""

from __future__ import annotations

from typing import Any

from lohra.workflow.engine import RunResult


def summarize(
    run_id: str,
    status: str,
    result: RunResult | None,
    error: str | None = None,
    *,
    pause: dict | None = None,
    budget: dict | None = None,
    progress: dict | None = None,
    spent_total: int | None = None,
    faults_total: list[str] | None = None,
) -> dict:
    """Build the rollup dict. ``result`` is None for a run that died before
    producing one (status carries the truth).

    ``pause`` (reason / resume_at / attempts) rides along for a run stopped by
    provider quota — without it the agent sees "paused" and cannot tell whether
    to wait, resume by hand, or give up.

    ``budget`` ({total, spent, remaining}, §7.1) is emitted BEFORE the result
    guard: it is read off the live engine, so a run still in flight — the one
    case where knowing what is left actually changes what the agent does — must
    report it even though ``result`` is still None.

    ``progress`` (per-node state, M6) rides the same live read for the same
    reason, and under its OWN key: the terminal ``nodes_total`` below means "the
    run produced a result", and a live run must not appear to have one.

    ``faults_total`` (WF-26) is every fault the run has collected ACROSS its
    stretches, reported only when it differs from this segment's ``faults``: a
    run that never paused has one list, and printing it twice would read as two
    different things. It spans processes too (WF-29): the faults of earlier
    stretches ride the run's durable line, so a resume in a fresh process still
    reports what stopped the stretch before it.

    ``spent_total`` (WF-23) is the whole run's cumulative cost, reported ALONGSIDE
    the ``tokens_in``/``tokens_out`` below — those only cover the stretch since
    the last launch, so a resumed run used to close by understating itself. It
    rides the same live read as ``budget``, and unlike ``budget`` it is emitted
    with or without a ceiling: a run with no cap still costs money."""
    rollup: dict[str, Any] = {"run_id": run_id, "status": status}
    if error:
        rollup["error"] = error
    if pause:
        rollup.update(pause)
    if budget:
        rollup["token_budget"] = budget
    if spent_total is not None:
        rollup["tokens_spent_total"] = spent_total
    if faults_total:
        rollup["faults_total"] = faults_total
    if progress:
        rollup["progress"] = progress
    if result is None:
        return rollup
    rollup.update(
        {
            "nodes_total": result.nodes_total,
            "null_count": result.null_count,
            "null_rate": round(result.null_rate, 3),
            "validation_retries": result.validation_retries,
            "cap_trips": result.cap_trips,
            "engine_faults": result.engine_faults,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "forcing_fallbacks": result.forcing_fallbacks,
            "faults": result.faults,
            "outputs": result.outputs,
        }
    )
    if rollup.get("faults_total") == result.faults:
        rollup.pop("faults_total")  # a single-stretch run: one list, reported once
    return rollup
