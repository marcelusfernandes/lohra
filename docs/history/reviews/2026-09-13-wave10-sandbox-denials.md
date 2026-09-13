# Wave 10 — sandbox refusals are visible (#89)

Source baseline: `72a240b85ffc6b8b5cf13dad7430a7756c9ebf5d`, branch
`codex/task-89`. Implementation and validation are local; independent review
and integration remain with the coordinator. No provider call or paid model
was used, and no push, PR or merge was performed in this lane.

## Contract and scope

A leaf can receive a refused `write_file`, then answer only `done`. The run
must preserve the refusal even though the answer says nothing about it. A
successful run remains `complete`; the advisory does not change the sandbox
policy, taint handling, operator authorization, cache key or `derive_status`.

The implementation uses the existing sandbox → gateway `tool.complete` → core
observation path. `sandbox_dispatch` returns the same JSON text as a `str`
subclass carrying a closed reason and tool category in process. Ordinary tool
results cannot manufacture that marker with matching prose or JSON fields.
The string remains JSON serializable and supports copying. MCP refusals use
the bounded `mcp` category instead of exposing arbitrary server/tool names.

`DenialCounts` observes before the optional audit sink and returns independent
per-leaf `{tool, reason, count}` snapshots from `collect().sandbox_denials`.
Concurrent events use a leaf-local lock. `DenialTally` folds positive deltas
under the engine result lock, including nonterminal leaves before timeout or
cancellation closes the books. Earlier snapshots remain available in the
engine tally even after registry eviction; repeated reads and stale callbacks
cannot double count or reopen a sealed result. At seal, each node/tool/reason
group enters the existing advisory path. Nested folding and durable
`prior_advisory` carry it without a new durable schema or cache format.

The existing `tool.completed` audit event gains only
`data.result.denied=true` and a closed `reason`; content remains redacted.
`audit_query().sandbox` reports `{scope: "retained_snapshot",
denied_tool_calls: N}` independently of page/filter, respecting
`snapshot_seq`. The count is deliberately of retained observations, not a
reconstruction of the whole execution from warning text.

## RED before production changes

Command from `backend`, with explicit Python 3.12.10 and local `PYTHONPATH`:

```sh
PYTHONPATH=. /Users/marcelusfernandes/.pyenv/versions/3.12.10/bin/python3 -m pytest -o addopts='' tests/test_workflow_sandbox_denials.py -q
```

Result: **2 failed in 0.34s**, the same discriminator with audit on and off.
Both used `WorkflowService`, the real sandbox/core/gateway and a `ScriptedClient`.
The refused path never reached the underlying dispatch and no file was made.
The leaf answered `done`, the output was present and status was `complete`, but
`len(advisory_faults)` was **0 instead of 1**. The expected warning was:

```text
writer: 1 tool calls denied by sandbox: write_file — path is outside the workflow working scope (sandbox denied) (advisory)
```

## Validation

- **26 focused tests passed**, including the two RED cases; both new modules
  (`tools/sandbox_denials.py`, `workflow/denials.py`) reached **100% statement
  coverage, 65 statements**. `tests/test_workflow_sandbox_denials.py` exercises
  real service/core/gateway paths. `tests/test_sandbox_denial_metadata.py`
  covers the closed vocabulary, repeated/stale snapshots and retained ledger.
- **247 relevant tests passed in 25.94s**: the two new files plus existing
  `test_workflow_sandbox`, `test_workflow_audit`, `test_workflow_audit_query`,
  `test_workflow_audit_e2e`, `test_workflow_audit_contract`,
  `test_workflow_audit_resilience`, `test_workflow_account_race`,
  `test_workflow_artifact_advisory`, `test_orchestration_core`,
  `test_delegate_task`, `test_workflow_pipeline`, and `test_workflow_nesting`.
- `python -m ruff check lohra tests`: **all checks passed**.
- `git diff --check`: clean.

Discriminators include all nine reasons, read/write and taint variants,
preserved string JSON/copy behavior, false denial prose/JSON on an allowed
tool, forty refused calls across concurrent leaves/tools, per-leaf snapshots,
per-node/tool/reason counts, sink exceptions and audit disabled, real leaf
failure retaining `failed`, fresh-service resume over a reopened SQLite DB,
one nested call, timeout/cancel before leaf termination, late accounting,
retention loss, frozen snapshots and filters that select no events. Canary
paths, URLs, args and arbitrary MCP names do not appear in audit metadata or
advisory text. A completed cached writer is never spawned on resume and its
one denial event/advisory is not duplicated.

## Limits and review points

- No full-suite run, Windows, real provider, CLI/TUI live run or public
  integration was done in this lane. The coordinator owns the combined suite.
- The audit may contain fewer refusals than the advisory when off, truncated,
  dropped or unavailable. Historical events without the marker remain
  unclassified; zero retained markers does not certify zero refusals.
- A refusal observed only after the engine seals cannot revise its finalized
  rollup. A draining leaf can still append audit detail; the service already
  waits for core shutdown before persisting its final boundary. The tests
  preserve facts observed before interruption and prove late callbacks do not
  duplicate them. Process death before durable run persistence retains the
  existing crash/recovery limits.
- `ToolDispatch` stays `Callable[..., str]`; no public dispatch remodel or
  dependency is introduced. Collection adds a JSON metadata list. Counters
  are bounded by closed categories/reasons and the workflow leaf-lifetime cap.
- #90 changes nested call/cache identity on another lane. The new nested test
  asserts advisory/fault reconciliation without fixing the old template label;
  integration must preserve whichever namespace `fold_nested` uses.
