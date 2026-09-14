# Relay terminal states and usage provenance — #133

Author implementation report, 2026-09-14, by the delegated author
`/root/issue_111_impl`. This is implementation evidence, not independent review.
Worktree `task-133`, branch `codex/task-133`, integrated base
`256af163341ff425b6e214dec1e0d31621915f10`.
[Issue and acceptance criteria](https://github.com/marcelusfernandes/lohra/issues/133),
[authorized claim](https://github.com/marcelusfernandes/lohra/issues/133#issuecomment-5669217336),
[header-phase clarification](https://github.com/marcelusfernandes/lohra/issues/133#issuecomment-5668892232).

**Current local validation: 672 distinct cases PASS in each runtime**, Python
3.11.15 in 12.93 s and Python 3.13.5 in 13.20 s. There are 104 new/adopted cases
and 568 existing controls, not 1,344 different cases. Ruff and diff-check pass.
CI and independent review of the public final SHA remain coordinator gates.
A separate multi-call content-prefix limitation is recorded below; this report
does not claim it repaired.

## Contract and implementation

The service consumes the loop's explicit completion and stop reason. Null or
empty content no longer decides stop versus length/filter; failure, interruption
and no-call exhaustion cannot become successful empty responses. Chat preserves
stop/length/content_filter. Responses emits completed/incomplete/failed with
matching object status; only observed, supported incomplete causes are emitted.
A structural refusal cannot override native truncation.

Native output_text/refusal identity is retained independently of canonical text.
Chat canonical content/history remains unchanged, including null content and the
existing history conversion to an empty string. Responses retains its prior
canonical flattening. Native parts are transient result metadata, not extra model
input, prompt changes or a new durable replay schema. A string-compatible delta
retains type/key through the existing queue's UTF-8 splitting. The serializer
tracks part keys and delivered lengths, not another text buffer, and emits typed
added/delta/done/item events for the observed native response. The local Chat
stream message preserves first-seen part order without accepting a JSON field as
that internal marker. A single chunk with content and refusal, refusal preceding
text across chunks, multiple ordinary text parts and pure refusal are covered.

Anthropic text remains text when its final reason is refusal. The accepted
Responses result carries the fixed extension
`lohra_native_outcome={"api_mode":"anthropic_messages","reason":"refusal"}`;
it does not retroactively retype deltas or buffer the entire response. This is a
specific distinction between native content type and terminal cause, not a
framework for arbitrary provider payloads. Chat retains its native-to-canonical
content_filter mapping for that reason.

Usage is standard and numeric only when every call reported a complete receipt.
The new per-turn completeness bit is monotonic in both call orders: missing first
or missing last measurement leaves the known aggregate as a floor. Errors and
interruptions also leave incomplete usage. `usage_uncertain` retains its existing
interruption meaning; this change neither rewrites the five-axis ledger nor
introduces billing/retry policy.

- Complete: ordinary `usage`, no extension.
- Known floor: `usage: null`, `lohra_usage.status: "lower_bound"`, and `observed`
  with all five canonical disjoint meters.
- Unknown: `usage: null`, `lohra_usage: {"status":"unknown"}`.

The five floor axes are uncached input, output, cache read, cache write and
reasoning. A reported zero floor remains an observation, not a complete bill.
Malformed floors do not become observations. HTTP/SSE Chat errors carry the
annotation inside `error`, where the SDK exposes it; Responses failed carries it
on the response. Downstream Lohra accepts a valid floor once and retains its
incompleteness. The receipt still snapshots observed usage separately from the
nullable public mapping, including cancellation between service return and publish.

The author found that both existing Chat/Responses normalizers ignored the
relay's already-emitted cache_write detail. The asymmetric control is canonical
`[11,19,13,17,7]`: 41 inclusive prompt, 19 output, 13 cache read, 17 cache write,
7 reasoning. Subtracting read and write once preserves those values; two complete
calls emit 82 inclusive prompt. Reasoning is not added to output again. The
coordinator explicitly confirmed this narrow correction within AC5.

The existing immediate SSE prefix remains. Validation/admission can reject before
headers; provider failure after the prefix has one semantic error terminal even
if no model delta occurred. Sender, worker admission, cancellation, drain limits,
shared SDK ownership and the #116 CLI shutdown contract are unchanged. Legacy
Python services supplying their own mappings remain trusted embedders.

## Baselines, REDs and adaptations

The 24 historical cases on bb191e65 predate #116. Their scenarios are represented
by 12 direct cases in the adopted SDK cohort and the other 12 in states; old
aborted-success assertions were replaced by #116's established refusal and #133
provenance assertions. They were not rerun wholesale as a current baseline.

Coordinator preparation on 6831b51 had 14 cases, 9 RED / 5 PASS per runtime.
The author independently ran those same 14 on 33ce7c3 (9 RED / 5 PASS), plus three
observational PASS demonstrating information loss. These are overlapping
historical runs, not 31 new cases. The author then compared 61e8c8a by source only
(0 tests): only the Responses assembler and normalizer changed in the #132 repair,
and neither repaired predicate matched these fixtures. All 15 relevant hashes
matched again at the claimed integrated 256af base, recorded in
`/tmp/lohra133-claimed-source.json`.

The adopted `test_server_relay_sdk.py` retains the 14 semantic oracles from the
coordinator's formatted, AST-identical preparation. Cleanup now asserts the
worker inventory in `finally`, including failure paths. Original files/logs are
untouched. The original compact preparation had 12 Ruff E701/E702 errors;
those are historical formatting diagnostics, not current runtime failures.

Current implementation stages below overlap; do not sum their rows as distinct
coverage. Logs use `/tmp/lohra133-<phase>-{311,313}.{txt,json}`. Durations are pytest
seconds, in 3.11 / 3.13 order.

| Phase | Result per runtime | Seconds | Attribution |
|---|---:|---:|---|
| information-red | 4 RED / 2 PASS | 0.72 / 0.74 | Desired forms of the three information-loss observations, before production edits |
| core-green | 6 PASS | 0.66 / 0.67 | Initial completeness/native-parts seam |
| wire-red | 40 RED / 8 PASS | 1.61 / 1.65 | State, floor, downstream and five-meter regressions |
| first-wire | 68 PASS | 1.43 / 1.61 | Initial combined new/adopted cohort |
| parts-red | 6 failures | 2.23 / 2.30 | Five semantic failures plus one fixture mismatch, below |
| parts-corrected-red | 6 RED | 2.20 / 2.28 | Two part-sequence cases and four native-authority cases |
| parts-green | 6 PASS | 2.16 / 2.14 | Typed part framing and native-authority repair |
| anthropic-parts-red | 1 RED / 1 PASS | 1.54 / 1.61 | Native text cannot become a refusal part after its deltas |
| mixed-parts-red | 1 RED / 1 PASS | 2.24 / 2.22 | Chat callback order; two-text control already passed |
| parts-expanded-green | 10 PASS | 2.01 / 2.15 | Above sequence/cause controls combined |
| compatibility-first | 3 failures / 643 PASS | 12.13 / 12.34 | Three explicitly superseded legacy wire expectations |
| boundaries-first | 50 PASS | 3.11 / 3.15 | 9 new boundary controls + 41 existing controls/adaptations |
| chat-preservation-red | 3 RED / 1 PASS | 1.90 / 1.92 | Canonical content preservation and reversed stream-part order |
| chat-preservation-green | 1 failure / 18 PASS | 2.32 / 2.33 | History-oracle mistake described below |
| focused-final | **672 PASS** | **12.93 / 13.20** | Current complete focused selection |

The coordinator's local WIP reading supplied the native-refusal/truncation
counterexamples, missing sequence discrimination and Chat mixed-chunk ordering
question. `/tmp/lohra-133-root-refusal-authority-probe.json` records four pure
calls, two counterexamples and two controls, on source hash ee6bfd48…; it was not
an SDK test or independent review. The two-output_text observation was already
addressed in the prior WIP patch and passed its added control. These origins are
separate from the author's actual SDK RED→GREEN executions. Coordinator SDK-model
metadata in `/tmp/lohra-133-root-sdk-event-contracts.json` helped verify required
indices/sequence/logprobs and valid message status; that metadata itself executes
no requests and is not a GREEN.

Two test-fixture/oracle mistakes remain preserved rather than counted as product
defects. The initial Chat/SSE truncation-refusal fixture returned JSON to a true
stream request, so EOF masked the intended native-state check. It was changed to
synthetic SSE with nullable usage; unchanged semantic assertions then gave the
valid RED. Later, the author's null-content preservation assertion incorrectly
expected null in stored history, whereas the preexisting `_assistant_message`
uses an empty string. The corrected test compares the entire history against a
control call without refusal, and preserves normalized/final null assertions.
The original versions are `/tmp/lohra133-parts-original-red.py` and
`/tmp/lohra133-chat-preservation-test-before-history-fix.py`. No production change
was made for either harness mistake. A metadata-only nodeid extraction initially
expected a backend/tests prefix instead of pytest's tests prefix and selected
zero rows; it was corrected against the same collection logs without rerunning
tests. Its note is `/tmp/lohra133-nodeid-extraction-first-error.txt`.

The three legacy expectations changed in place, with their failure logs kept:
service character estimates become unknown/null; interrupted known Responses
usage moves unchanged to the explicit floor; ordinary EOF error zeros become
unknown/null. No error, finish, cancellation, physical-close or numeric-meter
oracle was removed.

## Acceptance coverage and commands

| AC | Principal tests (under backend/tests) |
|---|---|
| 1 explicit termination | relay_sdk, relay_states, relay_boundaries no-call; existing service and native_outcome suites |
| 2 Responses/refusal coherence | relay_parts: native truncation, pure/mixed refusal, two text parts, Chat orders, Anthropic text/reason; relay_states unknown cause |
| 3 failure phase / one terminal | relay_boundaries actual ASGI prefix gates before/after a model delta; relay_sdk/states, existing app, stream lifetime and EOF surfaces |
| 4 missing/floor provenance | relay_information both orders and reported zero; relay_usage JSON/SSE errors and downstream; interrupted_outcome |
| 5 disjoint numeric meters | relay_usage asymmetric five-axis roundtrip and 14 malformed/zero floor controls; existing transport/loop receipts |
| 6 actual SDK consumers | relay_sdk/usage/parts/boundaries plus existing Responses SDK validators; real SDK HTTPX2 bytes and synthetic ASGI |
| 7 #116 ownership | unchanged stream_bridge/lifetime/workers/sse_binding/runner_shutdown controls; typed UTF-8 queue control |
| 8 validation/docs | 672 each, Ruff, diff-check, spec §5.3; public CI and isolated review pending coordinator |

New/adopted module counts: SDK 14, information 8, states 22, usage 40, parts 11,
boundaries 9 = **104**. The other **568** cases cover the affected existing
server, loop, transport, client, native-outcome and EOF contracts. The same 672
nodeids were collected in both interpreters and are frozen in
`/tmp/lohra133-final-nodeids.txt`; `/tmp/lohra133-final-coverage.json` records the
partition. Collection is not another test execution. Earlier stages are repeated
or smaller subsets (and preserve old names where parameterization changed).

The exact 33-module selection is `/tmp/lohra133-focused-files.txt`. Replay command:

```sh
/tmp/lohra-wave10-py311/bin/python /tmp/lohra133-run-implementation.py 311 focused-final @focused
/tmp/lohra-wave10-py313/bin/python /tmp/lohra133-run-implementation.py 313 focused-final @focused
```

The runner invokes each runtime's `python -m pytest` with absolute task-133/backend
PYTHONPATH, its bin first on PATH, PYTHONDONTWRITEBYTECODE=1, isolated LOHRA_HOME
and basetemp, `--no-cov -p no:cacheprovider -q`. HOME/CODEX_HOME are preserved;
provider/auth variables are not forwarded. Functional fixtures block DNS, socket
connect and Popen, use synthetic keys/trust_env=False/no retries, and release/join
controlled threads in finally. The SDK/HTTP body close and final worker inventory
are asserted. The JSON records identify base HEAD plus hashes of actual edited
source bytes (and, for later runs, test bytes), not a claim that uncommitted edits
were already in that base commit. Final byte comparison against both final logs
passed before freeze. The external final manifest adds the committed SHA/tree.

Ruff: `/tmp/lohra-wave10-py311/bin/python -m ruff check backend/lohra backend/tests`
(`lohra133-ruff-final.txt`); `git diff --check` (`lohra133-diff-check.txt`). The
one pytest warning per runtime is the preexisting Starlette BlockingPortal alias
deprecation. There were no skips/xfails in the 672. Full-suite CI was not repeated
locally. Installed versions exercised: OpenAI 3.13.0, Anthropic 1.5.0, HTTPX2
2.12.0, HTTPX 0.28.1, FastAPI 0.141.1, Starlette 1.6.0; native new SDK probes use
HTTPX2. Existing controls retain their original compatible HTTPX family.

## Limits and follow-up boundary

Most SDK relay tests use TestClient-buffered ASGI output then 23-byte body
fragments. They validate SDK parsing/shape, states, parts, usage and physical body
cleanup, not TCP disconnect latency or all SDK releases. Separate controlled
ASGI tests and the existing #116 family cover delivery/cancellation/ownership.
No live provider, paid API, personal profile, real MCP/tool process or release
was involved. Native malformed-response validation remains #132; financial
acquisition/reconciliation remains #111/#112.

A coordinator follow-up probe on this WIP observed two-call deltas PREFIXFINAL
while the final response contains FINAL. Its initial result was one RED and one
control PASS (`/tmp/test_lohra133_coordinator_multiturn_parts.py`,
`/tmp/lohra133-root-multiturn-py311.txt`). The coordinator subsequently confirmed
the same 1 RED / 1 control PASS in both 3.11 and 3.13 on the public baseline tree
48fb4a8 and this WIP: the behavior is preexisting and requires a separate contract
between agentic calls. No production edit was made for it, it is not part of the 672,
and single-native-response part coherence does not claim multi-call prefix
reconciliation. The parent #11 remains open for its remaining scope.
The coordinator published [follow-up #155](https://github.com/marcelusfernandes/lohra/issues/155)
and the [public scope boundary](https://github.com/marcelusfernandes/lohra/issues/133#issuecomment-5669815066)
before this candidate was frozen. No #155 repair was executed here.

Original and final artifacts, source/test byte checks, nodeids and commit identity
are listed in `/tmp/lohra133-final-manifest.json`. Historical preparation manifests
remain separate and unchanged. STATUS/CHANGELOG and public integration remain
owned by the coordinator.
