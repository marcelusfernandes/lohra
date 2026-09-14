# Skill platform index — author evidence for #98

Date: 2026-09-14. Claimed branch: `codex/task-98`. Public base:
`393752883f033b0634f7ae6cecdda0752353bb2c` (after #41).
This records local author validation; complete CI and independent approval of
the final published SHA remain coordinator gates.

## Behavior and scope

`SkillStore.index()` now consumes `platforms` after the existing first-name-wins
`scan()`. Darwin, Linux and Windows map to `macos`, `linux` and `windows`.
Unrestricted skills remain visible on every host; an unknown host exposes only
unrestricted entries. There is no new alias, wildcard or parser migration.

Filtering after dedup matters: an incompatible project copy still shadows a
same-name home/builtin copy, so that selected name disappears from the index
without substituting a lower tier. An entirely incompatible selection produces
an empty string, including no mandatory header. Discovery, explicit lookup/view,
write targeting, platform round-trip and frozen snapshots remain unchanged.

The workflow-authoring edit changes only its description. It retains the positive
trigger for authoring a workflow or interpreting its rollup, and adds an exclusion
for simple direct tasks when no workflow is needed. The description is 279
characters, down from 360, within the existing 1024-character limit. No token-cost
number was added or claimed as measured selection behavior.

The body is byte-identical to the claimed base; the file remains 799 lines,
version `1.0.0` and omitted `platforms` are preserved. Body SHA256:
`9c75eafd464e0124eaec57593934fca3c4c55cf11c5dc0b27d0229eeeb081492`.
The comparison is recorded in `/tmp/lohra-98-skill-content-check.json`, using
`/tmp/lohra-98-skill-claimed-before.md` as the exact pre-edit bytes.

The existing `test_skill_file_stays_within_the_line_budget` was generalized from
one hardcoded builtin to every builtin `SKILL.md`, retaining <=800 and requiring
at least one selected file. It was not duplicated. The skills section of spec03
now describes the actual store/index/snapshot path and supported platform names.

## Source and skill instructions

The live [issue98](https://github.com/marcelusfernandes/lohra/issues/98), updated
handoff and previous public preparation were read on the actual claimed base.
Graph discovery was attempted; exact-source reads confirmed the relevant store,
skill, metadata and budget-test seams. The original issue's claim that no budget
test existed is obsolete: that test already existed before this implementation.

The available `skill-creator` skill was read and applied before editing SKILL.md.
Its relevant guidance is to keep discovery precise, preserve supported metadata
and make a narrow edit proportional to the task. No new skill structure or
supporting resource was needed, and the requested scope excludes a body rewrite.

The skill's generic `scripts/quick_validate.py` was run before and after the edit.
Both return exit 1 with exactly the same format mismatch:

```text
Unexpected key(s) in SKILL.md frontmatter: version. Allowed properties are: allowed-tools, description, license, metadata, name
```

Logs: `/tmp/lohra-98-generic-validator-before.txt` and
`/tmp/lohra-98-generic-validator-after.txt`. This generic Codex allowlist excludes
Lohra's existing `version`; the failure predates the change. Neither the field
nor the system validator was modified to manufacture a pass. Lohra's real parser,
metadata checks, builtin content contracts and executable JSON examples pass.

## TDD and acceptance evidence

The prepared 140-line module was adopted byte-identically before implementation:
SHA256 `aaa8abd8ed6f0c9f90a1fdd810c94926719d9469ba49870a9f37ea29cb1a3b72`.
Those exact lines remain as the prefix of the committed module; the authorized
unknown-host case was appended. The preparation artifacts remain unchanged.

| Run | Python 3.11.15 | Python 3.13.5 |
| --- | --- | --- |
| Prepared 12 cases at claimed base, before production edit | 9 failed, 3 passed, 0.36 s | 9 failed, 3 passed, 0.39 s |
| New unknown-host and description cases, before production edit | 2 failed, 0.11 s | 2 failed, 0.13 s |
| Final focused matrix | **104 passed, 1.18 s** | **104 passed, 1.29 s** |

The final matrix has **104 unique cases: 14 new and 90 existing**. The 14 comprise
13 platform-index cases and one metadata/doctrine case. The generalized budget
check remains an existing case. RED runs, both interpreters and prior preparation
overlap this set; they must not be added to 104.

| Criterion | Tests/evidence |
| --- | --- |
| Compatible metadata-only index | Three simulated macOS/Linux/Windows cases: five discovered skills become expected sets of 3/3/2. Complete scan and explicit Windows skill view are checked before the index assertion. |
| Empty index when all incompatible | Three cases require the entire empty string, not a header with zero entries. |
| Filtering after precedence | Three cases preserve selected project lookup, explicit view and in-place update, leave home/builtin bytes unchanged, then require the incompatible selected name to remain hidden without fallback. |
| Frozen prompt and round-trip | Three controls preserve platforms/version during updates and compare both SkillStore snapshot and a real Agent's cached system prompt; fresh index/store see the disk update. |
| Unknown host | FreeBSD simulation leaves only unrestricted metadata visible, even with a `freebsd`-restricted entry; removing the unrestricted skill makes the index empty. Discovery still finds all entries. |
| Positive/negative description | Real builtin metadata is parsed, length-bounded and checked for workflow use plus a limited exclusion for single-step/file-edit/question tasks answerable directly without a workflow. This is a doctrine check, not a model-selection experiment. |
| Builtin budget and examples | Existing generalized nonempty <=800 check plus existing authoring, surface, JSON-spec examples and supervision-reading contracts all pass. |

The unknown-host test was added after that policy was documented as the bounded
default at claim. Earlier FreeBSD observations had no acceptance assertion
and did not establish a new supported OS or spelling. The implementation does
not infer an alias from those observations.

Preparation caveat retained: the initial `/tmp/lohra-98-pytest-baseline-py*.txt`
logs included six real REDs and three fixture errors because the test incorrectly
expected `description` in the `skill_view` envelope. That was corrected during
preparation to inspect body through view and description through `get`, before
the original index oracle. Corrected preparation logs have nine real REDs. The
claimed-base runs use that corrected module and had no fixture errors.

## Reproduction and limits

Logs: `/tmp/lohra-98-claimed-red-py311.txt` and `py313.txt`,
`/tmp/lohra-98-scope-red-py311.txt` and `py313.txt`,
`/tmp/lohra-98-focused-final-py311.txt` and `py313.txt`.
Run from `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-98/backend`:

```sh
env PATH=/tmp/lohra-wave10-py311/bin:$PATH \
  PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-98/backend \
  PYTHONDONTWRITEBYTECODE=1 \
  LOHRA98_EXPECTED_BACKEND=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-98/backend \
  /tmp/lohra-wave10-py311/bin/python -m pytest \
  tests/test_skill_platform_index.py tests/test_skills_store.py \
  tests/test_skills_tool.py tests/test_project_skills.py \
  tests/test_workflow_authoring_skill.py tests/test_workflow_authoring_surface.py \
  tests/test_skill_guidance_taxonomy.py tests/test_workflow_supervision_reading.py \
  -q --tb=short -p no:cacheprovider --no-cov \
  --basetemp=/tmp/lohra98-focused-final-py311
```

Use `py313` in runtime/PATH/basetemp for the second interpreter. Exact imported
source was asserted. All synthetic home/project/builtin state is temporary;
LOHRA_HOME/profile are isolated and HOME/CODEX_HOME preserved. The fake Agent
client refuses inference and network connection seams are denied. OS checks
simulate platform reports on this host, not native Windows/Linux executions.

From the task-98 root, Ruff (`python -m ruff check --no-cache backend`) and
`git diff --check` passed. No full local suite was added after the focused matrix.
No personal skill, provider/auth, release or old worktree was touched. Reserved
global documents are the coordinator's separate changes. Full CI and independent
review remain required before integration.
