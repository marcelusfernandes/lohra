# Relay terminal states and usage provenance — #133

Author implementation report, 2026-09-14, by the delegated author
`/root/issue_111_impl`. This is implementation evidence, not independent review.
Worktree `task-133`, branch `codex/task-133`, integrated base
`256af163341ff425b6e214dec1e0d31621915f10`.
[Issue and acceptance criteria](https://github.com/marcelusfernandes/lohra/issues/133),
[authorized claim](https://github.com/marcelusfernandes/lohra/issues/133#issuecomment-5669217336),
[header-phase clarification](https://github.com/marcelusfernandes/lohra/issues/133#issuecomment-5668892232).

The public candidate `b21507a` was subsequently rejected by independent review
for single-response empty-part identity (F1). The latest repair validation is
**372 PASS per runtime**, detailed in the final section; the earlier 672 + four
case history below remains evidence of the rejected candidate, not the repair.

**Original focused validation: 672 distinct cases PASS in each runtime**, Python
3.11.15 in 12.93 s and Python 3.13.5 in 13.20 s. There are 104 new/adopted cases
and 568 existing controls, not 1,344 different cases. Ruff and diff-check pass.
CI and independent review of the public final SHA remain coordinator gates.
A later documentation clarification adds four directed cases and reruns three
existing controls (seven PASS per runtime); see the receipt-completeness section.
It does not represent a single 676-case execution. That documentation-only
follow-up kept production byte-identical to 95cc405.
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
or missing last measurement leaves the known aggregate as a floor. Wire errors
and interruptions expose a floor rather than a final successful bill. The loop's
receipt-completeness bit can remain true on interruption between calls or during
a tool when all provider receipts are present; it is not turn completion.
`usage_uncertain` retains its existing missing-receipt interruption meaning;
this change neither rewrites the five-axis ledger nor
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

## Original focused acceptance coverage and commands

| AC | Principal tests (under backend/tests) |
|---|---|
| 1 explicit termination | relay_sdk, relay_states, relay_boundaries no-call; existing service and native_outcome suites |
| 2 Responses/refusal coherence | relay_parts: native truncation, pure/mixed refusal, two text parts, Chat orders, Anthropic text/reason; relay_states unknown cause |
| 3 failure phase / one terminal | relay_boundaries actual ASGI prefix gates before/after a model delta; relay_sdk/states, existing app, stream lifetime and EOF surfaces |
| 4 missing/floor provenance | relay_information both orders and reported zero; relay_usage JSON/SSE errors and downstream; interrupted_outcome |
| 5 disjoint numeric meters | relay_usage asymmetric five-axis roundtrip and 14 malformed/zero floor controls; existing transport/loop receipts |
| 6 actual SDK consumers | relay_sdk/usage/parts/boundaries plus existing Responses SDK validators; real SDK HTTPX2 bytes and synthetic ASGI |
| 7 #116 ownership | preserved stream_bridge/lifetime/workers/sse_binding/runner_shutdown ownership controls, with the documented usage-expectation adaptation; typed UTF-8 queue control |
| 8 validation/docs | 672 each, Ruff, diff-check, spec §5.3; public CI and isolated review pending coordinator |

New/adopted module counts: SDK 14, information 8, states 22, usage 40, parts 11,
boundaries 9 = **104**. The other **568** cases cover the affected existing
server, loop, transport, client, native-outcome and EOF contracts. The same 672
nodeids were collected in both interpreters and are frozen in
`/tmp/lohra133-final-nodeids.txt`; `/tmp/lohra133-final-coverage.json` records the
partition. Collection is not another test execution. Earlier stages are repeated
or smaller subsets (and preserve old names where parameterization changed).

The exact 33-module selection is `/tmp/lohra133-focused-files.txt`. These were the
commands recorded for the original 672 on the production/test bytes frozen in
95cc405; later tests add four cases to one module. Use a new phase name to replay
without overwriting the historical logs:

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

## Post-freeze receipt-completeness clarification

The coordinator identified a mismatch between prose and `_result`: the prose
said the completeness bit was false on every interruption, while the expression
tests `usage_uncertain` and error, not `interrupted`. The minimum correct
alignment preserves the runtime expression and explains the distinct concepts.
Interrupting a tool or stopping before its dispatch after a measured provider
response does not invent missing tokens. CompletionService still rejects success
and exposes a conservative known floor (or unknown when no measurement exists).

Four new cases in `test_server_relay_boundaries.py` use the real Agent/loop,
service, ASGI app and OpenAI SDK with a synthetic upstream client: interruption
before dispatch versus during the tool, each with measured versus absent usage.
All make exactly one provider call and return an HTTP error. Both measured cases
have `interrupted=True`, `completed=False`, `usage_uncertain=False`,
`usage_complete=True` and the unchanged asymmetric five-axis receipt. Both
unmeasured cases keep `usage_complete=False` and unknown usage. The complete
receipt is therefore distinguished from successful turn completion without
changing the wire or financial ledger.

The directed selection also reruns the three existing #116 service controls for
before-run, first-stream abort and measured prefix followed by aborted stream.
`/tmp/lohra133-completeness-directed-{311,313}.{txt,json}` records **7 PASS** in
2.01 s / 2.13 s. This is **4 additional cases + 3 overlapping controls**, not
seven added cases and not a new execution of the original 672. There was no
runtime RED or production patch; the passing controls establish the behavior
that the original documentation described incorrectly. One preexisting
Starlette warning appears per runtime. Ruff and diff-check pass again.

The original 672-case logs, nodeids and manifest remain unchanged, including
manifest SHA256 `2a0c85a005f2f482a282c5c56c094760a47eb54b5820706ac44e1401969e1143`.
Across the two selections there are 676 distinct validated cases per runtime
(108 new/adopted + 568 existing), with no single 676-case run claimed. New
evidence/commit identity is `/tmp/lohra133-completeness-final-manifest.json`;
`/tmp/lohra133-completeness-directed-nodeids.txt` records the seven directed
nodeids, and `/tmp/lohra133-combined-nodeids.txt` records their union with the
original 672. The normal follow-up commit changes only the test, this report and
the spec; production remains byte-identical to 95cc405. No functional matrix was
repeated merely for the prose correction.


## Public review F1 repair: empty parts within one native response

The [independent CHANGES_REQUIRED review](https://github.com/marcelusfernandes/lohra/pull/156#pullrequestreview-5202461912)
was published on `b21507a38a42ef5a90378f12a47aef222af9a14e` before this repair.
Its F1 correctly identified that dropping silent part identity let the first
nonempty delta occupy a preceding empty part's slot. `finish` then associated
delivered lengths by position: it could change the slot's type, duplicate text,
or fail the real SDK snapshot accumulator. This is one native response, distinct
from the multi-call #155 limitation. Only the public review was read; no private
reviewer files or worktrees were opened.

The author wrote new upstream SDK/MockTransport → ResponsesClient → Agent/Service
→ ASGI → downstream OpenAI SDK `responses.stream()` regressions. Three public
shapes are tested independently: empty text before refusal, empty refusal before
text, and an empty refusal between two text parts. Three controls cover one text
part, nonempty text/refusal/text, and parts provided only by the terminal. Both
runtimes reproduced **3 RED / 3 PASS** on unchanged production at b21507a before
the patch; all six then passed. The tests now accumulate actual SDK snapshots,
compare added/type/index, concatenated deltas, per-part done, item done and final
content, validate SDK models, and retain native completed + reported 9/3 usage.

The minimal repair adds an opt-in `PartCallback` to the relay producer's existing
callback. The Responses assembler emits structural starts from added/done and
identified empty delta events only to that callback. The notification is an
immutable empty `OutputDelta`: the existing queue counts it as one item with zero
text bytes. No second queue/text buffer, new timer, thread, or header/data barrier
is introduced. ContentStream reserves the native kind/key before text arrives;
repeated starts reuse the same slot and emit no artificial empty text/refusal
delta. Chat ignores those structural notifications. Ordinary Python callbacks
still receive exactly their prior nonempty text/refusal deltas. JSON/create,
normalization, native authority, state/usage, history/replay and the prompt are
unchanged. This does not implement the #155 call namespace or aggregate projection.

The two new modules contain **24 distinct cases** (19 snapshot/legacy/Chat + five
lifecycle). Additional controls cover all-empty parts, empty parts at both ends,
added repeated, done-only, empty-delta identity, terminal-only parts, exact legacy
callback payloads and no public empty Chat delta. The SDK snapshot cases run with
queue capacity one item/four bytes. Separate direct bridge controls prove empty
markers consume capacity, a blocked producer wakes on close/cancel, terminal
publication bypasses a full queue, late markers are discarded and UTF-8 splitting
retains identity. Abort after a structural start still physically closes the
upstream SDK stream.

A separate direct ASGI test holds the real upstream SDK's completed event behind
an Event. Headers, response.created, part.added and refusal text are observed by
ASGI send while the producer receipt is still absent; only then is the terminal
released. This proves incremental delivery without using TestClient buffering
as latency evidence. Every synthetic producer and gate has bounded cleanup;
real SDK request/body-close counts and an empty worker inventory are checked in
finally, including semantic RED exits. The SDK fixture uses HTTPX2 (OpenAI 3.13.0,
HTTPX2 2.12.0); no live provider, socket or process is contacted.

### Preserved failures, adaptation and validation

| Phase | Python 3.11.15 | Python 3.13.5 | Distinct/overlap |
| --- | --- | --- | --- |
| Initial six author cases | 6 FAIL, 1.75 s | 6 FAIL, 1.92 s | 3 F1 + 3 harness failures |
| Corrected baseline, before production | 3 FAIL / 3 PASS, 2.79 s | 3 FAIL / 3 PASS, 2.29 s | same six |
| First patched six | 6 PASS, 1.59 s | 6 PASS, 1.69 s | same six |
| Expanded controls, first attempt | 20 PASS / 1 FAIL, 4.72 s | 20 PASS / 1 FAIL, 3.90 s | one new harness failure |
| Directed ASGI harness correction | 1 PASS, 1.40 s | 1 PASS, 1.62 s | overlapping case |
| All new controls | 24 PASS, 1.98 s | 24 PASS, 1.91 s | 24 distinct total |
| Final focused 22 modules | **372 PASS, 12.47 s** | **372 PASS, 12.75 s** | **24 new + 348 prior controls** |

The initial three control failures compared raw `ResponseOutputMessage` objects
to the SDK's `ParsedResponseOutputMessage` classes. The adaptation compares all
the same serialized item fields while excluding the SDK-only `parsed` field;
no output/type/index/usage/cleanup oracle was removed. The original test and logs
are frozen as `lohra133-empty-parts-red-test.py` and `*-red-{311,313}.txt`; the
corrected pre-patch version is `*-red-test-corrected.py` with `*-red-corrected-*`
logs. These are three harness failures, not six relay defects.

The direct ASGI first attempt searched for compact JSON bytes but `responses_sse`
uses JSON with spaces. Its observer now parses the data line and checks the exact
event type/delta. Production did not change for that correction. The original
`lohra133-empty-parts-lifecycle-harness-original.py`, failing `*-controls-*` logs
and directed `*-delivery-corrected-*` logs remain preserved. One Starlette
deprecation and 14 Pydantic warnings while serializing the SDK's generic parsed
completed models occur in the final focus (15 warnings, no skip/xfail). They
remain in logs; the model validation and semantic assertions pass.

All 348 prior controls also belong to the previous 676-case union; the additional
24 were not previously tested. The repair did not rerun all 676, and no single
700-case execution is claimed. Its final focus retains all 108 original/adopted
relay cases, plus service/app/Responses, #116 binding/queue/workers/ASGI ownership
and #117 client/EOF/abort/consumer controls. The original manifests, logs and
commits remain intact. No independent approval is inferred from these author tests.

Commands/source hashes are in `/tmp/lohra133-empty-parts-*-{311,313}.json`; logs
share those stems. `/tmp/lohra133-run-empty-parts.py` launches each absolute Python
with the task-133/backend PYTHONPATH, runtime bin first, PYTHONDONTWRITEBYTECODE=1,
temporary LOHRA_HOME/basetemp and HOME/CODEX_HOME preserved. The target list and
wrapper are `/tmp/lohra133-empty-parts-focus.json` and
`/tmp/lohra133-run-empty-parts-focus.py`. Final nodeids and overlap counts are
`/tmp/lohra133-empty-parts-nodeids-{311,313}.txt` and
`/tmp/lohra133-empty-parts-coverage.json`. Ruff on all backend passes in both
runtimes; diff-check passes. The committed SHA/tree, exact files and historical
artifact verification are in `/tmp/lohra133-empty-parts-final-manifest.json`.
Full-suite CI and another independent review of the new public SHA remain with
the coordinator. TestClient+MockTransport snapshot tests do not prove TCP
behavior or other SDK versions. The single-response repair preserves incremental
streaming; multi-call presentation remains the explicitly open #155 follow-up.
