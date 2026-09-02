# T7 dogfood report — operator token-budget cap on an unbudgeted spec

Overall: **PASS**. All functional expectations passed.

Run ID: `7b320f8082694de6b8e50b13aebd6308`

## Command 1 — capped workflow launch and one status read

- Result: **PASS**
- Wall-clock: `17.76 s`
- Envelope cost: `{"usd": 0.558284, "gross_usd": 0.558284, "saved_usd": 0.0, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}`
- Envelope error: `null`
- Saved stdout: `C-T7-1.json` (`57,880` bytes)

### Expectation (a): implicit operator cap is reported

**PASS.** The `run_workflow` call omitted `token_budget`; its exact tool result was:

```json
{"ok": true, "run_id": "7b320f8082694de6b8e50b13aebd6308", "status": "started", "token_budget": {"total": 100, "source": "operator_cap", "operator_cap": 100}}
```

The agent's final `output` began with that same result verbatim:

```text
{"ok": true, "run_id": "7b320f8082694de6b8e50b13aebd6308", "status": "started", "token_budget": {"total": 100, "source": "operator_cap", "operator_cap": 100}}
```

### Expectation (b): fan-out gate pauses the run and the envelope reports it

**PASS.** Exact top-level envelope field:

```json
"workflows": [{"run_id": "7b320f8082694de6b8e50b13aebd6308", "status": "paused", "pause_reason": "token_budget_exhausted"}]
```

Exact fields from the one `workflow_status` tool result:

```json
{"status": "paused", "reason": "token_budget_exhausted", "token_budget": {"total": 100, "spent": 0, "remaining": 100}, "tokens_spent_total": 0}
```

Exact fan-out-gate fault:

```text
fan: fan-out of 3 needs about 6000 tokens; only 100 of the 100 token budget are left (~0 leaf/leaves) — token budget exhausted
```

The agent's final `output` ended exactly with:

```text
status: paused
reason: token_budget_exhausted
```

### Expectation (c): the hint assigns the remedy to the operator

**PASS.** The envelope's `workflow_status` tool result contained this exact `hint`:

```text
this run's ceiling is 100 tokens and this process's operator ceiling is 100, so a resume launched from here is clamped to 100 and would pause again on the first spawn; nothing resumes it on its own — report the token_budget/spend fields and the case for more to the HUMAN OPERATOR, who raises or unsets --token-budget-cap (lohra chat) / LOHRA_TOKEN_BUDGET_CAP and relaunches. A relaunch alone does NOT unstick it: the resume must also carry run_workflow(resume_run_id=..., token_budget=<above what the run already spent>), because the ceiling it would otherwise inherit is the spent one
```

This names the `HUMAN OPERATOR` and `LOHRA_TOKEN_BUDGET_CAP`; it does not tell the agent to ask a human for a larger authored `token_budget` as though the spec controlled the operator ceiling.

## Command 2 — zero-token watch with 60-second watchdog

- Result: **PASS**
- Exit code: `0`
- Wall-clock: `0.36 s`
- Cost: N/A; `workflow watch` emitted no JSON envelope and made no LLM call.

It exited on its own well before the 60-second watchdog. Exact stdout:

```text
7b320f80  paused [token_budget_exhausted]  1/1 nodes  0/100 tok  three-planet-paragraphs
```

Exact stderr line:

```text
workflow run '7b320f8082694de6b8e50b13aebd6308' paused (token_budget_exhausted) — watch is stopping: this run's ceiling is 100 tokens and this process's operator ceiling is 100, so a resume launched from here is clamped to 100 and would pause again on the first spawn; nothing resumes it on its own — report the token_budget/spend fields and the case for more to the HUMAN OPERATOR, who raises or unsets --token-budget-cap (lohra chat) / LOHRA_TOKEN_BUDGET_CAP and relaunches. A relaunch alone does NOT unstick it: the resume must also carry run_workflow(resume_run_id=..., token_budget=<above what the run already spent>), because the ceiling it would otherwise inherit is the spent one
```

The line explicitly names both `HUMAN OPERATOR` and `LOHRA_TOKEN_BUDGET_CAP`.

## Command 3 — uncapped no-tools control

- Result: **PASS**
- Exit code: `0`
- Wall-clock: `2.53 s`
- Envelope cost: `{"usd": 0.06824, "gross_usd": 0.06824, "saved_usd": 0.0, "basis": "api_equivalent", "source": "snapshot 2026-08-28"}`
- Envelope error: `null`

Exact reply field:

```json
"output": "PONG"
```

The exact top-level key list was:

```json
["api_calls", "completed", "cost", "error", "input", "model", "output", "reasoning", "session", "session_id", "stop_reason", "temperature", "tool_calls", "usage", "usage_total"]
```

`workflows` is absent.

## Surprises

1. Command 1's completed envelope embedded the entire `skill_view("workflow-authoring")` result. The turn therefore reported `137206` input tokens and an API-equivalent cost of `$0.558284`, even though the workflow itself spent `0` tokens at the fan-out gate.
2. After Command 1 had completed and written its valid envelope, the surrounding zsh capture wrapper attempted to assign `$?` to zsh's read-only variable `status`. The wrapper then exited `1`, so a direct Lohra exit code was not recorded. This happened after the Lohra process and `/usr/bin/time` completed; the saved envelope is valid, has `"error": null`, contains the complete final output, and the workflow was not rerun.
3. The background watchdog wrapper for Command 2 printed two harness-level `nice(5) failed: operation not permitted` warnings to the invoking terminal. They are not in Command 2's captured stdout or stderr, and the watched command still exited `0` in `0.36 s`.
