# Lohra — Orquestração de Sessões Paralelas (Fase 7)

> **Status (2026-08-31):** Milestones A (core + tool triad) e C (`delegate_task` retomável) shippados e validados ao vivo; steering ganhou endereçamento e freios na Wave 6 (SUP-03). **Milestone B (métodos WS §5.4) ADIADO** — sem consumidor; ver `docs/ROADMAP.md`. O spec 04 ainda lista esses métodos no vocabulário — aspiracional, não implementado (a fonte da verdade é `gateway/app.py`).


> **Pergunta de origem:** "o estado atual da Lohra já permitiria orquestrar sessões em
> paralelo, injetando/recebendo prompts/respostas delas?"
> **Resposta:** a fundação concorrente existe (o `SessionManager` segura N sessões vivas
> sem lock global), mas falta a **camada de controle**: prompt não-bloqueante, injeção
> em sessão viva (steer) e coleta assíncrona de resultados. Esta fase entrega isso.

> **Prior-art:** opencode (sst/opencode, TS/Effect). Referência de *padrão*, não de código
> (mesma relação que com o Hermes). Os file:line âncora estão embutidos abaixo porque o
> clone em `/tmp/opencode` é volátil (apagado no boot). Reclonar de
> `github.com/sst/opencode` se precisar reabrir a fonte.

---

## 1. Princípio diretor — quem orquestra é o **agente Lohra**

A pergunta original tem como sujeito a **Lohra-agente** orquestrando sub-sessões — não um
cliente externo dirigindo uma API. Logo, o entregável de payoff é uma **tríade de tools
do agente**. As superfícies WS/REST expõem **o mesmo core** a clientes externos.

> **Regra de arquitetura: um core, dois consumidores.** Implementar a orquestração uma
> vez (`lohra/orchestration/`) e expô-la duas vezes — como tool do agente e como métodos
> WS. Nunca duplicar a lógica.

```
                    ┌─────────────────────────────┐
   tool do agente → │   OrchestrationCore         │ ← métodos WS (cliente externo)
  spawn/steer/      │   (registry de sub-sessões  │   session.steer / prompt.background
  collect           │    + inbox + coleta async)  │   session.branch / session.most_recent
                    └──────────────┬──────────────┘
                                   │ reusa
                          SessionManager (manager.py)
                          GatewaySession (session.py)
```

---

## 2. O que já existe (pontos de extensão, verificados)

| Peça | Arquivo | Estado relevante |
|---|---|---|
| Registry de sessões vivas | `lohra/gateway/manager.py:27` | `dict[str, GatewaySession]`, sem lock global de turno |
| Busy-lock **por sessão** | `lohra/gateway/session.py:47,58` | `submit()` **rejeita** se ocupada (`return {"busy": True}`) |
| Turno + streaming via `emit` | `lohra/gateway/session.py:56` | sink-agnostic; thread do WS encaminha frames |
| Lineage fork (compactação) | `lohra/gateway/manager.py:80` | filha **reusa** Agent do pai + **herda busy-lock** (Invariante #1) |
| Delegação one-shot | `lohra/agent/delegate.py:203` | `ThreadPoolExecutor` cap 3, filho fresco, descartado no fim |
| Vocabulário WS já specado | `docs/specs/04-gateway-protocol.md:40-42` | `session.steer`, `prompt.background`, `session.branch`, `session.most_recent`, `session.resume` — **listados, não implementados** |

**Conclusão:** esta fase **completa métodos já specados** + adiciona uma tool por cima.
Não é escopo net-new; é a dívida de orquestração do spec 04 sendo paga.

---

## 3. Decisões travadas (não re-perguntar)

1. **Filhos orquestrados são sessões INDEPENDENTES.** Cada uma com seu **próprio Agent**
   e prompt congelado. Isso elimina por construção o problema de reentrância do Agent
   compartilhado. → **NÃO** herdar o busy-lock compartilhado da fork de compactação
   (`manager.py:111-119`); aquele mecanismo é exclusivo do lineage split. Não copiar por reflexo.

2. **Concorrência permanece LIMITADA.** O cap de 3 do `delegate_task` é segurança de
   custo/rate-limit, **não** um descuido. Manter um teto **configurável** (default p.ex. 4–8);
   pode-se elevar, mas como decisão deliberada — **não** adotar o "unbounded" do opencode.

3. **Steer v1 = injeção entre-iterações**, não mid-LLM-call. O texto enfileirado é lido
   pelo loop **antes da próxima iteração** e anexado como mensagem user marcada
   `<system-reminder>`. Mid-call real fica fora de escopo (mais difícil; pode vir depois).

4. **Steer não viola o Invariante #1.** A injeção entra no **tail** do histórico de
   mensagens, nunca no prefixo (system prompt congelado) → prefix-cache continua quente.
   (Confirmado; é exatamente o padrão opencode.)

5. **Resultado sintético.** Saída de filho injetada no pai é marcada `synthetic: true`
   (scaffolding de orquestração ≠ input real do usuário) — relevante p/ compactação/memória.

---

## 4. Padrões do opencode adotados (com âncoras)

| Padrão | opencode (file:line) | Como entra na Lohra |
|---|---|---|
| Prompt não-bloqueante + canal de resultado | `prompt_async` retorna 204, resultado via SSE `/event` — `server/.../handlers/session.ts:309-327` (`Effect.forkIn(scope,{startImmediately:true})`) | `spawn_session`/`prompt.background` rodam o turno numa thread, retornam id na hora |
| Injeção universal em qualquer sessão | `prompt(anySessionID, parts)` — `session/prompt.ts:1105` | `steer_session(id, text)` resolve via `SessionManager.get(id)` |
| Steer via msg sintética + reminder | scan de msgs user após última assistant, embrulhadas em `<system-reminder>` — `session/prompt.ts:1082,1307` | inbox por sessão lido no loop (ver §6) |
| Re-promptar filho vivo | `background.extend({id,run})` — `tool/task.ts:242`; `task` tool com `task_id` — `tool/task.ts:47` | `spawn_session` devolve id; `steer_session(id,…)` continua o mesmo filho |
| Coletar resultado | `background.wait({id})` → `{status,output,error}` — `core/background-job.ts` | `collect_session(id, wait?)` lê estado/saída do registry |
| Subscribe por sessão | event bus tipado + SSE — `bus/global.ts`, `.../handlers/event.ts:25-99` | canal WS agregado filtrável por `session_id` (consumidor externo) |
| Isolamento de capacidades | `deriveSubagentSessionPermission` — `agent/subagent-permissions.ts:14` | reusar guardas de subagente já existentes (`delegate.py:111`) |
| API northbound | ACP `newSession/prompt/fork/cancel/listSessions` — `acp/service.ts:54` | espelhado pelos métodos WS (não é módulo separado) |

---

## 5. Entregáveis (ordem de dependência)

### 5.1 `OrchestrationCore` — `lohra/orchestration/core.py`
O registry de sub-sessões + inbox + coleta. Camada fina sobre `SessionManager`.

- `spawn(prompt, *, model?, tools_allow?) -> sub_id` — cria `GatewaySession` independente
  (Agent próprio via `agent_factory`, persiste no SessionDB com `parent_session_id`),
  dispara o turno numa thread de um pool **com teto configurável**, retorna na hora.
  Captura eventos num buffer por sub_id (não bloqueia o pai).
- `steer(sub_id, text)` — enfileira no inbox da sub-sessão (ver §6). Se a sessão estiver
  ociosa, equivale a um novo `submit`.
- `collect(sub_id, *, wait=False, timeout?)` — retorna `{status, output, events?}`.
  `wait=True` bloqueia até o turno terminar (com timeout).
- `list_children(parent_id) -> [sub_id...]` e `cancel(sub_id)` (interrupt cooperativo).
  O flag é lido em TRÊS pontos do turno: topo da iteração, antes do dispatch de
  tool calls e — num turno que STREAMA — entre os eventos do stream, onde o
  consumidor FECHA a conexão em vez de esperar o provider terminar de gerar
  (issue #42, épico E3; `agent/stream_abort.py`). Como `usage` só chega no FIM de
  um stream, um leaf cortado assim reporta `usage_uncertain: true` no `collect` e
  seus tokens são um PISO, nunca a fatura — nada estima a diferença.

  **Latência do abort** (não é constante): o check roda quando o PRÓXIMO evento
  chega, então o que se espera é o intervalo até esse evento, com teto no read
  timeout do HTTP. Provider gerando continuamente → milissegundos; provider em
  silêncio → só quando voltar a falar. Seguem não-abortáveis: chamada
  não-streaming, stream em silêncio e tool já em voo.
- **Teto de concorrência** via `Semaphore` configurável; logar quando enfileira por estar cheio.

### 5.2 Steer no loop — `lohra/agent/loop.py` + `GatewaySession`
`run_conversation` ganha um hook opcional `inbox: Callable[[], list[str]] | None`.
Entre iterações, drena o inbox e anexa cada texto como mensagem user `<system-reminder>`
**antes** da próxima chamada ao LLM. `GatewaySession.submit` deixa de só-rejeitar quando
ocupada: se há inbox e turno ativo, o texto vai pro inbox (não erro `session busy`).
(Caso ocioso continua trivial = `submit` de hoje.)

### 5.3 Tool do agente — `lohra/orchestration/tools.py` (interceptada, como `delegate_task`)
Tríade exposta ao modelo:
- `spawn_session(prompt) -> {sub_id}` — sub-sessão paralela, não-bloqueante.
- `steer_session(sub_id, text) -> {ok}` — injeta prompt numa sub-sessão viva.
- `collect_session(sub_id, wait?) -> {status, output}` — colhe a resposta.

Wiring em `equip.py`/`cli.py` (espelhar `register_delegate_task_schema` + intercept).
Excluída de subagentes e do server (como `delegate_task`/`cronjob`). Guardas de subagente
reusadas no Agent das sub-sessões.

### 5.4 Métodos WS — `lohra/gateway/app.py` (segundo consumidor do mesmo core)
- `prompt.background {session_id, text}` → `{status:"streaming"}`, não bloqueia o socket.
- `session.steer {session_id, text}` → injeta no inbox.
- `session.branch {session_id}` → cria sub-sessão independente a partir do histórico.
- `session.most_recent` / `session.resume` → completar o vocabulário do spec 04.
- (Opcional) canal de eventos agregado filtrável por `session_id` p/ orquestrador externo.

### 5.5 `delegate_task` ganha continuidade (opcional, alinhado)
`delegate_task` passa a **retornar `sub_id`** e aceitar `resume_id` — o filho one-shot vira
filho retomável reusando o `OrchestrationCore` (em vez do `ThreadPoolExecutor` descartável).
Mantém o isolamento de hoje (sem memória/skills/histórico do pai). Decisão explícita:
o filho retomável **é** uma `GatewaySession` no `SessionManager` (persiste, lineage), porém
com Agent isolado. Pode ficar p/ um sub-marco se a fase ficar grande.

---

## 6. Mecânica do inbox (steer) — detalhe load-bearing

```
sub-sessão ocupada:                        sub-sessão ociosa:
  steer(id, txt) → inbox[id].append(txt)     steer(id, txt) → submit(txt)  (= hoje)

run_conversation, topo de cada iteração:
  for txt in drain(inbox):
      messages.append(user("<system-reminder>"+txt+"</system-reminder>"))
  → próxima chamada ao LLM já enxerga o texto injetado
```

- Inbox = `dict[sub_id, list[str]]` protegido por lock leve no core.
- Drenado **só entre iterações** (v1) → simples e seguro; sem corromper uma chamada em voo.
- Espelha opencode `prompt.ts:1307` (scan de msgs após última assistant).

---

## 7. Riscos / invariantes a não quebrar

- **Invariante #1:** steer injeta no tail, nunca no system prompt. Sub-sessões têm prompt
  próprio congelado. ✅
- **Custo/rate-limit:** teto de concorrência obrigatório e configurável. Logar enfileiramento.
- **Reentrância do Agent:** sub-sessões = Agents independentes. NÃO reusar o busy-lock
  compartilhado da fork de compactação.
- **Persistência:** sub-sessões persistidas com `parent_session_id` (lineage já suportado
  no SessionDB). Não persistir turno interrompido/erro (regra atual em `session.py:113`).
- **Server/subagentes:** tools de orquestração excluídas de ambos (evita RCE/loops).

---

## 8. Plano de testes (TDD, 80%+)

1. **Core:** spawn retorna id imediatamente; collect(wait) devolve saída; teto de
   concorrência enfileira o excedente; cancel interrompe.
2. **Inbox/steer:** texto enfileirado aparece como `<system-reminder>` na próxima
   iteração; steer em sessão ociosa = submit; ordem preservada.
3. **Tool triad:** spawn→steer→collect ponta-a-ponta com runner fake; isolamento
   (sub-sessão não vê memória/skills do pai); exclusão em subagente/server.
4. **WS:** `prompt.background` não bloqueia o socket; `session.steer` injeta;
   `session.branch` cria filha independente; rejeições/erros corretos.
5. **Invariante #1:** system prompt da sub-sessão idêntico antes/depois de steer.
6. **E2E (usuário, LLM real):** Lohra spawna 2+ sub-sessões, injeta follow-up numa
   delas, colhe as respostas e integra — sem travar o turno pai.

---

## 9. Encaixe no roadmap

Fase 6 está fechando (só restam mensageria opcional e packaging). Orquestração é **escopo
novo além da paridade Hermes** e depende de gateway+delegação+lineage (Fases 3 e 5, prontas)
→ é naturalmente a **Fase 7**. Branch sugerida: `feat/phase-7-orchestration`.
Sub-marcos: (A) core+inbox+tool triad · (B) métodos WS · (C) `delegate_task` retomável.
