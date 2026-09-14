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
