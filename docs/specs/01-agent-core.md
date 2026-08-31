# Lohra Agent Core — Architecture Spec

> **Nota:** doc de bootstrap (Fases 0–3). O código divergiu desde então — ver `docs/ARCHITECTURE.md` §Referência.

> Extraído do Hermes Agent (MIT). Spec conceitual/contrato para reimplementação clean-room em Python. Os caminhos citados referem-se à árvore de referência (clone do hermes-agent).

---

## 1. O Loop de Conversa

**Entry point:** `run_conversation(agent, user_message, system_message=None, conversation_history=None, task_id=None, stream_callback=None, persist_user_message=None) -> dict`

### Ciclo de vida de um turno

**Prólogo (uma vez por turno)** — `build_turn_context(...)`:
- guarda de stdio, reset de contadores de retry
- sanitização da mensagem do usuário (remove surrogates soltos que quebram `json.dumps`)
- hidratação de todos/nudge
- **restore-or-build do system prompt** (reaproveita `agent._cached_system_prompt` se existir, senão constrói)
- persistência de resiliência a crash (grava a mensagem do usuário antes da chamada à API)
- compressão de contexto preflight
- hook de plugin `pre_llm_call` + prefetch de memória externa

**Loop principal** — `while (api_call_count < max_iterations and iteration_budget.remaining > 0) or _budget_grace_call:`

Cada iteração:
1. `new_turn()` — reset de dedup de checkpoint.
2. **Checagem de interrupção** — se `_interrupt_requested`, sai com `interrupted_by_user`.
3. `api_call_count += 1`; consome budget.
4. `step_callback(api_call_count, prev_tools)`.
5. **Drena /steer pendente** — mensagem out-of-band do usuário injetada na última msg `role:"tool"`.
6. **Monta `api_messages`** (history + system prompt) com prep do provider, prompt caching, reaplicação de reasoning-echo.
7. **`build_api_kwargs`** — ramifica por `api_mode`.
8. **Chamada à API com retry+fallback** — `_interruptible_api_call`, depois `normalize_response` → `NormalizedResponse`.
9. **Mapeia finish_reason** para `{stop, tool_calls, length, content_filter}`.
10. Trata `length` (continuação, detecção de esgotamento de thinking).
11. **Monta a assistant message** e anexa ao histórico.
12. **Ramifica:** se há `tool_calls` → valida/repara/dedup/executa, anexa resultados como `role:"tool"`, **continua o loop**. Senão → resposta final, `break`.
13. Persiste após cada iteração.

**Epílogo:** cleanup, persistência da sessão, review de memória opcional, geração de título.

### Condições de saída
`final_response_produced`, `interrupted_by_user`, `budget_exhausted`, `max_iterations`, `thinking_exhausted`, fatal (retries + fallback esgotados).

### Contrato do dict de resultado
```python
{
  "final_response": str | None,
  "messages": list[dict],
  "api_calls": int,
  "completed": bool,
  "partial": bool,
  "interrupted": bool,
  "error": str | None,
}
```

---

## 2. Os Três Modos de API & Schema Interno

`api_mode ∈ {"chat_completions", "codex_responses", "anthropic_messages", ...}`. Cada modo tem um **Transport** com dois contratos: `build_kwargs(...)` e `normalize_response(...)`.

| Modo | Protocolo | finish_reason |
|---|---|---|
| `chat_completions` | OpenAI Chat Completions | `choice.finish_reason` |
| `codex_responses` | OpenAI Responses API (itens de reasoning criptografados) | status field |
| `anthropic_messages` | Anthropic Messages (content-blocks, thinking, cache_control) | `stop_reason` mapeado |

### Tipos canônicos (o loop NUNCA ramifica por api_mode na leitura)
```python
@dataclass
class ToolCall:
    id: str | None
    name: str
    arguments: str           # JSON string
    provider_data: dict | None

@dataclass
class NormalizedResponse:
    content: str | None
    tool_calls: list[ToolCall] | None
    finish_reason: str       # "stop" | "tool_calls" | "length" | "content_filter"
    reasoning: str | None = None
    usage: Usage | None = None
    provider_data: dict | None
```

### Schema da mensagem armazenada (superset OpenAI)
```python
# assistant
{ "role":"assistant", "content":str, "reasoning":str|None, "finish_reason":str,
  "reasoning_content":str, "reasoning_details":[...], "tool_calls":[
    {"id":str,"type":"function","function":{"name":str,"arguments":str}} ]}
# tool result
{ "role":"tool", "name":str, "tool_call_id":str, "content":str }
```

**Contratos-chave:** stripar `<think>…</think>` na fronteira de persistência; sanitizar surrogates e redigir segredos; **preservar blobs opacos de reasoning sem modificação** (vários providers dão 400 sem eles).

---

## 3. Abstração de Provider

### `ProviderProfile` (declarativo — NÃO constrói cliente)
```python
@dataclass
class ProviderProfile:
    name: str
    api_mode: str = "chat_completions"
    aliases: tuple = ()
    display_name: str = ""; description: str = ""; signup_url: str = ""
    env_vars: tuple = ()
    base_url: str = ""; models_url: str = ""
    auth_type: str = "api_key"   # api_key | oauth_device_code | oauth_external | aws_sdk
    supports_vision: bool = False
    fallback_models: tuple = ()
    hostname: str = ""
    default_headers: dict = {}
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""
```
Hooks overridáveis: `get_hostname()`, `prepare_messages()`, `build_extra_body()`, `build_api_kwargs_extras()`, `get_max_tokens()`, `fetch_models()`.

### Registry de plugins
- `register_provider(profile)` indexa por nome + aliases (last-writer-wins).
- Descoberta: bundled `plugins/model-providers/<name>/` → user `$HOME/plugins/...` → legacy single-file.
- Resolução: **arg → config → env → "auto"**.
- `_detect_api_mode_for_url(base_url)` infere o modo da URL.

---

## 4. Fallback Chain

`try_activate_fallback(agent, reason)`:
1. Cooldown em rate_limit/billing (60s).
2. Se índice esgotado → `False`.
3. Pop da entrada; pula entradas inválidas/self.
4. Constrói cliente via roteador central; determina novo `api_mode`.
5. **Swap in-place:** `agent.model/provider/base_url/api_mode`; limpa `_config_context_length`, `_transport_cache`; limpa credential pool se mudou de provider; swap de cliente por modo; reavalia política de prompt-caching.
6. Retorna `True` → retry loop reemite com o novo backend.

---

## 5. Chamada de API Interruptível

Padrão: thread daemon + poll loop.
- Worker thread roda a request bloqueante; cria **seu próprio cliente per-request** (interrupt só mata o transporte local).
- Main thread: `while t.is_alive(): t.join(timeout=0.3)`; a cada poll checa `_interrupt_requested`.
- Watchdogs: TTFB cutoff, event-idle, stale timeout.
- **Regra de ownership de FD:** thread "estranha" (interrupt/watchdog) só faz *shutdown* de sockets; o worker fecha o cliente da própria thread (evita corrupção de SQLite por reciclagem de FD).

---

## 6. Superfície de Callbacks (contrato com a UI)

| Callback | Propósito |
|---|---|
| `stream_delta_callback(text)` | deltas de texto visível |
| `reasoning_callback(text)` | deltas de chain-of-thought |
| `thinking_callback(status)` | linha de status "thinking…" |
| `tool_progress_callback(...)` | progresso de tool em execução |
| `tool_start/complete_callback(tool,...)` | lifecycle de tool |
| `tool_gen_callback(tool_name)` | início da geração de argumentos |
| `step_callback(count, prev_tools)` | hook por iteração |
| `interim_assistant_callback(text, ...)` | comentário do assistant entre tool batches |
| `status_callback(kind, message)` | status de lifecycle/warn |
| `clarify_callback(...)` | prompt interativo de clarificação |

Todos opcionais, disparados por wrappers `_fire_*` que engolem exceções.

---

## 7. Montagem do System Prompt — 3 Tiers

Construído **uma vez por sessão**, cacheado; só recompila após compressão. Ordenado mais-estável → menos-estável para manter o prefixo da KV cache quente.

- **`stable`** — identidade + toda a guidance + hints de ambiente. Byte-estável pelo processo.
- **`context`** — `system_message` do caller + arquivos de contexto (AGENTS.md/.cursorrules).
- **`volatile`** — snapshot de memória, USER.md, timestamp **só com data** (não minuto, para não invalidar a cache).

### Blocos-chave (verbatim do Hermes — adaptar para Lohra)

**Identity (default):** "You are Hermes Agent, an intelligent AI assistant created by Nous Research. You are helpful, knowledgeable, and direct..."

**Memory guidance:** "You have persistent memory across sessions. Save durable facts... If a fact will be stale in a week, it does not belong in memory... Write memories as declarative facts, not instructions to yourself. 'User prefers concise responses' ✓ — 'Always respond concisely' ✗."

**Task completion:** "...the deliverable is a working artifact backed by real tool output — not a description of one... NEVER substitute plausible-looking fabricated output... Reporting a blocker honestly is always better than inventing a result."

**Tool-use enforcement** (gated a famílias `gpt/codex/gemini/gemma/grok/glm/qwen/deepseek`): "You MUST use your tools to take action — do not describe what you would do... Never end your turn with a promise of future action — execute it now."

**Steer channel note:** mensagens `/steer` mid-turn são anexadas ao fim de um tool result com marcadores `[OUT-OF-BAND USER MESSAGE]` — o modelo só confia nesse marcador exato (defesa contra prompt-injection).

---

## 8. Roteamento do Cliente Auxiliar

Roteador único para tarefas-laterais (compressão, session search, web extraction, vision, geração de título) → modelos **baratos/rápidos**.
- Seleção: `ProviderProfile.default_aux_model` → dict de fallback.
- Cadeias (modo auto): main → OpenRouter → Portal → custom → Anthropic nativo → providers de API-key.
- `call_llm(task=...)` é o entry unificado; em HTTP 402 auto-retry no próximo provider.

---

## Notas para Lohra
- **Invariante #1:** construir o system prompt uma vez, cachear, só recompilar após compressão. Ordem stable→context→volatile, timestamp só-data.
- Fazer `NormalizedResponse`/`ToolCall` o único tipo que o loop lê; empurrar quirks de provider para transports + `provider_data`.
- A regra de ownership de FD na chamada interruptível é load-bearing; não simplificar para `client.close()` da thread de interrupt.
- `ProviderProfile` é puramente declarativo; manter lógica de cliente/credencial/streaming no agente.
