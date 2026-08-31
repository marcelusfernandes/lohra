# Lohra — Arquitetura

Lohra é um agente de IA self-improving: um runtime Python headless (CLI, orquestração via `--json`, servidor OpenAI-compat). O app desktop Tauri das fases iniciais está fora do repo (possível reescrita futura); as referências a ele neste doc são históricas. O projeto começou em 2026 tomando a arquitetura do Hermes Agent (Nous Research, MIT) como referência de design — nenhum código foi copiado verbatim — e desde então divergiu substancialmente: o núcleo atual (harness de workflows declarativo com 10 node-types, token budget, estado durável cross-process, subscription auth, profiles isolados) não tem equivalente na referência. Hermes permanece citado nos specs iniciais (`docs/specs/01–05`) como prior art histórico do bootstrap (Fases 0–3).

## Visão geral em 3 camadas

```
┌─────────────────────────────────────────────────────────────┐
│  CASCA DESKTOP (Tauri + Rust + React)            desktop/    │
│  • spawna e supervisiona o backend Python local             │
│  • renderer React: chat, terminal, settings, skills         │
│  • fala JSON-RPC sobre WebSocket + REST com o backend       │
└───────────────────────────┬─────────────────────────────────┘
                            │  ws://127.0.0.1:9119/api/ws  +  REST
┌───────────────────────────▼─────────────────────────────────┐
│  GATEWAY (FastAPI + uvicorn)              backend/lohra/gateway│
│  • /api/ws  — JSON-RPC 2.0 newline-delimited (turno + stream)│
│  • /api/*   — REST (sessões, config, model, skills, cron)   │
│  • /v1/*    — servidor OpenAI-compatível (porta 8642)        │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  AGENT CORE (Python)                       backend/lohra/    │
│  • loop de conversa interruptível          agent/           │
│  • abstração de provider (registry)        providers/       │
│  • sistema de tools (registry + dispatch)  tools/           │
│  • memória, skills, state SQLite+FTS5+notices  memory/, state/ │
└─────────────────────────────────────────────────────────────┘
```

## Princípios arquiteturais (invariantes load-bearing)

1. **Prefix-cache invariant.** O system prompt é construído uma vez por sessão em 3 tiers (stable → context → volatile), cacheado, e só recompilado após compressão de contexto. Memória e skills atualizam o disco imediatamente mas nunca o prompt vivo (frozen snapshot). Quebrar isso destrói o cache de prefixo do provider a cada turno. Além do prompt congelado, o TAIL da conversa pode carregar injeções efêmeras request-only — steer de sub-sessão e o overlay de notices duráveis (`lohra/agent/notices_overlay.py`) — reaplicadas a cada chamada, nunca persistidas na história canônica nem no prompt cacheado (Wave 6, SUP-05).

2. **Um tipo de resposta canônico.** `NormalizedResponse`/`ToolCall` são o único formato que o loop lê. Todo quirk de provider (Anthropic, OpenAI Responses, chat completions) vive nos transports e em `provider_data`. O loop nunca ramifica por `api_mode` na leitura.

3. **Dois protocolos separados.** O dashboard-WS (vocabulário `message.delta`, `tool.start`, `approval.request`) e o servidor OpenAI-SSE (`response.*`, `lohra.tool.progress`) compartilham o agent core mas são protocolos distintos.

4. **Imutabilidade no core.** Mensagens e estado são tratados como dados imutáveis; transformações retornam novas cópias (alinhado às regras de coding-style do projeto).

5. **Separação memory vs skills.** Memory = fatos declarativos (`§`-delimitado, char-bounded). Skills = procedimentos (SKILL.md class-level). A distinção é enforçada por guidance, não por código.

6. **Self-improvement isolado.** O loop de auto-melhoria é um agente forkado em daemon thread, whitelisted a só memory + skill tools, que nunca muta a conversa principal nem o prompt cache.

## Stack

| Camada | Tecnologia | Justificativa |
|---|---|---|
| Agent core / backend | Python 3.11+ (uv) | Ecossistema LLM maduro; SDKs openai/anthropic; arquitetura inicial referenciou o Hermes, divergida desde a Fase 6 |
| LLM SDK | `openai` (sempre) + `anthropic` (lazy) | OpenAI como cliente universal; provider SDKs lazy-instalados |
| State | SQLite + FTS5 | Sessões, busca full-text, lineage |
| Gateway | FastAPI + uvicorn | Async, WS + REST, OpenAI-compatible |
| Casca desktop | Tauri 2 + Rust | Binário leve (~10MB), webview nativa, seguro |
| Renderer | React 19 + Vite + Tailwind v4 | UI própria do zero; `assistant-ui` (cogitado na Fase 3) foi adiado em favor de um custom mínimo |
| Chat UI | `@assistant-ui/react` + nanostores | Streaming incremental |
| Terminal | xterm (renderer) + `portable-pty` (Rust) | ⚠️ node-pty não funciona no Tauri |

## Mapa de subsistemas → specs

| Subsistema | Spec | Diretório |
|---|---|---|
| Loop de conversa, providers, prompt builder | [01-agent-core](specs/01-agent-core.md) | `backend/lohra/agent`, `backend/lohra/providers` |
| Registry de tools, dispatch, approval, delegate | [02-tool-system](specs/02-tool-system.md) | `backend/lohra/tools` |
| Memory, skills, state SQLite, compression | [03-memory-skills-state](specs/03-memory-skills-state.md) | `backend/lohra/memory` |
| Protocolo WS JSON-RPC + REST + OpenAI server | [04-gateway-protocol](specs/04-gateway-protocol.md) | `backend/lohra/gateway` |
| Casca Tauri, renderer React, design system | [05-desktop-shell](specs/05-desktop-shell.md) | `desktop/` |
| Orquestração de sessões paralelas (spawn/steer/collect) | [06-orchestration](specs/06-orchestration.md) | `backend/lohra/orchestration` |
| Harness de dynamic workflows (10 node-types, budget, cache, supervisão) | [07-workflow-harness](specs/07-workflow-harness.md) · [08-workflow-node-audit](specs/08-workflow-node-audit.md) | `backend/lohra/workflow`, `backend/lohra/state` |

Ver [ROADMAP.md](ROADMAP.md) para o plano de implementação faseado.
