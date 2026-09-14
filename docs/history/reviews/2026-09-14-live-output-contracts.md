# Live output and optional registry contracts — #99

2026-09-14. Author validation on `codex/task-99`, public base
`205febdf38038135eaf2de85c68befeaebd89a77`. Full CI and independent review of the
published SHA remain coordinator gates.

The current [issue #99](https://github.com/marcelusfernandes/lohra/issues/99),
handoff and public preparation were rechecked on the claimed source. Graph
discovery plus source search confirmed no production consumer of
`validation.extract_structured_call`: only three tests used it. `DEFAULT_TOOLSETS`
occurred only in its definition and export. Both unused symbols were removed;
the post-edit search finds neither in `backend/lohra` or `backend/tests`.

The three tests now call the actual `loop._forced_call_arguments`. They preserve
raw arguments and the None fallback signal, then validate separately through the
workflow validator. Valid, missing/empty/unrelated, schema-mismatching and malformed
JSON cases are covered. No production extraction, validation or registry behavior
changed, and `Any` remains because other validation functions use it.

A new real Agent → Core → WorkflowEngine test feeds three synthetic forced-tool
responses that never match the schema. It verifies one client, two corrections,
three calls, null output, failed status, zero text fallbacks, forcing on every
request, an unchanged system prefix and correction messages carrying the schema
error. Existing success, provider-ignored fallback, ordinary schema nodes and
transport-default tests remain. Core and in-memory SessionDB are closed.

The new 85-line registry module uses an isolated registry and no real handlers.
Nine cases cover None/all available, empty/selected/unknown groups, true/false/
raising availability checks, later default definitions unchanged and exclusion
before availability evaluation. Cache checks use a local clock reference at 29
and 30 seconds, not sleeps or a global monotonic replacement. Existing integration
tests already used positional group filters; the missing coverage was the focused
selection/availability contract, not every possible use of the filter. This does
not implement deferred tools or alter the default catalog.

## Validation

The prepared registry module was adopted byte-identically (SHA256
`2070b763c9dd9c0c0b29a1a70b16198f6ca028411654227f39b8a569eb410da9`). The other
prepared module was migrated in place: three replacements and one addition,
without duplicate old tests or a same-file import; it uses the existing DB fixture.
Tests were migrated first and passed while production still matched the base.
These are preservation controls for dead-code removal; no RED was manufactured.

| Run | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Before production removal | 47 passed, 0.34 s | 47 passed, 0.42 s |
| After removal | 47 passed, 0.36 s | 47 passed, 0.45 s |

**47 distinct final cases**: 34 unchanged, three migrated and ten new (nine
registry + one persistent failure). Preparation had 50 because it included the
three old tests alongside their replacements. Its additional one-case directed
rerun per interpreter pinned failed status; it overlaps, and neither preparation,
before/after runs nor interpreters are added to the final count.

Logs: `/tmp/lohra-99-claimed-before-py311.txt` and `py313.txt`;
`/tmp/lohra-99-focused-final-py311.txt` and `py313.txt`;
Ruff: `/tmp/lohra-99-ruff-final.txt`. Ruff over backend and `git diff --check` pass.
Earlier probe, JSONL, preparation modules and logs remain intact; details are in
`/tmp/lohra-99-pytest-preparation.md`.

From the task-99 `backend` directory, the final command was:

```sh
env PATH=/tmp/lohra-wave10-py311/bin:$PATH \
  PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-99/backend:/tmp \
  PYTHONDONTWRITEBYTECODE=1 \
  LOHRA99_EXPECTED_BACKEND=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-99/backend \
  /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_tool_registry_filters.py tests/test_workflow_forced_output.py \
  tests/test_workflow_validation.py tests/test_smoke.py \
  -q --tb=short -p lohra99_test_guard -p no:cacheprovider --no-cov \
  --basetemp=/tmp/lohra99-focused-final-py311
```

Use py313 in runtime/PATH/basetemp for the second interpreter. The temporary
`/tmp/lohra99_test_guard.py` asserts exact imported source, isolates LOHRA_HOME/
profile and rejects network connections. HOME/CODEX_HOME are preserved. These
synthetic responses exercise real runtime layers; they do not measure live
provider compliance. No auth, personal state, real tool, release or old worktree
was used. Reserved global documents belong to the coordinator's separate commit.
