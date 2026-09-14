# Native outcomes before tool execution — #132

Author implementation report, 2026-09-14. This is not an independent review or
an exact-SHA CI verdict. Scope: [#132](https://github.com/marcelusfernandes/lohra/issues/132),
under parent #11 and the isolated-review policy in #102.
The [public claim/clarification](https://github.com/marcelusfernandes/lohra/issues/132#issuecomment-5668270153)
authorizes this slice after #117. Base: public
`6831b51aac5d0df437d7d40756bec9ca8f879310`; exclusive branch `codex/task-132`.

The 476-case matrix below belongs to the initial candidate subsequently rejected
by independent review; current repair validation (393 cases per runtime) is in
the final section, "Repair after the public CHANGES_REQUIRED review".

## Behavior and choice of boundary

The three native normalizers previously let unknown/absent reasons become a
successful stop; Responses calls overrode failed/incomplete status. Normalizing
outside the loop's catch also let a normalizer exception escape the structured
turn result. The forced-schema path could certify a call before any native gate.

A small `NativeOutcome` record now accompanies accepted `NormalizedResponse`
objects. Rejection uses the existing `ProviderCallFailed` family, extended with
optional native diagnostics and canonical Usage. This was chosen over a typed
rejected normalized result because `response.failed` already raises inside the
assembler: one exception path handles both origins, with normalization moved
inside the existing catch. It needs no invalid canonical finish, second failure
framework, route fallback, retry, or polling. Trusted custom Python transports
can still construct `NormalizedResponse` without native metadata.

Native records contain fixed scalar fields and protocol tokens limited to 128
ASCII characters. Unknown well-formed tokens remain diagnostic, absent values
remain absent, and malformed values become `<invalid>`. Error presence is a
boolean separate from its optional code. Payloads, headers and opaque error
messages never enter this record. The existing human message/code of streamed
`response.failed` remains intact in the error path.

Chat/Anthropic reasons must belong to their native vocabulary. Calls require a
tool reason even when the call is the forced answer. Responses calls require
`completed` and a function-item status of absent/None or `completed`; incomplete,
nonterminal and invalid function-item states cannot dispatch or certify output.
An error field that is not None contradicts success even if its code is absent.
Known incomplete text retains its cause with canonical length/partial behavior;
valid empty responses, refusal and Anthropic pause retain their existing roles.

The #117 SSE fallback remains: a known completed/incomplete event supplies
status only for an absent/None nested status. Invalid supplied status is never
erased. Conflicting completed/incomplete terminals retain the first diagnostic
and reject before effects; a coherent repeated terminal retains the last known
usage snapshot, without summing snapshots. `response.failed` still refuses
immediately with its failure metadata/message/code, preserving usage observed
earlier in that same stream when the failed event reports none. Abort after the
last callback still wins over completed/incomplete validation. Existing physical
stream close and SDK context-manager ownership are unchanged.

The accepted and rejected usage paths are mutually exclusive. A rejected call's
reported canonical five-meter vector is added once. `usage` is the latest call's
measurement (None if absent); `usage_total` is the known aggregate floor, also
after a failure or abort. It is **not proof of a complete bill** when a call
reported nothing. `usage_uncertain` retains its existing interruption meaning;
this change does not generalize the financial protocol or change #111/#112.
Quota classification and retry_after remain structural and use the existing
classifier, without starting another request.

Turn results and CLI envelopes expose native diagnostics directly, so a failed
second call cannot inherit a previous assistant's canonical tool_calls stop.
Accepted messages store an independent native dict in the existing SQLite
provider_data container. Forced output removes the synthetic tool_use while
retaining provider_data, including signed thinking/encrypted reasoning. Cold
replay proves those diagnostics do not become model input or alter the frozen
prompt. Rejection creates no synthetic assistant merely to persist an error;
its native diagnostic lives in the turn/envelope, not a new error-history table.

## Evidence provenance and TDD

Preparation remains frozen in `/tmp/lohra-132-author-preparation.md` (SHA256
`dc42bb01133be9efc0eeb019073a1714765ea1bca227f8132f5c0a03b331e2a0`), with
its 18-artifact manifest. Its 13 cases ran on public #117 candidate `893ebc5`,
6 RED / 7 PASS in each runtime (1.55 s / 1.62 s). The integrated base has the
same tree; the pertinent client/loop/types/transports hashes were reconciled
before editing. These are historical preparation results, not reruns on #132.
The coordinator published the attributed
[13-case evidence](https://github.com/marcelusfernandes/lohra/issues/132#issuecomment-5668174981).

The older 28 direct-normalization scenarios, six real-SDK Responses paths, and
three forced-cache scenarios originated on `bb191e6`. Their scripts asserted
observed defects; they were not rerun unchanged as supposed green tests. The
current modules deliberately express the desired outcomes and reuse their
scenario grids, not every old payload/side probe. The six SDK cases use real
SDK parsing again; the three cache cases now use SDK SSE bytes plus real
WorkflowService/SQLite. The 13 prepared tests were copied separately and their
oracles retained; the existing metadata-container tests are explicitly synthetic
container probes, supplemented by actual normalizer-to-cold-storage tests.

| Stage | Python 3.11 | Python 3.13 | Attribution |
|---|---:|---:|---|
| New controls before production | 35 RED, 0.22 s | 35 RED, 0.25 s | 14 calls/reasons, 6 event/payload contradictions, 15 invalid values; some unhashable values escaped as TypeError outside the old catch |
| First implementation focus | 80 PASS, 1.61 s | not run | Includes the 13 prepared cases; overlapping evidence |
| First existing-contract focus | 189 PASS / 5 FAIL, 3.85 s | not run | Three deliberately removed permissive contracts; two provisional regressions to streamed failure prose |
| Completed plus non-null error | 6 RED, 0.13 s | 6 RED, 0.15 s | Coordinator source hypothesis, reproduced before correction |
| Conflicting/repeated terminals | 3 RED / 1 PASS, 0.15 s | 3 RED / 1 PASS, 0.16 s | First authority was replaced; an absent later usage erased a known receipt |
| SDK/cold/usage consumer focus | 26 PASS, 2.09 s | not run | New wire and receipts modules, overlapping final cases |
| Intermediate combined focus | 390 PASS / 2 FAIL, 5.30 s | not run | One exact-history expectation needed additive metadata; one leftover assertion raised NameError during test adaptation |
| Forced reasoning cold replay | 2 RED / 4 PASS, 0.23 s | 2 RED / 4 PASS, 0.29 s | Plain forced replacement discarded provider_data; retain the already captured container |
| First runner attempt at larger focus | 472 setup errors, 14.54 s | 472 setup errors, 10.84 s | Author runner omitted parent directory for --basetemp; **zero test bodies executed** |
| Corrected runner focus | 474 PASS, 7.21 s | 474 PASS, 7.56 s | Before the last same-stream failed-prefix discriminator |
| Failed terminal after known receipt | 1 RED / 1 PASS, 0.14 s | 1 RED / 1 PASS, 0.15 s | Last failed event without usage erased earlier same-call measurement; explicit failed usage already passed |
| Final focused matrix | **476 PASS, 6.95 s** | **476 PASS, 7.31 s** | Final production and test source; one inherited Starlette deprecation warning each |
| Mistaken collection phase | 476 PASS, 6.94 s | not run | Runner initially recognized only the exact phase `collect`; `collect-final` reran the same set unintentionally. Preserved and excluded from unique counts; corrected collection uses `--collect-only`. |

All intermediate counts overlap the final set. They are not additional unique
tests. The broken runner and the NameError are author harness/adaptation errors,
not hundreds of runtime defects. The first Ruff run also found two unused test
imports; those were removed. Original failed logs and frozen RED modules remain
in `/tmp`; no result or original preparation artifact was overwritten.

Existing tests changed only three permissive-normalization expectations
(unknown Chat/Anthropic reason and empty Chat choices) plus one full-history
expectation that now includes native metadata. No artificial terminal was added
to turn an invalid response into a valid one. The existing failure-message,
stream ownership, interrupt, quota and usage assertions remain active.

## Final coverage and acceptance matrix

There are **123 new cases + 353 existing cases = 476 distinct nodeids** in the
focused matrix. New modules: contract 81, SDK/cache 16, wire 14, receipts 12.
Of those 123, 50 adopt the 13 + 28 + 6 + 3 historical/prepared scenario sets;
73 are additional discriminators/controls. The unchanged 54 #117 cases are part
of the 353 existing cases. A second runtime and reruns do not add unique cases.

| AC | Discriminating coverage |
|---|---|
| 1: bounded native metadata, separate canonical/replay state | `test_bounded_metadata_does_not_copy_payload_or_error_text`, invalid/absent values, actual cold metadata tests, reasoning preservation |
| 2: native vocabulary and terminal authority | Responses text matrix; Chat/Anthropic reason matrix; six real Responses SDK paths; valid pause/refusal/truncation; #117 marker controls |
| 3: calls/forced never override invalid state | Native calls/reason matrix, item-status SDK matrix, completed+error, terminal conflicts, real Chat/Anthropic forced JSON and SSE |
| 4: refuse before assistant/tools/cache | No-effect and user-only histories; five current forced-cache cases including completed control and incomplete/failed terminals; actual WorkflowService and cold SQLite |
| 5: usage once, absent versus floor | Two-call SDK failed receipts, canonical five-meter exception + native four-meter prefix, absent/abort/ordinary error/accepted-absent controls; repeated/failed stream receipts; existing quota/post-drain tests |
| 6: turn/envelope/cold serialization without new model input | Failed second-call native diagnostic, no inherited tool stop, six actual cold accepted/forced cases, three preserved container probes; unchanged request builders/prompt identity |
| 7: minimum model/error representation | One ProviderCallFailed family, one normalization catch, optional accepted NativeOutcome; no new retry/poll/fallback or outcome framework |
| 8: tests/docs/gates | 476 cases per Python, Ruff and diff-check; this report/spec; coordinator owns full CI and isolated exact-SHA review after publication |

## Reproduction and limits

Exact working source:
`/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-132/backend`.
Final commands and all source hashes are in `/tmp/lohra132-verified-311.json`
and `/tmp/lohra132-verified-313.json`; stdout is in the sibling `.txt` files.
The reusable runner is `/tmp/lohra132-run-focus.py`:

```sh
/tmp/lohra-wave10-py311/bin/python /tmp/lohra132-run-focus.py 311 verified
/tmp/lohra-wave10-py313/bin/python /tmp/lohra132-run-focus.py 313 verified
/tmp/lohra-wave10-py311/bin/python -m ruff check backend/lohra backend/tests backend/ci
git diff --check
```

The runner preserves HOME/CODEX_HOME, filters provider environment variables,
uses absolute PYTHONPATH and runtime-bin-first PATH, disables bytecode/cache,
and creates temporary LOHRA_HOME/pytest directories. New tests block external
DNS/socket connects/Popen and use SDK MockTransport, synthetic keys/payloads,
closed HTTP clients and joined service workers. SDKs: OpenAI 3.13.0, Anthropic
1.5.0; new SDK paths use HTTPX2 2.12.0. Existing #117 controls also exercise
supported legacy HTTPX 0.28.1 with OpenAI. Those classes are not aliases, and the
legacy path is not an invalid harness. Installed SDK annotations/source were
reconciled with the coordinator's preserved contract artifacts; permissive SDK
parsing is not itself authorization to act.

`/tmp/lohra132-final-nodeids.txt` is the distinct case list;
`/tmp/lohra132-final-manifest.json` records final commit/tree, file and artifact
hashes. RED logs use `new-red`, `error-red`, `repeat-red`, `forced-metadata-red`
and `failed-prefix-red`; the failed directory-setup attempt remains named
`focus-final` and is explicitly **not** the final green matrix. The final logs
are `verified`. The corrected `collect-nodeids` command uses `--collect-only`;
its command/collection output is preserved separately.

This is local macOS synthetic-byte evidence, not a live provider or native
Linux result. Full pytest and independent review remain the coordinator's gates.
There is no claim of broader native HTTP translation (#133), retroactive cache
revalidation, error-history schema, new capability fingerprint/#75 policy,
pricing/accounting protocol, auth, route/catalog, prompt, dependency, or release
change. Cold replay demonstrates records generated by this contract; historical
cached successes retain existing replay behavior and are not recomputed here.
No private reviewer material was consulted and no independent approval is inferred.

## Repair after the public CHANGES_REQUIRED review

The preceding 476-case matrix and manifest describe the first author candidate
`eee3d53a529c8206fd55e3d31dbb14ddee1d15e8`. With coordinator-only STATUS/CHANGELOG
it became public `33ce7c33ee564aa78e44823e160b906b98e3f433`, tree
`d9a851d23639c616e610e97d3cbeb2233e4d932f`. The
[public independent review](https://github.com/marcelusfernandes/lohra/pull/154#pullrequestreview-5201444920)
returned CHANGES_REQUIRED before this repair. Its two findings are attributed
to that reviewer, not represented as author discoveries. The coordinator
reported both original CI jobs green (4847 PASS / 5 SKIP); that does not negate
the new authority failures or approve either candidate.

F1 showed that `completed` plus a supplied `incomplete_details.reason` still
permitted ordinary dispatch and forced extraction. F2 showed that terminal
`response.output` could replace an earlier `output_item.done` function item
whose status was incomplete. The original normalizer then saw only the
replacement's permitted status. The author read the full public report,
including its independently documented fixture-alias correction, without
opening the reviewer's private tests, logs, worktree or manifests.

### Reproduction and narrow change

A new author module, `test_native_outcome_authority_repair.py`, uses its own
HTTPX2 MockTransport/real OpenAI SDK fixture and exact task-132 source. JSON is
a genuine SDK nonstreaming create; SSE runs through ResponsesClient. The event
builder creates separate done-item and terminal JSON values and checks that the
contradictory statuses differ before delivery. It does not reuse the reviewer's
fixtures or simulate the future implementation.

Before editing production, 28 cases on the rejected SHA produced **19 RED /
9 PASS per runtime** (3.11: 1.24 s; 3.13: 1.26 s). Eight F1 cases cover JSON/SSE,
ordinary/forced and reported/absent usage; eight F2 cases cover done incomplete
replaced by completed or optional status, ordinary/forced and reported/absent
usage. Three additional REDs exercise the same F1 with supplied malformed or
oversized reasons. Ordinary failures demonstrate a real synthetic callback
effect; forced failures demonstrate certified `{"n":7}`. Requests and physical
body closes are asserted before those failing semantic assertions.

The nine positive controls cover permitted/optional function-item status and
terminal output, absent/null incomplete cause, incomplete text with encrypted
reasoning, and abort in the last callback. No assertion, payload or fixture was
changed to turn a RED green. Original source and both logs remain frozen under
`/tmp/lohra132-authority-repair-original-red.py` and
`/tmp/lohra132-repair-red-{311,313}.{txt,json}`.

Production changes only two modules. The existing Responses validator rejects
completed with a non-absent captured incomplete cause, including the bounded
invalid token. Details without a cause remain absent. The assembler captures
the first invalid function-item status as a bounded immutable scalar from
both done events and terminal output snapshots. Later replacement, removal of
the status or disappearance from final output cannot erase it. Text and
reasoning items do not enter this check. The refusal occurs after stream drain
and the final abort gate, using the existing exception/usage path; this neither
sums snapshots nor rejects immediately before a later reported receipt arrives.
Top-level failure behavior, native vocabulary, meter normalization, and the
normalizer's accepted optional item status are unchanged.

The first narrow GREEN was **28 PASS** (3.11: 0.95 s; 3.13: 1.04 s). Two further
controls then exercised first-violation retention across multiple done events
and repeated terminal outputs, preserving known usage when later snapshots
omit it. Those two controls were added after the patch, so they are not claimed
as additional pre-patch REDs. Their first executions are in the final focus.

### Final repair validation and limits

The repair focus is **393 distinct cases per runtime**, all passing: 3.11
4.66 s; 3.13 4.80 s. Partition: **30 new repair cases + 123 prior #132 cases +
240 other existing cases**. The 30 consist of the original 28 plus the two
first-violation controls. Compared with the preceding author 476-case matrix,
323 nodeids overlap; the 70 newly selected nodeids are 30 new tests and 40
already-existing `test_client` controls. This is a focused repair matrix,
not a rerun of all 476 and not 393 additional unique product tests. All original
28-case RED/GREEN runs overlap the 30 new final cases. Independent review counts
are not included. There were no skips, xfails or functional harness errors in
this repair; one inherited Starlette/AnyIO warning appeared per focused run.

The focus includes all four original native modules, the Responses transport,
client/loop, forced output/envelope, stream abort and all four #117 EOF modules.
It preserves previous quota/native-failure, cold SQLite/cache, reasoning replay,
known/absent usage and physical-close controls through those modules. The new
SDK receipt is disjoint `(8 input, 7 output, 3 cache-read, 0 cache-write, 2 reasoning)`;
cache-write remains zero for this Responses fixture, so it is not a new claim
of all-five-nonzero native parsing. The existing canonical five-meter receipt
controls remain in the selected set. There is no new workflow-cache execution
specific to F1/F2: the new ordinary/forced checks prove refusal before assistant
and extraction, while previous real cache controls cover that consumer boundary.

Commands, test/source hashes and stdout:
`/tmp/lohra132-repair-focused-{311,313}.{json,txt}`. The runner is
`/tmp/lohra132-run-repair.py`; `collected` uses explicit `--collect-only` and
produces `/tmp/lohra132-repair-nodeids.txt` (393 unique entries). Ruff passed for
`backend/lohra backend/tests backend/ci`; `git diff --check` passed. The repair
manifest records original and final artifacts separately and checks that source
and tests at freeze match both final executions. Original #132 manifests/logs
are preserved, not overwritten.

No provider/network/process tool or personal profile was used. Environment
isolation, real SDK/HTTP close and temporary state follow the original author
contract. The graph lookup was unavailable for the named project in this
repair, so exact source was read from the authorized worktree. No #133 relay,
ledger, retry, request builder, dependency, version, schema or prompt change was
made. The public rejection remains historical evidence; full CI and a fresh
independent exact-SHA review of this repair belong to the coordinator.
