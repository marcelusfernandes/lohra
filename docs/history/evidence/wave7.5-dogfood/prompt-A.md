You are running a live dogfood battery of the Lohra CLI (installed in this repo, editable). Use the `use-lohra` skill in `.codex/skills/use-lohra/SKILL.md` for the delegation shape. Profile to use for EVERY lohra command: `--profile lohra-dogfood-w75` (subscription already enabled there). Never modify files in this repository (writing under ~/.lohra and the scratchpad directory is fine and expected). The sandbox has network access. Do not run pytest. Do not run `pip`, `git commit`, or anything destructive.

For each test below: run the commands, capture RAW outputs (save each `lohra chat --json` stdout to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/A-T<n>-<k>.json`), then judge PASS/FAIL against the expectation, quoting the exact evidence (fault strings, JSON fields). If a lohra turn errors with a transient provider error ("overloaded", 429), retry once after 20 seconds.

## T1 — leaf sandbox denies `terminal` by default (issue #4)
Command 1:
`lohra chat --profile lohra-dogfood-w75 --json "Use run_workflow with a spec containing exactly ONE node: id 'shell', type 'agent', prompt: 'Use the terminal tool to run the shell command: python3 -c \"print(6*7)\" and report ONLY the exact tool result you received, verbatim, including any error text.' Then poll workflow_status(run_id) until it is terminal and reply with: the run status, every fault string verbatim, and the leaf's output verbatim. Do not modify any files. Do not add extra nodes."`
Expectation: the leaf output (or fault) contains the sandbox denial: text like `'terminal' tool is disabled for workflow leaves (sandbox denied)` and names the remedy `allow_terminal` / `LOHRA_LEAF_ALLOW_TERMINAL`. It must NOT contain `42`.
Command 2 (opt-in): the same command prefixed with `LOHRA_LEAF_ALLOW_TERMINAL=1 `.
Expectation: the leaf output contains `42`.
Also run `LOHRA_PROFILE=lohra-dogfood-w75 lohra workflow list` and paste the lines for these two runs.

## T2 — disconnected DAG lint (issue #49)
`lohra chat --profile lohra-dogfood-w75 --json "This is a validator test: call run_workflow with a spec of exactly TWO nodes, both type 'agent', ids 'left' and 'right', prompts 'Say the single word LEFT.' and 'Say the single word RIGHT.', with NO depends_on field and NO \${ref} between them. The disconnected shape IS the test — do not connect them and do not wrap them in a parallel node. Reply with the run_workflow tool result verbatim as JSON (the whole dict you received), then wait for the run with workflow_status and report its final status."`
Expectation: the run_workflow result contains a `warnings` list with rule `disconnected_dag` and a message mentioning `parallel`/`depends_on`; the run still completes (`complete`).

## T3 — malformed depends_on is rejected (issue #2)
`lohra chat --profile lohra-dogfood-w75 --json "This is a validator test: call run_workflow with a spec of two 'agent' nodes 'a' and 'b' where node b has depends_on set to the STRING \"a\" (not a list). Send it exactly like that on purpose. Reply with the tool result verbatim (the validation error text, including any example it gives). Do not retry with a corrected spec."`
Expectation: run_workflow rejects with an issue whose rule is `depends_on_type`, text like `'depends_on' must be a list of node id strings, not str`, and an example like `depends_on: ["scan"]`. No run is started.

## Report
Write the final report in Markdown to `/private/tmp/claude-501/-Users-marcelusfernandes-Desktop-playground-ai-lohra/5656cdd2-c639-4295-ab2c-d035aceec985/scratchpad/dogfood-A-report.md`: one section per test with PASS/FAIL, the run_ids, the exact quoted evidence, and any surprise (unexpected fault, agent deviating from the instruction, confusing message). Be literal; do not paraphrase fault text. Also note total wall-clock per lohra turn and the `cost` field of each envelope.
