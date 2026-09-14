# Workflow search opt-in — issue #55

Base: public main `7fa83e9493789af941d27614b48ad3f11cf25ae2`.

## RED before implementation

`test_workflow_search_policy.py` initially contained four cases. Running it
against the unchanged base produced **3 failed, 1 passed**:

- Default `WorkflowPolicy()` passed `web_search` and its complete query to the
  underlying dispatch. The desired `reached == []` assertion failed with
  `[("web_search", {"query": "private-CANARY"})]`.
- A real operator JSON file containing `{"allow_search": false}` also allowed
  that call through `load_policy → WorkflowService → sandboxed leaf → base`.
- The JSON opt-in control reached the base as expected.
- The tainted opt-in control refused dispatch, but still advertised
  `web_search` in the leaf's definitions. Its visibility assertion failed.

All legs used the real service, orchestration core, agent tool-call loop,
sandbox wrapper and synthetic SQLite, with a scripted model client and a
recording base dispatch. No search backend or provider was contacted.

## Implemented contract

`WorkflowPolicy.allow_search` defaults to `False` and normalizes only actual
boolean `True` to a grant. `load_policy` reads `allow_search` from operator JSON
and `LOHRA_LEAF_ALLOW_SEARCH` with the existing terminal-env semantics:
`1/on/true/yes` opt in, `0/off/false/no` are silent false, garbage warns and
does not grant; whitespace/case normalize. File and env compose by OR even
when the file is missing or malformed. Env false never revokes a file grant.
An explicit `WorkflowService(policy=...)` keeps its existing precedence over
both file and env. Authored spec fields cannot grant search.

Dispatch checks taint first, then `allow_search`. A disabled search returns
the typed `search_disabled` reason and a didactic operator remedy. Taint keeps
`tainted_egress` with no opt-in override. Definitions omit search whenever it
is denied, while dispatch still refuses a stale or scripted tool call. The
query goes to the configured search backend only after opt-in; search and
`web_fetch`'s host allowlist are independent permissions.

The new reason flows through #89's trusted marker, leaf counts, per-node
advisory, and retained audit count. A successful leaf remains `complete` even
after refusal; backend prose saying “sandbox denied” never creates metadata.
Tests cover nested invocation labels, audit disabled, and query redaction.

`allow_search` is included in the policy fingerprint. #75's replay semantics
remain: closing search marks the cached cell with `policy_changed` and an
advisory, without running a new leaf. The new default also differs from the
historical four-field fingerprint, which represented implicit search access.
Both transitions are exercised after reopening SQLite.

## Verification

Final focused tests run in Python 3.11 and 3.13 using:

```sh
python -m pytest tests/test_workflow_search_policy.py tests/test_workflow_sandbox.py tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py tests/test_workflow_cache_policy.py tests/test_workflow_tools.py tests/test_workflow_supervision_doctrine.py tests/test_workflow_m7_features.py -q --no-cov
```

Run from this worktree's `backend/` with absolute
`PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-55/backend`
and the corresponding `/tmp/lohra-wave10-py311/bin` or `py313/bin` first in PATH.
Python 3.11: **242 passed** (6.92 s). Python 3.13: **242 passed** (6.08 s).
The 3.11 run used `-o addopts=''` with coverage limited to the three touched
units: `sandbox.py` **98%**, `sandbox_denials.py` **100%**, `cell_stamp.py`
**98%**; aggregate **98%**. Ruff over `backend/` and `git diff --check` pass.

The focused suite includes the real JSON discriminator, strict boolean/env
composition, explicit-policy precedence, definition filtering without parent
mutation, spec non-escalation, and unchanged search outside workflow sandbox.
The builtin workflow-authoring skill remains **799 lines**.

Only temporary files and databases were used. No provider, real operator
configuration, data migration, release metadata, push or publication changed.
The frozen prompt, shell/MCP/filesystem policy and global search implementation
remain unchanged. Redirect enforcement (#56) and filesystem-writing scope
(#97) are separate work; the spec now names the existing redirect residual
instead of repeating its unsupported per-hop allowlist guarantee. Independent
review and full backend CI remain coordinator-owned integration gates.
