# File-call order within one assistant message — issue #97

Base: public main `a2ac0db965f5b290f1d34f3b029041f62edbe9b8`.

## RED before implementation

The original `_execute_tool_calls` passed all calls to `pool.map` independently.
Result positions were stable, but a later file call could execute first. The
new test used the real conversation loop, a scripted model response emitting
`write_file(path=X, content="A")` followed by `write_file(path=X, content="B")`,
the registry and real file handler in a temporary directory.

A `ThreadPoolExecutor` subclass used Events to delay the first submitted job
until the last submitted job completed. This is a legal worker schedule,
constructed deterministically without sleeps or repeated probabilistic runs.
The test failed with **completed = `["B", "A"]`**, against expected
`["A", "B"]` (**1 failed**, Python 3.11). It also rejects a superficial fix
that merely lets whichever worker arrives first acquire a per-path Lock.

## Implemented contract

`tool_call_batches` constructs ordered index queues for the known `read_file`
and `write_file` names. After `Path.resolve(strict=False)`, existing files are
identified by device/inode; each resource queue executes sequentially in one
worker. Different resources and unrelated tools use separate jobs, up to the
existing eight-worker cap. Returned tool messages occupy their original
indices even when resource queues are interleaved in the emitted message.

Ordering covers overwrite, append and intermediate reads, as well as relative,
absolute, existing symlink, hardlink and case aliases observed by the filesystem.
Symlinks are resolved before `..`. Missing file/parent suffixes use their nearest
existing ancestor's identity plus a casefolded, Unicode NFC suffix: possible
case or Unicode-composition collisions
are ordered conservatively without assuming the volume's case behavior. Existing
files or ancestors with distinct identities, and noncolliding missing suffixes,
remain independent. `~` is left literal, matching
the actual file handlers. A different tool's arbitrary `path` field is not
probed or treated as a dependency.

Planning consults path metadata only. It never reads file contents, rewrites
arguments or authorizes a call. The original dispatch applies all gates and
produces the normal tool result/error. If any file identity is unavailable,
all file calls in that message share one conservative queue; unrelated tools
stay independent. Resolution exceptions are not exposed. Tests distinguish
allowed calls, outside-scope refusals and tainted refusals under synthetic
resolution errors, while checking arguments and error redaction.

Normal tool errors remain results and do not skip later queued calls. Submitted
resource futures are consumed with `as_completed`, then restored to original
result indices. On `BaseException`, shutdown remains
`wait=False, cancel_futures=True`; the failing worker immediately publishes the
local teardown Event, before the consumer wakes, preventing active resource
jobs from starting their queued tails. Tests cover KeyboardInterrupt/SystemExit, a tool-raised BaseException,
real SIGINT in a subprocess with a blocked file call, and the existing
parallel-tool SIGTERM subprocess regression. This does not interrupt an
already-running tool or change the cooperative abort behavior deferred to #68.

## Scope and limits

The queues exist only for this message; no global locks or resource registry
persist. The planner's identity snapshot does not prevent external symlink
or file replacement, new aliases created after planning, or races with writers
in other messages, sessions or processes. This is not
filesystem isolation, staleness detection or content merging. Overwrite still
replaces the whole file, and append still uses its existing O_APPEND contract.

The `write_file` description states the intra-message behavior and its scope.
Spec 02 documents normalization, fallback and teardown. The frozen prompt
machinery and builtin skills are unchanged; the builtin remains 799 lines.

## Verification

### Independent review correction: case aliases

At reviewed head `3f5ed9a`, the independent case-alias test reproduced two
successful writes through `Case.txt`/`case.txt` on this case-insensitive volume:
`samefile` was true and result order was A then B, but final content was **A**.
`normcase` on POSIX had not identified the alias. Documentation excluding the
alias did not satisfy the resource-ordering contract.

Before the identity correction, the new focused regressions gave **7 failed,
1 passed** on Python 3.11: case aliases, parent-case aliases, hardlinks, missing
case-colliding suffixes, and silent stat-failure fallback failed. The negative
control for distinct existing identities still overlapped. The independent
test also failed separately with final content A. Both controls use deterministic
reversed scheduling and real file handlers, not repeated probabilistic runs.

A further missing-file discriminator, composed uppercase `É.txt` versus
decomposed lowercase `e\u0301.txt`, failed under casefold alone (**1 failed,
2 passed**); Unicode NFC normalization now conservatively covers that potential
collision too. This normalization affects only planning keys, never arguments.

The correction uses metadata identity for existing resources and conservatively
groups only potential case collisions for missing suffixes. Tests also use
synthetic distinct inode evidence to prove overlap on case-sensitive volumes,
including missing leaves under distinct existing parents; this control does not
write real files because the host may map both spellings to one file.

### Independent review correction: later-worker failure

At reviewed head `bf7249d`, the independent Event-driven probe emitted
`X-head, Y-raise, X-blocked-tail`. The first resource future could not complete
until its blocked tail returned, so input-ordered `pool.map` hid the later
worker's `KeyboardInterrupt`/`SystemExit`. Both variants failed before releasing
X (**2 failed**, Python 3.11). Four repository regressions also failed: two for
prompt unwinding without join, and two showing that another resource could
start its tail before the caller consumed the fatal result.

The correction submits the existing resource queues and consumes futures by
completion, while preserving each result's original index. The failing worker
sets the existing stop Event and re-raises the same exception object. Setting
the Event only in the result consumer is insufficient: a test holds that
consumer until a sibling head returns, and verifies its queued tail never
starts. There is no new scheduler or process-global state. Active calls are
still allowed to finish; they are not forcibly interrupted.

The test executor now intercepts `submit` rather than `map`, so it continues to
run a later independent job before the first one with the new dispatch API.
This adversary was also checked against the pre-#97 loop in the reviewer's
`/tmp/review97_baseline_loop.py`: writes completed B then A, final content A,
despite result order A then B. Thus the scheduling control remains discriminating
after the API change. Signal/submission failure coverage uses the same seam.

### Focused validation

From this worktree's `backend/`, with absolute
`PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-97/backend`
and the corresponding `/tmp/lohra-wave10-py311/bin` or `py313/bin` first in PATH:

```sh
python -m pytest tests/test_loop_file_identity.py tests/test_loop_file_order.py tests/test_loop_file_teardown.py tests/test_loop.py tests/test_loop_interrupt_dispatch.py tests/test_loop_inbox.py tests/test_tools_fs.py tests/test_workflow_sandbox.py tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py tests/test_signal_epilogue.py tests/test_signals_convert.py tests/test_signal_e2e_subprocess.py::test_sigterm_with_parallel_tool_calls_dies_promptly -q --no-cov
```

Before the review correction: Python 3.11 **169 passed** (8.66 s); Python 3.13
**169 passed** (8.58 s). That 3.11 run replaced `--no-cov` with `-o addopts=''` and coverage limited to
`lohra.agent.tool_batches`, `lohra.agent.loop`, and `lohra.tools.fs`:
**100%**, **95%**, and **86%**, respectively; aggregate **94%**.

After the review correction, the command above passes **179 tests** on Python
3.11 (8.13 s) and **179 tests** on Python 3.13 (8.29 s). The independent reviewer's
`/tmp/test_review97_independent.py` passes **12 tests** on each runtime (0.27 s /
0.30 s), including the previously failing case-alias reproduction. Coverage of
the corrected planner is **97%** from the 33 file ordering/identity tests; the
unexecuted branch handles an unavailable filesystem root during ancestor lookup.

After the later-worker correction, the same focused command passes **183 tests**
on Python 3.11 (8.21 s) and **183 tests** on Python 3.13 (8.37 s). The independent
script's **14 tests** pass on each (0.28 s / 0.32 s), including both new fatal-worker
variants. Its old `map`-based reverse-scheduler double is no longer invoked;
the repository's `submit`-based adversary and the baseline control above retain
the ordering evidence. The isolated reviewer was notified to adapt that double.
Ruff over all `backend/` and `git diff --check` pass.

All file effects and databases are temporary; model responses are scripted.
No provider, personal state, release metadata, push or publication was used.
The full CI matrix, coordinator closure and independent review of the final
head remain separate integration gates.
