# Lohra — Roadmap de Implementação

Plano faseado de evolução da Lohra. Fases 0–6 buscaram paridade funcional com o Hermes Agent (referência inicial de arquitetura); da Fase 7 em diante o escopo é original, sem equivalente na referência (orquestração paralela, harness de workflows declarativo, subscription auth, runtime standalone). Cada fase entrega algo executável e testável (TDD: teste primeiro, 80%+ cobertura). As fases são incrementais — uma camada vertical funcional antes de ampliar horizontalmente.

## Fase 0 — Esqueleto & contratos
- [x] Estrutura de diretórios (backend + desktop + docs)
- [x] Specs dos 5 subsistemas extraídos do Hermes
- [x] `pyproject.toml`, `Cargo.toml`, `tauri.conf.json`, `package.json`
- [x] Stubs dos módulos core (tipos, ABCs, registry vazio)
- [x] `lohra --version` roda; `pytest` roda (mesmo com testes pendentes)

## Fase 1 — Agent core mínimo (chat funcional)
**Meta:** loop de conversa que fala com Anthropic e devolve texto, sem tools.
- [x] `NormalizedResponse` / `ToolCall` (tipos canônicos)
- [x] Transport `anthropic_messages` (build_kwargs + normalize) — `chat_completions` pendente
- [x] `ProviderProfile` + registry de providers (anthropic, openai)
- [x] Resolução de provider (arg → config → env → auto)
- [x] Loop de conversa básico (sem tools): user → API → resposta + CLI `lohra chat`
- [x] System prompt 3-tier (stable/context/volatile) com frozen snapshot
- [x] Callbacks de streaming (`stream_delta_callback`, `reasoning_callback`)
- [ ] Chamada interruptível (thread daemon + poll) — interrupt entre iterações já existe; falta mid-call
- [ ] Transport `chat_completions` (path OpenAI) — adiado
- [x] **Teste E2E:** `lohra chat "olá"` streama uma resposta real. ✅ confirmado contra a API

## Fase 2 — Tools & state
**Meta:** agente com tool-calling e persistência.
- [x] `ToolRegistry` (register, generation counter, check_fn TTL) — auto-discovery AST adiada (import explícito via `load_builtin_tools`)
- [x] Dispatch (single sequencial / multiple ThreadPool(8) preservando ordem)
- [x] Tools essenciais: `read_file`, `write_file`, `terminal` (backend local) — `patch`, `search_files` adiadas
- [ ] `web_search`, `web_extract` — adiadas (rede)
- [x] Loop com tool-calling (executa tools, anexa resultados, continua) — dedup/JSON-repair adiados
- [x] `SessionDB` (SQLite, schema completo, WAL+fallback) — FTS5 adiada (Fase 4: session_search)
- [x] Persistência de mensagens + lineage `parent_session_id`
- [x] Approval gate (DANGEROUS_PATTERNS + callback, cache por comando exato)
- [x] **Teste E2E:** agente lê arquivo, roda comando, persiste sessão recuperável. ✅ confirmado contra a API real

## Fase 3 — Gateway & desktop mínimo
**Meta:** app desktop conversando com o backend.

### Metade A — Gateway (backend Python) ✅ na branch `feat/phase-3-gateway`
- [x] FastAPI `lohra dashboard` (porta 9119) + serve REST/WS via uvicorn
- [x] WS JSON-RPC: `session.create/list/history/interrupt`, `prompt.submit` streamando eventos `message.*` e `tool.*`
- [x] REST: `/api/status`, `/api/sessions`, `/api/sessions/{id}/messages`, `/api/config`
- [x] Auth WS (token loopback, `compare_digest`, close 4401; `--insecure` desliga)
- ⏳ Interrupt concorrente mid-turn (hoje: um turno por socket; interrompe ao desconectar)

### Metade B — Desktop (Tauri + React) — na branch `feat/phase-3-desktop` (compila: tsc + cargo)
- [x] Casca Tauri: `start_backend` spawna `lohra dashboard`, port probe (9120-9199), token via env, liveness poll `/api/status`, logs → `backend-log`
- [x] Renderer React: cliente WS gateway em nanostores (`$messages/$busy/$approval`) + UI de chat mínima (streaming, tool rows, interrupt)
- ⏳ Approval bar inline — UI + protocolo prontos; falta o bridge server-side (agente bloqueia → emite `approval.request` → espera `approval.respond`). Depende do recebimento-WS concorrente (mesmo item do interrupt mid-turn). Hoje: comando perigoso é auto-negado no servidor.
- [ ] `@assistant-ui/react` + `incremental-external-store-runtime` — adiado (UI custom mínima por ora)
- [ ] Rendering rico (Streamdown + Shiki + KaTeX) — adiado (texto puro por ora)
- [x] **Teste E2E:** abrir o app, mandar prompt, ver streaming + tool calls (operações seguras). ✅ confirmado pelo usuário

## Fase 4 — Memory & skills (self-improving)
**Meta:** o diferencial do Hermes. Branch `feat/phase-4-memory`.
- [x] `MemoryStore` (MEMORY.md + USER.md, §-delimitado, char limits, escrita atômica, re-read-before-mutate) — `.lock` cross-process adiado
- [x] Injeção frozen snapshot no system prompt (memory + user no tier volatile)
- [x] Tool `memory` (interceptado, session-bound via compose_dispatch)
- [x] Skills: formato SKILL.md (frontmatter YAML), indexação progressive disclosure, `skill_view`, `skill_manage`
- [x] Wiring memory+skills no `lohra chat` e no gateway
- [x] `SOUL.md` persona (override do identity no tier stable)
- [x] `session_search` (FTS5: discovery/browse/read; trigger + backfill; degrada sem FTS5)
- [ ] Background review (daemon fork, whitelist memory+skills) — adiado (complexo/risco; único item da Fase 4 pendente)
- [x] **Teste E2E:** agente salva memória + cria skill; sessão nova recupera memória (snapshot) e skill (índice + `skill_view`); SOUL.md vira persona; session_search acha conversas. ✅ confirmado pelo usuário com LLM real.

## Fase 5 — Context compression & delegação
Branch `feat/phase-5-compression` (metade A — compactação).
- [x] `ContextEngine` ABC + `ContextCompressor` (threshold 50%, protege head/tail, summariza o meio, limpa pares tool órfãos) — prune de tool results adiado
- [x] Cliente auxiliar (`AuxClient`: summarize/title no modelo barato via mesmo transport)
- [x] Compactação preflight no loop (`result["compacted"]`) + lineage split no `lohra chat` (end "compression" → sessão filha com `parent_session_id`)
- [x] Lineage split no gateway — turno que compacta encerra o pai ("compression"), forka sessão-filha (`parent_session_id`) com o transcript comprimido, emite evento `session.forked` e registra a filha viva (reusa o mesmo Agent — prompt congelado, Invariante #1). Pai é despejado e bloqueado p/ novos turnos; filha herda o busy-lock do pai (sem dois turnos no mesmo Agent). `lohra/gateway/{session,manager,events}.py`. Frontend: renderer consome `session.forked` e faz rebind do `$sessionId` ativo (`desktop/src/gateway.ts`), compactação transparente p/ o usuário.
- [x] `compression_locks` (corrida na compactação) — lock advisory cross-process na tabela SQLite `compression_locks` (PK por session_id como árbitro single-winner + lease TTL p/ holder morto). Context manager `compression_lock(db, sid)` cede se contendido; guarda o split de lineage no `lohra chat` e no gateway. `busy_timeout=5000` + back-off em OperationalError (sem crash sob contenção WAL). `lohra/state/{db,compression_lock}.py`. 14 testes.
- [x] `delegate_task` (subagents isolados, caps pai=90/filho=50, depth 1, concorrência 3, auto-deny de comando perigoso no filho) — branch `feat/phase-5-delegate`. Contexto fresco (sem histórico/memória/skills/context files); reusa `run_conversation`; padrão de tool interceptado (`lohra/agent/delegate.py` + wiring em `equip.py`/`cli.py`). 24 testes.
- ⏳ **Teste E2E:** conversa longa compacta sem perder contexto (metade A, pronto p/ testar); subagente executa subtarefa (metade B — verde nos testes, aguardando teste do usuário com LLM real).

## Fase 6 — Paridade ampla
- [x] Servidor OpenAI-compatível — `POST /v1/chat/completions` (stream SSE + non-stream), `GET /v1/models`, `GET /health`, auth Bearer opcional. Modo **relay** (default): Agent fresco e sem tools por request contra o provider (o `model` do request é honrado). `lohra serve` (porta 8000). Usage real propagado do loop. **Modo agêntico** (`--tools <allow-list>`): roda tools server-side com guardas — allow-list que gateia **execução** (não só as definitions), reuso das guardas de subagente (auto-deny perigoso, intercepted bloqueado), aviso de RCE. **`POST /v1/responses`** (Responses API: `input`/`instructions` → `response` object; stream SSE tipado `response.created`/`output_item.added`/`content_part.added`/`output_text.delta`/`completed`/`failed`) — reusa o mesmo CompletionService; objetos e eventos **validados contra o SDK `openai` real** (todos os campos obrigatórios + `sequence_number`). `lohra/server/`. ⏳ falta `/v1/runs` (não é endpoint OpenAI padrão — sem shape concreto; adiado) e sandbox real p/ expor terminal/write_file com segurança.
- [x] Cron / jobs agendados — `HOME/cron/jobs.json` (`CronStore`, escrita atômica, tolera arquivo corrompido) + tipos `once`/`interval`/`cron` (matcher próprio sem deps, dow 0=domingo, aceita 7) + `scheduler.tick` (roda os due, isola falhas por job, marca run) + `run_scheduler_loop` (thread de fundo no dashboard) + runner (agente fresco sem tools por run, persiste sessão `source="cron"`, loga erro in-band) + tool **`cronjob`** interceptada (add/list/remove/pause/resume; excluída de subagentes/server) + `lohra cron <list|add|remove|pause|resume>`. `lohra/cron/`. Núcleo 100% testado. ⏳ jobs.json só com lock in-process (corrida rara CLI×dashboard); sem catch-up de minuto perdido; runs unattended são tool-less por segurança.
- [x] Terminal embutido no desktop — `portable-pty` (Rust/Tauri) spawna o shell do usuário num PTY; comandos `pty_open`/`pty_write`/`pty_resize`/`pty_close`, saída streamada como evento `pty-output` (+ `pty-exit`). Frontend: `TerminalPanel` (xterm.js + addon-fit) wirado aos comandos/eventos, toggle no header, fallback fora do Tauri. `desktop/src-tauri/src/pty.rs`, `desktop/src/Terminal.tsx`. Compila (cargo check + tsc); falta E2E do usuário no app.
- [x] Mais providers (chat_completions) — transport `chat_completions` (protocolo OpenAI; serve openai/openrouter/deepseek/groq/together/ollama via base_url) + `OpenAIClient` + factory `build_client(profile)` (escolhe client por api_mode, resolve api_key dos `env_vars`) + profiles OpenAI-compat + CLI/gateway genéricos (`_resolve_profile`/`_resolve_model`, `--provider`/`--model`, `LOHRA_MODEL`). Streaming com acumulador tolerante (index ausente, tool_calls incompletos descartados). Inclui **gemini** (via endpoint OpenAI-compat do Google) e **ollama** keyless (flag `requires_api_key=False` → placeholder no SDK). `lohra/providers/transports/chat_completions.py`, `lohra/agent/client.py`.
- [x] MCP client (tools dinâmicos) — conecta a servidores MCP (config `~/.lohra/mcp.json`, formato Claude Desktop/Cursor) e expõe as tools deles como tools normais do registry (`mcp_{server}_{tool}` sob toolset `mcp-{server}`), fluindo por `get_definitions`/`dispatch` sem mexer no agente. Ponte sync↔async (`ThreadedMCPSession`: loop em thread de fundo), `MCPManager` (connect best-effort, refresh nuke-and-repave, shutdown), guarda de colisão. stdio **e http** (dispatcher `connect_session` por `config.transport`; conectores SDK lazy/pragma). SDK `mcp` é extra opcional. Núcleo 100% testado com fakes (sem SDK). `lohra/mcp/`. ⏳ falta: wiring de `notifications/tools/list_changed` ao `refresh` (capacidade existe e é testada; falta a subscription via SDK live); conexão é por-invocação no `lohra chat`.
- [x] Vision, image_gen, browser tools — **vision** ✅: tool `vision_analyze` interceptada (path local → data URI base64 / url remota; prompt opcional, default "describe"), runner one-shot contra um modelo vision-capable (`make_vision_runner`), formato canônico de imagem = parts OpenAI (`image_url`), convertido pelos transports (chat_completions passa as parts; anthropic_messages traduz `image_url`→bloco `image` source base64/url). Wirada no CLI `lohra chat` e no dashboard; excluída de subagentes/server. `lohra/vision/`. Núcleo 100% testado. **image_gen** ✅: tool `image_gen` interceptada (prompt + `size`/`n` opcionais) → gera via OpenAI Images API (gpt-image-1), salva os PNGs em `~/.lohra/images/` e devolve os paths. Capacidade `generate_image` no `ModelClient` (base levanta erro limpo; `OpenAIClient` chama `images.generate`, decodifica `b64_json`); storage decodifica base64 → arquivo único (uuid). Wirada no CLI/dashboard, excluída de subagentes/server. `lohra/imagegen/`. Validado pelo usuário ao vivo. **browser tools** ✅ (search + fetch): tools **`web_fetch`** (baixa URL via httpx, extrai texto legível via `html.parser` — sem deps novas) e **`web_search`** (backend plugável `SearchBackend`; default keyless DuckDuckGo via endpoint HTML). Tools **normais** (stateless, registradas como fs/terminal) → funcionam em subagentes e gateáveis pela allow-list do server. **Guard SSRF na camada de fetch** (`validate_public_url`): só http(s), recusa loopback/privado/link-local (metadata 169.254.169.254)/reservado; redirects seguidos manualmente revalidando cada hop; body com cap de bytes; texto extraído com cap de chars. Busca distingue **indisponível** (`SearchUnavailable`, ex. 429/rede) de **zero resultados**. `lohra/web/`. Núcleo testado (safety 97% / fetch 92% / search 96% / extract 100%) + smoke ao vivo (DDG + fetch + SSRF recusando interno).
- [x] Profiles isolados — workspaces isolados: `LOHRA_PROFILE` (ou `lohra --profile <nome>`) re-rooteia **todo** o estado sob `~/.lohra/profiles/<nome>/` (memória, skills, sessões/state.db, cron, mcp.json, imagens). Seam de fonte única: `lohra_home()` resolve o profile (todo subsistema já passa por ela, então isola por construção); `lohra_base()` é o root independente. **Backward-compat:** sem profile → exatamente `~/.lohra` de hoje (dados existentes intactos). Validação anti-traversal (`validate_profile_name`: allowlist `[A-Za-z0-9][A-Za-z0-9_-]*`, cap 64, enforçada no read de `LOHRA_PROFILE`, não só na flag). CLI: `--profile` (parent parser, funciona após o subcomando) + `lohra profile list|create`. `lohra/memory/paths.py`. Testado (validação, isolamento dos 5 locais, backward-compat, listagem) + smoke ao vivo. Conftest passou a snapshotar/restaurar `os.environ` por teste.
- ⏳ Self-update (casca Tauri + backend git-pull) — **backend git-pull** ✅: `lohra update` resolve o repo pela **localização do pacote instalado** (`lohra.__file__`, não o CWD), roda pré-checks de estado como outcomes de 1ª classe (working tree sujo→aborta, sem upstream/detached HEAD, diverged≠erro, up-to-date) e faz `git pull --ff-only`. Reporta arquivos mudados + **restart-to-apply**; se `pyproject`/`setup` mudou, recomenda `--reinstall` (editable pega edições py de graça). `--check` (fetch + behind count, sem aplicar) e `--reinstall` (pip install -e). Git/pip atrás de runners injetáveis → testável offline (todos os outcomes). `lohra/selfupdate/`. Validado ao vivo: tree sujo aborta, sem-upstream limpo, e ff-pull real num clone atrasado (conteúdo atualizado, reinstall recomendado). ⏳ falta a **casca Tauri** (depende de packaging/releases — item posterior).
- [ ] Gateway de mensageria (telegram, discord, ...) — opcional
- ⏳ Packaging/notarização (dmg/appimage/msi) — **bundle macOS local (não-assinado)** ✅: backend congelado (PyInstaller **onedir**) e enviado como **resource** do Tauri (`Contents/Resources/lohra-backend/lohra`); `backend.rs::backend_executable` resolve o sidecar em release (**falha alto** se faltar) e usa `lohra` do PATH em dev. Freeze não-óbvio (4 gotchas em `docs/history/PACKAGING.md`): `pathex`=raiz do backend (editable esconde o source), coletar deps do `uvicorn[standard]` (uvloop/httptools/...), onedir>onefile, e **`uvicorn.run(lifespan="off")`** (handshake ASGI dá deadlock no binário congelado — fix decisivo). Validado: freeze roda (`--version`/`chat`/SDKs lazy), `dashboard`+`serve` congelados dão bind (`/health` 200, `/api/status` 200), `cargo check` verde. `desktop/scripts/build-macos.sh`, `backend/packaging/lohra.spec`. **UI de settings da API key** ✅ (validado ao vivo pelo usuário): app do Finder não herda o env do shell → backend carrega `~/.lohra/.env` no startup (`lohra/config/env_file.py`, parser próprio); desktop tem painel de Settings (provider + key) que grava em `~/.lohra/.env` (chmod 600) via `config.rs` (`provider_status`/`save_provider_key`), `restart_backend` (mata+recria), gate no boot (sem key → settings, não spawna). `.app` empacotado **roda standalone e o chat funciona** end-to-end. ⏳ **escrito mas não-verificado** (recursos externos): assinatura Developer-ID + notarização (cert Apple), appimage/msi (Linux/Windows), matriz CI, casca self-updater do Tauri. Chave em plaintext `~/.lohra/.env`, não Keychain.

## Fase 7 — Orquestração de sessões paralelas
**Meta:** a Lohra-agente spawna várias sub-sessões, injeta prompts (steer) e colhe
respostas de forma assíncrona. Completa os métodos de orquestração já specados no
gateway (spec 04: `session.steer`/`prompt.background`/`session.branch`/`session.most_recent`,
listados e não implementados) + uma tool por cima. Prior-art: opencode (sst), padrão não código.
Plano detalhado em `docs/specs/06-orchestration.md`. Branch: `feat/phase-7-orchestration`.
- [x] **A** `OrchestrationCore` (`lohra/orchestration/core.py`) — registry de sub-sessões + inbox + coleta; sub-sessões **independentes** (Agent isolado via `child_factory` de subagente, `on_compaction=None` → nunca forka mid-run); `ThreadPoolExecutor` com teto configurável (default 4, loga quando enfileira); `spawn`/`steer`/`collect(wait)`/`list_children`/`cancel` (queued→`future.cancel`, running→`interrupt`)/`shutdown`. 90% testado.
- [x] **A** Steer no loop — `run_conversation` ganhou `inbox`: drena entre iterações e injeta os textos **mergeados num único** `user` `<system-reminder>` no tail (evita user-em-sequência; Invariante #1 intacto). `GatewaySession.submit(inbox=…)` repassa; o `acquire` atômico do busy-lock é o árbitro (busy→inbox); steers órfãos (pós-último-drain) viram turno de follow-up.
- [x] **A** Tool triad do agente (interceptada): `spawn_session`/`steer_session`/`collect_session` (`lohra/orchestration/tools.py`); wirada no CLI (core + `session_id` do pai) e no gateway (core compartilhado, parent=None até B); **excluída de subagentes e do server** (via `_CHILD_EXCLUDED_TOOLS`, automático no `agentic.py`). 100% testado.
- [ ] **B** Métodos WS (2º consumidor do mesmo core): `prompt.background`, `session.steer`, `session.branch`, `session.most_recent`/`resume`; (opcional) canal de eventos agregado por `session_id`. **Adiado por falta de consumidor** (o desktop não chama essas; o payoff agent-facing já está em A/C). Requer restructure do WS handler (writer task + dispatch não-bloqueante) — risco num componente que o desktop depende. **Limpezas:** (1) ⏳ linkage real de `parent_session_id` no gateway (hoje parent=None; sem consumidor vivo — `list_children` só tem caller em teste — então fica com B: precisa do `AgentFactory` widened pra passar `session_id`); (2) ✅ inbox migrou pra `GatewaySession` (`enqueue_steer`/`drain_steers`; core perdeu o dict por sub_id + `_drain`); (3) ✅ eviction no `OrchestrationCore._children` (cap `DEFAULT_MAX_CHILDREN=200`, evicta só sub-sessões terminais; running nunca; loga).
- [x] **C** `delegate_task` retomável — reusa o `OrchestrationCore` (não mais o `ThreadPoolExecutor` descartável): cada task vira sub-sessão **persistida** (isolada, `parent_session_id`), o resultado carrega `sub_id`, e `resume_id` (+ instrução em `tasks`) continua um filho existente (steer→collect; o filho mantém o próprio histórico). `DelegateTaskTool(core, parent_session_id)` substitui o `(child_factory, runner, max_concurrent)`; concorrência/isolamento/erro-por-filho agora vêm do core. `build_session_dispatch` perdeu o param `child_factory` (delegate liga no core). Excluído de subagentes/server. `lohra/agent/delegate.py`. 683 testes verdes.
- [ ] **Teste E2E:** Lohra spawna 2+ sub-sessões, injeta follow-up numa delas, colhe e integra — sem travar o turno pai.

## Fase 8 — Harness de dynamic workflows
**Meta:** a Lohra-agente **autora e roda dynamic workflows** (DAG declarativo typed,
sem executar código autorado) com o rigor do Claude Code. Plano detalhado em
`docs/specs/07-workflow-harness.md`. Pacote `lohra/workflow/`.
Branches: `feat/phase-8-workflow-harness` (A–F, mergeada) · `feat/phase-8-pipeline-resume` (D+G + fixes JSON, **aguardando merge**).

### Entregue (validado ao vivo)
- [x] **A** Modelo + validador (`nodes`/`schema`/`refs`): node-types fechados; refs path-only single-pass (guard injeção 2ª ordem); `validate_spec` → WorkflowSpec|ValidationError didático (nunca levanta); rejeita tipos válidos-mas-sem-strategy.
- [x] **B** `engine` (interpreter, ordem topológica, engine-fault isolado) + strategies `agent`/`parallel` + **leaf sandbox** (`sandbox.py`: fs path-allowlist deny-by-default + egress allowlist + taint; policy do operador) + `budget` (fan-out/lifetime, nunca unbounded). Sandbox validado no caminho REAL (teste + discriminador).
- [x] **C** Structured output: `validation.py` (jsonschema) valida o leaf; mismatch → steer de correção bounded; falha → null. **Parsing tolerante** (`jsonio.py`: fences/prosa) — saída de LLM embrulhada valida em vez de nulificar.
- [x] **D** Pipeline **no-barrier**: core `spawn(on_done=…)` (hook não-bloqueante, 1x/sub-sessão, fora do lock); `run_pipeline` encadeia cada item pelos stages independentemente. Testes que definem correto: no-barrier + throughput (≤ pool_width, sem deadlock).
- [x] **E** Nós de rigor: **verify** (adversarial: N skeptics refutam, maioria mata), **judge_panel** (N attempts → judges → vencedor sintetizado), **loop_until_dry**.
- [x] **F** Tool surface `run_workflow`/`workflow_status`/`workflow_cancel` (interceptada): `WorkflowService` valida → core sandboxed por run → roda em background → run_id na hora. Wirada CLI+dashboard, excluída de subagentes/server.
- [x] **G** Resume/cache content-addressed: tabela `workflow_node_cache` (key por conteúdo, run-scoped, cross-run OFF); get-or-spawn por célula (scalar + pipeline per-(item,stage)); só completions cacheadas (dead/invalid re-spawnam no resume); `resume_run_id`. `${node.field}` extrai JSON de output string (cobre gen sem schema).

### Próximos passos (a implementar)
- [x] **J — Self-improvement loop**. `rollup.py` (rollup rico: `null_rate` first-class, cap_trips, engine_faults, validation_retries). `library.py`: run problemática → **prior** num arquivo de insights dedicado (`~/.lohra/workflows/insights.md`, lock+dedup+cap — **não** o MEMORY.md/prompt congelado); run limpa (complete + null_rate ≤ 0.2) → **template** validado em `~/.lohra/workflows/templates/`. Tool `workflow_templates` (list+get, devolve {templates, insights}) + nudge no `run_workflow`. Run cancelada não grava prior. Excluída de subagentes/server. ⏳ `aggregate_tokens` adiado (priors aprendem null-rate/faults, não custo/tokens).
- [x] **H — Nesting (`workflow` node)**. Recursão de engine (determinística): `nested_engine()` compartilha core/budget/cache/loader (sandbox/budget não escapáveis), `MAX_WORKFLOW_DEPTH=1`. `run_workflow` carrega o template ref'd (loader=`library.get_template`) → valida → roda aninhado → `fold_nested()` dobra as métricas no rollup do pai (falhas aninhadas visíveis; J não certifica composto quebrado). Agora **todos os 7 node-types executam** (o mecanismo `supported_types` virou unit test). §4.4.
- [ ] **Taint real** _(robustez de segurança)_. Hoje `tainted` é stub (ninguém liga `True`); proteção real = default-deny fs+egress. Fiar o sinal do `GatewaySession` pai (turno ingeriu web/MCP) → `WorkflowTool` propaga `tainted` → leaves com capacidade reduzida. §8.2 (controle 3), invariante testado.
- [x] **I — Forced `tool_choice`**. Part A: `Transport.build_kwargs(tool_choice=…)` (ABC + 2 transports; default None **byte-idêntico**, afirmado em teste) + synthetic `StructuredOutput` tool + detector de fallback. Part B: node `agent` com `tool_less: true` + schema → `core.spawn(configure=…)` seta `agent.forced_tool` → loop manda só a tool sintética + força, intercepta o call (args = resposta, valida); provider ignora → cai no §5.1 logado. Turno persiste como assistant-text (replay-safe, sem tool_use pendente). ⏳ fallback loga em info, não no rollup (nunca dispara pro Anthropic). §5.2-5.3.
- [x] **aggregate_tokens + forcing no rollup**. usage do leaf → core (tokens_in/out por sub) → `RunResult.tokens_in/out` + `forcing_fallbacks` (agregados 1x/leaf, **lock** nas escritas off-thread do pipeline) → rollup + priors. `forced_fallback` do `run_conversation` deixa de ser só log.
- [x] **Pipeline retry por stage**. Schema-inválido re-spawna um leaf FRESCO com correção (não steer; on_done não bloqueia worker), bounded (`MAX_PIPELINE_RETRIES=2`) por (item,stage), depois dropa; conta `validation_retries`.

## Fase 9 — Consciência de projeto (instruções + skills do projeto)
**Meta:** rodar a Lohra num projeto que já tem instruções de agente e skills →
ela **lê, entende e segue** o que está lá. As costuras existem mas ninguém popula:
`context_files` (tier de contexto do system prompt) e `environment_hints` estão
sempre vazios; o `SkillStore` só escaneia `~/.lohra/skills`. Invariante #1: tudo
injetado UMA vez na construção do Agent (prompt congelado).
- [x] **A1 — Discovery de raiz + instruções**. `lohra/project/`: `find_project_root(cwd)` (sobe até `.git`/marcador), `discover_instructions(root, cwd)` lê **AGENTS.md** (padrão cross-tool) e **CLAUDE.md** (cap de tamanho, trunca) → injeta em `Agent.context_files`; popula `environment_hints` (cwd, project_root). Wirado no `lohra chat` + dashboard (best-effort). Subagentes/leaves seguem isolados.
- [x] **A2 — Skills do projeto (ler)**. `SkillStore` escaneia roots do projeto (`.claude/skills/`, `.lohra/skills/`) além do home; o index (name+description, progressive disclosure) mescla todos com label de origem; `skill_view` lê de qualquer root. O agente vê as skills do projeto no prompt congelado → entende o que existe.
- [x] **A3 — Skills do projeto (editar/criar)**. `skill_manage` escreve no root certo (projeto vs home) via `scope`/target; default = projeto se houver dir de skill do projeto, senão home.
- [x] **A4 — "Usar" skill + ergonomia**. Garantir que o agente, vendo o index + AGENTS.md/CLAUDE.md, siga corretamente (guidance); ler arquivos auxiliares que a skill empacota é fs normal. Teste E2E: Lohra num projeto com AGENTS.md + uma skill → segue a instrução e usa a skill.

## Fase 10 — Auth por subscription (OpenAI / Codex SOMENTE) — opt-in, ToS-gray
**Meta:** usar a subscription ChatGPT/Codex do usuário em vez de API key paga.
Escopo travado: **OpenAI somente** (Anthropic descartado). Implementado com **ultracode**
(design+pesquisa panel + review-gate por fatia). ⚠️ **ToS:** usar a subscription de
consumidor num agente de terceiros provavelmente viola os Termos da OpenAI (risco de
ban). Só como **opt-in explícito**, default OFF, com gate de reconhecimento de ToS.

**Achados do panel (fatos públicos do repo open-source `openai/codex`, citados — nada fabricado):**
- A subscription **só fala Responses API** em `https://chatgpt.com/backend-api/codex/responses` (o path de chat.completions/`api.openai.com/v1` é só pra API key) → precisa de um **transport Responses como cliente**, não só troca de bearer.
- Token vem do **login do Codex CLI** (`~/.codex/auth.json`, chmod 600, `tokens.{access_token, refresh_token, account_id}`; keyring out-of-scope) — Lohra **reusa** o login (não re-implementa o PKCE → menos risco/manutenção).
- Headers: `Authorization: Bearer <access_token>`, `ChatGPT-Account-ID: <account_id>`, `originator: codex_cli_rs`. Refresh: POST `https://auth.openai.com/oauth/token` `{client_id: app_EMoamEEZ73f0CkXaXp7hrann, grant_type: refresh_token, refresh_token}` (access_token é JWT curto; Lohra refresca sozinha). UNKNOWN: model slug + flags store/stream do backend → testar com o token real do usuário.

- [x] **B1 — Credential subsystem** (`lohra/subscription/`): ler `~/.codex/auth.json` (safe/bounded parse, ausência ≠ erro) + store próprio `~/.lohra/auth.json` (auth_mode, `acknowledged_tos_risk` hard-gate, chmod 600); refresh via endpoint público (runner HTTP injetável p/ teste); constantes públicas citadas. Nunca logar token.
- [x] **B2 — Responses-API client** (`ModelClient` novo / api_mode `responses`): `openai.OpenAI(api_key=token, base_url=…, default_headers=…).responses.create(...)` → mapeia p/ `NormalizedResponse`. Unit-test com mocks; UNKNOWNs (model slug/flags) validados ao vivo com o token do usuário.
- [x] **B3 — Wiring + gating + CLI** (`lohra auth`): provider opt-in `openai-codex`/auth_mode; `build_client` ramifica; **excluído de subagentes/workflow leaves**; **`lohra serve` recusa relay de token de subscription**; fallback claro pra API key.
- [~] **B4 — Robustez** ✅ (token nunca no env; fallback claro). **Refresh transparente ADIADO** (gap #1): write-back no ~/.codex/auth.json corre com o Codex (rotação→revogação); expirado → erro claro pedindo `codex`.
- [x] **Reasoning replay** ✅ (paridade opencode): `include=["reasoning.encrypted_content"]` + captura em `provider_data` + re-injeção no input (só items com encrypted state) → continuidade sob store=false. Review ultracode limpo.
- [x] **B5 — first-party OAuth (gap #1 FECHADO)** ✅: `lohra auth login` device flow + store próprio (oauth.json, atômico 0600) + refresh transparente seguro (token family próprio). Precedência sobre o reuse do Codex.
- [x] **VALIDADO AO VIVO** (subscription do usuário): chat · tools · usage · dashboard · fan-out · reasoning · vision · **login próprio + auto-refresh**. **Fase 10 COMPLETA.**

## Convenções de desenvolvimento
- **TDD obrigatório:** RED → GREEN → REFACTOR, 80%+ cobertura.
- **Arquivos pequenos:** 200–400 linhas típico, 800 max. Muitos arquivos pequenos > poucos grandes.
- **Imutabilidade:** nunca mutar; retornar cópias novas.
- **Sem segredos hardcoded:** env vars / secret manager.
- **Conventional commits:** `feat:`, `fix:`, `refactor:`, etc.
- A cada feature: code-reviewer agent depois de escrever; security-reviewer antes de commit.
