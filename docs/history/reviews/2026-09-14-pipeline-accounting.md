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
