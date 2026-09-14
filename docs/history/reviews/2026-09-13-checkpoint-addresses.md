# Checkpoint answer addresses — issue #106

## Hypothesis and RED

Base: public main `1c7b4605cba46ca91199f7bbf359dddad88e6327`.
The root checkpoint id `sub[a]:cp` and child `cp` called by workflow node `a`
share one legacy answer key. This is independent of their correctly separated
cache cells (#90). Both checkpoints declare `accept: ["sim"]`.

Before implementation, the first two tests in
`backend/tests/test_workflow_checkpoint_address.py` failed with
`Failed: DID NOT RAISE ValueError`: a validated spec accepted the ambiguous map.
The same baseline was extracted with `git archive HEAD backend` into a temporary
directory and run again through the existing `test_workflow_nested_identity.run`
helper (real validator/engine, synthetic SQLite, scripted client). Observed:

```json
{"root_first":true,"status":"complete","outputs":{"sub[a]:cp":"sim","a":{"cp":"sim"}}}
{"root_first":false,"status":"complete","outputs":{"a":{"cp":"sim"},"sub[a]:cp":"sim"}}
```

Both runs received only `{"sub[a]:cp":"sim"}`. This directly confirms that one
human response opened two distinct gates; no provider was called.

## Contract implemented

The existing input accepts a structured list in addition to the legacy object:
`checkpoint_answers: [{address: ["a", "cp"], answer: "sim"}]`. Root addresses
contain only the checkpoint id. JSON type is the discriminator, so even ids that
look like a protocol prefix, JSON document, or nested label stay literal.
The pause adds `answer_address`; `node_id` and template metadata remain visible.

Legacy objects normalize only when a key has one possible destination across
all declared root checkpoints and workflow call prefixes. Any ambiguous entry
rejects the entire launch, before lease acquisition or state/cache writes. The
error shows the structured alternatives and asks which question the human
answered. Template loading is not needed for this decision; possible aliases
are conservatively reserved even when a template is missing or not yet visited.

Old pending questions recover their address from a persisted root id only in a
spec without workflow calls, or from one unique call matching a recorded template.
The independent reviewer found that pre-#78 child questions persisted a bare id
without template metadata: interpreting that absence as proof of root ownership
could transfer the old child default to a guarded root and stamp its unproven
cache row. That case now re-asks with no default; the SQLite-reopen regression
checks the root row stays unproven until a fresh explicit human answer arrives. No row is migrated. If the old
question has no provable identity, an answered resume is refused; a bare resume
re-asks it and never assigns its old default. Proven defaults still fill only
their own address. `null` remains a real cached answer, with the existing
null-output rollup semantics (all-null root `failed`, nested `degraded`).

Engine and preview share normalization and use tuple addresses. Nested engines
receive structured entries only, so they cannot reinterpret a root alias. The
cache keys, scope proof, legacy cached-approval protections, accept/rejection
policy, human authority and frozen session prompt are unchanged. Route-fault
commands retain the existing object format and cannot consume a structured list.

## Verification and limits

- New regression file covers both root/child orders, persistent reopen/replay,
  old/new pending payloads, punctuation collisions between child calls, strings
  resembling canonical prefixes, malformed/duplicate addresses, unloaded
  templates, old defaults, rejection, arbitrary answer values, route separation,
  and preview isolation.
- Existing #74/#78/#90, durable-state, route-answer, supervision and preview
  suites exercise unchanged guards. Existing payload assertions were updated
  with each actual invocation address; the legacy-answer assertions remain.
- Python 3.13 final focused suite: **301 passed** (10 files plus one steering
  contract, 10.39 s), including the independent-review fix.
- Python 3.11 final address/provenance/steering suite: **53 passed**, new address
  module **94% coverage** (3.17 s). The final provenance-guidance assertion then
  passed separately (**1 passed**, 0.83 s).
- Earlier Python 3.11 focused suite: **299 passed**. A broader pre-review-fix
  `test_workflow*.py` run from the repository root had **2082 passed, 1 xfailed**
  and one cwd-dependent failure reading `lohra/workflow/steering.py`; the same
  steering test passed from `backend/` in both final suites. No code change was
  needed for that launch-directory error. A full final backend suite remains CI's gate.
- The independent reviewer's `/tmp/repro_106_bare_legacy_child_default.py` also
  passes after the fix: root stays paused with `cp: null`, and the old row retains
  `node_scope_json: null` / `output_json: "ok"`. The compatibility hint now names
  the structured `answer_address`, pinned by the provenance regression.
- Ruff and `git diff --check` pass. Builtin workflow-authoring skill remains
  **799 lines**, within its hard 800-line budget.

All execution is offline with synthetic state. No real profile/database,
provider, frozen prompt, release metadata, or publication was changed. Legacy
matching intentionally may refuse an apparent alternative absent from a child
template; structured addressing is the supported remedy. Nesting remains one
level. Independent review and CI are coordinator-owned follow-up gates.

Final local commands (run from `backend/`, with its absolute path in `PYTHONPATH`
and the matching `/tmp/lohra-wave10-py311/bin` or `py313/bin` first in `PATH`):

```sh
python -m pytest tests/test_workflow_checkpoint_address.py tests/test_workflow_nested_checkpoint.py tests/test_workflow_nested_identity.py tests/test_workflow_nested_compat.py tests/test_workflow_m7_features.py tests/test_workflow_checkpoint_guard.py tests/test_workflow_durable_state.py tests/test_workflow_route_answer.py tests/test_workflow_supervision_doctrine.py tests/test_workflow_cache_preview.py tests/test_workflow_supervision_steering.py::TestCrossSurfaceConsistency::test_steering_module_docstring_pins_the_contract -q --no-cov
python -m pytest -o addopts='' tests/test_workflow_checkpoint_address.py tests/test_workflow_nested_compat.py tests/test_workflow_supervision_steering.py::TestCrossSurfaceConsistency::test_steering_module_docstring_pins_the_contract --cov=lohra.workflow.checkpoint_address --cov-report=term-missing -q
```
