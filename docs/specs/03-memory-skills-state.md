# Lohra — Memory, Skills & State Persistence Spec

> **Nota:** doc de bootstrap (Fases 0–3). O código divergiu desde então — ver `docs/ARCHITECTURE.md` §Referência.

> Do Hermes Agent (MIT). `HOME` default `~/.lohra`.

## 1. Persistência de Sessão (SQLite + FTS5)

DB: `HOME/state.db`. `SCHEMA_VERSION`. **WAL** com fallback para `journal_mode=DELETE` em NFS/SMB/FUSE.

### Schema principal (DDL essencial)
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
    model TEXT, model_config TEXT, system_prompt TEXT,
    parent_session_id TEXT,                -- lineage / compression-fork
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
    cwd TEXT, estimated_cost_usd REAL, actual_cost_usd REAL,
    title TEXT, api_call_count INTEGER DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT,
    tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
    reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
    codex_reasoning_items TEXT, codex_message_items TEXT,
    platform_message_id TEXT, observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1     -- soft-delete p/ rewind/undo
);
CREATE TABLE state_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE compression_locks (session_id TEXT PRIMARY KEY, holder TEXT, acquired_at REAL, expires_at REAL);
-- índices em source, parent, started_at, (session_id,timestamp), (session_id,active,timestamp)
```

### FTS5 (duas virtual tables)
`messages_fts` (unicode61) + `messages_fts_trigram` (CJK/substring), alimentadas por triggers. Conteúdo indexado = `content + ' ' + tool_name + ' ' + tool_calls`. Degrada graciosamente se FTS5 ausente.

### Lineage / branching
- Branching = cadeia `parent_session_id`. Na compactação: encerra sessão atual (`end_reason='compression'`), cria filha com `parent_session_id` = id antigo.
- `_session_lineage_root_to_tip` caminha pela cadeia (cap 100). Deleção orfana filhos (não cascateia).

### session_search (4 formas, custo LLM zero)
DISCOVERY (FTS5 BM25 + janela ±5), SCROLL (±window), READ (sessão inteira), BROWSE (recentes). Sintaxe FTS5: AND default, OR, NOT, "frases", prefix*.

## 2. Memory (MEMORY.md + USER.md)

- Em `HOME/memories/`. Delimitador de entrada: `"\n§\n"`. Limites de char (não tokens): MEMORY 2200, USER 1375 (orçamento do arquivo inteiro).
- Tool `memory` (actions `add|replace|remove`): identifica entrada por **substring única** (`old_text`). Mutação: lock `.lock`, re-lê do disco, checa drift externo, dedup, enforça limite, escrita atômica (temp + `os.replace` + fsync).
- **Drift guard:** se on-disk não round-trip pelo parser, recusa, salva `.bak.<ts>`.
- Threat scanning na escrita.

### Injeção (FROZEN SNAPSHOT — crítico)
`load_from_disk()` captura `_system_prompt_snapshot` no início da sessão. `format_for_system_prompt()` retorna o **snapshot congelado**, não estado vivo. Escritas mid-sessão atualizam disco imediatamente mas **não mudam o system prompt** (preserva prefix cache). Snapshot refresca só no próximo início de sessão.

### Guidance de escrita (resumo)
"Save durable facts... WHEN TO SAVE proactively: usuário corrige você, compartilha preferência/hábito, você descobre algo do ambiente, aprende convenção/quirk. NOT: progresso de task, logs de trabalho concluído, TODO temporário. Procedimentos vão para skills, não memory."

## 3. SOUL.md (Persona)
`HOME/SOUL.md`. Slot #1 do tier "stable". Se presente, vira a identidade (`skip_soul=True` nos context files p/ não duplicar). Se ausente, fallback para `DEFAULT_AGENT_IDENTITY`. Seedado no primeiro run.

## 4. Skills

### Localização
Bundled `skills/<category>/<name>/SKILL.md`; user/agent `HOME/skills/<[category/]name>/`.

### Formato SKILL.md (agentskills.io)
```markdown
---
name: skill-name              # ≤64 chars, lowercase + hyphens
description: Brief desc       # ≤1024 chars
version: 1.0.0
platforms: [macos, linux]    # opcional; omitir = todas
metadata:
  lohra: { tags: [...], related_skills: [...] }
---
# Título
Instruções...
```
Dirs de suporte: `references/`, `templates/`, `scripts/`, `assets/`.

### Indexação (progressive disclosure)
`build_skills_system_prompt()` indexa **só metadata** (name + description por categoria), não os corpos. Cache de duas camadas (LRU + snapshot em disco). Bloco "## Skills (mandatory)": antes de responder, scanear; se relevante, carregar com `skill_view(name)`.

### Self-improving (auto-criação)
**(a) Foreground `skill_manage`** (`create|patch|edit|delete|...`). Guidance: "Skills são memória procedural. Create quando: task complexa (5+ calls), erros superados, workflow não-trivial. Update quando stale/wrong."

**(b) Background review** — daemon thread forka o agente, replaya a conversa, e pergunta se deve salvar/atualizar memory ou skills. Inherita o runtime do pai (mesma prefix cache) mas é **whitelisted a só memory + skill tools**. A conversa principal e a prompt cache nunca são tocadas. Guidance: "Seja ATIVO — a maioria das sessões produz ao menos um update de skill. Prefira: UPDATE skill carregada > UPDATE umbrella existente > ADD support file > CREATE nova umbrella class-level. NÃO capture: falhas dependentes de ambiente, claims negativas sobre tools, erros transientes."

## 5. Context Compression

### ContextEngine (ABC pluggable)
`should_compress(prompt_tokens)`, `compress(messages, current_tokens, focus_topic)`. Defaults: `threshold_percent=0.50`, `protect_first_n=3`, `protect_last_n=20`.

### Algoritmo
1. **Prune tool results antigos** (pré-pass, sem LLM): substitui corpos por resumos de 1 linha.
2. **Protege head** (system + first_n).
3. **Tail por token budget** (~20K, não contagem fixa).
4. **Summariza o meio** com prompt estruturado (Active Task, Goal, Completed Actions, Active State, Blocked, Key Decisions, Pending User Asks, Remaining Work).
5. **Update iterativo** do summary existente.
6. Limpa pares tool_call/result órfãos.

Prefixo `[CONTEXT COMPACTION — REFERENCE ONLY]`: tratar como background, MEMORY/USER permanecem autoritativos. Usa modelo auxiliar (barato).

### Lineage na compactação
Split em SQLite: `commit_memory_session` → `end_session(old, "compression")` → novo `session_id` → `create_session(new, parent_session_id=old)`. `compression_locks` previne corrida.

## 6. Cron
`HOME/cron/jobs.json`. `scheduler.tick()` a cada ~60s (file lock). Tipos: `once` (run_at), `interval` (minutes), `cron` (expr). Job roda como agente forkado.

## 7. Layout HOME (~/.lohra)
| Path | Propósito |
|---|---|
| `state.db` | SQLite sessões + FTS5 |
| `config.yaml` | Config principal |
| `SOUL.md` | Persona (slot #1) |
| `memories/MEMORY.md` / `USER.md` | Notas do agente / perfil do usuário |
| `skills/` | Skills user + agent-created |
| `cron/jobs.json` | Jobs agendados |
| `logs/` | Logs |
| `plugins/` | Plugins do usuário |
| `profiles/<name>/` | Perfis isolados |

## Notas para Lohra
1. Prefix-cache invariant guia tudo: system prompt frozen por sessão; memory/skill writes atualizam disco, nunca o prompt vivo.
2. Memory = declarativo (§-delimitado, char-bounded); Skills = procedural (SKILL.md class-level).
3. Self-improvement = background agent forkado, tool-whitelisted, não muta a conversa.
4. Compactação forka nova sessão SQLite (`parent_session_id`), não edita a antiga.
