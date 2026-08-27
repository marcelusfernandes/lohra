# Lohra

**A self-improving AI agent runtime** — persistent memory, self-authored skills, and a
declarative multi-agent workflow harness with Claude-Code-grade rigor. Runs headless:
CLI, structured orchestration envelope, and an OpenAI-compatible server. No UI required.

```bash
pip install lohra          # Python 3.11–3.13
lohra chat "hello"
```

## Four entry points, none of them a UI

| Port | Command | For |
|---|---|---|
| Human CLI | `lohra chat` | you, in a terminal |
| Orchestration envelope | `lohra chat --json` | other agents/scripts — one parseable JSON per turn (input/output/reasoning/tool_calls/usage) |
| OpenAI-compatible API | `lohra serve` | any OpenAI client becomes a Lohra client |
| WS/REST gateway | `lohra dashboard` | optional, only if a UI attaches |

## What makes it interesting

- **Dynamic workflows as inert data**: the agent authors a typed DAG (10 node types —
  `agent`, `parallel`, `pipeline`, `loop_until_dry`, `verify`, `judge_panel`, `gate`,
  `completeness_check`, `checkpoint`, nested `workflow`) that an interpreter runs.
  No agent-authored code is ever executed; escape is inexpressible, not forbidden.
- **Failure is never silent**: every failure path produces a fault with its cause;
  run status is honest (`complete | degraded | failed | cancelled | paused`).
- **Never pay twice**: content-addressed per-cell cache — a resumed run replays
  completed work at zero token cost, across process restarts.
- **Human in the loop**: `checkpoint` nodes pause a run until a person answers —
  in another terminal, another process, another day. Durable state + single-winner leases.
- **Cost control**: token budgets with soft pre-spawn gates, quota pauses with
  auto-resume, per-node model/effort/provider routing, operator-owned model tiers.
- **Self-improving**: persistent memory, self-authored skills, and a workflow library
  that turns clean runs into reusable templates and bad runs into recorded priors.
- **Leaf sandbox**: filesystem allowlist (ro/rw), egress allowlist, and taint tracking —
  operator policy, never the spec.

## Configuration

State lives in `~/.lohra` (or per-workspace via `--profile`):
`.env` (API keys) · `workflow_policy.json` (leaf fs/egress) · `workflow_tiers.json`
(model tiers). Providers out of the box: Anthropic, OpenAI, OpenRouter, DeepSeek, Groq, Together,
Gemini, Ollama — plus an opt-in subscription mode (see the ToS warning in `lohra auth`).

**Driving Lohra from another agent?** `lohra skill export use-lohra --to .claude/skills`
(or `--to .codex/skills`) drops the delegation kit — a skill teaching Codex CLI /
Claude Code how to hand Lohra self-contained work through `lohra chat --json`.

MIT license. Alpha software — built and validated live, but young.
