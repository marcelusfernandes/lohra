# Dogfood E — T9 dead-route pause and adapted cache reuse

**Overall: PASS**

- Profile: `lohra-dogfood-w75`
- Run ID: `09745ca2cf6c4834a4b9e5a4d4c5b15c`
- The bogus OpenRouter model did **not** succeed; it returned HTTP 400 and the run paused as designed.
- No repository files were modified.

## Commands, wall-clock, and envelope cost

| Command | Exit | Wall-clock | Envelope error | Envelope cost |
| --- | ---: | ---: | --- | --- |
| Command 1 (`lohra chat --json`) | 0 | 47 s | `null` | `{"usd":0.649677,"gross_usd":0.70912,"saved_usd":0.059443,"basis":"api_equivalent","source":"snapshot 2026-08-28"}` |
| Command 2 (`lohra workflow watch`) | 0 | <1 s (`SECONDS=0`; executor observed 0.4 s) | no JSON envelope | no cost; durable-state reader |
| Command 3 (`lohra chat --json`) | 0 | 40 s | `null` | `{"usd":0.809652,"gross_usd":0.809652,"saved_usd":0.0,"basis":"api_equivalent","source":"snapshot 2026-08-28"}` |
| Audit (`lohra workflow audit`) | 0 | <1 s (`SECONDS=0`; executor observed 0.5 s) | no JSON envelope | no cost; durable-state reader |

Command 1 session: `e692b5e34d3f47d9b3556138caf32dac`  
Command 3 session: `8af0ad6ab1e74994b590ea4bab3f004a`

## Expectations

### (a) PASS — exhausted dead route paused as `route_fault`

Exact Command 1 answer evidence:

```text
- **run_id:** `09745ca2cf6c4834a4b9e5a4d4c5b15c`
- **status:** `paused`
- **reason:** `route_fault`
```

Exact `workflow_status` result fields:

```json
{"status":"paused","reason":"route_fault","resume_at":null,"attempts":0}
```

It was not reported as `degraded` or `failed`.

### (b) PASS — both same-route attempts, exhaustion, node, provider, and model are present

The complete `faults` array from the first status result contained exactly these strings:

```text
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'} (attempt 1/2)
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'} (attempt 2/2)
doomed: leaf failed on the same route after 2 attempt(s); re-spawns exhausted — run paused (route_fault): openrouter/nonexistent-vendor/no-such-model-xyz is not usable for this run, so no further node was scheduled onto it
```

Exact route payload:

```json
{"node_id":"doomed","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","error_kind":null,"cause":"doomed: leaf failed on the same route after 2 attempt(s); re-spawns exhausted","last_error":"Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'}"}
```

### (c) PASS — top-level envelope reports `pause_reason: "route_fault"`

Exact Command 1 top-level `workflows` value:

```json
[{"run_id":"09745ca2cf6c4834a4b9e5a4d4c5b15c","status":"paused","pause_reason":"route_fault"}]
```

### (d) PASS — hint constrains adaptation to the same billing route and does not recommend a pricier model

Exact hint from the first `workflow_status` result:

```text
a route this run depends on is DEAD, so the run stopped instead of scheduling more nodes onto it; nothing resumes it on its own (no resume_at, no auto-resume). Read the 'route' field — it names the provider, the model, the node and the failure kind. You may adapt the spec YOURSELF only within the SAME provider and the SAME credential/billing route, with catalog evidence and never onto a costlier model; a different provider, a different billing route, an unknown or higher cost, and any refused credential (401/403) are the HUMAN's decision — report the dead route and the case for a change, and act only on what the human answers verbatim. Either way resume the SAME run with run_workflow(resume_run_id=..., spec=<the adapted spec>): every completed cell replays from the cache, so only the node that died is paid for again
```

This explicitly says `never onto a costlier model`; it does not suggest one.

### Command 2 PASS — watch exited by itself and named `route_fault` on stderr

It exited code 0 in under one second, well before the 60-second alarm. Exact stderr:

```text
workflow run '09745ca2cf6c4834a4b9e5a4d4c5b15c' paused (route_fault) — watch is stopping: a route this run depends on is DEAD, so the run stopped instead of scheduling more nodes onto it; nothing resumes it on its own (no resume_at, no auto-resume). Read the 'route' field — it names the provider, the model, the node and the failure kind. You may adapt the spec YOURSELF only within the SAME provider and the SAME credential/billing route, with catalog evidence and never onto a costlier model; a different provider, a different billing route, an unknown or higher cost, and any refused credential (401/403) are the HUMAN's decision — report the dead route and the case for a change, and act only on what the human answers verbatim. Either way resume the SAME run with run_workflow(resume_run_id=..., spec=<the adapted spec>): every completed cell replays from the cache, so only the node that died is paid for again
```

Exact stdout summary:

```text
09745ca2  paused [route_fault]  2/2 nodes  363 tok  harness-test
```

### (e) PASS — adapted same-run resume preview replays `ok` and marks one cell never completed

The run ID stayed `09745ca2cf6c4834a4b9e5a4d4c5b15c`. Command 3 used `provider: "openrouter"`, `model: "deepseek/deepseek-chat"`, and `retries: 1` for `doomed`.

Exact `run_workflow` tool result, also reproduced verbatim at the start of Lohra's answer:

```json
{"ok": true, "run_id": "09745ca2cf6c4834a4b9e5a4d4c5b15c", "status": "started", "cache_preview": {"replay": 1, "invalidate": 0, "never_completed": 1, "tokens_to_repay": 0, "invalidated": []}}
```

Thus `replay` is 1, while the previously failed `doomed` cell is represented by `never_completed: 1` rather than invalidation.

### (f) PASS — final status is complete, `ok` is replayed as `ALPHA`, and was started only once

Exact terminal status and outputs:

```json
{"status":"complete","outputs":{"ok":"ALPHA","doomed":"Hello! How can I assist you today?"}}
```

The terminal progress was exactly:

```json
{"total":2,"done":2,"running":0,"pending":0,"nodes":[{"id":"ok","state":"complete"},{"id":"doomed","state":"complete"}]}
```

Audit evidence for `ok` across both segments:

```text
seq 4:  event_type="leaf.started",  node_path=["ok"], segment_id="de5c8639cdb644f9b105c89013c6ff93"
seq 21: event_type="cache.replayed", node_path=["ok"], segment_id="43c9b32a58a047dfb5765c1fca8ace2d", data={"tokens_saved":363}
```

Literal counts computed from the audit JSON:

```json
{"ok_leaf_started_count":1,"ok_cache_replayed_count":1}
```

The audit also records the first segment as `paused` and the resume segment as `complete`:

```text
seq 18: segment.completed data={"status":"paused"}
seq 19: segment.started data={"resume":true,"recovered_process":false,"spec_name":"harness-test","spec_version":0}
seq 29: segment.completed data={"status":"complete"}
```

## Surprises

- Semantic surprise: **none**. OpenRouter rejected the bogus model with HTTP 400; it did not route it to a default.
- CLI parsing surprise: an initial watch setup that combined `LOHRA_PROFILE=lohra-dogfood-w75` with a trailing `--profile lohra-dogfood-w75` was rejected with `lohra: error: unrecognized arguments: --profile lohra-dogfood-w75` (exit 2). The actual requested Command 2 was then run with the documented environment-profile form and passed. This setup error did not touch or resume the workflow.

## Preserved artifacts

- `E-T9-1.json` — raw Command 1 stdout, as requested
- `E-T9-1.stderr` — Command 1 stderr/live view
- `E-T9-2.stdout` / `E-T9-2.stderr` — successful watch output
- `E-T9-3.json` / `E-T9-3.stderr` — adapted resume envelope and live view
- `E-T9-audit.stdout` / `E-T9-audit.stderr` — durable audit output
