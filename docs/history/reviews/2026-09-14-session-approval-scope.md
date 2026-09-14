# Approval ownership per live dispatcher — issue #129

Implementation base: `bb191e65a9791d1d552bfc55d55ed53ce54ac9b4`.
Scope: isolate approval cache, callback and yolo between consumers in the same
process. This report records author validation; independent review and CI are
separate integration checks.

## Reproduction before the fix

Hermetic consumer tests exercise real `run_chat`, dashboard construction,
`GatewaySession`, `Agent`, registry dispatch and temporary SQLite storage.
Model replies and the shell executor are synthetic.

- The first baseline run produced **5 failed, 10 passed**: a CLI session grant
  leaked into a later JSON/no-input invocation, including reuse of the same
  persisted session ID; CLI yolo leaked into dashboard execution. Fresh
  dashboard, once, different-command and same-consumer controls discriminated
  the scope of the defect.
- A deterministic concurrent CLI test failed separately. Events place the
  interactive consumer inside its model request before a headless consumer
  replaces the singleton callback. The interactive consumer then incorrectly
  loses its own approver. Isolation must prevent both accidental grants and
  accidental denial.
- The additional CLI-once → dashboard reproduction on the read-only
  `75547e9` baseline observed **3 prompts and 3 fake executions**, rather than
  the one CLI prompt/execution. Two dashboard sessions called the stale CLI
  callback. The regression now checks both executions and prompts.
- Two actual Python processes using the same synthetic home/database and
  persisted ID were already isolated on that baseline in Python 3.11/3.13:
  interactive CLI executed once, headless CLI never prompted or executed.
  This is a same-process ownership defect, not evidence of a cross-process or
  remote exploit.

## Ownership and public API

`build_session_dispatch(..., approval_manager=...)` owns an explicit manager;
omitting it creates a fresh default-deny manager. CLI constructs and configures
its own manager with the existing TTY/headless callback and yolo rules. The
dashboard's existing factory gets an independent default manager automatically.

`bind_approval_dispatch(base, manager=...)` sets a `ContextVar` inside the
executing dispatch and resets its token in `finally`. This combines explicit
lifetime ownership with a small internal binding that reaches existing tool
handlers in executor workers. Passing a manager through handler kwargs would
break valid handlers that accept only their argument dictionary; intercepting
terminal separately could bypass a registry replacement. The chosen binding
does neither: registry/loop APIs, handler signatures and original arguments
remain intact. Tool JSON and handler kwargs cannot select the manager.

`ApprovalManager`, `bind_approval_dispatch` and `require_approval` are exported
from `lohra.tools`. The legacy `approval` object remains importable, but setting
it no longer grants ambient authority. Embedders that intentionally share it
must pass it explicitly to the binder or session dispatcher. Direct unbound
dangerous calls deny; commands outside the heuristic denylist retain their
existing behavior. This intentional compatibility change is documented in
spec 02, including an explicit binding example.

Exact-command session/always caching and once semantics are unchanged. A live
Agent's compaction continuation retains its dispatch and manager. A new CLI
invocation or database revival constructs a new manager, even with the same
persisted ID. There is no global map keyed by session IDs and no durable grant.

## Concurrency and guards

Tests use barriers/events, real batch executor workers, nested dispatches and
single-worker reuse. They verify independent yolo/default-deny consumers,
restoration after ordinary exceptions, `KeyboardInterrupt` and `SystemExit`,
queued cancellation, exact-command caches, invalid/failed callbacks and reset
of another consumer. A blocked callback does not hold the manager's lock.

Real canonical child factories, serve tool filtering and workflow sandbox
wrappers still reject dangerous commands under a parent yolo binding, including
tainted workflow execution. JSON/no-input consumers do not prompt. A local
registry handler accepting only its original dictionary demonstrates API and
argument preservation, including forged authority fields.

Cancellation of a queued call installs no binding; an executing call restores
the previous context when it exits. This does not interrupt an already-running
callback or shell process and does not implement issue #119. The approval
denylist remains a heuristic speed-bump, not filesystem/process isolation. No
interactive gateway queue, durable permissions or frozen-prompt change is added.

## Validation

From `backend/`, use the matching `/tmp/lohra-wave10-py311/bin/python` or
`/tmp/lohra-wave10-py313/bin/python`, with that runtime first in `PATH` and
`PYTHONPATH` pointing absolutely at this worktree's `backend`:

```text
python -m pytest tests/test_approval*.py tests/test_tools_terminal.py \
  tests/test_cli.py tests/test_auth_preference_consumers.py tests/test_gateway*.py \
  tests/test_server_agentic.py tests/test_delegate_task.py \
  tests/test_agent_delegate_scope.py tests/test_workflow_sandbox.py \
  tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py \
  tests/test_workflow_taint.py tests/test_workflow_tools.py tests/test_loop*.py \
  tests/test_list_models_tool.py tests/test_sup05_dead_turn_and_cli_notifier.py \
  -q --no-cov -p no:cacheprovider --tb=short
```

| Runtime | Focused result |
| --- | --- |
| Python 3.11.15 | 470 passed, 1 warning, 7.62s |
| Python 3.13.5 | 470 passed, 1 warning, 7.95s |

The warning is the existing Starlette/AnyIO `BlockingPortal` deprecation.
Local runs also loaded `/tmp/lohra129_no_shell.py` as a pytest plugin, rejecting
`os.system` and shell process launches through an audit hook. The separate
process control launches Python directly; terminal execution remains fake.
No provider, personal profile, personal database or real shell tool was used.

Directed coverage (`tests/test_approval*.py tests/test_tools_terminal.py`) passed
**83 tests**: approval module **100%**, terminal **92%**, session equipment
**84%**, aggregate **90%**. Uncovered branches are unrelated terminal `OSError`
and notifier fallbacks. `python -m ruff check .` and `git diff --check` pass.

The production change spans five files: approval binding, terminal consumption,
session equipment, CLI ownership and public exports. No dependency/version,
registry, scheduler or model route change is required. Full-suite CI and review
of the final integration SHA remain the coordinator's checks.
