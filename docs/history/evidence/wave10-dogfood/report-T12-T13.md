# Dogfood report — T12 (token budget stop line, #71) and T13 (checkpoint reject, #74)

Branch under test: `integration/wave10` (editable install). Profile: `lohra-dogfood-w75`.
Model used for all workflow nodes: `provider: "openrouter", model: "deepseek/deepseek-chat"` (mirrors `docs/history/evidence/wave8.5-dogfood/prompt-T11.md`).
Raw stdout for every command is saved under this directory as `T1x-*-raw.json` / `T1x-*-stderr.txt`.

All costs below are `cost.usd` from the envelope, `basis: "api_equivalent"` (flat subscription — not a literal per-call bill), `source: "snapshot 2026-08-28"`.

---

## T12 — token budget as a pre-spawn stop line (issue #71)

### T12(a) — overrun visible

Command: one `agent` node, prompt "Write exactly 40 words about rivers.", `token_budget: 50`.
File: `T12a-raw.json`. Wall-clock: 38.1 s. Cost: $0.603716. `usage_total`: 148124 in / 561 out / 181 reasoning.

Result (`run_id 0eaa11f98e554cf0a2d35787da88158c`):
```json
{
  "status": "complete",
  "token_budget": {"total": 50, "spent": 714, "remaining": 0, "overrun": 664, "overrun_max": 664},
  "faults": ["token budget overrun: spent 714 of 50 (leaf write)"],
  "outputs": {"write": "Rivers are vital natural waterways, ..."}
}
```

| Expectation | Verdict | Evidence |
|---|---|---|
| `status: complete` (not degraded) | **PASS** | `"status": "complete"` |
| `token_budget.spent > 50` | **PASS** | `spent: 714` |
| `overrun > 0` and `overrun_max` present | **PASS** | `overrun: 664, overrun_max: 664` |
| one fault, exact shape "token budget overrun: spent X of 50 (leaf \<node\>)" | **PASS** | `"token budget overrun: spent 714 of 50 (leaf write)"` — verbatim match of the template |
| NOT degraded | **PASS** | status is `complete` |

### T12(b) — stop line + renewal checkpoint

Probe run (`T12-probe-raw.json`, run `fad9d3c8...`, 86.5 s, $0.549444): a single isolated `agent` leaf ("Reply with the single word OK.") cost **657 tokens** (655 in + 2 out). Used N=657 for the first attempt.

**Attempt 1** (`token_budget: 1643 ≈ 2.5×657`, `T12b-1-raw.json`, run `bb952f74...`, 66.2 s, $0.440726): node `a`, run **inside a 3-node chain**, actually cost **857** tokens (854 in + 3 out) — 30% higher than the isolated probe (the harness's per-leaf overhead differs once a DAG with dependents exists). Because the estimator only had one sample, it used 857 as "measured average" and paused **before spawning `b`**, not before `c`:
```json
{"status":"paused","pause_reason":"token_budget_exhausted",
 "token_budget":{"total":1643,"spent":857,"remaining":786,"overrun":0,"overrun_max":0},
 "faults":["b: next leaf estimated at 857 tokens (measured average), only 786 left of 1643 — token budget exhausted"],
 "outputs":{"a":"OK.","b":null}}
```
This is a **SURPRISE relative to the task's calibration guidance**: the probe (isolated node, 657 tok) undersold the in-chain cost of the same prompt (857 tok), so `2.5×N` landed the pause one node earlier than intended (before `b`, not before `c`).

To reach the literal "b fits, c doesn't" shape, re-ran the identical spec with `token_budget: 2143` (`2.5×857`, the *measured* in-chain cost) — **`T12b-2-raw.json`**, run `c5801698...`, 40.5 s, $0.313655 — and it went to **`complete`**, all 3 nodes done, `spent: 691` total. Reason: node `b` and `c` hit **provider-side prompt caching** (640 of the ~655 input tokens reused from `a`'s call), so their real in-chain cost collapsed to `15 in + 2 out` each — far below the 857 "measured average" the estimator would have projected before they ran. **This is the key finding**: for a chain of near-identical leaf prompts on this model, the pessimistic first-sample estimate and the actual cached cost of later leaves diverge so much that there is effectively **no `token_budget` value that produces "pauses before `c` but not before `b`"** — a budget generous enough to survive the first (uncached, expensive) estimate check is also generous enough to sail through `b` and `c` once caching kicks in.

**Reproducing the exact "b fits, c doesn't, then resolves" shape via resume** (using the paused run from Attempt 1, `bb952f74...`, where `a` was NOT cached across resumes — Lohra's own cache replay of `a` does not carry forward the *provider's* session-level prompt cache, so `b` ran fresh and paid the full 857):

- **Resume 1**, `token_budget: 1972 = 1643 + 0.5×657` (`T12b-resume1-raw.json`, 25.8 s, $0.236112): `a` replayed from Lohra's cache (`cells_replayed: 1`, `tokens_saved: 857`), `b` executed fresh (857 tok, no provider cache carry-over), then paused again before `c`:
  ```json
  {"status":"paused","pause_reason":"token_budget_exhausted",
   "token_budget":{"total":1972,"spent":1714,"remaining":258,"overrun":0,"overrun_max":0},
   "faults":["c: next leaf estimated at 857 tokens (measured average), only 258 left of 1972 — token budget exhausted"],
   "outputs":{"a":"OK.","b":"OK.","c":null}}
  ```
  This **is** exactly the "b fits but c does not" shape the task specified, and the fault matches the template "next leaf estimated at \<X\> tokens (measured average), only \<Y\> left of \<Z\>" **verbatim**. **PASS** for this expectation (reached via 2 resumes off the mis-calibrated Attempt 1, not the originally-intended single first run — see surprise above).

  Note the bump used (`+0.5×N`) here was **not** the "does NOT buy one leaf" case predicted by the task — in this run it happened to still not buy `c` because `b`'s actual cost (857, no cache) matched the estimate. Whether a `+0.5×N` bump buys a leaf or not is contingent on caching state, not a fixed multiple of the base cost — another instance of the caching-dependent variance above.

- **Resume 2**, `token_budget: 2628 = 4×657` (`T12b-resume2-raw.json`, 28.7 s, $0.419184): reached **`complete`**.
  ```json
  {"status":"complete",
   "token_budget":{"total":2628,"spent":2371,"remaining":257,"overrun":0,"overrun_max":0},
   "cells_replayed":3,
   "outputs":{"a":"OK.","b":"OK.","c":"OK"}}
  ```
  Per-node detail shows `a` and `b` each with `"replayed": true, "replayed_cells": 1` (2 nodes replayed in this stretch), and `c` executed fresh (655 in + 2 out). **Discrepancy**: the top-level `cells_replayed` field reads **3**, not 2 — this is very likely a **cumulative-since-run-start counter** (Resume 1 replayed `a` once = 1, Resume 2 replayed `a`+`b` = 2, total 1+2=3) rather than "cells replayed in this stretch." The per-node breakdown (`a` and `b` marked replayed, `c` not) matches the task's literal intent ("a, b replayed from cache") even though the aggregate scalar does not equal 2. **PARTIAL** — flagging this as a naming/semantics ambiguity in the rollup schema (aggregate `cells_replayed` ≠ sum of per-node `replayed_cells` visible in the same response), not a functional bug — outputs for a, b, c are all present and correct.

| Expectation (as originally specified) | Verdict | Notes |
|---|---|---|
| pause before spawning `c`, fault shape "next leaf estimated at X tokens (measured average), only Y left of Z" | **PASS** (via the 2-resume path) | verbatim fault quoted above |
| resume with `budget + 0.5×N` (should NOT buy one leaf) → re-pause, same message shape | **PASS on the re-pause, but the causal claim ("does not buy one leaf") does not hold in general** — see surprise | fault re-quoted verbatim, matches template |
| resume with `token_budget = 4×N` → `complete`, `cells_replayed == 2` | **PASS on completeness/outputs, FAIL on the literal `cells_replayed` value** (it read 3, apparently cumulative) | per-node evidence (`a`,`b` replayed, `c` fresh) matches the *intent* |

### T12(c) — CLI read path for token_budget/overrun

`LOHRA_PROFILE=lohra-dogfood-w75 lohra workflow status <run_id>` **does not exist**: `lohra workflow --help` only offers `{list, watch, audit}` (confirmed via `lohra workflow status ...` → `error: argument workflow_cmd: invalid choice: 'status' (choose from list, watch, audit)`).

Tried the closest equivalents:
- `lohra workflow list` → shows, e.g., `0eaa11f9  complete  1/1 nodes  714/50 tok  harness-test` and `bb952f74  complete  3/3 nodes  2371/2628 tok  harness-test`. This **does** surface spent/total as a plain-text ratio, from which overrun is inferable (714 > 50), but there is **no structured `overrun`/`overrun_max` field** and no `--json` flag on `list` or `watch`.
- `lohra workflow audit <run_id>` → returns structured JSON events, but the `workflow.fault` event's `data.cause` is **redacted** (`{"cause": {"state": "redacted", "characters": 50}}` for the T12a run), so the fault text itself (which contains the numbers) is not recoverable this way either.

**Verdict: FAIL / not supported as literally described.** The full `token_budget` object with `overrun`/`overrun_max` is only visible through the `workflow_status` tool call inside a `lohra chat` turn (the JSON shown in T12a/T12b above); the zero-cost CLI read commands (`list`, `audit`, `watch`) do not expose it in structured form. This is a real gap worth flagging to the owner if the intent was for `list`/`audit` to be the operator's window into a paused/overrun budget without spending a token.

---

## T13 — checkpoint that knows how to say no (issue #74)

### T13(a) — `on_reject: fail` (default)

Spec: `checkpoint cp` (prompt "Approve publishing?", `accept: ["sim","yes"]`, no `on_reject`), `agent go` (`depends_on:[cp]`, prompt `Execute: ${cp}`), independent `agent side` (prompt "Say ALPHA.").

Initial run (`T13a-1-raw.json`, run `30746bec...`, 32.5 s, $0.477984):
```json
{"status":"paused","checkpoint":{"node_id":"cp","prompt":"Approve publishing?"},
 "faults":["checkpoint 'cp' is waiting for an answer: Approve publishing?"],
 "outputs":{"cp":null}}
```
- **PASS**: `status: paused`, checkpoint object has exactly `node_id`/`prompt`, **no `default` key**.
- Observed detail: `side` stayed `pending` while paused — the whole run halts at the checkpoint rather than letting the independent branch race ahead.

Resume with `checkpoint_answers={"cp": "não, cancele"}` (`T13a-2-raw.json`, run same id, 13.3 s, $0.095748):
```json
{"status":"degraded",
 "faults":["checkpoint 'cp' is waiting for an answer: Approve publishing?",
           "cp: checkpoint rejected by human: 'não, cancele'",
           "go: upstream null: cp"],
 "outputs":{"cp":null,"side":"ALPHA.","go":null}}
```
| Expectation | Verdict | Evidence |
|---|---|---|
| `go` NOT executed | **PASS** | `outputs.go: null` |
| fault "cp: checkpoint rejected by human: 'não, cancele'" | **PASS** | verbatim present |
| `status: degraded` | **PASS** | `"status": "degraded"` |
| outputs contain ALPHA | **PASS** | `outputs.side: "ALPHA."` |

### T13(b) — `on_reject: pause`

Same spec + `on_reject: "pause"` on `cp`. Initial run (`T13b-1-raw.json`, run `f91353f9...`, 21.7 s, $0.34302) → `status: paused`, checkpoint `{"node_id":"cp","prompt":"Approve publishing?"}`.

Resume with `"não"` (`T13b-2-raw.json`, 15.2 s, $0.035268):
```json
{"status":"paused",
 "checkpoint":{"node_id":"cp","prompt":"Approve publishing?","rejected":"'não'"},
 "faults":["cp: checkpoint rejected by human: 'não'","checkpoint 'cp' is waiting for an answer: Approve publishing?"]}
```
**PASS**: paused again, `checkpoint.rejected == "'não'"` verbatim, **no `default` key**.

Resume with `"SIM"` (uppercase) (`T13b-3-raw.json`, 17.1 s, $0.03616):
```json
{"status":"complete",
 "outputs":{"cp":"SIM",
            "side":"ALPHA. \n\nTask summary: ...",
            "go":"It seems like the instruction \"Execute: SIM\" is unclear. ..."}}
```
**PASS**: `status: complete`, `go` executed (not null) and its confused reply literally echoes the interpolated prompt "Execute: SIM" — confirms `${cp}` resolved to the accepted answer verbatim (case preserved, not normalized to lowercase in the interpolation) and the strip/lower-case handling only governs the accept-set match, not the value threaded downstream.

### T13(c) — validator rejects `accept` + `default` together

Spec: `checkpoint cp` with `accept: ["sim"]` and `default: "sim"`. (`T13c-raw.json`, 11.7 s, $0.207532, tool_calls: `skill_view`, `run_workflow` — refused before any node spawned.)

```
invalid workflow spec: [field_value] cp .default: a checkpoint with 'accept' is a HUMAN gate; a 'default' would auto-approve it on an unattended resume, including right after somebody answered no — drop 'default', or drop 'accept'
    e.g. accept: ["sim"]  # no default: only a person opens this gate
```
**PASS**: refused as an invalid spec, with the message stating a guarded gate ("a checkpoint with 'accept' is a HUMAN gate") cannot carry a `default` ("no default: only a person opens this gate").

### T13(d) — `completeness_check` with `required: true`

Spec: `agent work` (prompt "List three fruits."), `completeness_check cc` (`depends_on:[work]`, `task: "list FIVE fruits"`, `results: "${work}"`, `required: true`). (`T13d-1-raw.json`, run `8f63b7d1...`, 39.4 s, $0.48314.)

```json
{"status":"failed",
 "required_failure":"cc",
 "faults":["cc: completeness check found gaps: ['Two additional fruits'] — run aborted (required: true); the verdict is cached — change the spec or args to re-check"],
 "outputs":{"work":"Here are three common fruits:\n\n1. Apple\n2. Banana\n3. Orange",
            "cc":{"complete":false,"missing":["Two additional fruits"]}}}
```
| Expectation | Verdict | Evidence |
|---|---|---|
| `status: failed` | **PASS** | |
| `required_failure: cc` | **PASS** | |
| fault "cc: completeness check found gaps: [...]; the verdict is cached — change the spec or args to re-check" | **PASS (with extra clause)** | actual string inserts `— run aborted (required: true)` between the gaps list and the "verdict is cached" clause; content and order otherwise match |
| `outputs.cc` preserved as `{complete: false, missing: [...]}` | **PASS** | verbatim |

The critic did find a gap on the first try (asked for 5, got 3), so the "list TEN fruits" fallback was not needed.

---

## Cost / wall-clock summary

| Step | run_id | wall-clock | cost (usd, api_equivalent) |
|---|---|---|---|
| T12a | 0eaa11f9 | 38.1 s | 0.603716 |
| T12 probe | fad9d3c8 | 86.5 s | 0.549444 |
| T12b attempt 1 (budget 1643) | bb952f74 | 66.2 s | 0.440726 |
| T12b attempt 2 (budget 2143, full run) | c5801698 | 40.5 s | 0.313655 |
| T12b resume 1 (budget 1972) | bb952f74 | 25.8 s | 0.236112 |
| T12b resume 2 (budget 2628) | bb952f74 | 28.7 s | 0.419184 |
| T13a run | 30746bec | 32.5 s | 0.477984 |
| T13a resume | 30746bec | 13.3 s | 0.095748 |
| T13b run | f91353f9 | 21.7 s | 0.34302 |
| T13b resume "não" | f91353f9 | 15.2 s | 0.035268 |
| T13b resume "SIM" | f91353f9 | 17.1 s | 0.03616 |
| T13c (refused) | — | 11.7 s | 0.207532 |
| T13d run | 8f63b7d1 | 39.4 s | 0.48314 |
| **Total** | | **~437 s (7.3 min)** | **~$4.24** |

## SURPRISES (recap)

1. **In-chain leaf cost ≠ isolated probe cost.** An isolated single-node probe with the exact same prompt cost 657 tokens; the same prompt as the first node of a 3-node chain cost 857 tokens (+30%). Calibrating `token_budget` from an isolated probe run under-predicts in-chain cost and can shift the pre-spawn pause earlier than planned.
2. **Provider-side prompt caching collapses later-leaf cost inside one run.** In a fresh 3-node chain that ran uninterrupted, nodes 2 and 3 cost only 17 tokens each (15 in, cached, + 2 out) vs. node 1's 657 — because ~640 of the shared system/tool preamble tokens were served from cache. This makes the harness's "measured average" pre-spawn estimator (based on only the first, uncached sample) structurally too pessimistic for chains of similar leaves, to the point that **no single `token_budget` value reproduces "pauses before the 3rd node but not the 2nd"** in one shot — the estimator either blocks the 2nd node too, or (once past that gate) the 3rd is nearly free anyway.
3. **Lohra's own resume-cache replay does not carry the provider's session cache forward.** When `a` was replayed from Lohra's durable cache (no re-call to the model) rather than actually re-executed, the following fresh node (`b`) did NOT get the OpenRouter cache discount that it would have gotten had `a` really been re-sent in the same stretch — it paid the full uncached cost (857 tokens), matching node `a`'s original cost rather than the ~17-token cached cost seen in the uninterrupted run. This is what let the resume-based reproduction hit the exact "b fits, c doesn't" shape the task described.
4. **`cells_replayed` (top-level rollup) looks cumulative across resumes, not "this stretch."** On the final resume, per-node data flagged `a` and `b` as replayed (2 nodes) but the top-level `rollup.cells_replayed` read 3 — consistent with it being a running total since the run's first attempt (1 replay in resume 1 + 2 replays in resume 2 = 3), not the count of cells replayed in the current call. Worth a schema/doc clarification.
5. **No CLI-level, zero-cost way to read the structured `token_budget` object (with `overrun`/`overrun_max`) for a run.** `lohra workflow status` does not exist (only `list`/`watch`/`audit`); `list` shows a plain-text `spent/total tok` ratio with no explicit overrun field, and `audit` redacts the fault-cause text that would otherwise carry the numbers.
6. **Checkpoint pauses the whole run, not just the gated branch.** With `cp` pending, the independent `side` node stayed `pending` (not started) rather than running concurrently while the run waited on the human — the pause is run-wide, not scoped to the dependent subtree of the checkpoint.
7. **`${cp}` interpolation preserves the human's exact casing/text** ("SIM", not normalized) even though the accept-check itself is case/whitespace-insensitive (`strip/lower` per the task's own framing) — the normalization is only for gate matching, the value threaded downstream is verbatim.
