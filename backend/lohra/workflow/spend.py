"""The token ledger of a workflow run (spec §7.1) — what it has already cost.

Extracted from the service so the run loop keeps only its branch points. Three
questions live here, and each has exactly one honest answer:

- is this ceiling a ceiling at all (``validate_token_budget``);
- what has this run ALREADY spent, across every stretch of it and across every
  process that ran one (``seed_spend`` / ``spent_total``);
- may a resume run under the ceiling it was handed (``refuse_spent_budget``) —
  raise-only, because a cap at or under what the run already spent would pause
  it again on its first spawn and read as "the resume did nothing".
"""

from __future__ import annotations

from typing import Any

from lohra.state import SessionDB
from lohra.workflow.cache import NodeCache


def validate_token_budget(value: Any) -> str | None:
    """None if ``value`` is an acceptable token ceiling, else a didactic error.

    Didactic on purpose (the §2 house style): the model authored this call, so
    the reply has to show the corrected form rather than just refuse."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return (
            "token_budget must be a whole number of tokens greater than 0 "
            f"(got {value!r})\n    e.g. token_budget: 200000"
        )
    return None


def refuse_spent_budget(run_id: str, budget: int | None, spent: int) -> dict | None:
    """Raise-only resume: refuse a ceiling the run has already spent.

    Launching it would re-pause on the very first spawn — a silent loop that
    looks like the resume "didn't work". Refusing says the number to beat.
    """
    if budget is None or spent < budget:
        return None
    return {
        "error": (
            f"workflow run {run_id!r} has already spent {spent} tokens; a "
            f"token_budget of {budget} would pause it again on its first spawn — "
            f"resume it with a bigger one\n    e.g. token_budget: {spent * 2}"
        )
    }


def seed_spend(db: SessionDB, run_id: str) -> tuple[int, int]:
    """What this run has ALREADY spent, so a resume continues its tally.

    Two ledgers, each a LOWER BOUND that undercounts differently, so the larger
    one wins rather than whichever happens to exist:

    - the run-level row counts every leaf the engine collected, including the
      ones that died and cached nothing — but it is written when a run starts
      and when it stops, so a process that crashes mid-run leaves it holding
      that stretch's seed;
    - the per-cell costs (M5-a) survive any crash, but only cover cells that
      COMPLETED and were cached.

    Preferring the row outright would resume a crashed run as if it had spent
    nothing, since a row always exists from the moment a run starts. Whichever
    is larger is the honest floor. Still a floor: a crash can lose the leaves of
    the final stretch that never cached a cell. Accepted for v1.
    """
    row = db.run_spend_get(run_id)
    from_row = (
        (int(row["tokens_in"] or 0), int(row["tokens_out"] or 0)) if row is not None else (0, 0)
    )
    from_cells = NodeCache(db, run_id).total_cost()
    return from_row if sum(from_row) >= sum(from_cells) else from_cells


def spent_total(db: SessionDB, run_id: str, engine_spent: int) -> int:
    """What this run has cost ACROSS EVERY STRETCH of it (WF-23).

    ``RunResult.tokens_in/out`` only ever cover the segment since the last
    launch — the engine starts a fresh result on every ``run()`` — so a resumed
    run closed its rollup claiming a fraction of what it really spent (the
    dogfood: 2k on screen against a 30.7k ledger). The live budget IS seeded
    cumulatively on a resume, and the persisted ledger is the floor that
    survives a crash; the larger of the two is the honest number."""
    return max(engine_spent, sum(seed_spend(db, run_id)))
