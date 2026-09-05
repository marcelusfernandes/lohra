# T8 — same-route re-spawn bounded by `retries`

Overall result: **PASS**

Profile used for every Lohra command: `lohra-dogfood-w75`.

OpenRouter returned the intended HTTP 400 for `nonexistent-vendor/no-such-model-xyz`; it did not silently route to a default model.

## Command 1 — `retries: 2`

- Result: **PASS**
- `run_id`: `9cd6635c21d541bd9ca4e3bf819db4d4`
- Lohra `session_id`: `60c0328fca9e4aa38350e3fc0327a528`
- CLI exit code: `0`
- Envelope `error`: `null`
- Wall-clock: `23.54 s` (external runner measurement; shell `SECONDS` reported `23`)
- Workflow token cost: `0` tokens (`tokens_spent_total`)
- Envelope cost:

```json
{"usd":0.569752,"gross_usd":0.569752,"saved_usd":0.0,"basis":"api_equivalent","source":"snapshot 2026-08-28"}
```

### Expectation (a): terminal status is `degraded` or `failed`, never `paused`

**PASS.** Exact terminal status:

```text
failed
```

### Expectation (b): three same-route attempts and an exhaustion fault

**PASS.** The status result contains exactly four faults: three attempt faults (`1/3`, `2/3`, `3/3`) followed by the exhaustion fault. Exact fault strings:

```text
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_REDACTED'} (attempt 1/3)
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_REDACTED'} (attempt 2/3)
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_REDACTED'} (attempt 3/3)
doomed: leaf failed on the same route after 3 attempt(s); re-spawns exhausted
```

### Expectation (c): no quota/pause fault and the run does not pause

**PASS.** Case-insensitive matches for `quota|paus` across all four fault strings: `0`. Terminal status was `failed`, not `paused`.

### Command 2 audit evidence

**PASS.** All `leaf.started` and `leaf.failed` audit lines for node `doomed`, rendered as compact JSON from the audit events:

```jsonl
{"event_type":"leaf.started","seq":4,"node":"doomed","attempt":0,"sub_id":"11e9d80fd70b43d1999b79f2398337e0","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":null}
{"event_type":"leaf.failed","seq":5,"node":"doomed","attempt":0,"sub_id":"11e9d80fd70b43d1999b79f2398337e0","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":"error"}
{"event_type":"leaf.started","seq":7,"node":"doomed","attempt":1,"sub_id":"fdc050d3351a453e8b0cb5cae50a18de","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":null}
{"event_type":"leaf.failed","seq":8,"node":"doomed","attempt":1,"sub_id":"fdc050d3351a453e8b0cb5cae50a18de","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":"error"}
{"event_type":"leaf.started","seq":10,"node":"doomed","attempt":2,"sub_id":"e7af02f1df6046b1945306fd2c1ab907","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":null}
{"event_type":"leaf.failed","seq":11,"node":"doomed","attempt":2,"sub_id":"e7af02f1df6046b1945306fd2c1ab907","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":"error"}
```

Exact counts:

```json
{"leaf_started":3,"leaf_failed":3,"cache_stored":0}
```

The attempts are zero-indexed in audit metadata (`0`, `1`, `2`) and correspond to the one-indexed fault markers (`1/3`, `2/3`, `3/3`). Every start and failure names `provider: openrouter` and the identical model.

## Command 3 control — `retries: 0`

- Result: **PASS**
- `run_id`: `95ff5fb842b2453eac9e64f8f2965fdf`
- Lohra `session_id`: `2414dc6b89b94876ba55f4ed956f0445`
- CLI exit code: `0`
- Envelope `error`: `null`
- Wall-clock: `18.26 s` (external runner measurement; shell `SECONDS` reported `18`)
- Workflow token cost: `0` tokens (`tokens_spent_total`)
- Envelope cost:

```json
{"usd":0.445208,"gross_usd":0.445208,"saved_usd":0.0,"basis":"api_equivalent","source":"snapshot 2026-08-28"}
```

Terminal status:

```text
failed
```

The status result contains exactly one fault, with no attempt marker and no exhaustion fault. Exact fault string:

```text
doomed: leaf error: Error code: 400 - {'error': {'message': 'nonexistent-vendor/no-such-model-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_REDACTED'}
```

All `leaf.started` and `leaf.failed` audit lines for node `doomed`:

```jsonl
{"event_type":"leaf.started","seq":4,"node":"doomed","attempt":0,"sub_id":"606d1e5da3cd41b2a0d20907fa1ad06f","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":null}
{"event_type":"leaf.failed","seq":5,"node":"doomed","attempt":0,"sub_id":"606d1e5da3cd41b2a0d20907fa1ad06f","provider":"openrouter","model":"nonexistent-vendor/no-such-model-xyz","status":"error"}
```

Exact counts:

```json
{"leaf_started":1,"leaf_failed":1,"cache_stored":0}
```

Machine-read control counts: `fault_count=1`, `attempt_marker_count=0`, `exhaustion_fault_count=0`, `quota_or_pause_fault_count=0`.

## Surprises

None. Both bogus-model calls returned the expected OpenRouter 400. No run paused, no quota fault appeared, no leaf accumulated token cost, and no failed result was cached.
