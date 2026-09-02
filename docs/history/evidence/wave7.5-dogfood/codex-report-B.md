# Lohra CLI dogfood battery B

Profile used for every Lohra command: `lohra-dogfood-w75`.

No transient provider error containing `overloaded` or `429` occurred, so no retry was performed.

## T4 — PASS

Run ID: `06c2cfe184be4ee8b3bd9e08fe04d6a1`

Chat exit code: `0`  
Wall clock: `59.75s`

Exact envelope/tool evidence:

```json
"completed": true,
"error": null,
"status": "failed"
"progress": {"total": 2, "done": 2, "running": 0, "pending": 0, "nodes": [{"id": "a", "state": "null"}, {"id": "b", "state": "skipped"}]}
"faults": ["a: leaf timeout after 1s (cancelled; 1 leaf STILL RUNNING after 5.0s quiescence wait — shared working_root may be mutated)", "a: required node resolved to null — run aborted (required: true; the remaining nodes were not scheduled)", "b: skipped: required upstream 'a' failed"]
"outputs": {"a": null}
"required_failure": "a"
```

The accepted quiescence variant occurred exactly:

```text
a: leaf timeout after 1s (cancelled; 1 leaf STILL RUNNING after 5.0s quiescence wait — shared working_root may be mutated)
```

The other exact faults were:

```text
a: required node resolved to null — run aborted (required: true; the remaining nodes were not scheduled)
b: skipped: required upstream 'a' failed
```

Node `b` did not run: it is `skipped`, `outputs` contains only `a`, and the only audited `leaf.started` event is:

```json
{"event_type":"leaf.started","node_path":["a"],"sub_id":"e4a669a327724f1191474dbb8bf9c9ae"}
```

Audit exit code: `0`  
Audit wall clock: `0.39s`

Node lifecycle lines from the audit:

```json
{"event_type":"node.started","node_path":["a"],"data":{"state":"running"}}
{"event_type":"node.failed","node_path":["a"],"data":{"state":"null"}}
{"event_type":"node.failed","node_path":["b"],"data":{"state":"skipped"}}
```

Envelope `cost`:

```json
{"usd":0.46764,"gross_usd":0.550584,"saved_usd":0.082944,"basis":"api_equivalent","source":"snapshot 2026-08-28"}
```

Surprise: the audit represented all three `workflow.fault` causes as redacted metadata, including `"cause": {"state": "redacted", "characters": 122}`. The lifecycle evidence itself was not redacted: `b` is explicitly `"state":"skipped"`. Lohra also called `skill_view` before `run_workflow`; its submitted spec still matched the requested two-node shape and fields exactly.

## T5 — PASS

Run ID: `41f725ac62374979b6a7187dafd9b3d6`

Chat exit code: `0`  
Wall clock: `39.84s`

Top-level `workflows` value, verbatim:

```json
[
  {
    "run_id": "41f725ac62374979b6a7187dafd9b3d6",
    "status": "paused",
    "pause_reason": "token_budget_exhausted"
  }
]
```

This was the accepted `paused` variant, not `cancelled_on_exit`. Exact evidence from the single non-waiting status call:

```json
"status": "paused"
"reason": "token_budget_exhausted"
"token_budget": {"total": 100, "spent": 0, "remaining": 100}
"progress": {"total": 1, "done": 1, "running": 0, "pending": 0, "nodes": [{"id": "fan", "state": "null"}]}
"faults": ["fan: fan-out of 3 needs about 6000 tokens; only 100 of the 100 token budget are left (~0 leaf/leaves) — token budget exhausted"]
```

Envelope `cost`:

```json
{"usd":0.303356,"gross_usd":0.552188,"saved_usd":0.248832,"basis":"api_equivalent","source":"snapshot 2026-08-28"}
```

`workflow list` exit code: `0`; wall clock: `0.29s`.

Exact row:

```text
41f725ac  paused [token_budget_exhausted]  1/1 nodes  0/100 tok  three-ocean-paragraphs
```

`workflow watch` exit code: `0`; measured wall clock: `1s`. The 60-second watchdog remained `armed`, so watch exited on its own.

Watch stdout:

```text
41f725ac  paused [token_budget_exhausted]  1/1 nodes  0/100 tok  three-ocean-paragraphs
```

Watch stderr:

```text
workflow run '41f725ac62374979b6a7187dafd9b3d6' paused (token_budget_exhausted) — watch is stopping: the run spent its token budget; nothing will resume it on its own — report the available token_budget/spend fields and the case for more to the HUMAN; only after the human supplies a larger cap verbatim, use run_workflow(resume_run_id=..., token_budget=<human-authorized cap>)
```

Surprise: none in Lohra's behavior. The surrounding `zsh` printed `nice(5) failed: operation not permitted` while launching the two watchdog background jobs. That message was outside Lohra's captured streams and did not affect watch, which exited independently with code `0` in 1 second.

## T6 — PASS

### Command 1 — invalid value ignored

Session ID: `0c2a44ac51d44fce9d3373a1c5bda9a4`  
Exit code: `0`  
Wall clock: `4.37s`

Exact envelope evidence:

```json
"completed": true,
"output": "PONG",
"error": null
```

Exact stderr warning:

```text
LOHRA_PROVIDER_READ_TIMEOUT='abc' is not a number; ignoring, default read timeout (600s) stays in effect
```

Envelope `cost`:

```json
{"usd":0.009155,"gross_usd":0.067216,"saved_usd":0.058061,"basis":"api_equivalent","source":"snapshot 2026-08-28"}
```

### Command 2 — 0.05-second timeout honored

Session ID: `c721710df58d4a5d8b14de5ec33c6188`  
Exit code: `1`  
Wall clock: `2.62s` (within the expected ~15-second ceiling)

Exact envelope evidence:

```json
"output": null,
"usage": null,
"usage_total": null,
"cost": null,
"stop_reason": null,
"completed": false,
"error": "Request timed out.",
"api_calls": 1
```

Envelope `cost`, verbatim:

```json
null
```

Exact stderr error:

```text
error: Request timed out.
```

Surprise: none. The nonzero exit is consistent with the intentionally incomplete timeout envelope.

## Raw artifacts

- `B-T4-1.json` — raw T4 chat stdout
- `B-T4-audit.txt` — raw T4 audit stdout
- `B-T5-1.json` — raw T5 chat stdout
- `B-T5-list.txt` — raw T5 list stdout
- `B-T5-watch.txt` and `B-T5-watch.stderr` — raw T5 watch streams
- `B-T6-1.json` — raw T6 command 1 chat stdout
- `B-T6-2.json` — raw T6 command 2 chat stdout
