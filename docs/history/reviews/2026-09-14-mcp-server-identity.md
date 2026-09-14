# MCP server identity — issue #115

Author implementation and validation report, 2026-09-14. Scope: [#115](https://github.com/marcelusfernandes/lohra/issues/115), the registry-identity residual of [#7](https://github.com/marcelusfernandes/lohra/issues/7). The claimed source is `c904506f4955aa817a1cb89ccdde3281efaaf80e`, branch `codex/task-115`, in `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-115`. This report is not an independent review or a CI result.

## Change and boundaries

The existing frozen `ToolEntry.toolset` carries the original configured server. Policy loading now preserves exact identities, including case and dash/underscore distinctions; presentation names stay unchanged. Advertisement, preflight and final dispatch use the same provenance predicate. A registry dispatch captures an entry under its lock, checks the active guards against that entry, then invokes that same handler outside the lock. Registration after selection cannot substitute a different handler. A call already authorized against its captured entry may finish after that entry is deregistered.

A small private ContextVar stack binds guards inside the executing dispatch, intersects nested restrictions and restores the previous context even on BaseException. Calls still traverse the existing subagent, approval and taint wrappers. JSON/schema fields cannot grant provenance; unknown MCP names, missing provenance, default policy and taint deny. An MCP entry aliased as `terminal` satisfies both capability gates. Frozen tool definitions are snapshots, not live authority. The final guarantee covers chains reaching `ToolRegistry`: arbitrary embedder Python callbacks remain trusted, and a registry argument to preflight does not sandbox their own effects.

MCP listing preparation occurs outside the registry lock. Batch publication validates existing entries and earlier accepted entries before publishing any handler. Implicit foreign-owner collisions fail explicitly; builtin collisions retain the builtin, and same-listing tool-slug collisions retain the first original tool. Trusted explicit `override=True` remains supported, but authority follows the replacement entry. Publication invalidates availability caches/generation once. Server removal selects and removes ownership under one lock, avoiding stale name deletion.

`MCPManager` itself is unchanged. A failed connect preserves preexisting registrations and closes its failed session; no partial new listing is published. Refresh retains its existing nuke-and-repave behavior: remove the old listing before re-listing, propagate failure, retain the session for a later refresh, and do not promise restoration of an old snapshot. No runtime dependency, public tool rename, new policy file, OS sandbox or #130 author-time enforcement is introduced.

The effective-policy fingerprint adds `mcp_authority=registered_entry_exact_server`. Under #75, a known historical prefix-policy stamp yields a `policy_changed` replay advisory while retaining paid output, complete status and zero new leaves. NULL remains unknown and silent. The content cache key is unchanged. These compatibility discriminators do not claim #75 was broken under its unchanged former semantics.

## Acceptance evidence

| #115 criterion | Focused evidence |
| --- | --- |
| 1. Registry provenance independent of presentation | `test_mcp_identity`: authored schema/args cannot supply provenance; exact registered owners and public-name collisions |
| 2. Exact grants, ambiguous slugs and tool names | distinct dash/underscore and case servers; first original tool retained within one listing; positive same-server tool underscores |
| 3. Definitions and dispatch agree; no JSON grant | forged provenance, stale definition refilter, three MCP-as-terminal cases; existing sandbox/tool integration fixtures now use real registrations |
| 4. Default deny, taint, legitimate opt-in | existing sandbox, workflow tools, taint and typed-denial suites; real subagent/approval/taint wrapper chain |
| 5. Dynamic collision handling | implicit cross-owner rejection, trusted explicit override, connect/refresh partial-collision tests, direct mixed-owner batch rejection |
| 6. Compatibility, fingerprints and replay | five `test_mcp_policy_replay` cases; same-server refresh, unchanged public names, known and NULL stamp reopen controls |
| 7. Validation and independent review | both local Python versions and Ruff pass as detailed below; required CI and independent final-SHA review remain coordinator gates |
| 8. Entry/handler identity under updates and concurrency | held wrapper rebind, held final guard after entry capture, entered-handler control, concurrent owners, nested handler restrictions, deregister after-unlock contrast |
| 9. Original identity survives policy loading | real temporary policy file plus environment merge; exact identity fingerprints and canonical order/dedup control |

## RED provenance and harness corrections

The five original public probes were already reconfirmed against `58b2584829f3d7a9f273c25d9e5ad06aa70b3567` in both interpreters. They were not repeated at claim: a source comparison confirmed that `c904506` changed neither `backend/lohra` nor the existing MCP/sandbox tests relative to that base. Original script and observations remain in `/tmp/probe_issue115_registry_identity.py` and `/tmp/lohra-115-current-58b2584-py{311,313}.jsonl`.

The prepared 18 cases on that verified source have nine desired-contract REDs and nine passing controls: six enforcement failures and three discriminators for the corrected policy semantics. Initial `/tmp/lohra-115-author-tests-py{311,313}.txt` recorded six enforcement REDs, two compatibility REDs, two harness errors and eight passes (1.70/1.69 s). The harness incorrectly called `db._connection()` rather than accessing the Connection attribute. The original module and logs are preserved; only connection access/cleanup changed. The corrected replay pair in `/tmp/lohra-115-author-replay-py{311,313}.txt` produced one compatibility RED and one NULL control pass (1.16/1.24 s). Those two cases overlap the original 18.

Before production edits, new lifecycle probes recorded three runtime REDs (concurrent deregister and connect/refresh partial collisions) plus one missing-new-seam import failure in `/tmp/lohra-115-lifecycle-red-{311,313}.txt`. The import failure is scaffolding evidence, not a fourth runtime defect. Two additional nested-handler cases failed in `/tmp/lohra-115-nested-red-{311,313}.txt`: a broader inner guard could widen its outer policy. The prepared exception-restoration control was adjusted so the outer policy also permitted the inner target; this ensures it reaches the intended raising base while preserving exception/restoration oracles under the newly required intersection rule.

Initial implementation passed these 24 new cases in `/tmp/lohra-115-new-green-{311,313}.txt` (1.23/1.31 s). Coordinator inspection of the uncommitted candidate then led to concrete additional contrasts:

- `/tmp/lohra-115-alias-red-{311,313}.txt`: two failures and two passes. A terminal early return exposed an MCP alias although dispatch denied it; the provisional implementation also rejected the existing trusted explicit-override API. Both were repaired with unchanged behavioral assertions.
- `/tmp/lohra-115-listing-red-{311,313}.txt`: one failure. Provisional broad connect cleanup erased preexisting same-server entries when `list_tools` failed before registration. Atomic batch publication replaced that cleanup, and `manager.py` returned to its original implementation. Two further controls cover preserved generation on rejected publication and cache invalidation/builtin retention on accepted publication.
- `/tmp/lohra-115-batch-red-{311,313}.txt`: one failure after the main focused matrix. Direct mixed-owner entries sharing a name could overwrite within an initially empty batch. Validation now checks earlier accepted entries as well as the registry. The final six-case batch/connect/refresh rerun passed in both interpreters; no full matrix repetition was needed for this one-line correction.

These latter failures belong to the author's provisional candidate, not the original baseline. Coordinator feedback is not independent-review approval. No output/denial/replay oracle was weakened to obtain a pass.

## Final local validation

There are **274 distinct cases observed overall: 32 new and 242 existing**. The 32 new cases are 17 identity, five policy/replay and ten registry-lifecycle cases. A collection-only inventory is preserved in `/tmp/lohra-115-final-inventory.txt`.

| Execution | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Main focused matrix before the final direct-batch fix | 273 passed, 6.62 s | 273 passed, 6.81 s |
| Final directed batch/connect/refresh rerun | 6 passed, 4 deselected, 0.11 s | 6 passed, 4 deselected, 0.14 s |

Matrix logs: `/tmp/lohra-115-focus-{311,313}.txt`. Directed final logs: `/tmp/lohra-115-batch-green-{311,313}.txt`. Five of the six final cases overlap the 273-case matrix, and one is new. This is not a claim of a single 274-case final matrix; interpreter repetitions, earlier RED/GREEN runs and the original five public probes are not added to the distinct count.

The matrix contains the three new modules plus existing `test_mcp_tools`, `test_mcp_manager`, `test_mcp_config`, `test_tool_registry_filters`, `test_workflow_sandbox`, `test_workflow_tools`, `test_workflow_taint`, `test_workflow_cache_policy`, `test_delegate_task`, `test_approval_dispatch`, `test_workflow_sandbox_denials`, `test_sandbox_denial_metadata`, `test_workflow_fetch_egress`, `test_web_search` and `test_smoke`.

Commands ran from the claimed worktree's `backend`, using `/tmp/lohra-wave10-py311/bin/python` and `/tmp/lohra-wave10-py313/bin/python`, each runtime's bin first in PATH, `PYTHONDONTWRITEBYTECODE=1`, absolute `PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-115/backend`, temporary `LOHRA_HOME` and fresh pytest basetemp directories. HOME/CODEX_HOME were preserved. Pytest used `-m pytest --no-cov -p no:cacheprovider --basetemp=<temporary directory> -q` followed by the listed modules. Ruff checked `lohra tests ci`; `/tmp/lohra-115-ruff-final.txt` records success. `git diff --check` passed.

The tests use stock registry, wrappers, manager, workflow service and temporary SQLite with synthetic callbacks and controlled Events/Barriers. There were no real provider calls, external MCP processes, personal state, native OS containment tests or model-behavior claims. Full CI and independent review of the frozen commit are separate integration gates. The documented trusted-embedder and refresh boundaries remain explicit.

## Coordinator CI repair — historical DNS control

The first public candidate `5ab87748fafbddb99d62bc6a16a96bc640609d1a` failed [CI run 34861348474](https://github.com/marcelusfernandes/lohra/actions/runs/34861348474). Python 3.11 completed with two failed, 4568 passed, five skipped, one warning, in 200.70 s. Python 3.13 was cancelled by matrix fail-fast; the wheel gates were skipped. [Preserved failure evidence](https://github.com/marcelusfernandes/lohra/pull/150#issuecomment-5666394996) identifies both parameters of `test_workflow_dns_stamp`: the expected current hash still included only the DNS marker, omitting the new MCP authority marker. The assertion failed before the historical replay section; it was not evidence of reexecution or cache corruption.

The coordinator updated only that test's expected **current** payload to include `mcp_authority=registered_entry_exact_server`. The exact pre-pinning `old_effective` payload, historical/NULL values, source output, no-respawn/no-DNS checks, unchanged cached row and advisory assertions are preserved. Runtime code and the fingerprint implementation are byte-identical to the first public candidate. This attributed test repair does not replace the author's original validation history above.

The two DNS cases and five existing MCP policy/replay cases passed in both local runtimes: seven passed in 1.51 s (3.11.15) and 1.73 s (3.13.5). Five overlap the preceding 274-case focus; the two DNS cases were outside that selection. Logs are `/tmp/lohra-115-dns-repair-py311.txt` and `py313.txt`; Ruff for the changed test and diff-check pass. Original CI logs remain in `/tmp/lohra-150-ci-5ab8774-full.log` and `-failed.log`. No broad local suite was repeated. The corrected public SHA still requires a fresh complete CI run and exact-head independent review before merge.
