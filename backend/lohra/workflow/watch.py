"""``lohra workflow list|watch`` — the run's own line, read straight off disk.

The operator's view, and it costs nothing: no provider, no client, no agent, no
token. A ``RunStateStore`` over the session database is the whole dependency, so
this works while another process is running the workflow, after that process is
gone, and in a shell that has no API key configured at all.

It reads the DURABLE half only. Nothing here can see a live ``RunState`` — a
different process owns those — which is exactly the case the harness used to
answer with ``0/0/0`` before the progress snapshot started reaching SQLite
(WF-30). The line is now the only thing this needs.

Every wait is injected (``sleep``), so the poll loop is testable without one.
"""

from __future__ import annotations

from typing import Any, Callable

from lohra.providers.errors import QUOTA_EXHAUSTED
from lohra.workflow.liveview import render_run_row
from lohra.workflow.runstate_store import STALE_HINT, DurableRun, RunStateStore, pause_fields
from lohra.workflow.spend import seed_spend

# A run that reached one of these will never move again on its own.
TERMINAL = ("complete", "degraded", "failed", "cancelled")
DEFAULT_POLL = 2.0
DEFAULT_LIMIT = 20

Sleep = Callable[[float], None]
Write = Callable[[str], None]


def row_entry(store: RunStateStore, db: Any, row: DurableRun) -> dict[str, Any]:
    """One run as a listing row — the same shape ``workflow_list`` emits, built
    from the durable line plus the run's own token ledger."""
    from lohra.workflow.runstate_store import list_entry

    return list_entry(row, spent=sum(seed_spend(db, row.run_id)), stale=store.is_stale(row))


def list_rows(store: RunStateStore, db: Any, limit: int = DEFAULT_LIMIT) -> list[str]:
    """The recent runs, newest first, already rendered."""
    return [render_run_row(row_entry(store, db, row)) for row in store.recent(max(0, limit))]


def latest_run_id(store: RunStateStore) -> str | None:
    """The most recently touched run — what ``watch --last`` means."""
    rows = store.recent(1)
    return rows[0].run_id if rows else None


def resolve_run_id(db: Any, run_id: str) -> tuple[str, str | None]:
    """(resolved id, error) — the short prefix the listing prints resolves like
    a git short hash (issue #24). A full id passes through verbatim; a UNIQUE
    prefix resolves; an ambiguous one is a didactic error naming the candidates;
    no match passes through so the caller's own not-found path speaks."""
    if not run_id or db.run_state_get(run_id) is not None:
        return run_id, None  # empty is never a prefix of everything
    matches = db.run_state_ids_by_prefix(run_id)
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        listed = ", ".join(matches)
        return run_id, f"ambiguous run id prefix {run_id!r} — matches: {listed}"
    return run_id, None


def _pause_exit_line(row: DurableRun) -> str:
    """What to tell the operator when watch is about to stop on a pause that
    nothing will resume on its own (budget, a checkpoint, an explicit pause,
    or quota with its auto-resume attempts already spent).

    Reuses ``pause_fields`` — the SAME per-reason doctrine ``workflow_status``
    hands the agent — rather than a second copy of the remedy text. Budget's
    own hint already says the shape of it: the value comes from the HUMAN, via
    ``run_workflow(resume_run_id=..., token_budget=<human-authorized cap>)``."""
    fields = pause_fields(row.status, row.pause_reason, row.resume_at, row.attempts, row.checkpoint)
    hint = (fields or {}).get("hint") or (
        "nothing will resume it on its own — resume it by hand with "
        "run_workflow(resume_run_id=...) once you know what to change"
    )
    return f"workflow run {row.run_id!r} paused ({row.pause_reason}) — watch is stopping: {hint}"


def _quota_wait_note(row: DurableRun, now: float) -> str:
    """What to tell the operator once, on the tick a quota pause is first
    seen — watch keeps following it (``AutoResumeScheduler`` will retry it),
    but a bare status line does not say WHEN or WHY it is still worth waiting."""
    remaining = max(0, int(row.resume_at - now)) if row.resume_at is not None else 0
    return f"workflow run {row.run_id!r} paused by quota, auto-resume in ~{remaining}s"


def watch_run(
    store: RunStateStore,
    db: Any,
    run_id: str,
    *,
    write: Write,
    warn: Write,
    sleep: Sleep,
    poll: float = DEFAULT_POLL,
) -> int:
    """Follow one run until it stops, printing a line only when it CHANGED.

    Four exits, and the last two are the ones a naive loop gets wrong:

    - the run reached a terminal status — it is over;
    - its line vanished — nothing left to follow;
    - it PAUSED with nothing left to bring it back on its own — a token-budget
      pause, a checkpoint, an explicit pause, or a quota pause that already
      spent its auto-resume attempts. ``paused`` is deliberately not in
      ``TERMINAL`` (a quota pause below IS still moving), so this is decided
      by ``pause_reason``/``resume_at``, never by status alone — that was the
      infinite loop this closes (issue #47);
    - it is STALE: still marked ``running`` with nobody holding its lease, i.e.
      the process that owned it died. That run will never reach a terminal
      status on its own, so a loop watching only for one spins forever. Say what
      happened (the resume hint) and stop.

    A run paused by QUOTA with a live ``resume_at`` is the one pause watch
    keeps following — the scheduler behind it is expected to bring the run
    back on its own.
    """
    if store.load(run_id) is None:
        warn(f"no workflow run {run_id!r}")
        return 1
    previous: str | None = None
    quota_note_for: float | None = None  # dedupe: one note per scheduled retry
    while True:
        row = store.load(run_id)
        if row is None:
            warn(f"workflow run {run_id!r} is gone")
            return 1
        line = render_run_row(row_entry(store, db, row))
        if line != previous:
            write(line)
            previous = line
        if row.status == "paused":
            if row.pause_reason == QUOTA_EXHAUSTED and row.resume_at is not None:
                if row.resume_at != quota_note_for:
                    warn(_quota_wait_note(row, store.now()))
                    quota_note_for = row.resume_at
                sleep(poll)
                continue
            warn(_pause_exit_line(row))
            return 0
        if row.status in TERMINAL:
            return 0
        if store.is_stale(row):
            warn(STALE_HINT)
            return 0
        sleep(poll)


def run_command(
    action: str,
    *,
    db: Any,
    store: RunStateStore,
    sleep: Sleep,
    write: Write,
    warn: Write,
    run_id: str | None = None,
    last: bool = False,
    limit: int = DEFAULT_LIMIT,
    poll: float = DEFAULT_POLL,
) -> int:
    """``lohra workflow <action>``, with every side effect handed in.

    The CLI owns the database's lifetime and the two streams; the branching lives
    here, next to the reads it dispatches. ^C stops WATCHING, never the run — the
    run belongs to whichever process launched it, and this one only ever read."""
    if action == "list":
        rows = list_rows(store, db, limit)
        write("\n".join(rows) if rows else "no workflow runs")
        return 0
    target = run_id or (latest_run_id(store) if last else None)
    if not target:
        warn("watch needs a run id (or --last)")
        return 2
    target, ambiguous = resolve_run_id(db, target)
    if ambiguous:
        warn(ambiguous)
        return 2
    try:
        return watch_run(store, db, target, write=write, warn=warn, sleep=sleep, poll=poll)
    except KeyboardInterrupt:
        warn("")
        return 0
