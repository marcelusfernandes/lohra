---
name: workflow-authoring
description: Choose, size and author a dynamic workflow spec for run_workflow, and read its rollup honestly. Load this before authoring any workflow — it covers which node type fits which task shape, the parallel-vs-pipeline barrier trap, how much fan-out and verification a request actually deserves, schemas and refs, and what complete/degraded/failed/paused really mean.
version: 1.0.0
---

# Authoring dynamic workflows

`run_workflow` takes a declarative DAG and runs it as a swarm of isolated
sub-agents. The hard part is never the syntax — the validator will teach you
that, with a corrected example, before anything spawns. The hard part is
**judgement**: which shape fits the task, how big to make it, and whether to
believe the result. That is what this skill is for.

Reach for a workflow when the work is **wide** (many independent units), **needs
adversarial pressure** (a claim someone will act on), or **is worth caching**
(long, resumable, expensive). For anything you can just do yourself in a few
tool calls, do it yourself — a workflow costs a process, a pool and real tokens.

---

## 1. Task shape → node choice

| The task looks like | Use | Why |
| --- | --- | --- |
| N independent checks, all needed before you can say anything | `parallel` | Barrier fan-out; results arrive as a list in input order |
| N items, each walking the same stages | `pipeline` | No barrier between items — a fast item finishes while a slow one is still on stage 1 |
| "Keep digging until there's nothing left", size unknown up front | `loop_until_dry` | Repeats a body until K consecutive empty rounds, hard-capped by `max_rounds` |
| One claim that someone will act on | `verify` | N skeptics are told to *refute* it; a majority refutation kills it |
| Wide solution space, quality varies per attempt | `judge_panel` | N attempts → independent judges → the winner is rewritten by a synthesis prompt |
| A shape you've already proven | `workflow` | Runs a saved template by `ref`, one nesting level deep |
| One answer that has to meet a standard | `gate` | Draft → a reviewer leaf judges it → re-draft with the feedback, bounded |
| "Did we actually cover everything?" | `completeness_check` | One critic leaf answering the fixed `{complete, missing}` |
| A step a human must sign off before it happens | `checkpoint` | Pauses the run and asks; spawns nothing at all |
| A single leaf of work | `agent` | One prompt, one sub-agent, optional validated JSON back |

Composition beats cleverness: most good specs are two or three nodes — a wide
node, then one `agent` that reads its output and writes the answer.

### What each node returns

Downstream refs read these, so pick with the shape in mind:

- `agent` — the leaf's text, or the parsed object when a schema is set; `null` if it died.
- `parallel` — a list, one entry per branch, in input order. **Branch outputs are never schema-validated** (branches are plain prompts). If you need structure per unit, use `pipeline` stages or separate `agent` nodes.
- `pipeline` — a list in item order; a dropped item is `null` in its slot.
- `verify` — `{finding, survived, refuted, skeptics, verdicts}`. `finding` is `null` when it did not survive.
- `judge_panel` — the synthesized output.
- `loop_until_dry` — the list of non-empty round outputs.
- `workflow` — the nested run's outputs, keyed by nested node id.
- `gate` — the body output that PASSED review (parsed if the body has a schema); `null` if no attempt ever passed.
- `completeness_check` — `{complete, missing}`; `missing` is a list of strings.
- `checkpoint` — whatever the human answered (or the declared `default`).

---

## 2. The barrier smell test (parallel vs pipeline)

`parallel` is a **barrier**. Nothing downstream starts until every branch has
returned. That is correct when the next step needs the whole set:

> "Score these 12 candidate designs, then pick the best one." — the picker needs
> all 12 scores. `parallel` + an `agent` that reads `${scores}`.

`pipeline` has **no barrier between items**. Each item is chained through the
stages on its own, so item 3 can finish all its stages while item 7 is still on
stage 1:

> "For each of these 12 files: read it, classify it, write a fix note." — nothing
> about file 7 depends on file 3. `pipeline` with three stages.

**The test:** ask whether the next step processes each item *independently*. If
yes, it is a pipeline stage, not a downstream node behind a barrier. Modelling
per-item work as `parallel` → `parallel` → `parallel` makes every item wait for
the slowest item at *every* stage — the same total work, several times the
wall-clock, and a single slow unit stalls the whole run.

The mirror mistake is just as bad: using `pipeline` for work that genuinely
needs the full set (a ranking, a dedup, a total) gives you N independent
opinions and nobody to reconcile them.

---

## 3. Sizing: match the fan-out to the ask

Cost grows as *fan-out × stages × leaves-per-node*. Verify multiplies by its
skeptic count; `judge_panel` multiplies attempts by judges. Size to the request:

- **A quick question** — 2–5 leaves, no `verify`, no judges. One wide node and one
  synthesis. If you're authoring 20 leaves for a question the user expects an
  answer to in a minute, you picked the wrong tool.
- **A real piece of work** — 5–15 leaves. Schemas on anything downstream reads.
  Verify only the one or two claims the user will act on.
- **"Thorough" / "audit" / "be rigorous"** — this is the signal to spend. Wider
  pool, `verify` with 3–5 skeptics and explicit `lenses`, a synthesis node that
  reads the surviving findings. Reserve `judge_panel` for genuinely open-ended
  output (a design, a piece of prose) where quality varies attempt to attempt —
  it is the most expensive node in the set.

**The caps are never silent.** A static `branches`/`items` list over **64**
entries is rejected at author time. At runtime a fan-out over the budget (64
wide, **1000** leaf spawns per run) raises and the node is nulled with a
`cap_trips` bump and a fault. If you see `cap_trips > 0`, the run did less than
you asked for — read `faults`, don't read the outputs as complete.

**`token_budget` caps the spend, not the shape.** Pass it to `run_workflow` to
bound what the whole run may cost in tokens; it is checked before every leaf
spawn. A leaf already in flight always finishes and is charged, so `spent` can
land a little over `total` — only the next spawn is refused, and the run
**pauses** rather than quietly returning half a workflow. A **barrier fan-out**
(`parallel`, `verify`, `judge_panel`) is checked as a whole before it dispatches:
if what is left cannot pay for that many leaves, the fan-out is refused up front
— nothing spawns and the run pauses — because a barrier fires its whole width
before a single leaf has been charged. A `pipeline` needs no such check; it
dispatches item by item. Size the budget against the work: leaf spawns and
tokens are separate limits, and hitting either one stops the run. Omit it and
there is no ceiling.

For very wide work, fan out over a `${ref}` (bounded at runtime) rather than
inlining a huge literal list, and prefer a `pipeline` over items to a giant
`parallel` — the pipeline keeps the pool busy instead of blocking on a barrier.

---

## 4. Schemas: structure anything downstream reads

Give a leaf `schema` (an inline JSON-Schema object) or `schema_ref` (a name from
the spec's `schemas:` block) **whenever its shape matters downstream**. Without
one the leaf returns prose and the next node has to re-parse it in natural
language — that is where null rates come from.

- Use `schema_ref` for a shape used more than once; inline `schema` for one-offs.
- Never set both on one node — the validator rejects it.
- Keep schemas small and `required`-marked. A 20-field schema buys retries, not fidelity.
- **Keep the whole spec lean — it has to fit in one tool call.** Every node,
  prompt and schema you write travels in a single `run_workflow` argument; a
  spec bloated with verbose inline schemas is how a `nodes` list ends up
  truncated. Hoist any schema into `schemas:` and reference it with
  `schema_ref` the moment it stops being two or three fields.
- A schema-mismatched answer is corrected in-place (bounded: 2 retries) before the node nulls.
- Add `tool_less: true` **only** when the leaf needs no tools and the JSON must be
  exact — it forces structured output through a synthetic tool where the provider
  supports it, and falls back to the validate-and-correct path where it doesn't
  (visible as `forcing_fallbacks` in the rollup).
- `verify` and `judge_panel` already force their own internal verdict/score
  schemas. Do not try to attach one to them.
- **Pipeline stages** honour `prompt`, `schema`/`schema_ref`, `retries` and
  `max_iterations` — and nothing else. `model`, `effort`, `provider`, `timeout`
  and `tool_less` are `agent`-node knobs; putting them on a stage does nothing.

---

## 5. Refs: `${node.field}`, path-only, single-pass

- `${scan.bug}` reads an earlier node's output; `${args.dump}` reads the run inputs.
- Engine-provided roots: `${item}` and `${stage.result}` inside pipeline stages,
  `${winner}` inside a `judge_panel` synthesis prompt, `${round}` and `${so_far}`
  inside a `loop_until_dry` body.
- **Paths only.** No arithmetic, no calls, no conditionals. The moment a
  reference grows expression syntax you have reinvented code, and the validator
  rejects it.
- **Single pass, by design.** A leaf whose output happens to contain `${...}` is
  inserted as an inert literal and never re-scanned. That is the second-order
  injection guard, not a bug — do not try to route a ref through a leaf.
- **`depends_on` orders nodes that share no data.** Use it when B must run after
  A but reads nothing from it (a cleanup, a write, an ordering constraint).
- **A ref to `null` fails its node.** If an upstream node died, the dependent
  node is not run with the string `"null"` in its prompt — it records an
  `upstream null` fault and nulls too. This is deliberate: a leaf handed the word
  "null" reads it as content and confidently invents an answer. Design for it —
  a refuted `verify` finding *should* stop the writeup that depends on it.
- A fan-out container can itself be a whole-value ref: `"items": "${scan.ids}"`.
  If it resolves to anything but a list the node fails with a fault, never a
  silent empty fan-out.

---

## 6. Reading the result honestly

`workflow_status(run_id)` returns the rollup. The status is not decoration:

| status | Means | What to do |
| --- | --- | --- |
| `complete` | Every node produced output, zero faults | Trust it |
| `degraded` | At least one node nulled, or at least one fault | **Read `faults` before using `outputs`** |
| `failed` | Every node nulled — the run produced nothing | Re-author; don't paper over it |
| `cancelled` | Someone stopped it | Partial outputs are real but incomplete |
| `paused` | Stopped resumably — provider quota, the run's `token_budget`, or you | See below |

- **`degraded` is not "mostly fine".** Some of the outputs you are about to
  summarise are `null`. Say which parts are missing rather than writing around
  the holes.
- **`null_rate`** is the honest health metric. High and spread across nodes →
  provider trouble or leaves being asked for the impossible. High on one node →
  that node's prompt or schema is wrong. `validation_retries` climbing means your
  schema and your prompt disagree.
- **`faults` covers the current stretch only.** A run you resumed also reports
  `faults_total` — everything it has faulted on since it was launched, including
  what stopped the earlier stretch. When both are there, `faults_total` is the
  one to read before you trust the outputs.
- **`paused` is not failure**, and `reason` tells you which of the three it is.
  Either way the finished nodes are kept.
  - **`quota_exhausted`** — the provider cut you off. The run **retries itself**
    (up to 5 attempts, waiting at least a minute and honouring the provider's own
    `retry-after`); `resume_at` and `attempts` say where it is up to. **Do not
    cancel it** — cancelling kills the auto-resume and throws away work you
    already paid for. Wait, or resume early with
    `run_workflow(resume_run_id=...)`.
  - **`token_budget_exhausted`** — the run spent its cap. Waiting does **not**
    refill a budget, so nothing will resume this one on its own (`resume_at` is
    `null`). Read `token_budget` `{total, spent, remaining}`, decide whether the
    rest of the run is worth it, and resume with a bigger cap:
    `run_workflow(resume_run_id=..., token_budget=<more than spent>)`. A cap at
    or under what the run already spent is **refused**, not launched — it would
    pause again on its first spawn. The tally continues across the resume, so
    the replayed cells are not charged twice.
  - **`user_requested`** — you paused it yourself with `workflow_pause`. Nothing
    resumes it on its own either (`resume_at` is `null`), and there is nothing to
    raise: `run_workflow(resume_run_id=...)` continues it whenever you want.
- **`resume_run_id` is cheap.** Cells that *completed* are content-addressed and
  replay from cache, carrying what they cost so the resume does not re-bill
  them; only what died, nulled or failed validation re-spawns. Use
  it after a crash, a pause, or when you want to re-run with one node's prompt
  fixed. Change a node's prompt, schema, model, effort, provider, timeout,
  retries or `max_iterations` and that cell is a *different* cell — it re-runs,
  as it should.
  - **Granularity:** an `agent` node and each `(item, stage)` of a `pipeline`
    are their own cell; `parallel`, `verify`, `judge_panel`, `loop_until_dry`,
    `completeness_check`, `gate` and `checkpoint` cache **per node** — the whole
    node replays or the whole node re-runs. A fan-out that came back incomplete
    (a dead branch, a dead skeptic, an unscored attempt, a round that died) is
    never cached: half a panel must not read back as a finished one.
  - **The run's `args` come back with it.** A resume replays the inputs the run
    was launched with, so `run_workflow(resume_run_id=...)` alone is enough —
    send `args` again only to *change* them.
  - **A restart does not lose the run.** The spec, the args, the pause reason and
    a pending `checkpoint` are on disk, so `workflow_status`, `workflow_list` and
    `run_workflow(resume_run_id=...)` all still work in a later session — you
    never have to re-send the spec to continue a run. A run whose process died
    still reads `running`, with `stale: true` and a hint: resume it, and the
    rollup says it was recovered (its finished cells replay; whatever was in
    flight when the process went down was really lost).

### Watching a run that is still going

A long run is never a black box. Three things work *before* it finishes:

- **`progress`** comes back from `workflow_status` even mid-run, when there is no
  rollup yet: `{done, running, pending, total}` plus a per-node list where each
  node is `pending`, `running`, `complete` or `null` (a `pipeline` node also
  carries `items: {done, total}`). `done` counts every node that *settled*, so a
  `null` node is done too — read the per-node states, not just the count. Use it
  to tell "still working" from "wedged on one node" instead of guessing from
  elapsed time.
- **`workflow_list`** shows every run at once — id, name, status, how far it got,
  what it spent. Reach for it when you lost a `run_id`, or before launching
  another run, to see what is already in flight.
- **`workflow_pause`** is the stop that keeps the work. Unlike `workflow_cancel`,
  leaves already in flight finish and are charged, finished nodes stay in the
  resume cache, and the run reports `paused` / `user_requested`. Pause when the
  early outputs already tell you the spec is wrong and you want to re-author
  without throwing away what it has; cancel only when the whole run is garbage.

You do not have to poll a run to the end: when it stops, a one-line notice
(`workflow <name> (<id>) finished: <status>, spent <n> tokens`) arrives in your
turn on its own. Read the rollup then.

The human is not stuck waiting on you either: the plan, every node transition and
every fault are printed to **stderr** as they happen, and `lohra workflow list` /
`lohra workflow watch <run_id|--last>` read the same progress straight off disk
from any shell — no tokens, no turn of yours. Point the operator at them instead
of polling on their behalf.

---

## 7. Per-node robustness knobs (`agent` nodes)

- **`timeout`** — seconds this leaf gets before it is cancelled and nulled with a
  timeout fault. Default **120s**. Raise it for a leaf doing real reading or
  multi-step tool work; leave it alone for a classification.
- **`retries`** — bounded fresh re-spawns when the leaf answers *nothing*.
  Default **1**, capped at **3**. An empty answer is invisible downstream (it
  passes every schema-less path and counts as no null at all), so the retry is
  what keeps it from silently poisoning a synthesis. `0` opts out. A leaf that
  *died* is not retried here — it already carries its cause.
- **`max_iterations`** — provider round-trips this leaf gets before the loop
  cuts it off with a `max_iterations (N) reached` fault. Default **50**, capped
  at **128**. Raise it for a leaf that legitimately needs many tool rounds
  (`timeout` bounds its wall-clock, this bounds its round-trips — a leaf that
  keeps *working* past the cap needs this one, not a longer timeout).
- **`model` / `effort` / `provider`** — route cost per node. A cheap fast model
  for triage and extraction, an expensive one with high effort for the synthesis
  or the final judgement. This is usually a bigger win than adding leaves.
- **`tier`** — `small` / `medium` / `big`, the PORTABLE way to say the same
  thing. The operator maps each tier to a real model, so a spec written with
  `tier` still runs on another profile or provider — a spec written with a
  literal `model` slug only runs where that slug exists. Prefer `tier` for
  anything that might become a template; keep `model` for a deliberate override
  (it wins over the tier). A tier the operator never mapped is a warning in the
  rollup, not a failure: the node runs on the run's default model.
  `tier` is an `agent`-node knob — pipeline stages and gate bodies do not take
  it (they take no model knobs at all).
- Pipeline stages get their own `retries` (default 2, same cap of 3) and their
  own `max_iterations`; the whole pipeline node is bounded by a 30-minute barrier.

### Choosing models from the catalog (`list_models`)

Before you put a `model` or a `provider` on a node, call `list_models`. It is
read-only — it starts no session and spends no tokens — and reports, per
provider, what is reachable *right now*: a live listing for every provider whose
API key is configured, the local `ollama` daemon, and the subscription model when
subscription mode is on. A provider with no key comes back as `skipped`, naming
the variable to set. It also returns the operator's tier map, so you can see what
`small` / `medium` / `big` resolve to on THIS install. It reports at most `limit`
ids per provider (default **25**, max **100**) alongside the real `total`, and
takes `provider` and `query` filters — narrow it rather than raising the cap.

The catalog is information, not an allow-list. Only `tier` is a closed enum;
`model`, `effort` and `provider` are free fields the harness passes straight
through. **Nothing validates either at authoring time**, and the two fail
differently:

- A bad `model` slug still spawns the leaf. It dies on the provider's own error
  and lands in `faults` as that node's failure — loud.
- A `provider` the harness cannot build spawns nothing at all. The node drops to
  `null`, the run carries on, and `faults` names the cause as
  `<node>: provider unavailable: <why>` — so read `faults`, not just `outputs`
  (checklist item 10).

Never invent a slug, and never assume `list_models` checked one for you: seeing
it in the catalog is the whole check.

Providers can be MIXED inside one DAG — each `agent` node names its own
`provider`, so an Anthropic node and an `openai-codex` (subscription) node can
sit in the same spec, and a node whose provider cannot be built nulls alone
instead of taking the run down. Two things to know:

- A cross-provider node with no `model` falls back to that provider's *declared*
  default slug — your own run's slug is meaningless there. For `openai-codex`
  that default is the fixed `gpt-5.5`, which is not necessarily the slug your
  Codex config uses nor the one `list_models` reported for it, so name the
  `model` you actually saw rather than letting it default.
- `openai-codex` is gated: it is refused unless the human opted into
  subscription mode AND their stored auth preference routes there. A spec cannot
  escalate onto it on its own — a refusal nulls that node alone and lands in
  `faults` as `provider unavailable`.

If the user asked to **confirm** the assignment, present it in a `checkpoint`
before the expensive nodes, one line per node, and put the costly work behind it.
If they left model choice on automatic, just assign — prefer tiers — and run: a
checkpoint nobody asked for only stalls the run.

```json
{
  "meta": {"name": "mixed-provider-notes"},
  "nodes": [
    {
      "id": "model_plan",
      "type": "checkpoint",
      "prompt": "Model plan — draft: openai-codex/gpt-5.5 (subscription); audit: anthropic/claude-sonnet-4-6; rewrite: tier big. Reply 'go' to run it. To route it differently, cancel the run and ask for a new spec: this answer is recorded, not read back into the routing."
    },
    {
      "id": "draft",
      "type": "agent",
      "depends_on": ["model_plan"],
      "prompt": "Draft the release notes for:\n${args.changelog}",
      "provider": "openai-codex",
      "model": "gpt-5.5"
    },
    {
      "id": "audit",
      "type": "agent",
      "prompt": "List every claim in these notes the changelog does not support:\n${draft}",
      "provider": "anthropic",
      "model": "claude-sonnet-4-6"
    },
    {
      "id": "final",
      "type": "agent",
      "depends_on": ["audit"],
      "prompt": "Rewrite the notes without the unsupported claims:\n${draft}\n\nUnsupported:\n${audit}",
      "tier": "big"
    }
  ]
}
```

Everything expensive here sits downstream of `model_plan`, so the human sees the
routing before a single leaf spends anything. Two deliberate omissions: the
checkpoint declares **no `default`** — a checkpoint that declares one is
auto-answered by a plain `resume_run_id`, which is exactly what a confirmation
gate must not do — and the prompt never promises that a non-`go` answer stops
anything, because nothing compares the answer to `go`. `model` / `provider` /
`tier` are static spec fields, so re-routing means a new spec, not a reply.

### Holding one answer to a standard (`gate`)

`gate` is `judge_panel`'s cheap cousin. Use `judge_panel` when the solution space
is WIDE and you want several genuinely different attempts scored against each
other; use `gate` when there is one right shape and the risk is that the first
draft misses part of it. It costs two leaves per attempt (the draft and the
review) instead of `attempts x judges + 1`.

The `validator` is a prompt, not a schema — say what "good" means in it and tell
the reviewer to reject. The candidate is appended for you, and the reviewer is
forced to answer `{ok, feedback}`; its `feedback` is what the next draft is
given. An unreadable verdict is a REJECTION, never a pass. `attempts` defaults
to 2 and is capped at 3, and only the approved output is cached — a resume never
replays a draft that was rejected.

### Asking a human (`checkpoint`)

`checkpoint` PAUSES the run. It spawns nothing (asking a model to approve on the
human's behalf is exactly what a checkpoint refuses), reports
`status: paused`, `reason: checkpoint` and `checkpoint{node_id, prompt, default?}`,
and waits. Continue it with
`run_workflow(resume_run_id=..., checkpoint_answers={"<node_id>": "<answer>"})`;
the answer becomes that node's output and is cached, so a later resume never
asks again. Nothing auto-resumes it — a `default` is what lets an unattended
resume carry on instead of stalling.

Put a checkpoint before the irreversible step, never after it, and keep the
`prompt` self-contained: the human reads the question, not the run.

### Fields that validate but do nothing (yet)

`label`, `phase`, `required` (on any node), `budget` (on `loop_until_dry`) and
`min_success_ratio` (on `pipeline`) are accepted by the validator but the engine
does not act on them today. Do not build a plan that depends on them.

### What a leaf can and cannot do

Leaves are isolated sub-agents: no memory, no skills, no conversation history.
Everything they need must be in the prompt. Their filesystem access is confined
to the run's working directory (plus whatever the operator allowed) and network
egress is deny-by-default. If the turn that authored the workflow ingested web or
MCP content, leaves run with **no** filesystem reads and **no** egress at all.
So: never write a spec whose leaves must read arbitrary project files — read what
matters yourself and put it in `args`.

---

## 8. Before you author: check the library

Call **`workflow_templates`** first. It returns:

- `templates` — specs from past runs that finished clean (low null rate). Adapt
  one; a proven shape beats an invented one.
- `insights` — priors distilled from past *problematic* runs: which shapes failed
  and why. Read them before repeating one.

Adapt, don't copy blindly: keep the shape, replace the prompts and schemas.

---

## 9. Adaptable examples

All four validate as-is. Replace the prompts, schemas and `args` — keep the shape.

### (a) Fan-out then synthesize

Independent options generated in parallel, one high-effort leaf picking a winner.

```json
{
  "meta": {"name": "survey-options"},
  "schemas": {
    "CHOICE": {
      "type": "object",
      "properties": {
        "option": {"type": "string"},
        "why": {"type": "string"},
        "risk": {"type": "string"}
      },
      "required": ["option", "why"]
    }
  },
  "nodes": [
    {
      "id": "candidates",
      "type": "parallel",
      "branches": [
        "Propose the SIMPLEST possible solution to: ${args.problem}. State its main tradeoff.",
        "Propose the most PERFORMANT solution to: ${args.problem}. State its main tradeoff.",
        "Propose the most MAINTAINABLE solution to: ${args.problem}. State its main tradeoff.",
        "Propose the solution to ${args.problem} that a skeptic would pick, and say why."
      ]
    },
    {
      "id": "choice",
      "type": "agent",
      "prompt": "Constraints: ${args.constraints}\n\nCandidate solutions to ${args.problem}:\n${candidates}\n\nPick exactly one and justify it against the constraints.",
      "schema_ref": "CHOICE",
      "effort": "high",
      "timeout": 240
    }
  ]
}
```

### (b) Find, then verify adversarially

The finding is attacked from five angles before anything is written about it. If
a majority refutes it, `check.finding` is `null` and `writeup` nulls with an
`upstream null` fault — which is the point: a refuted claim gets no writeup.

```json
{
  "meta": {"name": "audit-change"},
  "schemas": {
    "RISK": {
      "type": "object",
      "properties": {"risk": {"type": "string"}, "evidence": {"type": "string"}},
      "required": ["risk"]
    }
  },
  "nodes": [
    {
      "id": "find",
      "type": "agent",
      "prompt": "Here is a proposed change:\n${args.change}\n\nName the single biggest risk it introduces. Be concrete and cite the part of the change that causes it.",
      "schema_ref": "RISK",
      "timeout": 180,
      "retries": 1
    },
    {
      "id": "check",
      "type": "verify",
      "finding": "${find.risk}",
      "skeptics": 5,
      "lenses": [
        "is this actually caused by the change, or pre-existing?",
        "is it already mitigated elsewhere?",
        "is the failure mode realistic at this scale?",
        "does the evidence really support the claim?",
        "is the severity overstated?"
      ],
      "kill_if_majority_refute": true
    },
    {
      "id": "writeup",
      "type": "agent",
      "prompt": "This risk survived adversarial review:\n${check.finding}\n\nWrite a mitigation plan: what to change, how to test it, and how to tell if the mitigation worked.",
      "effort": "high"
    }
  ]
}
```

### (c) Staged pipeline with per-stage schemas and retries

Each item walks classify → plan on its own; no item waits for another.

```json
{
  "meta": {"name": "triage-and-plan"},
  "schemas": {
    "ITEMS": {
      "type": "object",
      "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
      "required": ["ids"]
    },
    "TRIAGE": {
      "type": "object",
      "properties": {
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"}
      },
      "required": ["severity", "summary"]
    },
    "PLAN": {
      "type": "object",
      "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "effort_hours": {"type": "number"}
      },
      "required": ["steps"]
    }
  },
  "nodes": [
    {
      "id": "backlog",
      "type": "agent",
      "prompt": "From this report, list the distinct issue identifiers, at most 20:\n${args.report}",
      "schema_ref": "ITEMS",
      "tool_less": true
    },
    {
      "id": "worked",
      "type": "pipeline",
      "items": "${backlog.ids}",
      "stages": [
        {
          "prompt": "Issue: ${item}\nContext: ${args.report}\n\nClassify its severity and summarise it in one sentence.",
          "schema_ref": "TRIAGE",
          "retries": 2
        },
        {
          "prompt": "Issue: ${item}\nTriage: ${stage.result}\n\nWrite the concrete steps to fix it and estimate the effort in hours.",
          "schema_ref": "PLAN",
          "retries": 1
        }
      ]
    },
    {
      "id": "digest",
      "type": "agent",
      "prompt": "Per-issue plans (null means that item failed):\n${worked}\n\nWrite the ordered work plan. Name explicitly any item that produced no plan.",
      "effort": "high"
    }
  ]
}
```

### (d) Gated draft, completeness audit, human sign-off

The plan is re-drafted until a reviewer accepts it, audited for gaps, and only
then put to a human. Nothing irreversible happens before the `checkpoint`.

```json
{
  "meta": {"name": "gated-migration"},
  "schemas": {
    "PLAN": {
      "type": "object",
      "properties": {
        "steps": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}}
      },
      "required": ["steps", "files"]
    }
  },
  "nodes": [
    {
      "id": "plan",
      "type": "gate",
      "body": {
        "prompt": "Write the migration plan for:\n${args.change}\n\nList the concrete steps and every file each step touches.",
        "schema_ref": "PLAN"
      },
      "validator": "A migration plan is acceptable only if every step names the file it touches and the rollback is stated. Reject it otherwise and say exactly what is missing.",
      "attempts": 3
    },
    {
      "id": "gaps",
      "type": "completeness_check",
      "task": "Migrate: ${args.change}",
      "results": "${plan.steps}"
    },
    {
      "id": "approve",
      "type": "checkpoint",
      "prompt": "Plan:\n${plan.steps}\n\nStill missing: ${gaps.missing}\n\nProceed? Answer 'go', or say what to change.",
      "default": "go"
    },
    {
      "id": "runbook",
      "type": "agent",
      "prompt": "The human answered: ${approve}\n\nWrite the final runbook from:\n${plan.steps}",
      "tier": "big"
    }
  ]
}
```

---

## 10. Checklist before calling `run_workflow`

1. Did I call `workflow_templates` and check the insights?
2. Is the wide node a barrier (`parallel`) or per-item (`pipeline`)? Apply the smell test.
3. Does every leaf whose shape matters downstream have a `schema` or `schema_ref`?
4. Is the fan-out proportional to what the user actually asked for?
5. Does anything the user will act on go through `verify`?
6. Does every `${ref}` point at a node id (or `args`/`item`/`stage`/`winner`/`round`/`so_far`) that exists?
7. Is anything a leaf must read already in `args`, rather than assumed readable from disk?
8. Does anything irreversible sit behind a `checkpoint`, and does every model
   choice use a `tier` — or a slug I actually saw in `list_models` — rather than
   a guessed one? If the user asked to confirm the routing, is it in a
   `checkpoint` ahead of the expensive nodes?
9. Is the spec itself lean enough to fit in one tool call (schemas hoisted into
   `schemas:` and referenced by `schema_ref`)?
10. After it runs: did I read `status` and `faults` before believing `outputs`?
