# #111 — pipeline accounting before engine seal

Author evidence on public base `a55b436899fe4d021f981312386f8c404035add3`
(after #127 / PR #141), in `lohra-wt/task-111`, branch `codex/task-111`.
This is implementation evidence, not an independent review or a release claim.

## Confirmed failures and TDD

The six initial real Service/Core/Agent/SQLite tests ran before production edits
on Python 3.11.15. Three controls passed and three regressions failed:

| Discriminator | RED on base | Implemented result |
| --- | --- | --- |
| Pipeline provider reply after barrier, before seal | Durable 5/3 instead of 10/6 | Durable 10/6; SQLite reopen retains it; resume totals 15/9 |
| Terminal callback held at accounting entry across the barrier | Writes discarded cell `a#0#0`; replay skips the call | No discarded cell; replay executes that stage |
| Pipeline provider still running at seal | Uncertainty 0 instead of 1 | One uncertain leaf, no invented tokens, frozen result |
| Scalar before/after seal controls | Both passed | Preserved |
| Callback released before expiry | Passed | Successful cell/cache preserved |

The callback-held case is not another missing-usage observation: the provider
was already terminal. It separates accounting from functional acceptance. These
tests adapt the investigation's callback and partial-cache probes; the financial
case retains its original 10/6 then 15/9 oracle. Unlike the historical probe's
bounded seal polling, the committed tests use Events around the actual seal,
provider return, callback and next-node entry. Short timeout/quiescence caps
create expiration; every ordering assertion must fire and cleanup releases gates.

The first test-harness run also exposed an incorrect `NodeCost.input_tokens`
assertion (the field is `NodeCost.usage.input_tokens`); it was corrected before
the recorded three-RED/three-control run. That harness failure is not a runtime
regression.

## Implementation and alternatives

The existing pipeline callback now calls `account_leaf`, checks expiration,
and only then admits `_stage_done`. Accounting was removed from `_stage_done`
to keep one callback funnel. The check after accounting is the functional
acceptance point; there is no new cache transaction or lock across validation,
cache I/O, event publication, or worker submission. A cell admitted before
expiry can retain its cache if a later stage expires. The partial-cache control
runs directly and nested: three calls become four on reopen/resume, with stage 0
called once, and costs remain attributed to the spawning node and nested scope.

Cleanup gives accounting an opportunity for queued, running, already-terminal,
and terminal-during-cancel observations, including when cancellation raises.
Terminal charging/refunding remains deduplicated by sub-session UUID. Live
leaves enter the existing pending funnel without stealing the pipeline's owned
Core hook. The callback and stranded cleanup remain nonblocking; the node
barrier retains one shared quiescence cap. Expired callback exceptions are
logged without adding functional faults or dropping items after seal; the
existing pre-expiry exception test still faults and settles the item.

Only adding accounting to the expired-return branch was insufficient: it would
leave the held-in-accounting callback able to cache after expiry. Only moving
accounting before the guard was also insufficient: a callback that has not
fired cannot expose a live leaf's uncertainty at seal. The selected change
reuses existing accounting/defer machinery rather than adding an observer or
post-seal financial collector.

## Administrative stop origin

Adding pipeline pending entries activated the caveat in `_settle_pending`:
three quota controls were RED because a paused pipeline with a live sibling
treated its barrier and unknown-bill faults as ordinary failures. The tests
distinguish pause before cleanup, an earlier timeout followed by pause, and an
independent ordinary failure followed by pause. Each also has an explicit
cancel control; the final matrix repeats the cases for quota and route pauses.

The pipeline captures whether a pause already stopped it when its barrier
expires, before cleanup/wait. Pending entries store only that boolean with
`setdefault`; the first observation owns the cause. Later reads and the global
pause flag at seal cannot reclassify an earlier timeout. Administrative barrier
and uncertainty faults enter `pause_faults`; ordinary failures do not. An
explicit cancel wins the verdict. SQLite reopen proves `prior_degraded` remains
false for a clean pause/resume and true for independent earlier faults. The
per-stretch resumed status is `complete` in both cases; the carried flag is the
historical/certification oracle. An initial test incorrectly expected the
historical flag to change that per-stretch status, then corrected its oracle to
the actual durable flag. Another harness correction used `holder`, the real
RunStateStore constructor parameter.

The boolean describes this narrow pipeline stop origin, not general provider
failure causality or durable receipt provenance. It never changes financial
charging, overrides an earlier pending cause, or grants ownership.

## Verification

Synthetic clients only; real providers, networking and personal shell tools
were not invoked. Service state is temporary SQLite, reopened where specified.
The repository's autouse fixture isolates `LOHRA_HOME`; `HOME` and `CODEX_HOME`
are preserved. No personal database/migration, release, force-push or GitHub
mutation was performed by the author.

Python **3.11.15** and **3.13.5** each passed **335 distinct tests**:

- 263 in the final accounting/cache/fencing matrix, including **37 new cases**;
- 72 additional causality, nested identity, Core and Service submission controls.

These are 335 unique cases per interpreter, not the sum of overlapping RED,
development and final reruns. The earlier 204 + 50 run preceded the newly
confirmed tracking windows. Final matrix times: 27.44s / 27.55s; submission and
identity controls: 2.40s / 2.43s. Ruff over `backend` and `git diff --check` passed.

From this worktree's `backend/`, use the following command with each runtime:

```sh
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-111/backend \
PATH=/tmp/lohra-wave10-py311/bin:$PATH \
/tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_workflow_pipeline_accounting.py \
  tests/test_workflow_pipeline_accounting_stops.py \
  tests/test_workflow_pipeline_accounting_seams.py \
  tests/test_workflow_pipeline_accounting_tracking.py \
  tests/test_workflow_pipeline.py tests/test_workflow_pipeline_hardening.py \
  tests/test_workflow_account_race.py tests/test_workflow_lifecycle.py \
  tests/test_workflow_quiescence.py tests/test_workflow_quota.py \
  tests/test_workflow_token_budget.py tests/test_workflow_budget_stop_line.py \
  tests/test_workflow_nested_cost_labels.py \
  tests/test_workflow_cache.py tests/test_workflow_cache_identity.py \
  tests/test_workflow_ownership_fencing.py tests/test_workflow_sandbox_denials.py \
  -q -p no:cacheprovider --no-cov
```

The separate 72-case command uses the same environment and flags with
`test_workflow_causality.py`, `test_workflow_nested_identity.py`,
`test_orchestration_spawn_submission.py`, `test_workflow_service_submission.py`.
For 3.13 replace both occurrences of `py311` with `py313`.
The tracking module was promoted from `test_workflow_pipeline_accounting_spawn_probe.py`
after the final run; only its filename and introductory docstring changed.

The new seam controls cover queued refund once, no refund for started work,
terminal observations before/during cancel, duplicate accounting before and
after seal, expired exception isolation, and live budget refusal of a later
node. Empty/schema-invalid discarded replies neither retry nor submit a
successor. Existing no-barrier, worker-return, shared-quiescence, steered
already-billed refund and nested-cost controls remain green.

## Limits and cutoffs

The implemented financial cutoff is the **engine's internal seal**. Service's
functional decision is committed before Core drain; its final snapshot and
publication follow drain. These are different boundaries. After-seal negative
controls retain durable 0/0 despite receiving a later 5/3, expose one uncertain
leaf and deep-compare the unchanged `RunResult`. Numerical reconciliation during
drain, under the original acquisition fence, remains **#112**. This change does
not claim crash recovery, receipt durability, historical backfill, or a change
to the #126/#136/#138/#127 acceptance, cancellation, CAS or readiness contracts.

The original after-guard TOCTOU hypothesis is not turned into a new transaction:
the admission point is explicitly after accounting, and accepted partial cells
are valid. No new global claim, observer or cache invalidation protocol is added.

## Additional reproduced tracking windows

After the original fix, the named spawn/tracking hypothesis became a separate
RED: stage 1 was already running and tracked by the engine while its parent's
callback was held before the pipeline's own append. The barrier and seal
reported uncertainty 0. The coordinator explicitly included this observed case
in #111; the nine added controls are distinct from the original three REDs.

The engine now remembers each expired local pipeline's first stop origin. The
seal catches up unaccounted UUIDs in its existing inventory for that exact node.
Tracking after expiration registers the same pending origin under the seal's
lock, so it cannot be lost between the inventory snapshot and the seal. This
does not sweep Core globally, share nested inventories, or create a new
admission protocol. Eight Event-gated combinations cover tracking before/after
expiry, root/nested execution and ordinary timeout/quota pause; scalar b and a
successful sibling pipeline remain outside the expired scope. The parent of a
nested run has no marked local pipeline and counts the child's uncertainty once.

A second ordering contrast then reproduced a financial attribution error: the
stage's terminal callback completed before `_track`, after the node loop had
moved to b. The 5/3 receipt was billed under b and a remained at 5/3 instead of
10/6. The pipeline callback now supplies the owner already captured in `_Cell`
to accounting (including its denial observation), and the spawn path captures
its local owner from causal `node_path` before Core submission for `_track`.
No owner is inferred from the later global current node. The isolated control
now charges a 10/6 while b's provider remains blocked. During development, one
incorrect access to `CausalContext.node_id` was caught by the probe; the actual
structured field is `node_path`. It was corrected before the final matrix.

The cutoff remains explicit: if Core accepted work but neither the engine
inventoried its UUID nor its terminal callback admitted usage before seal, this
change does not discover that live work. Core acceptance itself is not replaced
or redefined, and #112's later collector is not implemented here. No assertion
claims the untracked/no-receipt interval or post-seal usage is recovered.

## PR #142 review repair: owner before causal construction

The independent review of candidate `bf90fd44841a6bc3c9ac7bd83b088692abdf5739`
returned **CHANGES_REQUIRED**, despite its eight preparation oracles and the
335 repository cases passing. Its additional discriminator delayed stage 1 in
`cache_lookup`, after `_advance` passed its first guard and **before** its causal
context existed. Pipeline p expired and follower q started. Context construction
then read q from `_current_node`; `_track` preserved that incorrect owner and
overrode the callback's correct fallback. The accepted terminal bill was present
run-wide, but attributed p:5/q:5 instead of p:10/q:0. This is an attribution defect
inside the accepted inventory cutoff, not the Core-only or #112 residual.

Review: https://github.com/marcelusfernandes/lohra/pull/142#pullrequestreview-5196640949.
The rejected SHA and the reviewer's original files remain unchanged.

Before changing production, the author added four Event-gated regressions in
`test_workflow_pipeline_accounting_owner.py`: root/nested execution crossed with
terminal callback before/after engine tracking. All four were RED on bf90fd4 in
each interpreter (3.11: 1.19s; 3.13: 1.01s), without a harness correction. In the
after-track cases, local costs were a:5/b:5 and the causal path ended in b. In the
before-track contrast, the existing callback fallback correctly charged a:10,
but the causal path still ended in b; it would subsequently poison tracking.
The nested cases retained the call prefix but named the wrong local node.

The repair passes `node_id=cell.owner_node_id` to the existing causal-context
constructor in `_advance`. This binds ownership at its origin, even if the
preceding cache lookup outlives the barrier. Core, engine acceptance/tracking,
the financial fence and callback ownership are unchanged. The regression checks
real accepted and started work, correct inventory and causal paths, a:10 before
b replies, preserved first-stage partial cache, discarded second-stage output,
and final a:10/b:5. It explicitly observes callbacks on both sides of tracking.
The prior stop-cause, sibling-scope, no-barrier, refund and seal controls remain
part of the validation matrix. No new cache transaction or drain is introduced.

### Repair validation, counted separately

| Check | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| New author regressions before production edit | 4 failed / 1.19s | 4 failed / 1.01s |
| Repository matrix after repair | 339 passed / 28.43s | 339 passed / 28.87s |
| Preserved reviewer probes, executed by author | 9 passed / 3.07s | 9 passed / 2.95s |

The repository matrix is the original **335** cases plus **4** new cases:
**339 distinct cases per interpreter**, including 41 new #111 cases overall.
The nine separate preserved oracles include the review's new delayed-context
discriminator; they are not added to the repository count. RED and development
reruns are not summed. The four-case development GREEN on 3.11 passed in 0.98s
before the complete focused matrix; it is a rerun, not four additional cases.
No full suite ran. These are author results and confer no independent approval
on the repaired commit. `python -m ruff check backend` and `git diff --check`
passed after the repair; the original reviewer probe modules remain byte-identical.

The repository command is the earlier 263-case command plus
`tests/test_workflow_pipeline_accounting_owner.py` and the four modules named in
the separate 72-case command, in one invocation. Absolute PYTHONPATH, runtime bin
first in PATH, `PYTHONDONTWRITEBYTECODE=1`, `-m pytest`, `--no-cov` and
`-p no:cacheprovider` remain the same. Synthetic clients and temporary per-test
LOHRA_HOME/SQLite preserve HOME/CODEX_HOME and do not call real providers or tools.
The preserved external probes additionally reject network/provider/process calls
and verify the imported source path. Reproduce those with the same environment:

```sh
LOHRA111_EXPECT_FIXED=1 PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-111/backend \
PATH=/tmp/lohra-wave10-py311/bin:$PATH \
/tmp/lohra-wave10-py311/bin/python -m pytest \
  /tmp/lohra111-independent-bf90fd4/probes/test_issue111_independent.py \
  /tmp/lohra111-independent-bf90fd4/probes/test_issue111_owner_boundary.py \
  -q -s --tb=short -p no:cacheprovider --no-cov
```

Repair logs: `/tmp/lohra-111-repair-owner-red-py311.txt` and `py313.txt`,
`/tmp/lohra-111-repair-focused-py311.txt` and `py313.txt`, and
`/tmp/lohra-111-repair-preserved-oracles-py311.txt` and `py313.txt`.
The specification now states the explicit original-owner binding and replaces
the obsolete §10 assertion that expired pipelines never enter accounting with
the current #111 inventory cutoff. The #112 post-seal limit remains explicit.

## CI harness repair after ece5b8c1

CI run `34834990709` tested `ece5b8c1b2d5fcee35f4c2203c0db6ec9fd567fb`.
Python 3.11 failed four cases (4423 passed); Python 3.13 passed all 4427.
The SHA's independent review had approved the production repair, but CI did not
pass and no merge occurred. The failed SHA and earlier reports are preserved.

The two resumed stop-origin cases recorded **new** p timeouts in the recovered
stretch, yielding `degraded` rather than `complete`. The earlier-timeout/cancel
case reported one uncertain leaf instead of two; the log said earlier settled
in 0.0s, without recording whether its provider had started. The tracking case
expired a before its required second-stage acceptance Event fired. All four
tests allowed a 100ms wall-clock deadline to run before their prerequisites, and
the stop-origin tests reused that same induced deadline during recovery.

### Directed evidence before changing the tests

Four controlled contrasts against the original tests failed identically on
both local runtimes (3.11.15: 7.73s; 3.13.5: 7.92s). Real Core/Agent/Service,
real executor Futures and temporary SQLite were used, with synthetic clients:

- Hold a recovered p response until p's real expiration completes: the two
  original resume assertions fail `degraded != complete`. The new timeout is
  expected runtime behavior for that schedule, not a contaminated prior flag.
- Occupy every real Core worker until earlier expires: its accepted Future is
  genuinely queued, cancellation succeeds, status is `cancelled`, `landed=False`,
  usage is 0/0 and the earlier provider never runs. One remaining uncertain leaf
  is correct. The original test's implicit premise that earlier was live was
  not established by its Events.
- Hold stage 0 at `_fire_done` until a expires: the terminal 5/3 exists, but the
  callback correctly refuses the expired continuation, so stage 1 is never
  accepted and the original acceptance assertion fails.

The CI logs do not identify the exact earlier Future state or scheduling delay.
These are reproducible supported interleavings producing its assertion failures,
not a claim to have reconstructed every CI thread transition. They establish a
harness ordering defect; no new production defect was demonstrated.

The retained isolated evidence is `/tmp/lohra-111-ci-harness-red-py311.txt` and
`py313.txt`, generated by `/tmp/test_lohra111_ci_harness_repro.py`. A first
exploratory invocation was excluded from the retained paired evidence because
the `/tmp` test location bypassed the repository's LOHRA_HOME fixture; explicit
isolation and network/provider/process guards were added before both recorded
runs. The original test sources extracted byte-for-byte from ece5b8c1 and a
frozen-import adapter live in `/tmp/lohra111-ci-harness-ece5b8c1/`.
The adapter was checked against the unchanged production after the test repair:
the same four old assertions still failed on 3.11 in 7.48s, recorded in
`/tmp/lohra-111-ci-harness-frozen-adapter-py311.txt`. This is a preservation rerun,
not four new failures or test cases; it keeps the counterexample reproducible
without reverting the repaired test files.

### Test-only change and validation

`tests/pipeline_deadlines.py` controls each selected pipeline's own barrier wait
with an Event. Once the driver releases that deadline, a zero-time observation
of the real completion Event chooses completion or the real `_expire` path.
It does not mock expiration, cleanup, accounting, Core acceptance or seal, and
never patches `threading.Event` globally. Other pipelines must complete naturally;
the five-second waits are assertions guarding a hung test, not workflow deadlines.

The stop-origin driver now proves earlier's provider started before inducing an
ordinary timeout, and proves the live slow sibling plus the processed pause
callback before inducing p's administrative timeout. The first-stretch deadline
mapping is cleared before reopening/resuming. The tracking driver proves stage 1
was accepted and started, explicitly waits for tracking in the before-expiry
variant, and latches pause where required before releasing the deadline. Its
callback-before-track contrast uses the same ordered setup. All original cost,
uncertainty, cause, cancellation, prior-degraded, scope and recovery assertions
remain unchanged. No timeout was merely widened, skipped or xfailed.

| Validation after test repair | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Changed modules, ordinary scheduling | 21 passed / 3.77s | 21 passed / 3.85s |
| Same cases with deliberate startup/callback latency | 21 passed / 25.18s | 21 passed / 25.12s |
| Final repository focused matrix | 339 passed / 28.53s | 339 passed / 28.74s |

The perturbation profile `/tmp/lohra111_ci_latency.py` occupies all real Core
workers before the first submission, proves its Future is queued, and releases
them after 250ms. It also delays terminal delivery by 200ms with Events/Timers.
Both delays exceed the former 100ms assumption, including on recovery. All
queued first submissions survived until actual start in the repaired tests.
Its per-case JSON records that fact and terminal-delay counts; output is in
`/tmp/lohra-111-ci-harness-latency-py311.txt` and `py313.txt`.

Run the two changed modules with the earlier absolute runtime/PYTHONPATH and
pytest flags. To apply the perturbation, append `:/tmp` to PYTHONPATH (keep this
worktree's backend first) and add `-p lohra111_ci_latency -s`. The final matrix
uses the same 339-case command as the ownership repair, with output in
`/tmp/lohra-111-ci-harness-focused-py311.txt` and `py313.txt`. Ruff over backend
and diffcheck passed. The repository count remains **339 distinct cases per
interpreter**: the 21 ordinary and latency runs are overlapping checks, not 42
additional tests. No new test cases or production changes were added. The full
4427-case CI suite was not blindly repeated locally; its next result remains a
separate integration gate. Local patch versions differ from the CI runtimes.

Only these two test modules, their barrier helper and this author report changed.
Production remains byte-identical to ece5b8c1; the timing contract itself continues
to be exercised by the existing pipeline/quiescence tests in the focused matrix.
The repaired candidate still requires independent review and successful CI.

## Owner-test deadline residual after e8e46f5

Before publishing `e8e46f5a619ebf6a71b9483b6d43348b44aeeebb`, the coordinator
identified a remaining hypothesis in the four ownership regressions. Their
stage-1 lookup prerequisite still raced the 100ms deadline, and the preceding
21-case latency profile covered only stop-origin and tracking tests.

The author applied the **same** 250ms queued-start / 200ms terminal-delivery
profile to those four cases, changing only its module allowlist in the separate
`/tmp/lohra111_ci_owner_latency.py`. Before edits, all four failed at
`lookup_entered.wait(5)` in both Python versions (21.43s each). The profile
recorded the first Future cancelled before its workers were released. Its
survival assertion also produced four teardown errors, one per affected case;
these are not four additional cases or a second runtime defect. Causal and cost
assertions were never reached because the required lookup did not begin.

The owner tests now use the existing `control_pipeline_deadlines` helper. The
driver waits for `lookup_entered` while lookup is held after the real `_advance`
guard, releases a's deadline, waits for follower b, and only then releases the
lookup. This preserves the pre-causal-construction delay that caught the actual
ownership defect. Root/nested paths, callbacks before/after tracking, a:10 before
b's response, partial cache, discarded second-stage output, final a:10/b:5 and
uncertainty assertions are unchanged. Cleanup releases the new deadline gate.

| Owner cases after adaptation | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Ordinary scheduling | 4 passed / 0.54s | 4 passed / 0.58s |
| Same deliberate latency profile | 4 passed / 4.21s | 4 passed / 4.20s |

The successful profile records each first submission still uncancelled when
startup is released, three terminal delays per case, correct a/call-a causal
paths and a:10 before b replies. There are **four distinct cases per runtime**
in this narrow follow-up; normal/profile runs overlap and add no repository
test cases. The prior 339-case matrix is preserved, not claimed as a rerun on
this follow-up. No other test family was explored or newly implicated.

Raw logs: `/tmp/lohra-111-ci-owner-latency-red-py311.txt` and `py313.txt`,
`/tmp/lohra-111-ci-owner-latency-green-py311.txt` and `py313.txt`, and
`/tmp/lohra-111-ci-owner-normal-green-py311.txt` and `py313.txt`. The original
owner module from e8e46f5 is preserved at
`/tmp/lohra111-ci-harness-ece5b8c1/original_owner_e8e46f5.py`. Use the prior
absolute runtime environment with `tests/test_workflow_pipeline_accounting_owner.py`;
for the latency run append `:/tmp` to PYTHONPATH and select
`-p lohra111_ci_owner_latency -s`. The original 21-case profile is unchanged.

Ruff over backend and diffcheck passed. Only the owner test module and this
report changed; the helper and production remain unchanged from e8e46f5.
This author check does not inherit or confer independent approval on a new SHA.

## Partial-cache CI failure and remaining deadline-order audit after e552e830

CI run `34837500708` tested `e552e830ff56e49879ff24a1966f7199e0a8d106`.
Python 3.11 passed 4427 cases with five skips. Python 3.13 passed 4426 with five
skips and failed the root partial-cache recovery assertion: `[None]` instead of
`["SECOND"]`. Its log records a new pipeline timeout in the recovered stretch.
The nested variant has the identical scheduling dependency. This family was
outside the previous 25-case latency profiles, despite passing the focused
matrix. No merge occurred; the failed candidate and prior evidence are retained.

### RED evidence and scope selection

Before editing tests, `/tmp/lohra111_ci_partial_resume_latency.py` retained the
resumed stage-1 terminal delivery at the real `Core._fire_done` entry until the
real pipeline expiration returned. Both root and nested variants failed the
original recovery assertion on both interpreters: two failures / six deselected
in 0.99s (3.11.15) and 1.01s (3.13.5). Its facts show terminal `complete`, 5/3
usage, the correct a or call/a path, and a new expiration in the recovered
stretch. This supported schedule demonstrates that the successful replay
expectation depended on callback delivery within 100ms. It does not reconstruct
the exact CI thread schedule or establish a new production defect.

The audit covered all **41 new #111 cases** in the five accounting modules:

| Family | Cases | Intended ordering / deadline coverage |
| --- | ---: | --- |
| Terminal before seal; live leaf at seal | 4, scalar/pipeline | Provider already live before selecting timeout; durable recovery where present |
| Callback held in accounting | 2 | Actual callback enters accounting before expiry, versus natural completion |
| Partial cache and reopen | 2, root/nested | Stage 1 accepted and stage 2 live before expiry; natural recovery |
| Direct cleanup states | 4 | Direct queued/running/terminal/during-cancel observations; no barrier wait |
| Expired accounting exception | 1 | Callback already inside accounting before expiry and seal |
| Discarded reply and live budget | 3 | Provider live before expiry; terminal bill before later admission |
| First-stop origin and prior failures | 12 | Event-selected ordering since e8e46f5; previous latency evidence retained |
| Inventory, append gap and callback before track | 9 | Event-selected ordering since e8e46f5; previous latency evidence retained |
| Original owner after held lookup | 4 | Event-selected ordering since 83cf867; previous latency evidence retained |

None of these new cases asserts elapsed deadline duration. The first seven rows
contain the remaining 16 cases: **11 with a 100ms prerequisite race**, one
natural five-second callback control, and four direct cleanup controls. The
corrected `/tmp/lohra111_ci_remaining_latency.py` profile held all real Core
workers until 250ms after the first accepted submission, then delayed each
terminal delivery by 200ms. Before edits it produced **11 failed / 5 passed**
on both runtimes (61.63s / 61.48s). Its queued-start survival assertion produced
11 teardown errors for those same 11 cases; they are not additional cases.
The failures included providers that never started and cleanup entering the
first accounting gate instead of the assumed callback. The partial-cache pair
overlaps these 11 failures; it is not two more distinct failing tests.

The first exploratory version of that profile reported 12 failures because its
artificial worker occupancy was released only by a first spawn. A fully cached
resume has no spawn and the profile itself blocked shutdown. This injector
artifact is excluded from the 11. The corrected profile releases unused warmup
workers at Core shutdown, leaving actual submitted-leaf timing unchanged. The
initial script and logs are retained separately, without claiming a runtime
shutdown defect.

### Test-only repair and validation

The affected pipeline cases use the existing per-pipeline deadline helper only
after their live-provider or callback prerequisites. The partial-cache cases
prove stage 2 is live after the accepted first cache entry; both root and nested
replays clear the first-stretch deadline mapping and require natural completion.
The two accounting-gate families also compare the held observer's thread with
the hook's recorded thread before releasing expiry. Cleanup can no longer
silently stand in for the callback actor that the tests intend to exercise.

The scalar comparison cases showed the same queued-versus-live defect. Their
small helper selects only blocking agent collects for the named test node. It
waits for the provider prerequisite, then calls the real Core collect with a
zero-time observation, preserving the real engine timeout, cancellation and
quiescence paths. After clearing the mapping, recovery requires a terminal
result within a five-second assertion watchdog. Nonblocking observations,
other nodes and the four direct cleanup cases are unchanged.

Output, partial-cache, cost, dedup, uncertainty, refund, retry, successor, budget
and reopen assertions remain intact. Existing real-time pipeline/quiescence
controls are untouched, including timeout fault/degradation, straggler rejection,
pipeline cancellation barriers, the shared quiescence cap, and the elapsed
worker-return/no-barrier controls. No production behavior, timeout constant,
test skip or expected result was changed to make this schedule pass.

| Current repair validation | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Remaining two modules, ordinary scheduling | 16 passed / 2.06s | 16 passed / 2.13s |
| Same cases, 250ms startup / 200ms terminal delivery | 16 passed / 14.89s | 16 passed / 14.86s |
| Final focused matrix, including actual deadline controls | 339 passed / 25.63s | 339 passed / 25.79s |

The 3.11 latency run emitted one pytest old-temp-directory cleanup warning;
all case assertions and profile checks passed. The final matrix uses distinct
temporary roots per interpreter. These runs overlap: the repository remains
**339 distinct focused cases per interpreter**, including the 41 new #111 cases.
The two-module normal/profile runs do not add 32 cases, and RED/profile reruns
are not added to the unique count. Previous 25-case latency evidence and the
independent production oracles are retained, not claimed as rerun here.

Evidence: `/tmp/lohra-111-ci-partial-resume-red-py311.txt` and `py313.txt`;
`/tmp/lohra-111-ci-remaining-latency-red-v2-py311.txt` and `py313.txt`;
`/tmp/lohra-111-ci-remaining-normal-green-py311.txt` and `py313.txt`;
`/tmp/lohra-111-ci-remaining-latency-green-py311.txt` and `py313.txt`;
`/tmp/lohra-111-ci-partial-final-focused-py311.txt` and `py313.txt`.
The initial profile is `/tmp/lohra111_ci_remaining_latency_initial.py`; its logs
are `/tmp/lohra-111-ci-remaining-latency-initial-py311.txt` and `py313.txt`.
Original e552e830 sources
are preserved as `original_accounting_e552e830.py` and
`original_seams_e552e830.py` in `/tmp/lohra111-ci-harness-ece5b8c1/`.
The compact inventory is `/tmp/lohra-111-new-family-deadline-inventory-e552e830.md`.

Reproduce the two-module runs with the absolute runtime/PYTHONPATH environment
above and `tests/test_workflow_pipeline_accounting.py
tests/test_workflow_pipeline_accounting_seams.py`. For latency append `:/tmp`
to PYTHONPATH and add `-p lohra111_ci_remaining_latency -s`. The dedicated old
partial-resume RED used `-k accepted_partial` and
`-p lohra111_ci_partial_resume_latency -s` against the original source. The final
matrix uses the same 339-case selection documented above. All use real local
Service/Core/SQLite and synthetic clients; LOHRA_HOME is temporary, while
HOME/CODEX_HOME are preserved. The profiles reject real provider/network/process
calls. The 4427-case CI suite was not repeated locally without a diagnosis.

Ruff over backend and diffcheck passed. Only these two test modules, their helper
and this report changed. Production
remains byte-identical to e552e830/ece5b8c1, and #112 remains separate. Independent
review of the new SHA and successful CI remain coordinator integration gates.
