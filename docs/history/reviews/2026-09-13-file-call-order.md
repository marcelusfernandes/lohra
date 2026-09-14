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
and `write_file` names. Paths are identified with `Path.resolve(strict=False)`
and platform `normcase`; each resource queue executes sequentially in one
worker. Different resources and unrelated tools use separate jobs, up to the
existing eight-worker cap. Returned tool messages occupy their original
indices even when resource queues are interleaved in the emitted message.

Ordering covers overwrite, append and intermediate reads, as well as relative,
absolute and existing symlink aliases. Symlinks are resolved before `..`;
not-yet-created file/parent suffixes are supported. `~` is left literal, matching
the actual file handlers. A different tool's arbitrary `path` field is not
probed or treated as a dependency.

Planning consults path metadata only. It never reads file contents, rewrites
arguments or authorizes a call. The original dispatch applies all gates and
produces the normal tool result/error. If any file identity is unavailable,
all file calls in that message share one conservative queue; unrelated tools
stay independent. Resolution exceptions are not exposed. Tests distinguish
allowed calls, outside-scope refusals and tainted refusals under synthetic
resolution errors, while checking arguments and error redaction.

Normal tool errors remain results and do not skip later queued calls. On
`BaseException`, shutdown remains `wait=False, cancel_futures=True`; a local
teardown Event also prevents an active resource job from starting its queued
tail. Tests cover KeyboardInterrupt/SystemExit, a tool-raised BaseException,
real SIGINT in a subprocess with a blocked file call, and the existing
parallel-tool SIGTERM subprocess regression. This does not interrupt an
already-running tool or change the cooperative abort behavior deferred to #68.

## Scope and limits

The queues exist only for this message; no global locks or resource registry
persist. The planner's identity snapshot does not prevent external symlink
replacement, hardlink aliases, case aliases not unified by platform `normcase`,
or races with writers in other messages, sessions or processes. This is not
filesystem isolation, staleness detection or content merging. Overwrite still
replaces the whole file, and append still uses its existing O_APPEND contract.

The `write_file` description states the intra-message behavior and its scope.
Spec 02 documents normalization, fallback and teardown. The frozen prompt
machinery and builtin skills are unchanged; the builtin remains 799 lines.

## Verification

From this worktree's `backend/`, with absolute
`PYTHONPATH=/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/task-97/backend`
and the corresponding `/tmp/lohra-wave10-py311/bin` or `py313/bin` first in PATH:

```sh
python -m pytest tests/test_loop_file_order.py tests/test_loop_file_teardown.py tests/test_loop.py tests/test_loop_interrupt_dispatch.py tests/test_loop_inbox.py tests/test_tools_fs.py tests/test_workflow_sandbox.py tests/test_workflow_sandbox_denials.py tests/test_sandbox_denial_metadata.py tests/test_signal_epilogue.py tests/test_signals_convert.py tests/test_signal_e2e_subprocess.py::test_sigterm_with_parallel_tool_calls_dies_promptly -q --no-cov
```

Python 3.11: **169 passed** (8.66 s); Python 3.13: **169 passed** (8.58 s).
The 3.11 run replaced `--no-cov` with `-o addopts=''` and coverage limited to
`lohra.agent.tool_batches`, `lohra.agent.loop`, and `lohra.tools.fs`:
**100%**, **95%**, and **86%**, respectively; aggregate **94%**.
Ruff over all `backend/` and `git diff --check` pass.

All file effects and databases are temporary; model responses are scripted.
No provider, personal state, release metadata, push or publication was used.
The full CI matrix, coordinator closure and independent review of the final
head remain separate integration gates.
