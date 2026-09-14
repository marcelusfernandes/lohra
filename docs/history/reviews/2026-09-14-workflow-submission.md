# Workflow submission acceptance — issue #138

Author implementation on public base
`61e0714ab438a9f1a40170d60dff7b92ba5dbc21`, after #126 and #136.
[Accepted contract](https://github.com/marcelusfernandes/lohra/issues/138#issuecomment-5661038695).
Independent review and full CI of the integration SHA remain coordinator checks;
this report does not claim either approval or publication.

## Reproduction

The initial regressions ran before production changes in task-138 on Python 3.11:
**4 failed, 1 passed** (`/tmp/lohra138-red.log`). Adding replay-metadata and
post-acquire budget-read controls gave **6 failed, 1 passed**
(`/tmp/lohra138-red-expanded.log`). These are overlapping passes, not eleven
independent failing cases.

A real ThreadPoolExecutor's first worker is held in its first task. Its next
submit enqueues the Service callable and attempts to create a second `wf-run`
worker. Only that Thread.start fails; no real resource is exhausted. Tests cover
RuntimeError with queue drainage before/after cleanup, a synthetic BaseException,
an actually closed executor and ordinary successful acceptance. Public executor
shutdown drains queued work before negative assertions. Service, Core, Agent,
WorkflowEngine, registry, cache and temporary SQLite are real; model/tool handlers
are synthetic. Events determine order, without sleeps or repeated race attempts.

On the baseline a refused queued callable could enter the engine, publish a
failed/zero notice after cleanup, or execute two synthetic model calls and one
tool before cleanup, with no returned Future. BaseException skipped cleanup.
A closed executor ran nothing but left durable `running` with no lease. Refused
replay overwrote the previous metadata, and an exception in `_effective_budget`
after lease acquisition leaked that lease.

Read-only preparation revalidated the original probes on both 3.11.15/3.13.5 with
Core #136 present (`/tmp/lohra-138-candidate-{normal,abort}-py{311,313}.json`). The
original negative result is preserved: the nested Core submit cannot reach the
client while Thread.start is still raising under the stdlib global shutdown
lock. The demonstrated execution window begins after that exception unwinds,
before cleanup. Executed usage in that baseline was correctly retained; this
was not evidence of a new accounting error.

The preparation boundary probe also observed SQLite, PLAN and cleanup under the
old lifecycle mutex, and a fast completion callback before Future publication.
Its initial claim that every later DONE must also see that mutex was refuted on
3.13; the final oracle restricts itself to the directed boundaries.

## Mechanism and observable contract

`LaunchAdmission` owns a distinct ticket per Service attempt. Under one short
Condition gate, submit must return a real Future before that Future and permission
are published. The worker checks its exact ticket before the whole `_run`
try/finally, including the accepted preamble. A callable left queued by a refusal
is a no-op, even if it drains before cleanup or after a valid replay. No private
executor queue access, retry, fake Future, client close or usage compensation is
involved. Once accepted, the tracked run owns cleanup even if the caller is
interrupted while constructing its reply.

PLAN/recovery/execution-segment events moved into the authorized worker preamble.
The contract is **Future → PLAN → first leaf**. `started` confirms tracked
acceptance; it no longer promises that PLAN already rendered before returning.
The live-view consumer is installed before launch and still sees the DAG before
NODE events. A held PLAN callback proves start can return while rendering waits,
with no leaf started. Reentrant callbacks see the actual Future and may inspect,
refuse same-run replay, start another run or cancel their own run without an
admission/registry mutex held. No Future callback registration was added.

Preparation reserves admission, but executes SQLite and cleanup outside the gate.
Shutdown marks closing, waits for preparation/cleanup with the Condition releasing
its mutex, then preserves producer-before-sink drainage. Tests hold budget read,
SQLite write, cleanup and actual submit. If submit is already inside the gate,
acceptance can win; if closing wins first, submission is refused. Synchronous
shutdown from the preparing thread explicitly refuses instead of waiting for
itself. This is not general shutdown-from-worker-callback support.

A same-Service `resume_run_id` is reserved before acquiring its lease. The initial
coordinator hypothesis that two preparations always produce two losers was
**partially refuted**: without expiration, SQLite refuses the second acquire.
The distinct WIP discriminator confirms two losers only when the held local
preparation crosses TTL (`/tmp/lohra138-admission-expiry.log`: 1 failed, 1 passed).
The reservation fixes that local window and retires on acceptance/cleanup; another
Service/process may still take over after expiry. It is not a global filesystem
or cross-process admission lock.

## Refused metadata and cleanup

If preparation wrote its initial running snapshot, cleanup uses the immutable
launch revision and captured acquisition fence. A `refuse_launch` transaction
restores the previous metadata and configured token cap together. It does not
rewrite the five usage meters, node cache or prior outputs. A cancel or successor
winning the revision/fence comparison is left intact. The prior fence itself is
never restored: even a refused acquisition advances ownership identity.

The existing JSON payload carries a closed `launch_failure` field:
`submission_refused` or `preparation_failed`, exposed by durable workflow_status.
It contains no exception prose and needs no schema migration. A previous settled
or paused run recovers its status, owner, spec, args, pause data and audit marker.
A new preparation, or a prior `running` run without work, becomes `failed` with
this cause; the previous marker is retained if present. Restoring `running`
would recreate the ownerless-live appearance. A subsequent accepted launch
clears the attempt metadata. A failure before any launch write preserves the
previous row instead of manufacturing a new execution/result.

A new audit marker was only prepared, never emitted. Refusal removes it or
restores the predecessor's marker, without fabricating `segment.started`,
`segment.completed`, `process_crash`, a completion notice or spend. Reopened
SQLite tests cover new, completed, paused and ownerless-running priors with the
real audit sink. Earlier audit events remain byte-for-byte unchanged.

Two additional WIP REDs were fixed before the matrix
(`/tmp/lohra138-restoration-red.log`: 2 failed, 2 passed): restoring run metadata
alone left a newly requested cap in the ledger, and an exceptional restoration
skipped resource cleanup. Configuration now restores in the same CAS; metadata,
Core teardown and captured-fence release are isolated. Failure of the Core cleanup
is also tested with BaseException. The original caller exception survives.
If storage itself refuses/fails, cleanup logs it and the durable line can remain
uncorrected; the report never treats that as successful persistence. The queued
callable still cannot execute. No shared client is closed or workspace deleted.

A separate WIP interruption test failed after acceptance: broad cleanup removed a
tracked run whose caller raised while formatting its reply. Restricting abandonment
to an unaccepted ticket preserves tracking and lets that accepted run finish once
(`/tmp/lohra138-accepted-interruption-{red,green}.log`). This is distinguished from
the baseline submit failure rather than counted as another original cause.

## Validation

The new `tests/test_workflow_service_submission.py` has **28 deterministic cases**.
They cover the five original submission paths; prior metadata/cap preservation;
post-acquire failure; same-run admission with/without expiration; metadata/core
cleanup failure; real Future visibility and callback reentry; four shutdown
boundaries plus preparation reentry; durable refusal/audit across four prior
states; valid replay before/after old-callable drainage; cancel/successor winning
cleanup; and caller interruption after acceptance. Two existing tests changed:

- `test_workflow_liveview.py` explicitly replaces its synchronous-PLAN promise
  with the accepted Future → PLAN → first-leaf contract.
- `test_workflow_cancel_snapshots.py` forwards the new internal kwargs in its
  held `_run` wrapper. The marker-closure/new-acquisition oracle is unchanged.

The 32-file matrix ran once on each runtime: **567 passed, 1 failed, 1 xfailed**
(61.64s on 3.11; 62.59s on 3.13). The single failure was the latter wrapper's
TypeError before its Event gate, because it accepted only positional arguments.
After that test-only repair, its entire file passed **12/12 on each runtime**.
No production changed after the matrix. Combined evidence validates **568 distinct
passing cases and one existing xfail per runtime**, not a second whole-matrix run.
The xfail remains the known early timer-arming contract assigned to #127.

Commands use each `/tmp/lohra-wave10-py{311,313}/bin/python`, matching PATH,
`PYTHONPATH=/tmp:/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-138/backend`,
`PYTHONDONTWRITEBYTECODE=1`, cwd `task-138/backend`, and pytest flags
`-q --no-cov -p no:cacheprovider -p lohra129_no_shell --tb=short`.
The exact matrix file list is `/tmp/lohra138-focused-files.txt`, runner
`/tmp/lohra138-focused-runner.py`, results `/tmp/lohra138-focused-py{311,313}.log`;
seam reruns are `/tmp/lohra138-snapshot-seam-py{311,313}.log`.

The matrix includes Service submission/liveview/lifecycle/durable state/recovery;
#126 cancellation, cross-process transitions, publication guards and ownership;
audit writer/query/resilience/rerouting; operability/quota/operator caps;
pipeline/hardening/budget stop line; Service steering and Core #69/#136 cases.
`ruff check .` passes for the backend; `git diff --check` passes. Full CI and the
coordinator's preserved financial/timer probes are separate pending checks, not
added to the local counts. No provider call, real shell tool, personal state,
resource exhaustion or publication occurred.

## Limits

This is a launch-acceptance fix. It does not change financial sealing or late
accounting (#111/#112), timer-plan arming/replacement (#127), the publication
guard's established TTL limit, frozen prompts or leaf authorization. It does not
provide a crash-atomic transaction across arbitrary factory code, SQLite and
thread creation, nor promise to interrupt uncooperative I/O. Shutdown may still
wait for such work. Storage unavailability is observable failure, not proof that
a correction was written. Obsolete binaries do not obey the new admission protocol.
