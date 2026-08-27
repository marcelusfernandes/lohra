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

4. Add `--yolo` only when the user has explicitly authorized Lohra to modify the
   agreed scope and run commands without interactive approval. Never infer that
   authority from a request to merely analyze or recommend.

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

## Continue a useful session

Use the returned `session_id` only when an additional turn benefits from the same
Lohra context:

```bash
lohra chat --profile "lohra-<project>" --json --session "<session_id>" \
  "Address the failed test and rerun the focused validation."
```

Start a fresh session for an unrelated task.

## Minimal user-facing request

The user should be able to say:

> Use Lohra to inspect this project and implement the safest high-value reliability
> improvement. Keep changes scoped to the project and validate the result.

Do not require the user to name tools, flags, files, or an implementation plan unless
those details genuinely define the intended outcome.
