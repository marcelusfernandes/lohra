---
name: use-lohra
description: Delegate substantial, self-contained work to Lohra through its CLI. Use when the user asks Codex to use Lohra, needs a persistent sub-agent with memory or skills, or wants Lohra to inspect, implement, test, research, or run workflows in the current project.
---

# Delegate work to Lohra

Use Lohra as an independent sub-agent. Give it the outcome and boundaries; let it
choose the relevant tools, implementation, and validation steps.

## Run the delegation

1. Work from the root of the target project so Lohra can read its instructions and
   project skills.
2. Formulate a task with only the non-negotiable context:
   - desired outcome;
   - allowed scope (especially for code changes);
   - what counts as done.
3. Invoke Lohra in structured, tool-enabled mode:

   ```bash
   lohra chat --profile "lohra-<project>" --json "<task>"
   ```


> **Cost footgun:** a fresh profile does NOT inherit the shared home's Codex
> subscription opt-in (the ToS acknowledgment is per-store, fail-closed). Until
> you run `lohra auth enable --profile "lohra-<project>" --yes`, Lohra silently
> falls back to any per-token API key it finds — which bills per call. Enable
> the subscription in each new profile if that is your cheap path.

   Use a project-specific profile to keep Lohra's memory, skills, and sessions
   isolated. Do not use `--no-tools` for repository inspection, implementation,
   testing, or research; that flag is only for an explicitly conceptual answer.

   The chat agent's turn budget defaults to 90 tool-call rounds. If a run dies
   with an error naming `max_iterations (N) reached`, rerun with
   `--max-iterations <n>` (or the `LOHRA_MAX_ITERATIONS` env var) set above the
   `N` it reports. That limit counts tool-call rounds, not wall-clock time, so
   raising `timeout` does not help. Do not pass a value below the default to
   "budget" a run — it only lowers the ceiling and kills long tasks earlier.

   Model routing: `lohra models --profile "lohra-<project>"` lists what is
   actually reachable on this machine (per API key, local ollama, and the
   subscription). A delegated task can ask Lohra to pick models per workflow
   node from that catalog (`list_models`) and to pause at a `checkpoint`
   presenting the assignment; a paused run costs nothing and is resumed by a
   later `lohra chat --profile "lohra-<project>" --json --session
   "<session_id>" "approve the checkpoint with go"` turn. When billing
   matters, pin the credential route before delegating:
   `lohra auth prefer <auto|subscription|api_key> --profile "lohra-<project>"`
   — every one of these commands needs the SAME `--profile` as the delegation,
   or they read and write the shared home instead.

   One-off provider override: with the stored preference on `auto`, an explicit
   `--provider <name>` on the chat command outranks an active subscription for
   THAT invocation only (a stderr note says so; `--provider openai-codex`
   switches back). An explicit stored preference always beats the flag — to
   change the route durably, use `lohra auth prefer`, not `--provider`.

   Long-running workflows: once a delegated task starts a `run_workflow`, watch
   its progress from the shell at zero LLM cost — `lohra workflow list
   --profile "lohra-<project>"` (recent runs: status, nodes, tokens) and
   `lohra workflow watch --last --profile "lohra-<project>"` (follow a run
   until it stops). Both read only the durable run state; neither spends a
   token. Prefer these over polling with another `lohra chat` turn. `watch`
   stops on its own once a run pauses with no auto-resume coming (a token
   budget, a checkpoint) — it keeps following a quota pause, which retries
   itself.

   Bound each workflow run UP FRONT in headless: `--token-budget-cap <tokens>`
   (or `LOHRA_TOKEN_BUDGET_CAP`) pre-authorizes a ceiling PER RUN, applied to
   every run the turn launches, so a run whose spec asked for no `token_budget`
   is bounded anyway. It is not a ceiling on the turn: N runs can still cost up
   to N×cap, so size it per run and read `workflows` in the envelope. Lohra's own
   `token_budget` is clamped to it (on a resume too) and can never raise it —
   only you can. Raising it later takes two steps: relaunch with the bigger cap
   AND resume with an explicit `token_budget` above what the run already spent,
   since a bare resume inherits the old, spent ceiling. Without a cap a run is
   unlimited, which in a one-shot turn nobody is there to notice.

   A one-shot `--json` turn has no next turn to read a paused run's own
   notice — check the envelope's `workflows` field instead: a `pause_reason`
   of `token_budget_exhausted` needs a human-authorized `token_budget` before
   `run_workflow(resume_run_id=...)`, never an invented one; `checkpoint`
   needs the human's answer relayed the same way. An entry with
   `cancelled_on_exit: true` was still going when the turn's own process
   exited and cancelled it. Absent entirely when there is nothing to report.

4. Add `--yolo` only when the user has explicitly authorized Lohra to modify the
   agreed scope and run commands without interactive approval. Never infer that
   authority from a request to merely analyze or recommend.

   Guarantee: under `--json` Lohra never reads stdin and never prompts — a
   dangerous command (e.g. `rm -rf`, `sudo`) is auto-DENIED and surfaces as
   that tool call's error instead of hanging the process. If the task
   legitimately needs such a command, that is what `--yolo` is for.

## Verify the delegation

Treat a run as successful only when all of these are true:

- the CLI exits with code `0`;
- the JSON envelope has `error: null`;
- the reported work matches the user's request.

For a task that asks Lohra to inspect, implement, test, or research with project
evidence, require at least one `tool_calls` entry. A zero-tool response is a failed
or incomplete delegation, not code evidence. Report that clearly; do not turn a
generic recommendation into a project-specific conclusion.

Independently inspect the changed files and run the relevant validation before
presenting an implementation as complete. Include the `session_id`, tools used,
and validation result in the handoff.

The envelope also reports what the turn cost, ready to relay: `usage_total`
sums the token meters over every API call of the turn (`usage` is the LAST
call only — do not bill from it), and `cost` prices the turn in USD:
`usd` (real, cache-aware), `gross_usd` (as-if-uncached), `saved_usd` (the
cache's contribution — negative when a write premium dominated), `basis`
(`api_list_price`; `api_equivalent` marks a notional estimate under a flat-fee
subscription; `local` is a free local model) and `source` (the dated price
snapshot, or the operator's `~/.lohra/pricing.json` override). `cost: null`
means no known list price for that model — never treat it as free.

## Continue a useful session

Use the returned `session_id` only when an additional turn benefits from the same
Lohra context:

```bash
lohra chat --profile "lohra-<project>" --json --session "<session_id>" \
  "Address the failed test and rerun the focused validation."
```

Start a fresh session for an unrelated task.

Lohra also carries durable operational notices across turns and processes: a
crashed/interrupted turn, and the terminal outcome of a background workflow,
each leave a note the NEXT turn on the same session lineage receives
automatically. These notices never appear in the `--json` envelope — they only
shape Lohra's own next reply. Concretely: if a previous turn on this session
died, simply resubmitting on the same `session_id` is the correct recovery —
Lohra's next response already accounts for what happened. This covers death by
signal too (SIGTERM/SIGHUP, e.g. a harness timeout): the dying process
publishes the notice and exits by the signal itself (128+N), with the `--json`
envelope still emitted. To audit continuity without spending a token,
`lohra notices <session_id>` lists what the session still has pending versus
what it already consumed (with reason and timestamps).

## Minimal user-facing request

The user should be able to say:

> Use Lohra to inspect this project and implement the safest high-value reliability
> improvement. Keep changes scoped to the project and validate the result.

Do not require the user to name tools, flags, files, or an implementation plan unless
those details genuinely define the intended outcome.
