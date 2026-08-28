# Changelog

Todas as mudanças notáveis da Lohra, por versão publicada no PyPI
(`pip install lohra`). Formato inspirado em [Keep a Changelog](https://keepachangelog.com/);
versões seguem SemVer (fase 0.0.x: qualquer release pode conter mudanças incompatíveis).

## [Não publicado]

### Adicionado
- Providers diretos **xai** (Grok, alias `grok`), **glm** (Zhipu, aliases `zhipu`/`zai`)
  e **kimi** (Moonshot, alias `moonshot`) — OpenAI-compat; catálogo, chat e roteamento
  por nó de DAG funcionam automaticamente. 8 → 11 providers builtin.

## [0.0.8] — 2026-08-28

### Adicionado
- **Model routing** — o tema da release:
  - `lohra auth prefer <auto|subscription|api_key>`: o usuário escolhe a rota de auth
    por profile (antes a subscription ativa sempre vencia). `api_key` preserva o aceite
    de ToS; `subscription` inutilizável falha com remédio nomeado, nunca em silêncio.
  - `lohra models [--provider] [--json]`: catálogo REAL de modelos alcançáveis por
    provider (listagem live por API key, ollama local via API nativa, subscription via
    config do Codex). Falha por provider isolada; erros token-free; sem key → `skipped`
    nomeando a env var.
  - Tool `list_models` (tempo de autoria): resposta bounded (limit 25/100, totais
    explícitos — nunca cap silencioso), filtro por provider/query, tier map fresco.
    Excluída de subagentes e do server.
  - Guidance + skill `workflow-authoring`: a Lohra propõe modelo por nó do DAG a partir
    do catálogo; providers MISTOS no mesmo run (ex.: subscription + API key); usuário
    pediu confirmação → node `checkpoint` com o plano antes dos nós caros.
- Skill `use-lohra` ensina o model-routing ao orquestrador (Claude Code/Codex), com
  `--profile` em todos os comandos correlatos.

### Corrigido
- **Sob `--json`/`--no-input` a Lohra nunca mais prompta**: comando perigoso
  (`rm -rf`, `sudo`…) é auto-negado em vez de pendurar o processo esperando aprovação
  num stdin aberto-e-mudo (`--yolo` segue sendo a rota pré-autorizada).
- **Streams `chat_completions` capturam usage** (`stream_options.include_usage`, com
  fallback para servidores que o recusam; retry SÓ para essa recusa — timeout/429/5xx
  re-levantam): leaves openrouter/ollama não contam mais 0 tokens.
- Recusa de provider num nó vira fault nomeado (`<nó>: provider unavailable: <causa>`),
  não null mudo.
- Contrato "stdout = exatamente 1 objeto JSON" vale para todo `--json` até no erro
  pré-dispatch de profile inválido.
- Catálogo: cap de 4MB limita o download real (streaming + `Accept-Encoding: identity`
  contra bomba de descompressão); `close()` que levanta não descarta entries.
- `detect`/doctor leem o store de auth UMA vez (par ativo+preferência sempre coerente).

## [0.0.7] — 2026-08-27

### Corrigido
- Stages de pipeline recebem a mesma validação didática de `max_iterations` do node
  `agent` (antes, valor inválido rodava em silêncio sob default/clamp e dividia a
  identidade de células iguais no cache).
- `delegate_task` com `resume_id` recusa `max_iterations` (era aceito e ignorado).
- `max_iterations: null` explícito é recusado nas tools (igual ao validador de node).
- Skill `use-lohra` afirmava default 8 no chat — o real é 90 (`PARENT_MAX_ITERATIONS`);
  o conselho antigo REDUZIRIA o teto.

## [0.0.6] — 2026-08-27

### Adicionado
- **`max_iterations` configurável em três camadas** (era constante de código):
  por nó/stage de workflow (1–128, validação didática, entra na cell identity só
  quando declarado), por sessão (`lohra chat --max-iterations` + `LOHRA_MAX_ITERATIONS`,
  flag > env > default) e por subagente (`delegate_task`/`spawn_session`).
  `DEFAULT_LEAF_MAX_ITERATIONS=50` nomeado e pinado (leaves já rodavam nesse teto
  implícito). Estouro sempre honesto: fault `max_iterations (N) reached` no rollup.

## [0.0.5] — 2026-08-27

### Adicionado
- **Onboarding de primeiro uso** (ONB-1..9): `lohra init` (detecção do ambiente +
  wizard de provider em TTY), `lohra doctor` (diagnóstico linha a linha com o comando
  que corrige; `--json`), detecção de provider por key/env/config/daemon, e
  `lohra auth login` absorvendo o `enable` (um comando, uma intenção — aceite de ToS
  inline). Contrato headless: `--no-input`/`LOHRA_NO_WIZARD`, nunca prompta.

## [0.0.4] — 2026-08-26

### Adicionado
- **Live view dos workflows**: DAG impresso na aceitação do spec, progresso por
  nó/item ao vivo no stderr (faults na hora, custo item a item), modo TUI in-place
  (`LOHRA_LIVEVIEW=fancy|plain|off`), progresso durável cross-process e
  `lohra workflow list|watch` (espectador sem LLM, zero tokens).

## [0.0.3] — 2026-08-26

### Corrigido
- `--version` com fonte única (`importlib.metadata`) — a 0.0.2 instalada dizia
  "lohra 0.0.1" (a versão vivia em dois lugares).

## [0.0.2] — 2026-08-26

### Adicionado
- Kit de delegação `use-lohra` embutido no pacote + `lohra skill export <name> [--to DIR]`
  (a skill que ensina um orquestrador — Claude Code/Codex — a delegar à Lohra).

## [0.0.1] — 2026-08-26

### Adicionado
- **Primeira publicação no PyPI** — a Lohra vira runtime standalone instalável
  (`pip install lohra`): wheel py3-none-any (Python 3.11–3.13), CLI completa
  (`chat` com `--json` para orquestração, `dashboard`, `serve`, `cron`, `profile`,
  `auth`, `workflow`, `update`), agent core com tools (fs/terminal/web/MCP), memória
  e skills self-improving, harness de dynamic workflows (DAG declarativo, 10
  node-types, cache content-addressed, resume durável cross-process, token budget,
  checkpoint humano), orquestração de sub-sessões paralelas, multi-provider
  (8 providers) + subscription OpenAI/Codex opt-in, e skill builtin
  `workflow-authoring`. MIT.
