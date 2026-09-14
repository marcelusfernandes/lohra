# #117 — terminal integrity at the stream assemblers

Author implementation report, 2026-09-14. This is attributed author evidence,
not an independent review verdict. Scope: [#117](https://github.com/marcelusfernandes/lohra/issues/117)
and its seven acceptance criteria, under objective #108. Implementation checkout
`task-117`, branch `codex/task-117`, integrated base
`22e75a046288662876a016fcd580933e0d2cb0fc`. No reviewer worktree or private evidence
was read. Publication, full CI and exact-head independent review belong to the
coordinator; no push, PR, merge, release or dependency change was performed here.

## Change and boundaries

Only `backend/lohra/agent/client.py` changes runtime behavior. Chat requires a
nonblank textual `finish_reason`; SDK `[DONE]` alone does not certify success.
Anthropic requires `message_stop` and a nonblank textual final `stop_reason`.
Responses requires an observed completed/incomplete terminal, deriving a missing
nested status from that event, and preserves the existing failed exception/code.
Unknown nonblank reason strings remain available to existing normalization;
#132/#133's broader native reason/status vocabulary is not implemented here.

All three folds check the same per-call abort gate after iteration, before EOF
classification. They continue draining after terminal observation, retaining
trailing usage. Chat/Responses retain finally-close; Anthropic retains normal SDK
context-manager ownership and explicitly closes on abort/error, including callback
exceptions. Real SDK tests count physical response-body closes, separately from
idempotent wrapper `close()` calls. Shared clients stay open across server requests.

EOF raises a protocol `ValueError` before normalization, assistant insertion, tool
dispatch, schema extraction or workflow cache acceptance. No loop, normalization,
accounting, lifecycle, publication, guard or prompt-building code changed. Actual
JSON `OpenAIClient.create`/`AnthropicClient.create` remain valid without SSE markers;
`ResponsesClient.create` really streams internally and receives the same guard.

Spec: `docs/specs/01-agent-core.md`, terminal-integrity section. The preserved server
boundary matters: loop and receipt usage are absent when unobserved; ordinary
Responses `response.failed` still carries legacy wire zeros. This is distinct from
#116's nullable interruption usage and remains #133, not an observed zero bill.

## Reconciliation and RED provenance

Read AGENTS, architecture, current issue and public
[author-preparation comment](https://github.com/marcelusfernandes/lohra/issues/117#issuecomment-5667751450).
Graph discovery was used for the assemblers and consumers. Graph source points to
the main checkout; exact worktree source was read and reconciled. `git diff` from
the prepared public PR152 head `ef87af01559cd02efe566423bd1edfc9f37c21a9` to the
integrated base is empty for `backend/lohra` (the whole Git tree is identical).

The coordinator's **seven SDK cases, 4 RED / 3 PASS per interpreter**, remain
attributed to `c1946c2` (2.49/2.36 s). The author's **seven create/abort cases,
3 RED / 4 PASS**, remain attributed to `ef87af0` (1.27/1.28 s). They were not rerun
as a new combined baseline. The older 14-test `84f62e` triage is a separate
historical selection and is not added to any count here. Six of the coordinator's
seven source hashes matched the preparation; loop.py differed only in #116's
docstring, with executable AST unchanged.

Frozen originals remain untouched:

- `/tmp/test_stream_eof_sdk_preparation.py`, SHA256
  `fbac78138636a8baaf3156b6260a4a77f1972db4444bc87a11a6abc445d29567`;
  adopted as `test_stream_eof_sdk.py` with explicit protocol-message assertions.
- `/tmp/test_stream_eof_author_preparation.py`, SHA256
  `5b11c26714947c1883ca8adcd7489baba5d5744a62bc38c059a695ed34488dad`;
  adopted as `test_stream_eof_create_abort.py` with the same stronger error check.
- Preparations, runner/source/version records and original logs named in
  `/tmp/lohra-117-author-preparation.md` remain available.

Before editing production on `22e75a0`, 20 additional boundary cases produced
**18 RED / 2 PASS** in each runtime (3.11: 1.11 s; 3.13: 1.43 s). These include
structural reason rejection, empty-EOF/final-reasoning-callback abort, real Chat
SDK `[DONE]` refusal with trailing usage, and incomplete-terminal status fallback.
The two passes were the valid Chat usage control and completed-status fallback.
Logs: `/tmp/lohra117-red-boundaries-py{311,313}.{txt,json}`. Client SHA256 before
production: `062881f4edeab08b8ce8f68a92eb1e7c17aac4356eb0a68012b5aee70f34ccb3`.
Two later unknown-nonblank-reason controls increase this module from 20 to 22;
they are not retroactively included in the RED count.

## Fixture adaptations and preserved failed runs

First production focus: **144 PASS / 7 failures**, 3.10 s on 3.11, log
`/tmp/lohra117-first-green-py311.txt` (the phase filename does not claim it passed).
These were exactly the seven protocol-incomplete cases inventoried beforehand:

| Fixture | Adaptation; original control retained |
| --- | --- |
| Chat reasoning callback | append stop; exact callback text |
| Chat empty completion | append stop; null content |
| Chat orphan tool, `finish=None` | expect protocol refusal; explicit-stop siblings still assert fallback/logs |
| Abort latch per call | first non-aborted stream gets stop; logging/isolation and next abort unchanged |
| Anthropic normal helper | append message_stop; final message/text/thoughts and standalone `closed == 0` |
| Concurrent subscription requests | terminal after the original Events; exact credential snapshots/client identity |
| Open subscription stream | terminal after credential mutation/delta; exact old/new headers and callback text |

After those adaptations, **151/151** passed in both runtimes (2.98/3.20 s), logs
`/tmp/lohra117-scaffold-green-py{311,313}.{txt,json}`. No final-content, credential,
ownership or callback oracle was removed. None-EOF success was explicitly changed
to refusal, not hidden by adding a terminal to that test parameter.

The first new surface run had **6 PASS / 9 failures** (3.11, 1.39 s), preserved in
`/tmp/lohra117-surfaces-first-py311.txt`. Eight were author harness errors: SessionDB
does not implement a context manager; `contextlib.closing` now guarantees cleanup.
The ninth was an overbroad new assertion of nullable ordinary-error wire usage;
source explicitly reserves that existing zero mapping to #133. The corrected test
pins both wire zeros and absent observed receipt usage, without production changes.
The repaired 15 cases passed (1.32 s), `/tmp/lohra117-surfaces-repaired-py311.txt`;
three later real-SDK native terminal/error controls bring that module to 18.
All these runs overlap the final matrix and are not additional distinct cases.

One issue-comments lookup used the wrong tool argument before its schema was read;
one source lookup guessed a nonexistent cli_chat.py. Neither ran tests. The final
matrix carries one pre-existing Starlette/AnyIO BlockingPortal deprecation warning.

## Final validation and acceptance mapping

**310 distinct selected cases: 54 new + 256 existing controls**, passed once as a
final matrix in each runtime: Python 3.11.15 **9.39 s**, Python 3.13.5 **9.63 s**.
New module counts: boundaries 22, SDK/cache 7, create/abort 7, surfaces 18. This is
310 cases across two interpreters, not 620 distinct tests. Seven adapted existing
cases retain their count. Earlier reruns overlap these 310.

| AC | Evidence in the final selection |
| --- | --- |
| 1 Chat terminal | structural reasons and real Chat SDK `[DONE]`/finish-then-usage pair |
| 2 Anthropic/Responses terminal | real Anthropic marker/reason pair, Responses completed/incomplete/failed SDK controls, native failure code |
| 3 no certification | real SDK → Agent/Gateway/SQLite with text and complete-looking tools; real WorkflowService/cache text and forced-schema pairs |
| 4 interruption/ownership | final text/reasoning callback and empty-EOF abort; physical body close on success, protocol error, abort, native failure and callback exception; existing interrupted known-floor/provenance and binding tests |
| 5 usage/no early return | real Chat post-finish 11/5 usage; Anthropic 11/5 and Responses completed/incomplete 11/5; existing reasoning/output/reconstruction controls |
| 6 consumers/create | true SDK JSON create pair; real Responses create inside CLI JSON with persistence; actual CompletionService + both ASGI routes in streaming/nonstreaming modes; Gateway; existing partial-tool guards |
| 7 gates | both focused local runtimes plus Ruff/diff-check; full required CI and independent exact-head review remain coordinator gates |

Commands/source hashes are recorded in
`/tmp/lohra117-final-focus-py{311,313}.{txt,json}`; exact selection is
`/tmp/lohra117-final-selection.txt`; collection-only inventory is
`/tmp/lohra117-collect-final-py311.txt`. Runner `/tmp/lohra117-run-focus.py` invokes
the selected runtime's `python -m pytest --no-cov -p no:cacheprovider`, with exact
absolute `task-117/backend` PYTHONPATH, runtime bin first, PYTHONDONTWRITEBYTECODE,
and temporary LOHRA_HOME/basetemp. Parent HOME/CODEX_HOME are preserved by the
runner; inherited subscription fixtures use their own temporary monkeypatches.
New tests never access personal credentials; sockets/DNS/process creation are
guarded. SDK/HTTP/database/service resources close in finally/context managers;
SSE producer threads are captured and explicitly joined with bounded waits.

`ruff check backend/lohra backend/tests backend/ci` and `git diff --check` passed.
Final implementation client SHA256:
`069a87e6a7255ef0a5a625f164a69425d9065828b21e5040086e564d32db2f25`.
The final commit/tree/file hashes are delivered in `/tmp/lohra117-final-manifest.json`.

## Limits

Real SDK parsing uses synthetic SSE/JSON bytes over MockTransport, not real
provider inference or physical TCP truncation. Versions: OpenAI 3.13.0,
Anthropic 1.5.0, HTTPX 0.28.1, HTTPX2 2.12.0. The original OpenAI preparation used
supported legacy HTTPX clients; new Chat and surface SDK cases use HTTPX2, the
current default family. These are compatible distinct client classes, not aliases.
Anthropic SDK cases use HTTPX2. No claim of a broken historical HTTPX2 harness or
exclusive support for one HTTP family is made.

The real Anthropic SDK's derived event already caught the original last-text
callback abort; only Chat/Responses failed that prepared case. Direct helper
reasoning/empty-EOF discriminators demonstrate the missing final gate separately.
Visible deltas already delivered cannot be recalled. Arbitrary cooperative SDK
I/O and tools already in flight remain bounded by their existing contracts, not
a new thread-kill mechanism. #116/#126/#127/#130 were preserved, not redesigned.
This slice does not complete native outcome semantics, numeric usage accounting,
the parent #11, provider compatibility certification, or a release.
