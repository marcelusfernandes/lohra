# Post-drain accounting — author evidence for #112

Date: 2026-09-14. Claimed branch: `codex/task-112`. Public base:
`a0724db0ff14af753fda0b609fcfc39b62f35ce2` (integrated #111/PR142).
This is implementation and local validation evidence, not independent approval.
Required full CI and exact-head review remain coordinator gates.

The implementation follows the ten criteria in
[#112](https://github.com/marcelusfernandes/lohra/issues/112) and the public
[epilogue correction](https://github.com/marcelusfernandes/lohra/issues/112#issuecomment-5662313367).
The older pre-#126 design was treated as historical. Graph discovery was used,
then source was checked in this exact worktree because graph snippets can point
at the main checkout. No private reviewer preparation was read.

## Result and boundaries

One acquisition ledger now retains normalized terminal usage by execution UUID,
shared by root and nested engines. It survives Core registry eviction. Received
and applied cumulative receipts use componentwise maxima across input, output,
cache read, cache write and reasoning. Only input/output debit the token budget.

`Budget` commits counters, measurement count and the five-axis applied book with
one immutable `TokenState` reference assignment. All preparation happens before
that assignment. Failure before it leaves the old state; failure after it leaves
both debit and marker. Retrying finalization, including halfway through a batch
or before freezing its result, applies only missing UUID deltas. Concurrent
finalizers return the same frozen financial object. Existing unkeyed consumers
retain their prior behavior; lifetime reservation/refund is separate.

The optional Core observer runs synchronously outside the Core lock, independent
of functional hook ownership, before the hook and before a positive worker turn
finishes its Future. It copies canonical normalized `Usage`, not provider prices
or estimated unknown usage. Capture exceptions remain visible and fail isolated.
The observer does no provider work, SQLite access or worker drain.

Service preserves this order:

1. Seal the engine result and commit the functional decision (#126).
2. Prepare pause intent and keep the pre-drain financial floor.
3. Drain Core while retaining the acquisition's lease.
4. Finalize numbers and replace the complete financial row under the original
   fence, before audit close, final snapshot and release.
5. Bind outcome/event/notification numbers to the acquisition's frozen snapshot;
   publish only with financial acceptance and the existing publication guard.
6. Arm quota auto-resume only after the producing Future finishes (#127).

Finalization and the complete write each have at most two attempts. An ambiguous
write retries the identical immutable full row, not an additive debit. A refused
fence is never replaced by a successor's fence. Failure remains visible in local
status/completion and suppresses outcome/notification publication while cleanup
continues. `financial.committed` distinguishes write acceptance from
`capture_complete`; a committed known floor after capture failure is not presented
as complete capture. Functional status is not re-decided on a financial error.

The historical `RunResult`, outputs, caches, faults, verdict, uncertainty and
node attribution stay at the engine seal. Local financial metadata identifies
that cutoff. A Core-only execution not yet inventoried by the engine at seal can
still contribute a later normalized financial receipt without retroactively
changing historical uncertainty. The resulting money/history difference is
intentional and explicit in spec07.

The applied book is ephemeral. Per-UUID measurement idempotence holds within this
acquisition. On resume, the existing `seed_charges()` still derives its denominator
from `NodeCache.cost_count()`; uncached prior spend lacks a durable measurement
denominator. No schema or attribution redesign is claimed here.

## Acceptance criteria to tests

All test names below are under `backend/tests/`; parametrized counts are distinct
cases, not reruns. The new tests total **35 cases**.

| AC | Evidence |
| --- | --- |
| 1 — late scalar/pipeline, reopen and resume | Four root/nested × scalar/pipeline cases in `test_workflow_post_drain_accounting.py::test_late_receipt_is_financial_only_and_resume_adds_only_new_execution`; the two existing #111 final/reopened financial expectations now require 5/3 instead of 0/0. |
| 2 — nested siblings and eviction | `test_nested_siblings_and_evicted_receipts_keep_disjoint_usage` uses repeated calls to one child template, evicts both late child UUIDs, preserves disjoint root contributions, then resumes children with root cache hits. |
| 3 — cumulative dedup and concurrent finalize/cache replay | Eleven `test_workflow_usage_ledger.py` cases plus nested/cache replay above; `test_orchestration_terminal_usage.py` includes repeated hook/observer delivery and external queued drops. |
| 4 — immutable functional history | All six `test_workflow_post_drain_accounting.py` cases deep-compare results and cache; `test_core_only_receipt_is_financial_without_rewriting_inventory_at_seal` preserves the explicit inventory cutoff. Existing #111 controls remain in the final matrix. |
| 5 — own final totals, five meters, overrun and effects | Four late/reopen cases and `test_final_write_is_complete_after_drain_and_failure_keeps_cleanup`; delayed publication inspects frozen outcome arguments before a successor writes unrelated numbers. |
| 6 — original fence and successor isolation | Four same/different holder × takeover/ambiguous-commit cases in `test_final_financial_retry_never_borrows_successor_fence`; successor financial row, functional row and lease remain byte-equal. |
| 7 — visible error and cleanup | Six clean/transient/ambiguous/write-failure/capture-failure/finalizer-failure cases; Core throwing-observer and ledger failure controls. Audit close then lease release remain observed after drain; no success effect on failed capture/commit. |
| 8 — process death at commit | Two `test_workflow_financial_crash.py` cases run the importable `financial_crash_child.py`, SIGKILL the owned child immediately before/after final SQL commit, then reopen SQLite. |
| 9 — measurements, report axes, refund, unknown usage | Ledger keyed/unkeyed/report-only/cumulative cases; external zero/prefix queued-drop controls; existing #111 queued cancellation/refund and token-budget families in the matrix. No finalizer refund or usage estimate was introduced. |
| 10 — compatibility and gates | **637 passed in each of Python 3.11/3.13**, Ruff and diff check pass locally. Full CI and independent approval of the published final SHA are still pending coordinator execution. |

New case inventory: usage ledger 11; Core terminal observer 5; post-drain
integration 6; financial settlement/fencing 11; crash 2. The final matrix has
**637 unique cases: 35 new and 602 existing**. Ten existing cases were edited:
two intentionally updated financial expectations and eight synchronization
repairs described below. Subprocess helpers add no pytest case. Reruns and
directed profiles below overlap this same set and must not be added to 637.

Canonical asymmetric usage is `(11,13,17,19,7)`. The normalizer fixture injects
canonical `Usage` into synthetic responses; it verifies the real Agent/Core/
Service path, not provider parsing. First post-seal receipt gives five durable
meters equal to that vector, budget spend 24, one measurement and overrun 14
under cap 10. Reopen/resume produces exactly twice the vector and spend 48, with
the overrun high-water mark retained. Nested eviction produces four vectors,
then six after only two new child executions. A ledger unit contrast also uses
nonuniform cumulative corrections and disjoint UUIDs, so aggregate max cannot
accidentally satisfy it.

## TDD and observed failures

Before production edits, six runtime assertions failed on a0724db: the two
existing scalar/pipeline expectations changed only at their declared financial
cutoff, and four new five-meter root/nested cases. Seal/live/cache prerequisites
passed; the final SQLite amount was still zero. Outputs are preserved at:

- `/tmp/lohra-112-initial-red-py311.txt`: 6 failed, 6 deselected, 0.82 s.
- `/tmp/lohra-112-initial-red-py313.txt`: 6 failed, 6 deselected, 0.88 s.
- Original existing test source:
  `/tmp/lohra112-a0724db-baseline/test_workflow_pipeline_accounting.py`.
- `/tmp/lohra-112-ledger-api-red-py311.txt`: 9 failures because the proposed
  ledger API did not exist, 0.12 s. This is an API-shape RED, not nine additional
  independently reproduced runtime defects.

Development runs exposed three new fixture mistakes, all retained rather than
reported as production regressions: an incorrect `budget` status key (actual
key `token_budget`), resetting the child's controlled deadline again on resume,
and an event-name assertion using `run.done` instead of the `DONE` constant.
They were corrected without relaxing financial/history assertions. Overlapping
logs are `/tmp/lohra-112-first-integration-py311.txt`,
`lohra-112-core-nested-py311.txt`, and `lohra-112-settlement-first-py311.txt` in
the same `/tmp` directory; corrected directed runs are retained beside them.

The first 637-case matrix exposed a separate new-test spy ordering error and an
old auto-resume test race:

| Preserved matrix | Python 3.11 | Python 3.13 |
| --- | --- | --- |
| `/tmp/lohra-112-focused-first-py*.txt` | 635 passed, 2 failed, 38.68 s | 636 passed, 1 failed, 38.82 s |
| `/tmp/lohra-112-focused-final-py*.txt` | 637 passed, 36.35 s | 636 passed, 1 failed, 37.25 s |
| `/tmp/lohra-112-focused-freeze-py*.txt` | **637 passed, 38.00 s** | **637 passed, 38.48 s** |

The new settlement fixture originally installed `record_outcome`'s spy after
Service had already captured its callable. Moving that spy before `start()`
made its actor ordering explicit. This was a fixture defect, separate from the
pre-existing Future/arming contract below. No outcome assertion was removed.

### Focused timer-readiness diagnosis

`Future.result()` may return before that Future's done callbacks finish. #127
deliberately arms the timer in such a callback. An immediate `timers.last` access
can therefore precede timer creation; a cancellation/shutdown can also validly
revoke the prepared plan before any timer exists. A test claiming to cancel an
already pending timer must first establish its accepted arming.

The first matrix hit
`test_workflow_durable_state.py::test_a_quota_pause_rearms_its_timer_in_the_next_process`
in both runtimes. A read-only a0724db backend archive at
`/tmp/lohra112-a0724db-timer-baseline/backend` reproduced its `IndexError` with
the first `_arm_resume` retained on an Event, before any timer was created:

- `/tmp/lohra112_timer_readiness_probe.py` and
  `/tmp/lohra-112-timer-baseline-red-py{311,313}.txt`: one failure each,
  0.23/0.20 s, 28 deselected.
- `/tmp/lohra112_timer_readiness_after_status.py` holds that callback until
  `status(wait=True)` demonstrably returns while the Future is done and arming
  incomplete, then releases it. The repaired test observes accepted arming
  explicitly before using the timer and before ending its second quota stretch.
  Directed green: one case each, 1.18/1.17 s, 28 deselected, in
  `/tmp/lohra-112-timer-directed-green-py{311,313}.txt`.

The second matrix exposed the same assumption in
`test_workflow_quota.py::test_auto_resume_restarts_the_run_with_resume_run_id`.
The resulting focused inventory covered all equivalent direct TimerFactory
consumers, rather than waiting for another random failure:

| Test family | Cases and disposition |
| --- | --- |
| quota | Six: status deadline, restart same run id, retry cap, cancel pending timer, shutdown pending timer, clean backlog resume. All reproduced on a0724db with arming held; all now await an accepted-arming Event. |
| token budget | One: quota resume inherits its persisted budget. Same deterministic baseline failure and same synchronization repair. |
| durable state | One cold-rearm case above; its second stretch also waits for the next accepted arming. |
| other TimerFactory consumers | Direct scheduler operations, constructor/recovery scans and lease heartbeats are synchronous at their tested seam; existing #127 readiness tests already use explicit observations. These were inventoried without a blanket patch. |

`/tmp/lohra112_quota_timer_readiness_probe.py` retains the callback until the
baseline's first timer access, or until the scheduler is closed in the shutdown
case. Six quota tests fail with zero timers in 0.53/0.48 s; the budget variant
fails in 0.21/0.19 s. Logs:
`/tmp/lohra-112-quota-timer-baseline-red-py{311,313}.txt` and
`/tmp/lohra-112-quota-budget-timer-baseline-red-targeted-py{311,313}.txt`.
An initial budget-only invocation had not matched the plugin selector after a
failed shell edit and simply passed; its unsuffixed log is retained and excluded
from controlled-latency evidence.

The green plugin `/tmp/lohra112_quota_timer_readiness_after_status.py` holds the
callback until `status(wait=True)` returns and records both `future_done` and
`arming incomplete`, then releases it. All seven repaired cases pass, **0.57 s
on each runtime**, in `/tmp/lohra-112-quota-timer-directed-green-py{311,313}.txt`.
The changed tests await accepted arming; no arbitrary sleep, increased deadline,
skip, xfail, identity relaxation or production #127/status change was introduced.
These eight old-case repairs are covered again by the final 637-case matrix.

## Reproduction and checks

Worktree: `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-112`.
Runtimes: `/tmp/lohra-wave10-py311/bin/python` (3.11.15) and
`/tmp/lohra-wave10-py313/bin/python` (3.13.5). Tests use temporary SQLite and
`LOHRA_HOME`; `HOME`/`CODEX_HOME` are preserved. The source path is explicit because
the existing editable runtime installation can point at another checkout.
Synthetic clients and Event-controlled deadlines establish prerequisites before
expiry; successful resume does not rely on a sub-100 ms inference.

The exact 45-file command is saved as
`/tmp/lohra-112-focused-command-py311.sh`; the freeze run changes only its
`--basetemp` suffix from `focused-final` to `focused-freeze`. For 3.13, substitute
`py313` for `py311` in the runtime, PATH and temporary directory. Its common form,
run from the worktree's `backend`, is:

```sh
env PATH=/tmp/lohra-wave10-py311/bin:$PATH \
  PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-112/backend \
  PYTHONDONTWRITEBYTECODE=1 \
  /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_workflow_pipeline_accounting.py \
  tests/test_workflow_pipeline_accounting_stops.py \
  tests/test_workflow_pipeline_accounting_seams.py \
  tests/test_workflow_pipeline_accounting_tracking.py \
  tests/test_workflow_pipeline_accounting_owner.py \
  tests/test_workflow_pipeline.py tests/test_workflow_pipeline_hardening.py \
  tests/test_workflow_account_race.py tests/test_workflow_lifecycle.py \
  tests/test_workflow_quiescence.py tests/test_workflow_quota.py \
  tests/test_workflow_token_budget.py tests/test_workflow_budget_stop_line.py \
  tests/test_workflow_nested_cost_labels.py tests/test_workflow_cache.py \
  tests/test_workflow_cache_identity.py tests/test_workflow_ownership_fencing.py \
  tests/test_workflow_sandbox_denials.py tests/test_workflow_causality.py \
  tests/test_workflow_nested_identity.py tests/test_orchestration_spawn_submission.py \
  tests/test_workflow_service_submission.py tests/test_orchestration_limits.py \
  tests/test_orchestration_steer_identity.py tests/test_orchestration_steer_races.py \
  tests/test_orchestration_partial_submit.py tests/test_orchestration_core.py \
  tests/test_orchestration_steer_lifecycle.py tests/test_orchestration_tools.py \
  tests/test_orchestration_terminal_usage.py tests/test_workflow_cancel_snapshots.py \
  tests/test_workflow_cancel_effects.py tests/test_workflow_cancel_transition.py \
  tests/test_workflow_cancel_process.py tests/test_workflow_autoresume_boundaries.py \
  tests/test_workflow_autoresume_recovery.py tests/test_workflow_autoresume_readiness.py \
  tests/test_workflow_autoresume_scheduler.py tests/test_workflow_durable_state.py \
  tests/test_workflow_liveview.py tests/test_workflow_costs.py \
  tests/test_workflow_usage_ledger.py tests/test_workflow_post_drain_accounting.py \
  tests/test_workflow_financial_settlement.py tests/test_workflow_financial_crash.py \
  -q --tb=short -p no:cacheprovider --no-cov \
  --basetemp=/tmp/lohra112-focused-freeze-py311
```

Final static checks from the worktree root:
`/tmp/lohra-wave10-py311/bin/python -m ruff check backend` — all checks passed
(`/tmp/lohra-112-ruff-freeze.txt`); `git diff --check` — clean.
No full local suite was added after the focused matrix; mandatory CI runs the
complete suite. No provider call, credentials, personal database, release,
schema change, GitHub mutation or push was made by this author lane.

## Explicit residuals

- Before-final-commit process death retains only the pre-drain floor. The crash
  tests observe one vector before commit and two after commit; they do not claim
  receipt durability before that SQL boundary. The child has an allowlisted
  environment and an audit hook rejecting network/nested subprocess operations.
- Stale-owner usage rejected after takeover is not transferred to the successor.
  Unknown usage and failure before normalized delivery remain parent #10 work.
- Pool drain is not a global external-callback drain. The real Core controls
  reproduce late external queued drops with zero or the already observed positive
  five-meter prefix. The earlier positive observation completes inside its prior
  Future. New positive growth after financial freeze is diagnosed, never used to
  reopen the frozen values; no broader drain protocol was invented.
- The ledger retains every acquisition UUID without the Core eviction cap. Its
  immutable applied-map replacement copies the map on a changed receipt; this is
  an explicit in-memory tradeoff, not a new retention limit or a throughput claim.
- Local immutable totals and financial completeness metadata are acquisition
  observations. Historical per-node costs and resume measurement seeding retain
  their existing narrower durability boundaries.

Reserved global docs, version/release metadata and task111 remained untouched.
The coordinator owns publication, the full CI results and independent review.
