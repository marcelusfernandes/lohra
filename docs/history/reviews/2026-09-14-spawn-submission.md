# Rejected child submission — issue #136

Author validation on public base `59f2bc6788246a666f3d9c4dc4d50737729b215a`,
after #69 and #126. Independent review and full-suite CI of the final integration
SHA remain coordinator checks; this report does not claim their approval.

## Reproduction and scope

The first nine directed regressions ran before the production edit on Python
3.11: **6 failed, 3 passed** (`/tmp/lohra136-red.log`). Both actual WorkflowEngine
funnels, `spawn_leaf` and `spawn_leaf_with_done`, exhibited the same defects:

- A real ThreadPoolExecutor queued the child before Thread.start raised. Core
  propagated the exception but retained a running child without a future. The
  engine refunded lifetime and never tracked it; draining the executor still
  called the synthetic model and tool, and the completion hook where supplied.
- A genuinely closed executor ran nothing but left a running, futureless child.
- With retention capacity one, refused creation evicted the previous completed
  child. At capacity two, it retained the previous child but added the orphan.
  Ordinary accepted creation at capacity one correctly evicted the previous child.

The two ordinary engine submissions and ordinary capacity-one eviction were the
three passing controls. Eviction is therefore confirmed behavior, replacing the
source-only hypothesis from early preparation.

The partial-submit tests occupy the first executor worker in its first-ever task,
so the next submit must try to start a second worker. Only that Thread.start is
made to fail; the executor, Core, GatewaySession, Agent, WorkflowEngine, Budget
and temporary SQLite are real. Public shutdown drains residual queued work before
negative assertions. No private queue access/removal, probabilistic repetition,
real thread exhaustion or sleeps determine the outcome.

## Final contract

Each new spawn now owns an initially unauthorized `_Submission`, reusing the
mechanism already established for idle steer by #69. Under the Core lock, submit
must return successfully before eviction, registry insertion and authorization.
The child is visible only with its returned future. Workers acquire that same
lock before observing the child and permission, so an enqueued refused callable
cannot reach the session, client, tool, usage, event sink or completion hook.
A later valid spawn cannot authorize the earlier attempt.

The engine's existing BaseException refund contract is unchanged: a rejected
creation returns one lifetime slot exactly once, is not tracked, and has no hidden
execution to account. A valid retry consumes its own slot and executes once.
KeyboardInterrupt and SystemExit raised by Thread.start are preserved, with the
same revoked-execution behavior. There is no automatic retry or fake future.

The small `orchestration/preparation.py` extraction keeps Core within its existing
800-line cap (795 lines; helper 42). Agent/configuration, frozen system prompt,
GatewaySession construction and SQLite creation still occur outside the Core
lock. Compaction remains disabled for the child session as before. This does not
move dynamic input into the system prompt or change dispatch/tool authorization.

### Prepared metadata and cleanup

A failed submit leaves no accepted child in the registry and does not alter prior
children, their future/result/causal metadata, or the parent session. Its newly
prepared SQLite session row is retained and ended through the existing
`end_session(sub_id, "spawn_rejected")` API, outside the Core lock. The row records
the attempted preparation, including its already frozen prompt and lineage; it
has no conversation messages or model usage. This is not a completed/cancelled
child and does not emit `on_done` or chain a pipeline continuation.

No session is deleted and no client is closed: a factory may share a client with
other agents. A normal storage exception while recording the end marker is logged
and the original submit exception still propagates. In that case the row may
remain without `ended_at`/`end_reason`; absence of the marker does not authorize
execution or create a Core child. Tests also cover factory/configure/database
failures before successful preparation and preserve an existing session. This is
not a crash-atomic transaction across arbitrary factory code, SQLite and threads.

## Validation

The new file contains **25 deterministic cases**: both engine funnels with partial,
closed and accepted submit; refusal/acceptance at two retention capacities; valid
retry before and after refused-call drainage; KeyboardInterrupt/SystemExit;
cleanup success/storage error outside the lock; three preparation failure seams;
fast completion with a reentrant chained spawn; accepted queued cancel/shutdown.
Assertions about callback observations are made outside callbacks so their
intentional exception isolation cannot hide a failure.

| Run | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Main focused matrix | 349 passed, 23.61s | 349 passed, 24.26s |
| Accounting/settlement complement and new regressions | 97 passed, 5.86s | 97 passed, 5.82s |

The complement repeats the 25 new cases, giving **421 distinct passing cases per
runtime**. The main matrix covers orchestration/steer lifecycle and identity,
gateway/delegate, pipeline, causal/budget/quiescence, and #126 cancellation,
cross-process transitions and guarded publication. The complement covers M7,
accounting races and actual SQLite steering settlement/limits. Its targeted
coverage pass reports **100% statements and branches for preparation.py**, and
79% for Core under that subset; this is not a whole-suite coverage claim.

Commands were run from `backend`, with the matching runtime's bin directory first
in PATH, `PYTHONDONTWRITEBYTECODE=1`, and absolute
`PYTHONPATH=/tmp:/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-136/backend`.
Both passes use `-q -p no:cacheprovider -p lohra129_no_shell --tb=short`; the main
pass uses `--no-cov`. The temporary plugin refuses actual shell-tool execution.
The main test selection is:

```text
tests/test_orchestration_*.py
tests/test_gateway_session.py tests/test_agent_delegate_scope.py tests/test_delegate_task.py
tests/test_workflow_engine.py tests/test_workflow_pipeline.py tests/test_workflow_pipeline_hardening.py
tests/test_workflow_causality.py tests/test_workflow_budget_stop_line.py
tests/test_workflow_operator_budget.py tests/test_workflow_token_budget.py tests/test_workflow_quiescence.py
tests/test_workflow_cancel_snapshots.py tests/test_workflow_cancel_effects.py
tests/test_workflow_cancel_transition.py tests/test_workflow_cancel_process.py
tests/test_workflow_publication_generation.py tests/test_workflow_publication_guard.py
tests/test_workflow_audit_e2e.py
```

The complement selects `test_orchestration_spawn_submission.py`,
`test_workflow_account_race.py`, `test_workflow_m7_features.py`,
`test_workflow_steer_settlement.py` and `test_workflow_steering_limits.py`, all under
`tests/`, with `-o addopts='' --cov=lohra.orchestration.core
--cov=lohra.orchestration.preparation --cov-branch`. Logs:
`/tmp/lohra136-focused-py311.log`, `...-py313.log`,
`/tmp/lohra136-complement-py311.log`, `...-py313.log`; JSON coverage is
`/tmp/lohra136-coverage-py311.json` and `...-py313.json`.
Ruff on the whole backend and `git diff --check` passed.

## Limits

No provider, real shell tool, personal profile/database or resource-exhaustion
experiment was used. Native execution evidence is macOS with Python 3.11/3.13;
the protocol uses public ThreadPoolExecutor behavior and no platform-specific API.
Refusal means the submit call raised, not necessarily that the pool is closed.
The test's bounded Event waits are fail-fast controls, not production permission
waits. Existing accepted cancellation, shutdown, causal accounting and pipeline
hooks retain their semantics.

This slice changes Core.spawn only. WorkflowService's separate executor submission
is tracked in #138, and timer identity in #127. The known scalar/pipeline late
accounting boundaries of #111/#112 remain; rejecting previously untracked work
does not claim to settle those financial cases or change #126's durable decision
and publication protocol.
