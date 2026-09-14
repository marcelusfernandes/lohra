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

`api_mode ∈ {"chat_completions", "responses", "anthropic_messages", ...}`. Cada modo tem um **Transport** com dois contratos: `build_kwargs(...)` e `normalize_response(...)`.

| Modo | Protocolo | finish_reason |
|---|---|---|
| `chat_completions` | OpenAI Chat Completions | `choice.finish_reason` |
| `responses` | OpenAI Responses API (itens de reasoning criptografados) | status field |
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
    native_outcome: NativeOutcome | None = None
```

### Autoridade do término nativo (#132)

Os três normalizers validam a evidência nativa antes de o loop anexar assistant,
extrair schema forçado ou despachar tools. `NormalizedResponse.native_outcome`
é opcional para transports Python confiáveis existentes. Nos transports nativos,
carrega diagnóstico separado do `finish_reason` canônico: modo, status/razão,
`incomplete_details.reason`, código/presença de erro e, nas recusas pertinentes,
evento terminal, status do item e categoria estrutural de recusa. São campos
fixos e escalares: tokens ASCII de protocolo com até 128 caracteres; ausência
continua ausente e valores malformados viram `<invalid>`. Nunca inclui payload,
headers ou texto opaco do erro. O texto humano preexistente de `response.failed`
continua em `error`, separado desses metadados.

- Chat aceita `stop`, `length`, `tool_calls`, `content_filter` e o legado
  `function_call`; Anthropic aceita `end_turn`, `stop_sequence`, `max_tokens`,
  `model_context_window_exceeded`, `tool_use`, `pause_turn` e `refusal`.
  Ausência/valor desconhecido não vira `stop`. Calls presentes exigem a razão
  nativa de tools, inclusive no caminho forçado. O descarte de deltas órfãos
  pelo assembler Chat sob um finish explícito de texto continua o contrato #117.
- Responses JSON exige status válido. `completed` permite calls somente quando
  seu status de item é ausente/None ou `completed`; `incomplete` preserva texto
  e causa com `length`/`partial`, mas não autoriza calls. `failed`, `cancelled`,
  estados não terminais, desconhecidos e valores inválidos são recusados.
  `error` não nulo contradiz sucesso mesmo sem código de erro.
  `completed` com `incomplete_details.reason` fornecido também é contraditório,
  inclusive quando a causa é malformada; detalhes sem causa não inventam uma.
- Em SSE, o evento conhecido `response.completed`/`response.incomplete` fornece
  status apenas quando o campo aninhado é ausente/None (#117). Não apaga status
  fornecido inválido ou contraditório. Terminais completed/incomplete conflitantes conservam o primeiro diagnóstico
  e são recusados; repetição coerente conserva o último recibo de usage
  não ausente, sem somar snapshots. `response.failed` recusa imediatamente com
  seu erro nativo e conserva usage anterior da mesma chamada se não reportar outra. O abort após o último callback precede essa
  validação. O fechamento físico e ownership dos streams permanecem os da #117.
  O primeiro status inválido observado de um function item é conservado até
  essa validação, tanto em `output_item.done` quanto no output terminal.
  Um snapshot posterior não pode apagar essa evidência substituindo o item,
  removendo seu status ou omitindo-o; texto/reasoning não são function items.

A recusa usa `ProviderCallFailed` com `NativeOutcome` e `Usage` opcionais; a
normalização está dentro do mesmo catch da chamada. Usage reportada pela resposta
recusada entra uma vez no agregado; ausência não cria medição nem herda a última
chamada. `usage` descreve a chamada mais recente e `usage_total` conserva o piso
conhecido das chamadas do turno, inclusive após falha/abort; não certifica uma
conta completa quando falta uma medição. `usage_uncertain` continua reservado
à interrupção. Classificação de
quota, `retry_after` e política de retry existentes não mudam. Não há polling,
fallback de rota nem protocolo financeiro novo.

O resultado do turno e o envelope CLI expõem `native_outcome`; `stop_reason`
explícito impede herdar `tool_calls` de um assistant anterior quando a chamada
seguinte falha. Respostas aceitas persistem uma cópia do diagnóstico em
`provider_data.native_outcome`, pelo contêiner SQLite existente. A substituição
forçada remove o tool_use sintético e conserva provider_data, inclusive thinking
assinado/reasoning criptografado. Os builders de request continuam lendo apenas
seus campos de replay conhecidos: os novos diagnósticos não entram como model
input, nem alteram o prompt congelado. Uma recusa não cria assistant artificial
para guardar metadados; diagnóstico de falha fica no resultado/envelope, sem
nova tabela de histórico de erros. A tradução HTTP do servidor permanece #133.

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

### 5.1 Lifetime do servidor SSE (#116)

As duas rotas OpenAI (`/v1/chat/completions` e `/v1/responses`) têm uma ponte
por request entre a execução bloqueante e o sender async. O owner observa
`http.disconnect` tanto em ASGI 2.0 quanto em 2.4, falha de `send` (inclusive
antes do primeiro body) e cancelamento da task. O sinal é sticky desde antes
da construção do Agent; o binding seleciona `request_interrupt` uma vez e chama
fora do lock. O sinal precede o wake de um produtor bloqueado na fila, para que
ele não avance e destaque seu Agent antes de observar o cancelamento.

`CompletionService.run(...)` mantém o contrato anterior. O protocolo opcional
`run_cancellable(cancellation=..., ...)` permite o binding; serviços legados
continuam com delivery/admissão limitados, mas não recebem um sinal de Agent.
Callbacks de interrupção são sinais cooperativos curtos: não fazem close de SDK,
join ou espera de provider. Callbacks Python arbitrários do embedder continuam
uma fronteira confiável; não há introspecção de assinatura nem retry por TypeError.

Defaults internos de `StreamLimits`, sem novas flags/env/configuração:

| Limite | Unidade e alcance |
| --- | --- |
| 64 peças / 256 KiB | payload UTF-8 **enfileirado** por request; backpressure enquanto conectado |
| 16 produtores | ativos + draining + lançamento reservado; vaga liberada só após a thread terminar |
| 250 ms | espera máxima de drain por resposta, após sinalizar e soltar a fila |
| 1 s | um prazo coletivo de drain no shutdown do inventário, não um prazo por produtor |

Contagens são inteiros positivos; byte capacity é pelo menos quatro; prazos são
finitos e não negativos. Deltas grandes são repartidos sem cortar um codepoint;
texto concatenado e ordem são preservados. O cap não limita a string já recebida
do SDK, o assembler, o resultado completo ou toda a memória do processo.
Cancelamento descarta deltas enfileirados e futuros, libera puts bloqueados e
não precisa colocar um sentinel na fila cheia. Notificações async são coalescidas.

A primeira decisão local de delivery não reabre. O recibo final imutável é
separado: só existe quando o produtor realmente publica resultado/erro. Um
cancelled ainda vivo permanece draining, sem inventar um recibo terminal ou
consumo. Um resultado tardio pode acrescentar ao único recibo sua medição
observada, sem voltar a entregar sucesso. A publicação do recibo não libera a
vaga de uma thread que ainda está executando seu epílogo.

Interrupção é um `UpstreamError` específico antes do mapeamento para texto vazio,
`stop` ou estimate. Preserva `usage_uncertain` e o piso reportado por chamadas
anteriores. Sem medição, usage permanece ausente; em Responses failed é `null`,
não zero. Chat conserva error-then-DONE, Responses conserva failed. O resultado
dict-compatible carrega uma marca local de proveniência que **não** entra na
serialização pública: mesmo cancel entre retorno do serviço e publish não
transforma uma estimativa legada de sucesso em `receipt.usage` observada.
O mapping normal de sucesso continua igual; normalização geral de razões/usage
nativas permanece em #132/#133. O close cooperativo do stream continua pertencendo
ao assembler (#42), nunca ao cliente compartilhado na thread de HTTP.

Apps embutidas drenam o inventário pelo lifespan. O entrypoint real `lohra serve`
mantém `lifespan="off"`, workaround histórico de PyInstaller, mas configura
`timeout_graceful_shutdown=0`: Uvicorn cancela os requests ativos na saída, sem
esperar um SSE silencioso antes de chegar à limpeza. O `finally` do CLI executa
explicitamente o drain coletivo de até 1 s **após** o retorno do runner. Esse
prazo não é um deadline global de processo, loop, SDK ou callback de close.

O host fecha o shared client somente se a admissão já estiver fechada e não
houver produtores vivos. A seleção desse callback é única e sua chamada ocorre
fora do lock; falha do callback é visível e não ganha retry implícito. Com thread
não cooperativa após o prazo, o CLI registra o inventário draining e mantém o
client aberto para teardown do processo. Não há watcher extra nem promessa de
fechamento eventual automático; um embedder pode repetir explicitamente a limpeza
depois da saída física dos produtores. O slot não desaparece enquanto I/O vive.

Admissão cheia ou fechada responde JSON 503 antes do HTTP 200/SSE. Falha de start
sem thread iniciada devolve a reserva; exceção depois de start aceito retém e
cancela aquela thread. O limite cobre SSE, não todas as chamadas não-streaming.
Python não mata threads; I/O silencioso e tools já em voo podem continuar até
evento/timeout/término natural (#119). #126/#127 são controles entregues e não
foram redesenhadas; esta fatia não encerra a parent #8. Evidência e limites:
[relatório #116](../history/reviews/2026-09-14-server-stream-lifetime.md).

### Integridade do término do stream (#117)

EOF do iterador não certifica um turno. Chat Completions exige `finish_reason`
textual não vazio nem branco; `[DONE]` sozinho é apenas um delimitador do SDK.
Anthropic exige o evento `message_stop` e `stop_reason` final textual não branco.
Responses exige `response.completed` ou `response.incomplete`; `response.failed`
preserva o erro e código nativos já propagados. A interpretação do vocabulário
mais amplo de razões/status pertence a #132/#133, sem whitelist nova nesta etapa.

Os assemblers drenam o stream até o fim, preservando usage posterior ao finish.
Consultam o mesmo gate de abort depois do último callback/EOF, antes de validar
o terminal: interrupção continua `AbortedStream`, não falha de protocolo. EOF
sem terminal válido levanta `ValueError` antes de inserir assistant, despachar
tools ou certificar/cachear output no workflow. Deltas já entregues não podem ser
recolhidos. O loop mantém usage não observada ausente; o formato legado de zeros
no `response.failed` ordinário do servidor permanece separado, reservado à #133.

Chat/Responses fecham o iterador em `finally`. Anthropic mantém o ownership
normal do context manager do SDK e fecha explicitamente também em abort/erro.
Close idempotente de wrappers não significa duas liberações físicas do body.
O cliente compartilhado nunca é fechado pelo assembler. `OpenAIClient.create`
e `AnthropicClient.create` genuinamente JSON não exigem eventos SSE;
`ResponsesClient.create` usa SSE internamente e exige o mesmo terminal.

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
