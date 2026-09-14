# #127 — quota retry readiness and pause authority

Author validation, 2026-09-14. Base: public `e5733b8bbafef1a66a20abbf410036555faf7fd9`
(#138 / PR #140), branch `codex/task-127`. Scope and recovery policy follow
[#127](https://github.com/marcelusfernandes/lohra/issues/127) and the
[pre-implementation recovery decision](https://github.com/marcelusfernandes/lohra/issues/127#issuecomment-5661738424).
This is an implementation report, not an independent review or release approval.

## Result

Quota deadlines are now committed with the authoritative functional pause.
Planning installs no timer. Local completion arms the identified plan only after
the producing Future completes; recovery separately checks that no local worker
or live lease still owns the run. An overdue ready deadline receives delay zero,
without resetting the saved deadline or applying the initial 60-second floor a
second time.

The scheduler compares each local entry's identity for replacement, arming,
cancellation and claim. Timer factory/start/cancel and resume effects run outside
its mutex. A callback invoked during start records an early firing and returns;
it can resume only after start returns successfully. Ordinary failure revokes the
entry and reports failure. KeyboardInterrupt/SystemExit revoke it and propagate.
Shutdown closes admission to late completion callbacks.

Automatic acquisition compares run/fence/status/reason/deadline/attempts in the
same SQLite transaction that checks the lease and advances the fence. Benign
progress/audit revisions preserve that pause; snapshots still use revision CAS.
The recovery query now projects the durable fence as well as the pause row.
The store's accepted-acquisition callback invalidates older local intent before
heartbeat setup, including when that setup subsequently fails. Rejected
validation/acquisition preserves a valid timer. A #138 restoration under a new
fence can be recovered explicitly, but cannot authorize its predecessor's timer.

Recovery deduplicates exact plans, preserves saved deadlines, and uses a local
backoff for legacy None deadlines without writing invented historical timing.
Live foreign leases skip with an ownership-busy diagnostic/count zero; release
or expiry requires a later scan, new Service or manual resume. There is no TTL
watcher, polling or catch-all retry loop. Only newly accepted starts count,
including accepted timers that immediately fire.

## RED evidence and directed controls

Before production edits, three tests failed on the public base with the expected
mechanisms (`/tmp/lohra127-red.txt`, Python 3.11): an obsolete callback consumed a
replacement timer with or without intervening cancel; and an actual Service had
persisted paused/deadline1060 and released its lease while its Future remained
blocked in `_emit_done`, yet the timer had already started.

Two additional candidate REDs informed the implementation:

- A fenced-out state is hidden by the functional `_get` view. Recovery initially
  overlooked its still-live Future and returned count1. The readiness path now
  examines the retained local registry, and the regression requires count0 until
  drain, then an explicit scan after invalidation.
- Four KeyboardInterrupt/SystemExit × factory/start cases, each with an inline
  callback, initially retained `starting=True` bookkeeping after propagating.
  They now prove revocation, no resume, the original interrupt, and clean
  admission for a later explicit plan.

The historical strict xfail is replaced by a real success oracle: an immediate
timer after one quota call completes the two-node retry exactly once (three
synthetic calls total), attempts1, no pending retry and no premature-refusal log.
Its old mitigation-only test was removed because that refusal is no longer the
expected behavior. New readiness tests explicitly observe arming completion;
`status(wait=True)` is not claimed to wait for all Future done-callback effects.

Other directed cases cover cancel at planning, before/after functional
persistence and during prepare; rejected functional persistence; already-done
Future callback registration; delayed old completion versus a new local pause;
manual validation, submit and renewal-setup refusals; timer claim followed by
cancel, manual resume, takeover or shutdown; semantic-token mutation at actual
SQL acquisition versus a benign progress revision; a lease appearing after the
recovery precheck; missing legacy fence/deadline, deadline+5, overdue deadline,
cap, deduplication and accepted-immediate-start counting.

Existing cold-start fixtures installed synthetic timers after construction. With
exact overdue recovery, that left a real timer able to fire during constructor
recovery, so the test helpers now inject timers/clock before construction via
`resume_timer_factory`. Cancellation/process oracles remain intact. The old
durable backoff test expected120 seconds despite the provider retry-after30
having persisted deadline1060 at now1000. It now verifies that exact saved
60-second deadline; None-deadline fallback remains separately checked at240.

## Validation

Both runtimes imported this worktree explicitly, with bytecode disabled and
their own runtime directory first in PATH. The same 21-module focus ran on
Python 3.11.15 and 3.13.5:

| Validation | Python 3.11 | Python 3.13 |
| --- | ---: | ---: |
| Final focused matrix | 371 passed / 18.17s | 371 passed / 19.00s |
| New #127 cases in that matrix | 57 | 57 |
| Existing cases, including converted historical regression | 314 | 314 |

These are **371 distinct cases**, repeated across two runtimes. The 57 new cases
are included in371, not additive. Earlier subsets and RED/GREEN reruns overlap
the same cases. This focus has no xfails. It is not the full backend suite or a
coverage measurement; complete CI and isolated review remain coordinator gates.
`ruff check backend` and `git diff --check` passed after implementation/spec edits.

Final invocation, from this worktree's `backend/`, once per runtime (311/313):

```sh
env PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-127/backend \
  PYTHONDONTWRITEBYTECODE=1 PATH=/tmp/lohra-wave10-py311/bin:$PATH \
  /tmp/lohra-wave10-py311/bin/python -m pytest --no-cov -q \
  tests/test_workflow_autoresume_readiness.py tests/test_workflow_autoresume_scheduler.py \
  tests/test_workflow_autoresume_recovery.py tests/test_workflow_autoresume_boundaries.py \
  tests/test_workflow_quota.py tests/test_workflow_lifecycle_triage.py \
  tests/test_workflow_durable_state.py tests/test_workflow_cancel_transition.py \
  tests/test_workflow_cancel_snapshots.py tests/test_workflow_cancel_effects.py \
  tests/test_workflow_cancel_process.py tests/test_workflow_recovery_fencing.py \
  tests/test_workflow_service_submission.py tests/test_workflow_acquisition_setup.py \
  tests/test_workflow_recovery_notice.py tests/test_workflow_durable_notifier.py \
  tests/test_workflow_token_budget.py tests/test_workflow_watch.py \
  tests/test_workflow_operability.py tests/test_workflow_lifecycle.py tests/test_workflow_pivot.py
```

Logs: `/tmp/lohra127-focus-py311.txt`, `/tmp/lohra127-focus-py313.txt`.
The equivalent313 invocation substitutes py313 in PATH and the executable.

## Limits and preserved scope

Directed tests use actual Service/Core and SQLite, with synthetic clients,
timers, clocks and Events; durable recovery reopens temporary databases. The
existing cancellation suite also uses actual importable spawned processes.
No provider/network call, real tool shell execution, personal database, forced
thread kill or production incidence measurement was part of this work.

Timer installation cannot be atomic with another process's later SQL change;
the final acquisition predicate rejects a raced stale callback. Cancelled native
I/O is not claimed to have been forcibly interrupted. Noncooperative work that
never completes continues to block readiness. Arbitrary direct database edits
are not a supported way to manufacture a second pause under one fence.

No engine/Core, ledger, output/cache reopening, release script or version change
belongs to this implementation. #126 cancel/CAS/publication guards, #136 executor
acceptance and #138 submission/rollback protections remain exercised. Financial
#111/#112 and crash-time usage loss remain separate; the coordinator's existing
financial/timer probes were not repeated here. No push, PR, merge or release was
performed by this author. The coordinator owns final overview documents,
independent final-SHA review, CI, publication and integration.
