# Author-time entry scope — issue #130

Author implementation report, 2026-09-14. Scope: [#130](https://github.com/marcelusfernandes/lohra/issues/130), the metadata enforcement residual of [#7](https://github.com/marcelusfernandes/lohra/issues/7) following #84/#115. Claimed base: `c4ca5e25583ad67468a1d0ad13cae0247225c927`, branch `codex/task-130`, worktree `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-130`. CI and independent review of the final SHA are separate coordinator gates.

## Implementation

The small `tools/author_scope.py` predicate reads `ToolEntry.author_time_only`, never schema/argument JSON. Child definitions combine this metadata with legacy depth/stateful exclusions. Child dispatch binds the corresponding guard through #115's existing ContextVar stack, retains dangerous-shell auto-denial, and preflights the registered entry before an interceptor. The registry's final check judges the same captured entry whose handler executes. Server builders reuse these helpers and retain the explicit frozen allowlist, including the empty tool-less case. The leaf sandbox also applies the shared metadata predicate, including a custom Agent factory that reaches the registry without the default child wrapper.

Guards intersect across wrapper order and nested registry calls; exceptions/BaseException restore the previous token. An unguarded author, including a concurrent author thread, remains permitted. An already selected runtime entry can finish after replacement with author-only metadata; the newly registered handler cannot replace the captured handler. Definitions and system prompt snapshots remain unchanged after rebind. Runtime entries without the flag retain other consumer gates and the legacy builtin table remains pinned.

The optional `tool_registry` on child filter/dispatch helpers follows the existing #115 sandbox seam. Custom catalogs must be supplied consistently to preflight wrappers. Arbitrary embedder Python callbacks remain trusted: preflight can refuse a known marked entry before an interceptor, but does not sandbox that callback's own effects. The same-entry guarantee covers chains reaching `ToolRegistry`; no closure inspection, semantic capability framework or OS sandbox was added. Registry selection/stack implementation from #115 is unchanged; registry edits here document the metadata contract.

`author_time_scope="registered_entry"` participates in the effective-policy hash. It marks harness semantics, **not a durable inventory of individual registrations or metadata changes**. Known pre-#130 cells replay paid output with `policy_changed` advisory, complete status and zero respawns; NULL remains unknown. Content keys, published package version and historical stamp payloads are unchanged. The DNS-stamp test updates only its expected *current* payload; both historical known/NULL controls remain intact. Specs 02/07 and the builtin-inventory test documentation describe the new contract.

## Provenance and RED evidence

Preparation used provisional #115 candidate `5ab87748fafbddb99d62bc6a16a96bc640609d1a`. At claim, all 11 inventoried source/test files had identical SHA-256 hashes and `git diff` found no changes anywhere in `backend/lohra` against integrated `c4ca5e2`. The final #115 correction affected the DNS test/current expectation and reporting only. `/tmp/lohra-130-claim-reconciliation.json` records this comparison and the actual imported source path. Therefore the original 25-case baseline was retained without automatic repetition.

Both preparation interpreters measured default-policy hash `f4bb7c91d06ecfc8dd4dff5d4671ba2b104d728154342b2c937bda9f9b08e1f9`; the integrated base produced the same value. The literal historical payload in `test_author_time_replay.py` is frozen to that verified value. `/tmp/lohra-130-source-fingerprint-{311,313}.json` preserves the earlier candidate hashes; it is not relabeled as an integrated execution.

| Stage | Observed result per interpreter | Evidence |
| --- | --- | --- |
| Prepared 25 cases on `5ab8774` | 17 RED / 8 PASS, 1.69 s (3.11), 1.74 s (3.13) | `/tmp/lohra-130-prepared-{311,313}.txt` and matching JSON |
| Three additional guard lifecycle cases before production edits on `c4ca5e2` | 2 RED / 1 PASS, 0.24 / 0.29 s | `/tmp/lohra-130-lifecycle-red-{311,313}.txt` |
| Initial implementation: 28 new plus two DNS controls | 30 PASS, 1.63 / 1.73 s | `/tmp/lohra-130-new-green-{311,313}.txt` |
| Two old custom-registry MCP fixtures before catalog adaptation | 2 failures, 5.13 / 5.15 s | `/tmp/lohra-130-custom-registry-before-{311,313}.txt` |
| Final focused matrix | **317 PASS**, 7.19 / 7.40 s | `/tmp/lohra-130-focus-{311,313}.txt` |

The aggregate pre-implementation evidence for the 28 new cases is 19 desired-contract REDs and nine controls: 17 enforcement failures plus two prospective compatibility discriminators. The compatibility REDs do not allege an existing #75 bug under unchanged pre-#130 semantics. The three additional cases exercise wrapper exception/BaseException paths and an author concurrent with an active child; the two REDs show the nested marked handler ran before the exception, not a falsely attributed ContextVar restoration defect.

The two existing MCP fixture failures occurred on the provisional implementation. Their sandbox had an explicit synthetic registry, while the newly checking child wrapper defaulted to the stock singleton. The held-wrapper precondition failed and the positive call returned a denial. Passing that same synthetic registry explicitly to `subagent_dispatch` repaired the fixture composition; all rebind, taint, approval, depth and shell assertions remain. These are not two extra distinct tests or baseline enforcement findings.

No new harness errors occurred in #130 preparation/implementation. The older #7 probe's client/factory instrumentation errors remain recorded in `/tmp/lohra-7-author-metadata-triage.md`, separate from product failures. Its original probe and 67-test baseline were not rerun or added to this count. Preparation modules, logs and runner remain in `/tmp`; adopted tests changed only attribution comments before the three lifecycle cases were added.

## Acceptance and final validation

| Acceptance criterion | Evidence |
| --- | --- |
| 1. Marked entries hidden/refused in child/server/leaf; author allowed | Three real-consumer cases, direct author control, custom Agent leaf reaching the registry |
| 2. Runtime, legacy exclusions, terminal and server allowlist preserved | Unmarked runtime/schema controls, unmarked legacy memory/delegate names, synthetic dangerous/safe terminal, empty server after late registration; existing scope/delegate/server suites |
| 3. Same entry verified/invoked under mutation | Six Event-controlled re-register/deregister-rebind cases, captured-entry control, nested handlers and concurrent author; existing #115 identity/lifecycle tests |
| 4. JSON cannot forge metadata; explicit callback boundary | Opposite schema/argument flag values, marked opaque preflight refusal, documented trusted embedder limit |
| 5. Frozen definitions/prompt, readable revocation | Snapshot equality/object identity before and after all six rebind cases; concrete error envelopes |
| 6. Tests/docs/review | Focused matrix in both runtimes and Ruff passed; full CI and independent final-SHA review remain pending coordinator gates |
| 7. Effective fingerprint and #75 | Three new semantic/known/NULL tests, both DNS-stamp parameters, existing MCP-policy and cache-policy replay suites |

The final matrix has **317 distinct cases: 28 new and 289 existing** across 23 modules. The initial 30, lifecycle three and custom-registry two all overlap that matrix; interpreter repetition and historical RED runs do not increase the count. New modules contain 25 consumer/lifecycle cases plus three replay cases. Existing coverage includes delegate scope/task, server agentic, DNS stamp, MCP identity/replay/lifecycle/tools/manager/config, registry filtering, workflow sandbox/tools/taint/cache-policy, approval dispatch, typed sandbox denials, fetch egress, web search and smoke.

Commands used `/tmp/lohra-wave10-py311/bin/python` (3.11.15) and `/tmp/lohra-wave10-py313/bin/python` (3.13.5), runtime bin first in PATH, `PYTHONDONTWRITEBYTECODE=1`, and exact `PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-130/backend`. Pytest ran from that backend with `-m pytest <listed modules> --no-cov -p no:cacheprovider --basetemp=/tmp/lohra-130-focus-<runtime> -q`, isolated temporary LOHRA_HOME, and preserved HOME/CODEX_HOME. Ruff checked `lohra tests ci` (`/tmp/lohra-130-ruff-final.txt`); `git diff --check` passed.

Clients, tool handlers and network labs are synthetic; Service/SQLite replay and consumer builders/dispatches are real and temporary. No provider inference, external MCP process, real shell command from a tool, personal profile, dependency/version change or release was used. This scoped guard does not complete the broader capability model of parent #7. Coordinator feedback and author tests are not independent-review approval.
