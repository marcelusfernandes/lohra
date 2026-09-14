# #116 — bounded server stream lifetime

2026-09-14. Author implementation and local validation by the delegated Codex
author; coordinator findings are attributed below. This is not the independent
review verdict, a CI result, or provider/frozen-binary validation.

Claimed checkout: `lohra-wt/task-116`, branch `codex/task-116`, public base
`c1946c2eb0f584713984823948207a4e63ee9dcb`. Scope:
[#116](https://github.com/marcelusfernandes/lohra/issues/116), eight acceptance
criteria; parent #8 stays open. The implementation does not redesign the delivered
#126/#127 lifecycle, #130 tool guards, or provider/tool-process cancellation.

## Reconciliation and historical evidence

The read-only preparation on public `c4ca5e2` is preserved in
`/tmp/lohra-116-author-preparation.md`. At claim, the eleven relevant source hashes
in `/tmp/lohra-116-author-source-{311,313}.json` matched this checkout;
`/tmp/lohra-116-claim-reconciliation.json` records the exact imported service path.
#130's server agentic change was retained and covered in the final focus.

The coordinator-supplied original probes are historical executions on `c904506`:
`/tmp/probe_issue8_sse.py` (five failing disconnect/task-cancel discriminators and
one positive real-Agent interrupt control) and
`/tmp/probe_issue116_worker_lifetime.py` (one six-request ownership experiment).
Their `lohra-116-current-c904506-py*.jsonl` and
`lohra-116-workers-c904506-py*.jsonl` logs were not rerun or relabeled as this base.
They are seven distinct experiments, not fourteen from the two runtimes.

The author's two preparation modules produced **11 REDs per runtime** on `c4ca5e2`:
three interrupted-service outcomes and eight ASGI send-failure/late-binding cases.
Logs `/tmp/lohra-116-author-prep-{311,313}.txt` retain 6.16/6.26 s respectively.
Real Service/Agent/loop/assemblers used synthetic SDKs, including the 11-input/
5-output known prefix; all gated threads were released/joined, request streams
closed once, and the shared client remained open for a subsequent request.
Source identity justified adopting these tests without another automatic baseline.

## Resulting behavior

Four small server modules separate request cancellation, bounded delivery,
physical worker ownership, and ASGI response cleanup. The optional
`run_cancellable(cancellation=..., ...)` protocol binds the fresh Agent; the old
explicit `run(...)` signature remains valid, including the unchanged FakeService.
No signature introspection or TypeError retry is used. Interrupt callbacks run
outside locks and are required to be short cooperative signals.

Both SSE routes race sender and disconnect listener for ASGI 2.0 and 2.4. Request
cancel, failed response-start/body send, and disconnect signal interruption before
waking blocked producers. The async consumer never blocks on a synchronous queue
get. Queue limits are 64 pieces and 256 KiB of UTF-8 payload; Unicode-safe splitting
preserves concatenated text and empty deltas. Notifications coalesce, and terminal
publication uses a separate slot, including when the data queue is full.

Up to 16 producers include active, draining, and reserved launches. A published
receipt does not release a still-live thread. Capacity/shutdown rejection returns
JSON 503 before SSE/200; a refused start releases its reservation, while an observed
partial launch remains owned and cancelled. Response cleanup waits up to 250 ms;
inventory shutdown uses one collective 1 s deadline. Limits validate finite
nonnegative times, positive integer capacities, and at least four UTF-8 bytes.

Delivery's first disposition and the producer's final immutable receipt are
different. A live cancelled producer has no invented final receipt. Observed late
usage can enter its one receipt without reopening successful delivery. Interrupted
Service output is an UpstreamError subtype before empty-success/estimate mapping;
it preserves uncertainty and known floor. Responses failed uses nullable usage when
unknown, validated against the installed SDK model. A local provenance attribute
on the dict-compatible Service result is absent from wire JSON and prevents a late
cancel from promoting the ordinary success estimate to observed receipt usage.
The normal successful wire estimate remains unchanged for #133.

The coordinator found the actual CLI boundary after the first implementation:
`lohra serve` disables lifespan and directly closed the shared SDK; installed
Uvicorn also waits for active requests **before** lifespan shutdown, with unlimited
default grace. Merely enabling lifespan would not solve a silent active response.
The chosen fix preserves the historical PyInstaller workaround (`lifespan="off"`),
uses public `timeout_graceful_shutdown=0` to cancel active HTTP tasks at runner exit,
and calls the inventory drain explicitly in the CLI's `finally`.

The **1 s is the producer drain after the runner returns**, not a global deadline
for process, event loop, Uvicorn, SDK, or arbitrary host cleanup. The shared client
closes only with closed admission and an empty producer inventory. Cleanup selection
is idempotent and outside-lock invocation preserves visible callback errors without
implicit retry. If I/O outlives the drain, the CLI logs retained ownership and leaves
the client open for process teardown. There is no automatic reaper; an embedding
host can explicitly retry teardown after physical exit. Dashboard behavior is unchanged.

## Acceptance coverage

| AC | Discriminating tests in the final focus |
| --- | --- |
| 1: bounded HTTP lifetime | `test_server_sse_binding`: both routes/ASGI versions, silent disconnect, task cancellation, second cancel during actual drain, failed start/body send; `test_server_stream_lifetime`: cancellation while send is held and queue full |
| 2: request binding | late factory construction/return, sticky before/after bind, stale detach identity, separate concurrent request bindings, throwing callback, foreign-thread lock acquisition asserted outside the callback |
| 3: bounded delivery | four-byte Unicode capacity, full-writer cancellation, empty delta preservation, coalesced notifications, late-put discard, order/text preservation |
| 4: idempotent outcome/cleanup | completion-first/cancel-first, full-queue terminal error, immutable receipt/usage, duplicate publish/close, interrupted known floor and once-only stream close, host cleanup failure visibility and no duplicate callback |
| 5: honest interruption/wire | five real-service cases; 11/5 versus unknown late usage, ordinary service estimates unchanged, existing Chat/Responses framing and real SDK failed-event validation |
| 6: ownership/admission/shutdown | active + draining share slots; receipt published but tail held; refused/partial Thread.start; JSON 503; actual FastAPI lifespan collective deadline; CLI runner fake and installed Uvicorn Server.shutdown with no sockets |
| 7: delivered controls | #130 author-time consumers, server agentic restrictions, #42 stream-abort and before-dispatch tests, #127 inline-timer lifecycle test, #126 functional cancel/financial-fence tests, accepted queued child and once-only shutdown/on_done controls |
| 8: release gates | local Python 3.11/3.13 focus and Ruff passed; full required CI and independent exact-SHA review belong to the coordinator after author freeze |

The seven new worker tests use explicit gates and join every launched test thread.
The ASGI fixtures retrieve cancelled tasks, release factories/providers/send gates,
join recorded producers, and assert none remains alive. A bounded observation is
not described as forced thread termination. The controller lock test records the
acquisition result and asserts it **outside** the callback: ordinary callback
exceptions are intentionally caught, so an assertion inside it would be weak.

## Executions, failures, and counts

Final collection: **227 unique cases = 72 new + 155 existing controls**.
`/tmp/lohra116-final-collection.json` includes all node IDs and per-file counts;
`/tmp/lohra116-collect.txt` is collection-only, not another test run. The 72 new cases
are five interrupted outcomes, 24 binding/ASGI cases, 21 bridge cases, seven worker
cases, 12 wire/admission cases, and three runner cases. The historical 11 are a
subset of these, not additional cases.

| Execution | Result | Preserved log under `/tmp/` |
| --- | --- | --- |
| First implementation focus, 3.11 | 62 passed, 3.65 s | `lohra116-first-311.txt` |
| Bridge/worker/ASGI focus, 3.11 | 44 passed, 5.70 s | `lohra116-second-311.txt` |
| Wire/admission/service focus, 3.11 | 15 passed, 1.95 s | `lohra116-third-311.txt` |
| Runner/partial-launch/second-cancel focus, 3.11 | 20 passed, eight deselected, 3.61 s | `lohra116-fourth-311.txt` |
| First matrix before late-usage adjustment | 225 passed each; 9.91 / 10.01 s | `lohra116-focused-{311,313}.txt` |
| Final matrix, Python 3.11.15 | **227 passed**, 9.80 s | `lohra116-final-311.txt` |
| Final matrix, Python 3.13.5 | **227 passed**, 9.91 s | `lohra116-final-313.txt` |

These are overlapping selections/repetitions, not additive unique coverage. Both
matrix logs retain the existing Starlette TestClient deprecation warning. Ruff
passed for `backend/lohra`, `backend/tests`, and `backend/ci`
(`/tmp/lohra116-ruff-final.txt`); `git diff --check` passed.

Additional REDs concern **the evolving candidate**, not newly alleged public-base
bugs: sticky-before-wake (one RED, then one GREEN), partial-start ownership (one
RED), duplicate host cleanup (two REDs), empty delta preservation (one RED), and
late usage provenance (one RED plus the reported-usage passing control). Logs:
`lohra116-order-{red,green}-311.txt`, `lohra116-launch-red-311.txt`,
`lohra116-close-{red,green}-311.txt`, `lohra116-empty-red-311.txt`, and
`lohra116-late-usage-red-311.txt`. All repaired paths pass the final two-runtime focus.
The runner's two cases were also RED with **cli.py restored byte-for-byte from the
claimed HEAD while the new bridge remained present**; they failed at the missing
grace-setting assertion, before the later close-policy assertions. This was not a
whole-base run: `lohra116-runner-red-corrected-311.txt` (two failed, 0.28 s).
The first runner command used `tests/...` from repository root and collected zero;
that harness error remains in `lohra116-runner-red-311.txt`, excluded from counts.

Commands are preserved in `/tmp/lohra116-run-focused.py` and
`/tmp/lohra116-focused-nodes.txt`. Each interpreter used absolute task-116/backend
PYTHONPATH, runtime bin first in PATH, PYTHONDONTWRITEBYTECODE=1, temporary LOHRA_HOME,
`python -m pytest --no-cov -p no:cacheprovider` and a separate temporary basetemp.
HOME/CODEX_HOME were preserved. The installed Uvicorn source is 0.52.4; its real
`Server.shutdown` was exercised with a gated task and empty server/socket lists.
The first wrong-path command and all RED logs were preserved rather than rewritten.

## Limits

No provider inference, external MCP, server socket, real terminal tool, personal
profile, dependency change, infrastructure change, release, or full suite ran here.
Python versions were local macOS runtimes; Ubuntu CI, native Windows/Linux behavior,
frozen PyInstaller startup, SDK physical close latency, and a workload-capacity
benchmark were not validated. Existing frozen-binary rationale was preserved,
not re-proven. The queue cap excludes SDK/assembler/final-response memory.

Legacy services get delivery/ownership bounds but no Agent signal unless they
implement the optional protocol; their supplied usage remains an embedder contract.
Arbitrary Python callbacks remain trusted cooperative boundaries. Silent provider
I/O and tools already in flight can remain alive; no forced kill or durable receipt
after process exit is promised. Ordinary non-streaming lifetime, native reason/
usage normalization (#132/#133), and tool-process ownership (#119) remain separate.
