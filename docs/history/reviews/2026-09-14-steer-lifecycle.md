# Steer lifecycle and lock-free settlement — issue #69

Author validation on base `d4ebc24a65f8a328d0db50746ead933e8cb82c50`.
The production change is confined to OrchestrationCore and GatewaySession.
Independent review and full-suite CI of the final integration SHA are separate
coordinator checks; this report does not claim their approval.

## Defects and discriminators

Before the fix, the ten directed tests produced **8 failures and 2 passing
controls** in the implementation worktree (Python 3.11, 2.28s). Preparation on
public `bb191e6`, with byte-identical relevant source, also reproduced them in
Python 3.11/3.13:

- A steer from `on_done`, while the future is still pending but the series has
  ended, returned success and left text in an inbox with no consumer.
- The actual executor was shut down. Rejected submit raised RuntimeError and
  changed accepting_steer and causal history. The regression also fills retained
  history and seeds its dropped count at seven to detect truncation on refusal.
- Both read and discarded callbacks ran under the Core lock. Reentrant calls and
  cancellation/shutdown from another thread could not progress until release.
- Eviction between lookup and decision removed an idle target. Steer then
  returned successful submission, but the worker could no longer find the child.
- Positive controls prove accepted A/B delivery once in order, and a worker
  scheduled before submit returns observing the publication lock and new causal
  identity. Refusing all input would fail these controls.

The historical submit probe patched RuntimeError rather than closing the pool;
this validation uses a genuinely closed ThreadPoolExecutor. New full-service
SQLite tests also failed on the old source: `steering.read` audit, and
`steering_release` plus `steering.discarded` audit, observed the Core lock held
(**2 failures**, Python 3.11, 1.23s). Assertions inspect recorded observations
outside callbacks, whose Exception handling intentionally isolates failures.

## Final contract

Lookup, acceptance and successful submission publication happen in one Core-lock
hold. An evicted target receives the existing absent-child error. A live series
queues input; a settled series whose future is still in its epilogue/on_done
refuses it. The operator can await `collect_session` with `wait:true` and then
send a new steer. A closed executor returns an error without publishing changes.
The worker first acquires that same lock, so submitting before publication
preserves causal consistency without an extra Event or executor protocol.

GatewaySession exposes a small internal two-phase seam: `take_steers` detaches
an immutable batch without callbacks; its owner calls `settle_steers` outside
all caller locks. Existing enqueue/drain/discard signatures remain unchanged.
The Core captures the batch and decides continuation or closed acceptance in one
hold. Moving the entire drain outside that hold would reintroduce an
empty-inbox → accepted-input → closed-series race.

Callbacks are ordered within their captured batch, fail-isolated and outside
Core/inbox locks. New input accepted during a read callback belongs after that
batch. Each captured entry has one settlement owner/outcome; read means delivery
to the consumer, not proof that a provider received or completed the instruction.
No global callback queue or durable inbox protocol was added.

The Core rechecks cancellation before executing a committed continuation.
Cancellation during settlement closes acceptance, discards remaining input
outside locks and prevents that continuation from starting. Previous costs stay
intact: a dropped continuation is interrupted when a prior turn landed; only a
subsession that never executed is cancelled/refundable (#60). Existing completed
turn finalization is otherwise preserved. Busy handoff retains the winner's
completion hook; the losing worker does not fire it.

## Coverage of interactions

Tests use real Core/Gateway workers, deterministic Events, synthetic ModelClients
and temporary databases. They cover reentrant read/discard callbacks cancelling
or shutting down their own series, a different child progressing during a blocked
callback, queued cancellation/shutdown, ordinary submit failure, busy handoff,
exact-once hooks, causal history, eviction and unchanged cumulative accounting.

The service integration uses actual WorkflowService, OrchestrationCore,
GatewaySession, SteeringLimits, audit writer and file-backed SessionDB. Read
keeps its durable budget charge; discarded releases it exactly once. Reopening
SQLite verifies the count and ordered accepted/outcome audit records. Instruction
text is absent from those audit records. A second cancel/shutdown does not refund
again. No personal data, profiles, providers or real shell tools were used.

## Validation matrix

From this worktree's `backend`, with the matching runtime first in PATH and an
absolute PYTHONPATH pointing at this worktree's backend:

```text
python -m pytest tests/test_orchestration*.py tests/test_gateway*.py \
  tests/test_workflow_steer*.py tests/test_workflow_service_steering.py \
  tests/test_workflow_supervision*.py tests/test_workflow_quiescence.py \
  tests/test_workflow_causality.py tests/test_workflow_audit*.py \
  tests/test_stream_abort.py tests/test_approval_dispatch.py \
  tests/test_approval_lifetime.py -q --no-cov -p no:cacheprovider --tb=short
```

| Runtime | Result |
| --- | --- |
| Python 3.11.15 | 428 passed, 1 warning, 27.48s |
| Python 3.13.5 | 428 passed, 1 warning, 28.20s |

The warning is the existing Starlette/AnyIO BlockingPortal deprecation. Local
runs additionally loaded a temporary audit-hook pytest plugin rejecting
os.system and shell launches; no such calls escaped the synthetic handlers.

Directed coverage over Core/steer, gateway session and SQLite settlement tests:
**Core 84%, GatewaySession 90%, aggregate 86%**. `python -m ruff check .` and
`git diff --check` pass. Core remains at the 800-line limit; the related
_settle_dropped explanation was shortened without changing its accounting rule.

## Limits

This does not interrupt a provider/tool/callback already running, change the
loop's cooperative cancellation protocol, implement #119 or resolve #70. A
callback shutting down its own worker uses wait=False; wait=True does not gain
self-join semantics. The prompt stays frozen. Workflow budget/audit formats,
public tool arguments, registry, approval ownership and dependencies are unchanged.
