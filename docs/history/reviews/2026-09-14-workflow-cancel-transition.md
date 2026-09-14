# Workflow cancellation as an authoritative transition — #126

Date: 2026-09-14. Author worktree: `task-126`, branch `codex/task-126`, public baseline `148862e0f5bd5b759afc8566fd191d84cd310e6e`. Scope is issue #126 under objective #108. Publication, isolated review, CI and merge are coordinator responsibilities; this report records local implementation evidence.

## Contract and implementation

SQLite now arbitrates functional transitions for both a live acquisition and an ownerless caller. A winning cancel remains cancelled even if the engine later succeeds, pauses or raises. A committed complete/degraded/failed outcome refuses cancellation. A paused row remains cancellable; already-cancelled is idempotent. Ownerless cancellation reads state, lease and the current payload in one transaction and patches only cancellation fields, preserving the current successor's attempts, counters, owner, spec/args, taint, progress and audit marker.

A nullable additive `workflow_run_state.revision` (legacy NULL means zero) orders deferred snapshots within the existing acquisition fence. Dedicated launch, finish, snapshot, cancel and pause-acquisition operations return typed receipts distinguishing refusal from storage failure. A pause-only resume validates the paused row/revision/fence in the lease transaction; a stopped local RunState cannot revive a cancelled durable row. Explicit `start(resume_run_id=...)` replay remains supported, including completed/cancelled runs when ownership is available. A result whose functional persistence was refused emits a local error and cannot publish; its completed Future does not keep the dead acquisition permanently “live”, so explicit recovery remains possible in the same service.

The functional receipt seals the result before drainage. The exact result/acquisition and a final accepted snapshot are both required for library publication, substitution insights and completion notification. A successful later running/cancelled metadata write cannot authorize an earlier refused result. Local application ignores older receipts, including a paused receipt returned after cancellation already applied. Scheduling happens outside snapshot locks; a schedule returning after cancellation cannot restore the pause payload or deadline.

Engine event callbacks carry their original fence and never resolve a successor RunState as their authority. Lease heartbeats use internal (run_id, fence) keys. Release only removes its own renewal record/timer and its SQL DELETE is fenced; a tick already in flight cannot renew a successor or deliver its lease loss to the new acquisition. The store's internal lease-loss callback now carries (run_id, fence), and the service checks that identity before stopping the captured engine. Existing standalone heartbeat callbacks still receive their supplied key.

Transactions contain only local data work and SQLite. Snapshot locks do not surround SQLite, timer operations, shutdown, library writes or external callbacks. The pre-existing broad launch/shutdown lifecycle lock was not redesigned. No OrchestrationCore, provider, prompt, cache-key or financial-fence semantics were changed.

## RED evidence and test adaptation

Before production edits, the converted public Service/SQLite regressions produced **6 failures and 3 controls** (Python 3.11, 1.18 s; `/tmp/lohra126-red.log`). These are behavior failures: cancel becoming failed, acknowledged storage/fence refusal, stale pause-only resume, an already-captured success published over a cancelled line, and an old engine event persisting the new acquisition.

Preparation also reproduced the three actual multiprocessing-spawn cases on Python 3.11 and 3.13: delayed ownerless cancel after a completed successor; delayed cancel losing the newer paused attempt count; and child-process cancel winning before the old parent service resumes. Those became `test_workflow_cancel_process.py`, retaining actual processes, temporary file-backed SQLite, bounded pipe coordination, explicit joining and reopened-database checks.

During implementation, a coordinator-identified release interleaving was confirmed RED: pause release between its local fence check and heartbeat.stop; expire/reacquire the same store; resume the old release. It cancelled the NEW timer. The regression failed at `assert not current_timer.cancelled` (`/tmp/lohra126-heartbeat-red.log`), then passed with acquisition-keyed cleanup. Its paired test holds an old tick before the actual renew SQL and verifies unchanged successor expiry, no stale loss callback, and current-generation loss still delivered.

The first compatibility slice had **248 passed, 1 xfailed, 7 failures**. Five failures came from tests intercepting the replaced bool-save/read-before-write seams; the equivalent typed/atomic seams now retain their original no-leaf/no-notice/cleanup/winner-row assertions. One failure exposed an unsynchronized historical worker-win assertion; takeover tests now await the old worker before forcing the lease transition, and distinguish its completed Future from the new manually persisted winning line. One was the intentionally changed audit observation: the functional decision can now be durable before segment closure. The retained assertions require its lease and marker to remain, blocked replay during drainage, and no false gap. A later matrix caught the compatibility re-export `service.FINISHED_STATUSES`; it was restored using the shared terminal vocabulary.

Final scope review confirmed three additional REDs (1.12 s,
`/tmp/lohra126-late-effects-red.log`): an exception cleanup after a same-store
reacquisition released the newer lease; a late real `NodeCache.put_complete`
callback renewed the successor's expiry from 920 to 1300; and the second late
snapshot after an ownerless abort erased the operator's fault after the first
snapshot had already learned the cancelled revision. All deferred release and
cache-renew effects now carry their original fence. Applying a durable snapshot
adopts only missing fault occurrences, preserving administrative faults without
counting the current RunResult twice. The regression includes byte-identical
fault occurrences and repeated later snapshots. A stale route-abort remedy also
checks its captured revision before the cancelled-idempotence branch, so it
cannot add a fault only to the local copy after a plain cancel won.

## Observable drainage window

A new test reads through a separate SessionDB and WorkflowService connection while the owning run is held before its final spend write. It observes:

- durable `complete`, a live lease and a non-null audit segment;
- explicit replay refused; no library publication or completion notification yet;
- status and watch reading the current ledger floor of **0**, with watch allowed to exit on the functional outcome;
- after drainage, **8** synthetic tokens persisted, marker cleared, lease released, and exactly one publication/notification.

Thus functional completion is not an assertion of final accounting. Existing slow audit-close controls still cover absence of a manufactured gap. Delayed snapshots preserve the audit writer's current marker (including NULL); a legitimate successor launch writes its own new segment.

## Validation

All tests use synthetic clients/engines, Events or injected clocks/timers and temporary databases. No provider/model/network call, personal profile/database, real tool shell, release or publication was used. Local runs used the existing no-shell audit plugin `/tmp/lohra129_no_shell.py`; HOME and CODEX_HOME were preserved.

| Check | Result |
| --- | --- |
| Python 3.11.15 focused matrix | **438 passed, 1 xfailed**, 35.00 s |
| Python 3.13.5 focused matrix | **438 passed, 1 xfailed**, 35.77 s |
| Additional focused branch coverage (67 tests) | new transaction module **92%**, heartbeat **100%**, run-state store **86%** |
| Ruff, both runtimes | passed |
| git diff --check | passed |

The strict xfail is the existing premature auto-resume case assigned to #127. The branch-coverage invocation inherits the repository's broad coverage collection; its 36% repository-wide total is not presented as full-suite coverage. Full matrix CI and isolated review are still required on the final submitted SHA.

Exact focused test set (run from this worktree's `backend`):

```text
tests/test_workflow_cancel_transition.py
tests/test_workflow_cancel_effects.py
tests/test_workflow_cancel_process.py
tests/test_workflow_cancel_snapshots.py
tests/test_workflow_lifecycle.py
tests/test_workflow_lifecycle_triage.py
tests/test_workflow_ownership_fencing.py
tests/test_workflow_recovery_fencing.py
tests/test_workflow_durable_state.py
tests/test_workflow_operability.py
tests/test_workflow_quota.py
tests/test_workflow_route_fault_pause.py
tests/test_workflow_quiescence.py
tests/test_workflow_durable_notifier.py
tests/test_workflow_audit_resilience.py
tests/test_workflow_audit_e2e.py
tests/test_workflow_audit_contract.py
tests/test_workflow_audit_rerouted.py
tests/test_workflow_service_steering.py
tests/test_workflow_steer_settlement.py
tests/test_workflow_steering_durable.py
tests/test_workflow_liveview.py
tests/test_orchestration_steer_identity.py
tests/test_orchestration_steer_races.py
tests/test_orchestration_partial_submit.py
tests/test_orchestration_steer_lifecycle.py
```

For each runtime N = 311 or 313, the local command was:

```sh
PYTHONDONTWRITEBYTECODE=1 \
PATH=/tmp/lohra-wave10-pyN/bin:$PATH \
PYTHONPATH=/tmp:/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-126/backend \
/tmp/lohra-wave10-pyN/bin/python -m pytest <test set above> \
  -q --no-cov -p no:cacheprovider -p lohra129_no_shell --tb=short
```

Logs: `/tmp/lohra126-final-py311.log`, `/tmp/lohra126-final-py313.log`, `/tmp/lohra126-coverage-report.log`. The matrix includes the final historical takeover oracle and all three late-effect regressions.

## Limits

This revision orders functional state; it is not a new financial fence. The original fence still accepts legitimate late financial writes after release when no successor exists and rejects them after takeover. #111/#112 reconciliation is not implemented. There is no noncooperative I/O abort, output resurrection, schema backfill of invented history, or claim that simultaneous obsolete binaries obey the new CAS protocol.

The cancelled-pause scheduling control covers a still-current local acquisition whose pending Future blocks local replacement. General obsolete auto-resume timer replacement/takeover remains #127. The Core.spawn partial-submit issue #136 is separate and unchanged. Builtin skills and frozen prompts are untouched. No new dependency or version change is required.

## Publication repair after independent review of f2afdca

The independent review rejected `f2afdcae0029f188e6da6b113da903b5858d1436`: the final accepted snapshot was followed by release, then an unguarded external effect. A delayed generation 1 could let generation 2 complete and publish, then overwrite its real template (v2 → v1) and send the old 11-token completion after the new 22-token notice. This was a remaining acceptance gap, not evidence that the earlier release ordering was introduced here.

The independent probe was reproduced unchanged on both runtimes, with socket/shell/subprocess audit guards, in `/tmp/lohra126-repair-publication-red-py311.json` and `py313.json`. The authored regression first returned **1 failed, 1 passed** on the published head (`/tmp/lohra126-repair-publication-tests-red.log`), discriminating obsolete publication from valid current publication. A separate WIP RED exposed overly strict revision comparison: a benign same-fence/status snapshot advanced metadata revision and incorrectly suppressed the valid template (`/tmp/lohra126-repair-metadata-red.log`). The final check uses the accepted functional decision plus current fence/status under serialization; unrelated metadata revisions are allowed.

`state/publication.py` adds a 93-line dedicated, nonblocking publication guard per database/run. Canonical paths plus existing database device/inode identity share the native lock across connections, processes and existing case aliases. Separate file databases and private in-memory databases remain separate; in-memory guards share the SessionDB identity with weakly retained per-run locks. New native files/directories use 0600/0700 and lock inodes are never unlinked. No dependency or auth-lock reuse was added.

The guard covers the effective library/candidate writes and synchronous completion callback, and competes with acquisition and cancellation. A publisher delayed before entry loses to a successor; one already inside finishes before succession. No Service/Core/RunState/Store mutex or SQLite transaction spans its effects. Reentry receives `publication_busy` promptly and other runs can acquire. `RunStateStore.acquire_result` carries the typed refusal to Service; the existing `acquire` bool API remains. Publication contention does not claim a live lease or retry ETA. Financial draining, heartbeat identity, original financial fences and #111/#112 semantics remain unchanged.

The dedicated guard is a narrow exception to lock-free callbacks, explicitly accepted in the issue: a stuck live publication callback prevents succession of **that run** until it returns or its process dies, even if the SQLite lease has expired. Native locks release on process death and descriptors close in finally on exceptions. The process-death test proves ordinary TTL takeover then succeeds. This is not a durable outbox/exactly-once delivery protocol, nor a guard for asynchronous effects a custom callback starts after returning. Arbitrary hardlink aliases or replacement of an open WAL database, concurrent obsolete binaries, and Windows-native behavior are not established by these tests; the Windows locking call is separately simulated, while native tests run on macOS.

The first repair-wide invocation had **448 passed, 1 xfailed, 8 failures** in both runtimes: seven multiprocessing cases could not import a main module because the pytest launcher was fed through stdin; one historical ledger probe intercepted the old bool acquisition seam. The runner is now an importable script. Four historical interception helpers now target the receipt seam, preserving their original ordering, no-recovery-notice, winning-row and post-acquire financial-seeding assertions. No failing behavioral oracle was removed.

The final repair set is the preceding 26-file matrix plus `test_workflow_publication_generation.py`, `test_workflow_publication_guard.py`, and `test_workflow_supervision_e2e.py` (29 files). New coverage includes actual Services/SQLite/template writes and unchanged callbacks; a delayed file write and a delayed notifier **after** ownership inspection; genuine spawned-process replay and process termination; temporary SQLite aliases/memory separation; reentrant Service acquisition; distinct runs/databases; current/obsolete publication and benign metadata; exception isolation; and truthful guard-storage refusal. Existing durable notifier tests also exercise the real notice store. The strict #127 early-resume xfail remains unchanged.

The adapted independent probe preserves the same delayed-publication ordering and checks v2 stays v2 with only the successor notice; it passes in `/tmp/lohra126-repair-publication-green-py311.json` and `py313.json`. The original independent probe remains unedited. Branch coverage of the new guard module is **88%** (14-test slice, `/tmp/lohra126-publication-coverage-report.log`); this is module coverage, not a claim of full-suite coverage. The later case-alias control is an additional test; it skips only on a volume where the two existing spellings identify distinct files.

Final repair validation on the complete authored head: **458 passed, 1 xfailed** on Python 3.11.15 (39.11 s) and **458 passed, 1 xfailed** on Python 3.13.5 (39.87 s), including the native case-alias control on this macOS volume. Ruff passed on both runtimes for every changed Python file; `git diff --check` passed. Logs are `/tmp/lohra126-repair-final3-py311.log` and `py313.log`; manifest `/tmp/lohra126-repair-testset.txt`. The exact importable runner `/tmp/lohra126-repair-run-tests.py` invokes `pytest.main` only under `if __name__ == '__main__'`, with that manifest plus `-q --no-cov -p no:cacheprovider -p lohra129_no_shell --tb=short`, under the same absolute PYTHONPATH/PATH and backend cwd as the preceding matrix. This local focused matrix is not the complete CI suite. No providers, real shell tools, personal databases, release, push or merge were used for this repair.
