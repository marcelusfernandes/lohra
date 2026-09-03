You are running ONE live dogfood probe of the Lohra CLI (installed in this repo, editable). Use the `use-lohra` skill in `.codex/skills/use-lohra/SKILL.md`. Profile for EVERY lohra command: `--profile lohra-dogfood-w75`. Never modify files in this repository (writing under ~/.lohra and the scratchpad directory is fine). The sandbox has network access. Do not run pytest, pip, or git commit.

## T11 — the OPERATOR's route envelope re-routes a dead route without pausing; an exhausted envelope pauses
Setup: write the envelope file for the profile: `~/.lohra/profiles/lohra-dogfood-w75/workflow_routes.json` with exactly:
```json
{"routes": {"openrouter/nonexistent-vendor/no-such-model-xyz": {"fallback": ["openrouter/deepseek/deepseek-chat"], "on": ["terminal_leaf_failure"]}}, "max_fallbacks_per_run": 2}
```
(If the docs in `docs/specs/07-workflow-harness.md` §7.7 describe a different shape/path, follow the DOCS and report the difference.) Note: price comparison is fail-closed — if the report says the re-route was refused because the price of either side is unknown (OpenRouter is a dynamic-price provider), report that as the observed behaviour and check whether the docs name an override (e.g. `~/.lohra/pricing.json`) — apply it ONLY under the profile home and retry once.

Command 1 (save stdout to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/G-T11-1.json`):
`LOHRA_AUDIT=1 lohra chat --profile lohra-dogfood-w75 --json "This is a harness test; author the spec EXACTLY as described. Call run_workflow with two 'agent' nodes: node 'ok' with prompt 'Say the single word ALPHA.'; node 'doomed' with depends_on: ['ok'], provider 'openrouter', model 'nonexistent-vendor/no-such-model-xyz', retries: 1, prompt 'Say hello.'. Poll workflow_status(run_id) until terminal and reply with run_id, status, every fault string verbatim, and outputs."`
Expectations: (a) status `complete` (NOT paused); (b) a fault names the re-route by the operator envelope (old route → new route, `route_envelope`); (c) `LOHRA_PROFILE=lohra-dogfood-w75 lohra workflow audit <run_id>` shows one `node.rerouted` event with `channel: route_envelope`, `from` the bogus model, `to` deepseek; (d) outputs contain ALPHA and a hello.
Command 2 (exhausted envelope): remove the fallback list (`"fallback": []`) in the same file, run the same spec again → expectation: `paused` / `route_fault` with a hint that mentions the envelope was consulted/exhausted.
Restore the envelope file to the Command 1 content at the end (leave it in place).

## Report
Write Markdown to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/dogfood-G-report.md`: PASS/FAIL per expectation with exact quoted evidence, run_ids, wall-clock and `cost` per envelope, surprises (especially anything about price fail-closed). Be literal.
