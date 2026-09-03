You are running ONE live dogfood probe of the Lohra CLI (installed in this repo, editable). Use the `use-lohra` skill in `.codex/skills/use-lohra/SKILL.md`. Profile for EVERY lohra command: `--profile lohra-dogfood-w75`. Never modify files in this repository (writing under ~/.lohra and the scratchpad directory is fine). The sandbox has network access. Do not run pytest, pip, or git commit.

## T8 — same-route re-spawn on a terminal provider failure is bounded by `retries` (issue #43, epic E1)
The trigger is a model id that does not exist on OpenRouter, so the provider answers with a 4xx error (not quota, not timeout) and the leaf dies without cost.

Command 1 (save stdout to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/D-T8-1.json`):
`LOHRA_AUDIT=1 lohra chat --profile lohra-dogfood-w75 --json "This is a harness test; author the spec EXACTLY as described and do not 'improve' it. Call run_workflow with ONE 'agent' node: id 'doomed', provider 'openrouter', model 'nonexistent-vendor/no-such-model-xyz', retries: 2, prompt 'Say hello.'. Then poll workflow_status(run_id) until terminal and reply with: final status, and EVERY fault string verbatim, one per line."`
Expectations: (a) final status `degraded` or `failed` (not `paused`); (b) faults show the leaf failed and was re-tried on the SAME route up to `retries` times — look for attempt markers like `attempt 1/3`, `attempt 2/3`, `attempt 3/3` (or equivalent wording naming attempts) and a final fault saying the retries were exhausted; (c) NO fault mentions quota/pause; the run did NOT pause.
Command 2: `LOHRA_PROFILE=lohra-dogfood-w75 lohra workflow audit <run_id>` — paste all `leaf.started` and `leaf.failed` lines for node `doomed`. Expectation: exactly 3 `leaf.started` (1 + 2 retries), all with `provider: openrouter` and the same model; 0 `cache.stored` for `doomed`.
Command 3 (control): the same chat command but with `retries: 0` → expectation: exactly 1 `leaf.started` in the audit and a single failure fault, no attempt markers beyond the first.
If OpenRouter returns something other than a 4xx for the bogus model (e.g. it silently routes to a default model and SUCCEEDS), report that as a surprise and stop — do not try other tricks.

## Report
Write Markdown to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/dogfood-D-report.md`: PASS/FAIL per expectation with exact quoted evidence (fault strings, audit lines), run_ids, wall-clock and `cost` per envelope, surprises. Be literal.
