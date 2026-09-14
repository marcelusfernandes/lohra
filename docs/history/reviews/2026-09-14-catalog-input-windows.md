# Catalog input windows — author evidence for #41

Date: 2026-09-14. Claimed branch: `codex/task-41`. Public base:
`0ec0307c985c86d0db9dd8808a56cfca0ed78614` (after #112).
This report records implementation and local validation. Full CI and independent
review of the final published SHA remain coordinator gates.

## Change and source contract

The catalog already fetched each provider's model listing, extracted window
metadata, persisted it in the explicit home's provider/model cache, and let the
Agent resolve context locally. `_WINDOW_KEYS` omitted Anthropic's
`max_input_tokens`, so an unseeded model publishing only that field still used
the profile fallback. A smaller input limit also lost to a larger recognized
native/route field.

The production change adds that one key and updates `_row_window`'s docstring.
The existing positive-integer/non-bool validation and minimum across top-level
and nested `top_provider` fields now apply to it. `max_tokens` remains an output
limit. Resolver, cache implementation, endpoint selection, pagination, numeric
seeds, authentication and the public catalog JSON envelope are unchanged.
`docs/STANDALONE.md` now explains the catalog/cache flow and fallback limits.

Current [issue41](https://github.com/marcelusfernandes/lohra/issues/41), the handoff,
the prepared probe/tests and the coordinator's primary-source refresh were read
before implementation. Graph discovery succeeded; exact task-41 source reads
confirmed the seam rather than trusting a graph indexed from another checkout.
The public sources were also reopened on 2026-09-14:

- [Anthropic Models API](https://platform.claude.com/docs/en/api/models): the
  listing's model metadata declares nullable `max_input_tokens` and a separate
  nullable `max_tokens` limit. The existing listing is sufficient for this field;
  no per-model fetch is required by this implementation.
- [OpenAI Models API](https://developers.openai.com/api/reference/resources/models):
  its basic model object does not declare a context window. The API route keeps
  the current local fallback; no documentation scraping or invented field was
  added. API and `openai-codex` subscription cache identities remain separate.

These are public documentation checks, not an authenticated account listing.
No current provider window number was inferred from the synthetic tests or used
to change the checked-out static seeds.

## Tests and TDD

`backend/tests/test_model_catalog_input_windows.py` was adopted byte-identically
from the 182-line prepared module, SHA256
`ca2ac60e4cfb8151c2b34df7693874d236665d9e2faa84a22c7bddf5ef27419a`.
Its preparation docstring and optional source-check environment variable are
retained; they do not select an implementation or relax an oracle. The original
module, preparation report and baseline logs remain intact under `/tmp`.

Before changing production, the 15 cases ran on the actual claimed base:

| Run | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| New module, before production edit | 3 failed, 12 passed, 0.14 s | 3 failed, 12 passed, 0.16 s |
| New + existing catalog/windows/context suites, after edit | **118 passed, 0.33 s** | **118 passed, 0.39 s** |

Logs: `/tmp/lohra-41-claimed-red-py311.txt`, `lohra-41-claimed-red-py313.txt`,
`lohra-41-focus-final-py311.txt`, and `lohra-41-focus-final-py313.txt` in `/tmp`.
There are **118 unique cases: 15 new and 103 existing**. The RED run overlaps
the final matrix, and the two interpreters execute the same set; neither is
added to that count. No test expectation or fixture changed after adoption.

The three REDs capture all three stages before asserting their values:

| Synthetic metadata | Listing / disk / resolver before | All three after |
| --- | --- | --- |
| Input-only 123456, unrelated output 512 | absent / absent / 200000 fallback | 123456 |
| Native 300000, nested input 65536 | 300000 / 300000 / 300000 | 65536 |
| Input 32768, nested max-context 65536 | 65536 / 65536 / 65536 | 32768 |

These are three manifestations of one omitted key. The fourth minimum case
(input 300000, existing route 65536) passed before and after. Eight invalid or
absent cases cover both booleans, zero, negative, float, string, null and missing;
each retains a valid alternative field and a separate valid model while the
invalid-only row stays uncached. Passing those controls on the old baseline
alone did not prove validation of a field it ignored; they now run with the
positive cases passing too.

The remaining three controls cover output-only `max_tokens`, OpenRouter's
conservative route minimum, and a basic OpenAI listing followed by API-only
cache injection. An API cache value of 777777 does not affect subscription.
The pre-claim public probe observed code-defined API/subscription fallbacks of
1050000/400000 on a0724db; those observations remain historical code evidence,
not fresh provider measurements. That probe was not rerun during implementation.

Acceptance coverage:

| Criterion | Evidence |
| --- | --- |
| Listing metadata reaches cache | Four new listing/disk/resolver cases, including three before/after REDs. |
| Static fallback is preserved | Invalid/missing/output-only controls plus existing context-resolution seed and fallback tests. |
| API/subscription separation | New basic-listing/cache-injection case plus existing per-provider/per-model resolution tests. |
| No added chat-path network | Each new listing has exactly one MockTransport request; each local resolver is read 20 times with catalog fetch/build and socket seams denied. |
| Compatibility suite | 118 focused cases pass in both runtimes; full CI and independent review remain pending. |

## Isolation, commands and checks

All new HTTP clients use a context manager and are checked closed. Their
MockTransport responses are synthetic; the Agent client refuses inference.
Network guards retain attempted-call records, so even a swallowed exception
cannot masquerade as a passing fallback. Clearing the window memo before reads
proves persistence through the actual temporary file. `LOHRA_HOME` is temporary,
`LOHRA_PROFILE` is isolated, and `HOME`/`CODEX_HOME` remain unchanged.

Run from `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-41/backend`:

```sh
env PATH=/tmp/lohra-wave10-py311/bin:$PATH \
  PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-41/backend \
  PYTHONDONTWRITEBYTECODE=1 \
  LOHRA41_EXPECTED_BACKEND=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-41/backend \
  /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_model_catalog_input_windows.py tests/test_model_catalog.py \
  tests/test_model_windows.py tests/test_context_window_resolution.py \
  -q --tb=short -p no:cacheprovider --no-cov \
  --basetemp=/tmp/lohra41-focus-final-py311
```

For Python 3.13, replace `py311` with `py313` in runtime/PATH/basetemp.
The RED command selected only the new module and used a `claimed-red` temporary
directory. Exact source resolution was asserted in both phases.

From the task-41 root,
`/tmp/lohra-wave10-py311/bin/python -m ruff check --no-cache backend` passed;
`git diff --check` passed. No full local suite was added after the focused matrix.
The author made no provider/auth request, release, GitHub mutation or push, and
edited no old worktree or coordinator-reserved document. The fix consumes only
metadata actually supplied by the existing listing; absent metadata continues
to use the established local fallback.
