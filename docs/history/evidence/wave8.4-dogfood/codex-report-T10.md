# T10 dogfood report — FAIL

Run ID: 6e26d53d16014dc6ac0e3bf37e916d5b

Overall result: **FAIL**. The command-channel reroute worked, the completed “ok” cell replayed, and the run completed. Expectation (e) nevertheless fails because the workflow audit itself contains no fault/event naming the old route, new route, and human-command provenance. That exact fact appears only in terminal workflow_status.faults_total.

## Expectation results

### (a) PASS — dead route paused the run

Command 1’s workflow_status said exactly:

> "status": "paused", "reason": "route_fault"

The dead route was:

> "node_id": "doomed", "provider": "openrouter", "model": "nonexistent-vendor/no-such-model-xyz"

The bogus model did not route to a default. OpenRouter returned:

> nonexistent-vendor/no-such-model-xyz is not a valid model ID

It failed on attempts “1/2” and “2/2”, then paused.

### (b) PASS — hint prescribes COMMAND and preserves SUP-04

The full hint text, verbatim, was:

> a route this run depends on is DEAD, so the run stopped instead of scheduling more nodes onto it; nothing resumes it on its own (no resume_at, no auto-resume). Read the 'route' field — it names the provider, the model, the node and the failure kind. ANSWER IT BY COMMAND, on the SAME run: run_workflow(resume_run_id=..., checkpoint_answers={"<route.node_id>": {"provider": "...", "model": "...", "effort": "..." (optional)}}) re-routes that ONE node in the spec already on file, and checkpoint_answers={"<route.node_id>": "abort"} cancels the run instead. Every completed cell replays from the cache, so only the node that died is paid for again. You may choose the new route YOURSELF only within the SAME provider and the SAME credential/billing route, with catalog evidence and never onto a costlier model; a different provider, a different billing route, an unknown or higher cost, and any refused credential (401/403) are the HUMAN's decision — report the dead route and the case for a change, and pass back only what the human answered verbatim. The answer moves ONLY that node's provider/model/effort: to change anything else, send the whole adapted spec instead (run_workflow(resume_run_id=..., spec=<adapted spec>)) — one channel per resume, never both. If the dead node routes by 'tier', answer with BOTH 'provider' and 'model': a model alone leaves the tier's provider in place and the node dies on the same route again

This includes the required command shape and the SUP-04 boundary:

> run_workflow(resume_run_id=..., checkpoint_answers={"<route.node_id>": {"provider": "...", "model": "..." ...}})

> only within the SAME provider and the SAME credential/billing route, with catalog evidence and never onto a costlier model

> a different provider, a different billing route, an unknown or higher cost ... are the HUMAN's decision

### Wrong-node control PASS — refused didactically and remained paused

Command 2’s tool result, verbatim, was:

> {"error": "workflow run '6e26d53d16014dc6ac0e3bf37e916d5b' is paused on the dead route of node 'doomed'; ok is not that node. While a run is paused on a route, the only answer it reads is the one for the node that died (answers already given to checkpoint nodes are cached — they never need re-sending)."}

The subsequent zero-LLM list showed exactly:

> 6e26d53d  paused [route_fault]  2/2 nodes  363 tok  harness-test

### (c) PASS — human answer accepted without a spec; cache preview present

Command 3 called run_workflow with these complete arguments; there is no “spec” key:

> {"resume_run_id":"6e26d53d16014dc6ac0e3bf37e916d5b","checkpoint_answers":{"doomed":{"provider":"openrouter","model":"deepseek/deepseek-chat"}}}

The tool result, verbatim, was:

> {"ok": true, "run_id": "6e26d53d16014dc6ac0e3bf37e916d5b", "status": "started", "rerouted": {"node_id": "doomed", "from": "openrouter/nonexistent-vendor/no-such-model-xyz", "to": "openrouter/deepseek/deepseek-chat"}, "cache_preview": {"replay": 1, "invalidate": 0, "never_completed": 1, "tokens_to_repay": 0, "invalidated": []}}

Required replay evidence:

> "cache_preview": {"replay": 1, ...}

### (d) PASS — terminal completion and preserved output

The terminal status was:

> "status": "complete"

The outputs were:

> {"ok": "ALPHA", "doomed": "Hello! How can I assist you today?"}

### (e) FAIL — one “ok” leaf start passed, but explicit reroute audit event is missing

The audit count is exactly:

> "ok_leaf_started": 1

The audit also contains one replay event for “ok”:

> {"seq":21,"event_type":"cache.replayed","node_path":["ok"],"data":{"tokens_saved":363}}

So “ok” did not execute again. The audit has two old-route leaf.started events for “doomed” (the authored retry series) and one new-route leaf.started event:

> "old_route_leaf_started": 2

> "new_route_leaf_started": 1

But the number of audit events whose data explicitly names a reroute or human/command provenance is:

> "explicit_reroute_events": 0

The audit exposes the old route in leaf.started sequences 10 and 13 and the new route in sequence 25, but no single fault/event names:

> openrouter/nonexistent-vendor/no-such-model-xyz -> openrouter/deepseek/deepseek-chat; answered through checkpoint_answers (the command channel)

That exact text exists only in final workflow_status.faults_total:

> doomed: re-routed after a route_fault pause — openrouter/nonexistent-vendor/no-such-model-xyz -> openrouter/deepseek/deepseek-chat; answered through checkpoint_answers (the command channel), never chosen by the harness

Because the expectation specifically requires workflow audit to show a fault/event naming that reroute, this expectation fails.

### (f) PASS — final status omits active reason and route

The final workflow_status had:

> "status": "complete"

Direct key checks returned:

> "has_reason": false

> "has_route": false

Historical route-fault and reroute facts remain in faults_total, but there are no active top-level reason or route fields.

## Envelope wall-clock and cost

| Command | Session ID | Wall-clock | cost |
| --- | --- | ---: | --- |
| 1 | ed5ebd70dc9042318e8b0dcf57613b87 | 49.63s | {"usd":0.487507,"gross_usd":0.600864,"saved_usd":0.113357,"basis":"api_equivalent","source":"snapshot 2026-08-28"} |
| 2 | 3721e4ac7e0045dbaeed71caf3fe7d07 | 7.52s | {"usd":0.139704,"gross_usd":0.139704,"saved_usd":0.0,"basis":"api_equivalent","source":"snapshot 2026-08-28"} |
| 3 | f0f3eb7a476a4e89b765ff81751cc689 | 14.93s | {"usd":0.217,"gross_usd":0.217,"saved_usd":0.0,"basis":"api_equivalent","source":"snapshot 2026-08-28"} |

All three envelopes had "completed": true and "error": null.

## Surprises

1. **Audit visibility gap:** the explicit reroute/human-command sentence is present in terminal workflow_status.faults_total but absent from the durable audit event stream. This is the only expectation failure.
2. **Documented profile-flag placement mismatch:** the checked-in use-lohra skill documents “lohra workflow list --profile ...”, but this CLI rejected that placement with “lohra: error: unrecognized arguments: --profile lohra-dogfood-w75”. “lohra workflow --profile lohra-dogfood-w75 list” succeeded. The rejected read-only invocation made no state change and still included the required profile flag.
3. **No bogus-model fallback:** OpenRouter rejected the bogus model with HTTP 400, so the instructed surprise-stop condition did not trigger.

No repository files were modified. No pytest, pip, or git commit command was run.
