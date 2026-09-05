# Inventário read-only — issue #53 / T4 / E7 (Pai ← filho)

**Branch-alvo da tarefa:** `integration/wave9`. **Anomalia de checkout:** o
checkout compartilhado do repo mudou de `integration/wave9` para
`integration/wave10.1` NO MEIO desta investigação (confirmado — `git branch
--show-current` respondeu `integration/wave9` no primeiro comando desta sessão
e `integration/wave10.1` a partir de certo ponto, sem nenhuma ação minha; a
tarefa é read-only e eu não fiz `checkout`). Reagi lendo tudo com `git show
integration/wave9:<path>` a partir do momento em que percebi a troca, e
**revalidei contra `integration/wave9` cada trecho que eu já tinha lido** no
HEAD errado. Todo `file:line` abaixo é o número de linha **em
`integration/wave9`**, salvo indicação contrária explícita. `merge-base`
`integration/wave9`↔HEAD = `2086a1214d58dee60debfe459142b5ef2d65119b` — os
dois divergiram (wave9 não é ancestral do HEAD atual), e a diff mostrou algo
relevante por si só: `integration/wave9` **já tem E1/E2/E4 implementados**
(`recent_insights` estruturado, fingerprint por `issue.rule`, `run_id` no
template certificado) que a `integration/wave10.1` atual não tem — ou seja,
wave10.1 bifurcou de um ponto anterior a esse trabalho, e wave9 seguiu
sozinha. Isso não muda nenhuma conclusão de E7 abaixo (os dois pontos onde
comparei achado-em-HEAD vs achado-em-wave9 deram idênticos, exceto onde
citado).

---

## Canal 1 — nó `workflow` aninhado → `engine.fold_nested`

`fold_nested` vive em `backend/lohra/workflow/engine.py:814-911`, chamado de
`backend/lohra/workflow/strategies.py:1166` (`run_workflow`, spec §4.4). O
`RunResult` do filho vem de `engine.nested_engine(node.id).run(parsed,
sub_args)` (`strategies.py:1160`) — um `WorkflowEngine` que **compartilha**
`core`/`budget`/`cache`/`loader`/`cancel_event`/`pause`/`checkpoint_answers`/
`on_audit`/`run_id`/`segment_id`/`artifact_scope` com o pai, um nível mais
fundo (`engine.py:779-810`, doc própria do método). Isso já responde de
saída duas das perguntas da tarefa: **auditoria** e **checkpoint/pause** não
passam por `fold_nested` — passam por objetos COMPARTILHADOS por referência.

### Tabela — campo do `RunResult` do filho × destino no pai

| Campo do `RunResult` filho | Produzido pelo filho? | Alcança o pai? | Como (mecanismo) | Persistido? | Perdido p/ aprendizado? |
|---|---|---|---|---|---|
| `null_count`, `nodes_total`, `cap_trips`, `engine_faults`, `validation_retries`, `tokens_in/out`, `cache_read/write_tokens`, `reasoning_tokens`, `usage_uncertain_leaves`, `forcing_fallbacks`, `cells_replayed`, `tokens_saved`, `leaf_respawns`, `replay_divergences`, `artifact_advisories` | sim | sim | somado (`+=`) em `fold_nested` (`engine.py:818-843,893-901,936-937`) | sim (`workflow_run_state`/rollup) | **não** |
| `node_costs` (dict) | sim | sim | copiado com chave `f"sub[{ref}]:{node_id}"` (`engine.py:836-837`) | sim | não |
| `faults`, `pause_faults`, `recovered_faults`, `advisory_faults` | sim | sim | prefixado `f"sub[{ref}]: {f}"` (`engine.py:856,860,866-868,873-875`) | sim | não |
| `pause_fault` (singular) | sim | sim | anexado a `pause_faults` com prefixo (`engine.py:893-894`) | sim | não |
| `route_fault` (dict) | sim | sim, mas **renamespaceado**, não copiado como campo | `self._pause.renamespace({...})` (`engine.py:895-905`) — reescreve o `node_id`/`template` no PRÓPRIO objeto de pausa compartilhado | sim (payload de pausa) | não |
| `required_failure` | sim | sim, se o pai ainda não tinha um | `f"sub[{ref}]:{...}"`, só se `self._result.required_failure is None` (`engine.py:910-911`) | sim | não |
| `outputs` | sim | sim, **mas fora de `fold_nested`** | é o retorno de `run_workflow` (`strategies.py:1168` `return nested.outputs`), vira o valor do nó `workflow` no `context` do pai | sim (como qualquer output de nó) | não |
| `status` (do filho) | sim | **não é copiado** | o pai recalcula o PRÓPRIO `status` via `derive_status(result)` a partir dos campos já somados acima (`engine.py:2291`) — arquitetura correta, não um esquecimento | — | não (redundante por design) |
| `pause_reason`, `retry_after`, `checkpoint` | sim, escritos pelo filho | sim | via `self._pause` **compartilhado por referência** (não por `fold_nested`): quando o filho pausa, ele escreve em `self._pause` (mesmo objeto do pai); ao selar, o PAI lê `self._pause.reason/.retry_after/.payload` (`engine.py:2282-2289`) | sim | não |
| `rerouted_faults`, `reroutes` | sim, se o filho re-roteou algo | **NÃO** — nenhuma linha em `fold_nested` toca esses dois campos | nenhum — e é **deliberado**: `nested_engine` não passa `routes`/`route_fallback_try` ao filho, com o comentário explícito "*a node inside a template is not in the spec this run persists, so a re-route down here could never be carried forward by a resume*" (`engine.py:807-810`) | não | **não é perda de aprendizado** — é uma decisão de design já documentada (reroute não nasce dentro de um `workflow` aninhado porque não há spec própria pra persistir o resultado) |

### O que o filho produz que NÃO chega ao pai (canal 1)

1. **Rejeição de spec de um `ref` aninhado nunca vira insight candidate, e
   em `integration/wave9` nem sequer vira fault.**
   `strategies.py:1144-1146` (wave9):
   ```python
   parsed = validate_spec(spec_dict, supported_types=frozenset(STRATEGIES))
   if isinstance(parsed, ValidationError):
       logger.warning("workflow: nested ref %r failed validation: %s", ref, parsed.message)
       return None
   ```
   Isso é **só um `logger.warning`** — não chama `engine.record_fault`, não
   toca `self._result.faults`, não aparece em `workflow_audit`, não aparece
   no rollup. É invisível ao pai por completo, a não ser que alguém leia o
   log de processo do servidor (que a arquitetura não trata como canal de
   evidência de run). Confirmado que `integration/wave10.1` JÁ conserta a
   METADE fault deste buraco (issue #79 — `strategies.py:1147-1155` no HEAD
   atual adiciona `engine.record_fault(f"{node.id}: nested template '{ref}'
   rejected: {first_issue.rule}{locator}")`), mas **nem lá vira insight
   candidate** — ver item 2.

2. **`_record_spec_candidate` (E1, o único produtor de `insights.record()`
   em todo o código) tem UM ÚNICO call site, e não é este.**
   `backend/lohra/workflow/service.py:940` (`insights.record(...)`) é chamado
   só de `service.py:546`, dentro do fluxo de `run()`/launch de um workflow
   **top-level** (`agency_authored and explicit_spec`, guarda em
   `service.py:539`). Busquei `insights.record(` em todo o `backend/lohra/`
   (wave9): **uma única chamada, `service.py:940`**. Nem `strategies.py`
   (o caminho do nó `workflow` aninhado) nem `delegate.py` (subagente) jamais
   chamam `insights.record()`. Logo: mesmo que o `ref` aninhado tivesse sido
   autorado pela agência corrente (o caso interessante para #53), sua
   rejeição **nunca pode virar candidate**, hoje nem em wave9 nem em
   wave10.1 — só o `record_fault` textual chega (e só depois do #79).

3. **`cache_preview`/artefatos/`cache stamps` do filho: NÃO estão perdidos.**
   `backend/lohra/workflow/cache_preview.py:37-42` explicita que o nó
   `workflow` é percorrido recursivamente usando o MESMO loader e a MESMA
   convenção de namespace `sub[<ref>]:<node id>` que `fold_nested` usa — ou
   seja, o preview de cache de uma run que vai reexecutar já concilia com o
   que o filho vai custar. `artifact_scope` é compartilhado (`engine.py:806`
   do wave9 read), então os manifestos de artefato do filho escrevem na
   MESMA árvore de run do pai — não há um manifesto "só do filho" que fique
   de fora.

**Conclusão do canal 1:** a perda real e específica de E7 é **evidência de
autoria de spec rejeitada dentro de um `workflow` aninhado** — não porque o
dado não exista (o `ValidationError` existe, com `issue.rule` estruturado,
igual ao que `_record_spec_candidate` já sabe consumir), mas porque o
call site que produziria o candidate está noutra função e nunca é
alcançado por esse caminho. Tudo o resto do canal 1 (métricas, faults,
pause/checkpoint, route_fault, required_failure, outputs, cache) **chega ao
pai** — por soma, por prefixo namespaced, ou por objeto compartilhado — sem
nenhuma exceção encontrada além dessa.

---

## Canal 2 — `delegate_task` (subagente)

`backend/lohra/agent/delegate.py`. Isolamento confirmado por código:
`_CHILD_EXCLUDED_TOOLS` (linhas 50-73, wave9) inclui `memory`, `skill_view`,
`skill_manage`, `session_search`, `spawn_session`, `steer_session`,
`collect_session`, `run_workflow`+família, `list_models`, `delegate_task` —
o filho não tem NENHUMA dessas. `build_child_agent` (linhas 171-196) passa
`memory_store=None`, `skill_store=None`, `context_files=()`, `identity=None`.
Isso não está em disputa (E6 já congelou isso em teste anti-drift,
`backend/tests/test_agent_delegate_scope.py`).

### O que o `handle()` do `delegate_task` devolve ao pai

`DelegateTaskTool._summary` (`delegate.py:311-318`, idêntico em wave9 e
HEAD):
```python
@staticmethod
def _summary(sub_id: str, collected: dict) -> dict:
    if "error" in collected:
        return {"sub_id": sub_id, "status": "error", "summary": collected["error"]}
    return {
        "sub_id": sub_id,
        "status": collected["status"],
        "summary": collected["output"] or "(subagent produced no output)",
    }
```
Só **3 campos** chegam ao agente que chamou `delegate_task`: `sub_id`,
`status`, `summary` (= texto final do filho, ou o texto de erro se o turno do
filho morreu).

### O que `collected` (= `OrchestrationCore.collect()`) na verdade contém

`backend/lohra/orchestration/core.py:412-440` (idêntico em wave9 e HEAD):
```python
return {
    "status": sub.status,
    "output": sub.output,
    "tokens_in": sub.tokens_in,
    "tokens_out": sub.tokens_out,
    "cache_read_tokens": sub.cache_read_tokens,
    "cache_write_tokens": sub.cache_write_tokens,
    "reasoning_tokens": sub.reasoning_tokens,
    "provider": sub.provider,
    "model": sub.model,
    "forced_fallback": sub.forced_fallback,
    "usage_uncertain": sub.usage_uncertain,
    "error_kind": sub.error_kind,
    "retry_after": sub.retry_after,
}
```
**9 dos 13 campos são descartados por `_summary` antes de voltarem ao
agente**: `tokens_in`, `tokens_out`, `cache_read_tokens`,
`cache_write_tokens`, `reasoning_tokens`, `provider`, `model`,
`forced_fallback`, `usage_uncertain`, `error_kind`, `retry_after` (11, na
verdade — só `status`/`output` sobrevivem, renomeados).

`sub.error_kind` é setado em `core.py:810` (`_finalize`) a partir de
`result.get("error_kind")` — a MESMA classificação estrutural
(`classify_provider_error`) usada para o run/leaf top-level e citada no
T0/plano como candidata a diferenciar `environment` de `agency`. Quando o
turno do filho falha, `sub.output = result["error"]` (texto cru) **também**
chega via `summary` — então o TEXTO do erro não some, mas a
**classificação estruturada** (`error_kind`), o `provider`/`model`
efetivamente usado (útil quando o pai delegou com `model=` inválido — o
cenário exato de "slug escolhido pelo autor, rejeitado pelo provider" do
T0 §2.4/§4, só que agora o autor é o PAI escolhendo pro FILHO), e o custo
em tokens **não chegam de jeito nenhum** ao agente que orquestrou.

### Uma segunda rota já existe — e não é usada por `delegate_task`

O mesmo `OrchestrationCore` também está amarrado, na MESMA sessão
top-level, aos tools crus `spawn_session`/`steer_session`/`collect_session`
(`backend/lohra/agent/equip.py:173-180`, idêntico em wave9/HEAD — `triad =
OrchestrationTool(orchestration_core, ...)`, registrados ao lado de
`delegate_task` no MESMO dispatch). `collect_session`
(`backend/lohra/orchestration/tools.py:136-143`) faz `tool_result(**out)` —
ou seja, devolve **os 13 campos inteiros**, sem descartar nada. Como
`sub_id` retornado por `delegate_task` é o MESMO id usado pelo `core`
internamente (`core.py:276` `sub_id = uuid4().hex`, o mesmo id em ambos os
caminhos), o agente que chamou `delegate_task` **já pode, hoje, sem
nenhuma primitive nova**, chamar `collect_session(sub_id=<id do
delegate_task>)` e receber `error_kind`/tokens/`provider`/`model` que
`delegate_task` sozinho esconde. Isso NÃO está documentado em
`DELEGATE_GUIDANCE` (`delegate.py:82-89`), que só menciona reusar o `sub_id`
para `resume_id`. **Isto é presentation/guidance gap, não perda de dado** —
exatamente a advertência do T0 (§ hipótese nula) e a primeira coisa que a
issue pede pra testar antes de qualquer primitive.

Ressalva: `collect_session`/`spawn_session`/`steer_session`/`session_search`
estão marcados `author_time_only=True` no registry em wave9
(`backend/lohra/tools/registry.py:123-131`), mas um teste dedicado
(`backend/tests/test_agent_delegate_scope.py:16`) documenta que **isso é só
metadado**, não o mecanismo de exclusão — o mecanismo real é o
`_CHILD_EXCLUDED_TOOLS` do próprio `delegate.py`. Ou seja, o `author_time_only`
não impede a sessão top-level (a "autora") de usar esses tools — só marca a
intenção de que não deveriam vazar pra dentro de um filho/leaf, o que já é
garantido por outro mecanismo.

### Transcrição completa do filho: persistida, mas não indexada para descoberta

`OrchestrationCore.spawn` (`core.py:276-290`, idêntico wave9/HEAD) cria uma
sessão real: `self._db.create_session(sub_id, source="orchestration",
model=agent.model, system_prompt=..., parent_session_id=parent_id)` — o
`sub_id` do `delegate_task` é literalmente um `session_id` na MESMA
`SessionDB` do pai (mesmo `db` passado a `OrchestrationCore(db, ...)` e a
`build_session_dispatch(..., db=db, ...)` em `cli.py:485-509`, idêntico
wave9/HEAD). `GatewaySession(sub_id, agent, self._db, ...)` persiste as
mensagens do filho pelo caminho normal de qualquer sessão.

- `session_search(mode="read", session_id=<sub_id>)` →
  `db.load_messages(session_id)` (`state/db.py:1237-1244`) **não filtra por
  `source`** — funciona para ler a transcrição inteira do filho (tool calls,
  resultados, raciocínio) se o pai souber o `sub_id`.
- `session_search(mode="discovery", query=...)` → `db.search()`
  (`state/db.py:1246-1259`, FTS5) também **não filtra por `source`** — uma
  busca textual pode, em princípio, trazer conteúdo de um filho por
  acidente.
- `session_search(mode="browse")` → `db.list_sessions()`
  (`state/db.py:369-384`) **filtra explicitamente** `source !=
  'orchestration'` (linha 377, comentário: "*Orchestration sub-sessions are
  internal scaffolding ... keep them out of the user-facing list*"). Ou
  seja: **o pai não pode DESCOBRIR os filhos por browse**, só ler um filho
  específico se já tiver o `sub_id` (que `delegate_task` sempre devolve).

Conclusão: a transcrição completa do filho **não está perdida** — está
durável e legível pelo pai por um canal que já existe (`session_search`),
mas (a) não é listável por browse, e (b) `DELEGATE_GUIDANCE` não menciona
essa possibilidade.

### O que É perdido de verdade, sem nenhum outro canal (dentro do processo)

`OrchestrationCore` **nunca chama `session_add_usage`** nem qualquer outro
método de persistência do `db` além de `create_session` no spawn
(busquei `self._db.` inteiro em `core.py`: só a linha 284). Isso significa
que `tokens_in/out`, `cache_*_tokens`, `reasoning_tokens`, `provider`,
`model` (atribuição pós-configure), `forced_fallback`, `usage_uncertain`,
`error_kind`, `retry_after` **vivem SÓ no `_SubSession` em memória**
(`core.py:92-` `_SubSession` dataclass) — nunca gravados em nenhuma tabela.
Duas consequências:

1. Enquanto o processo está de pé e o filho ainda está em `self._children`
   (não foi despejado pelo `DEFAULT_MAX_CHILDREN=200`, comentário em
   `core.py:32-36`: "*the DB row persists, so only in-memory resume/collect
   of an evicted child is lost*"), o pai PODE recuperar isso via
   `collect_session`.
2. Depois que o processo termina, ou depois que o filho é despejado do
   registro em memória (LRU sobre `_children`), **`error_kind`, os 5
   medidores de tokens, `provider`/`model` efetivo e `forced_fallback` somem
   PARA SEMPRE** — mesmo que a transcrição de mensagens continue na tabela
   `sessions`/`messages`. Isso é uma perda real e específica: o texto cru do
   erro sobrevive (está em `messages`, e também ecoou em `summary` na hora),
   mas a CLASSIFICAÇÃO estruturada que distinguiria `agency` de
   `environment` (o próprio objetivo da Wave 9) não sobrevive além da vida
   do processo/registro em memória.

### Filho não tem `memory`/insight — nada equivalente a `_record_spec_candidate` existe para ele

Busquei `insights.record(` em todo `backend/lohra/` (wave9): **uma única
chamada**, `service.py:940`, inacessível a um filho de `delegate_task`
(que nem tem `run_workflow`, e portanto nunca passa por
`validate_spec`/`ValidationError` daquele jeito). Um filho que "aprendeu"
algo só pode dizer isso em prosa no seu texto final — que vira `summary`,
texto livre, sem mecanismo de classificação (`mechanism`/`responsibility`/
`confidence`) e sem fingerprint. Isso confirma, por código, a premissa
explícita da issue: "*texto de lição produzido pelo filho continua sendo
alegação do modelo*" — e mostra que, HOJE, **não existe NENHUM canal
automático** que promova uma alegação de um filho a `insights.record()`,
nem mesmo quando o filho relata um erro estrutural real (`error_kind`
setado) — porque `error_kind` nem chega ao agente pai pelo `delegate_task`.

---

## Ranking — top 3 perdas por impacto no objetivo da Wave 9 (aprender de falhas de agência)

### 1. `delegate_task` descarta `error_kind`/tokens/`provider`/`model` do filho (Canal 2)
- **Impacto:** é o caso mais direto de "falha de agência do filho, invisível
  ao pai de forma estruturada" — e coincide exatamente com a aresta §2.4 do
  T0 (modelo/slug inválido, rejeitado pelo provider) só que aplicada a um
  filho delegado em vez de a um leaf de workflow.
- **Evidência:** `delegate.py:311-318` vs `core.py:424-440`.
- **Menor primitive:** fazer `_summary()` incluir `error_kind`, `provider`,
  `model`, e os 5 medidores de uso no dict devolvido — **sem inventar
  schema novo**, só parar de descartar campos que `collect()` já produz e
  que `collect_session` já expõe integralmente. Tamanho **S** (um método,
  um arquivo, teste de contrato análogo ao de E2).
- **Coberto por wave10.1 (#78/#79)?** Não. Nem #78 (checkpoints aninhados)
  nem #79 (fault de spec aninhada) tocam `delegate.py`.

### 2. Rejeição de spec de um `workflow` aninhado nunca produz insight candidate (Canal 1)
- **Impacto:** é o único ponto do canal 1 onde uma decisão de AUTORIA (a
  escolha de um `ref` que resultou em spec inválida) desaparece sem deixar
  rastro utilizável — hoje nem fault (wave9), e mesmo depois do #79
  (wave10.1) só um fault textual, nunca um candidate causal comparável ao
  que `_record_spec_candidate` já sabe fazer para specs top-level.
- **Evidência:** `strategies.py:1144-1146` (wave9) / `service.py:546,940`
  (único call site de `insights.record`).
- **Menor primitive:** chamar `_record_spec_candidate`-equivalente (mesma
  interface pública, reaproveitando o fingerprint por `issue.rule` que E1
  já implementou) a partir de `run_workflow` quando `agency_authored` for
  verdadeiro para aquele nó — mesma guarda de autoria que o call site
  top-level já usa, só extendida ao caminho aninhado. Tamanho **S**, dado
  que E1/E2 já existem em wave9.
- **Coberto por wave10.1 (#78/#79)?** Parcialmente — **#79 cobre só a
  metade fault**, não a metade candidate/insight.

### 3. Estado estruturado do filho de `delegate_task` só existe em memória de processo (Canal 2)
- **Impacto:** mesmo se o item 1 for corrigido (a informação passa a
  alcançar o agente pai NO MOMENTO da chamada), nada garante que ela
  sobreviva para uma sessão FUTURA reabrir o caso — não há linha em nenhuma
  tabela hoje. Isso é o que impede qualquer auditoria offline ("quantas
  vezes um filho delegado bateu em `error_kind=AUTH_FAILED` no mês
  passado?").
- **Evidência:** `core.py` nunca chama `session_add_usage` nem equivalente
  para sub-sessions de orquestração; `_SubSession` é só em memória, sujeito
  a despejo por `DEFAULT_MAX_CHILDREN`.
- **Menor primitive:** ao finalizar (`_finalize`, `core.py:775-820`), gravar
  um resumo estruturado mínimo (`status`, `error_kind`, `provider`,
  `model`, os 5 medidores) numa coluna/])tabela existente ligada à sessão
  (ou reusar `session_add_usage` + uma coluna `error_kind` na tabela
  `sessions`) — não um `lesson` livre, um bundle tipado do harness, no
  espírito do que o T0 já recomenda como alternativa segura ao campo livre.
  Tamanho **M** (schema novo, ainda que pequeno).
- **Coberto por wave10.1 (#78/#79)?** Não.

---

## O que eu NÃO consegui verificar (limites deste inventário)

- **Não rodei os "3-5 runs reais com falha"** que o próprio T4/E7 pede como
  experimento mínimo ("instrumentar e diffar ao vivo"). Este documento é
  uma auditoria ESTÁTICA de código (o que o mecanismo permite/impede por
  construção), não uma medição empírica de runs reais — não tenho evidência
  ao vivo de que um filho tenha de fato produzido uma "lição" agency-class
  que se perdeu; tenho evidência de que, SE produzisse, o caminho para
  perdê-la existe exatamente como descrito acima. O próprio T0 já apontava
  isso: "*a comparação exige runs ao vivo com instrumentação nos dois
  lados; nada no disco preserva a visão do filho separada da do pai*" — e
  isso continua verdadeiro; eu inventariei os CANAIS, não coletei uma
  AMOSTRA.
- **Não confirmei se `messages` de um filho de `delegate_task` incluem,
  de fato, os tool calls inteiros e não só o texto final** — inferi isso
  do fato de `GatewaySession.submit()` ser o mesmo caminho usado por
  qualquer sessão normal (mesma classe, mesmo `db`), mas não abri
  `gateway/session.py` para confirmar linha a linha o que é persistido por
  mensagem.
- **Verifiquei os dois outros call sites de `OrchestrationCore(` e a
  assimetria é real, não hipotética.** `cli.py:1015` (modo dashboard/serve,
  wave9) constrói o `OrchestrationCore` do jeito interativo **sem
  `event_sink`**, idêntico ao `cli.py:485-487` — ou seja, `delegate_task`
  também não tem auditoria nem em modo `serve`. Já
  `workflow/service.py:659` constrói um `OrchestrationCore` **DIFERENTE**
  (usado para os LEAVES do harness de workflow, não para o `delegate_task`
  de uma sessão comum) com `event_sink=lambda sub_id, context, frame:
  self._audit.record_gateway(frame, context, sub_id=sub_id)` — É esse o
  motivo estrutural pelo qual o Canal 1 (workflow) tem trilha de auditoria
  e o Canal 2 (`delegate_task` de sessão interativa) não tem NENHUMA: são
  DUAS instâncias de `OrchestrationCore` distintas, uma com sink de
  auditoria (workflow) e outra sem (CLI/dashboard). Não confirmei se um
  LEAF de workflow que por sua vez chama `delegate_task` (se isso for
  possível — `delegate_task` está em `_CHILD_EXCLUDED_TOOLS`, então um leaf
  de workflow comum não o tem; não segui se algum caminho de nó do harness
  ainda assim invoca subagentes por fora do dispatch normal) herdaria o
  `core` com auditoria ou o sem-auditoria — ponto que ficou sem resposta.
- **Não testei o cenário adversarial** que a issue pede (filho tentando
  plantar uma conclusão falsa) — fora do escopo deste inventário read-only,
  que é sobre "o que existe/desaparece", não sobre segurança do que já
  existe.

## Veredito sobre as afirmações do T0 relativas a E7

- "**Nada no disco preserva a visão do filho separada da do pai**" (T0,
  §4/#53) — **parcialmente correto, refinado aqui**: para o canal 2, a visão
  COMPLETA do filho (transcrição) SIM está no disco (tabela `sessions`/
  `messages`, mesmo `db`), só que sob `source='orchestration'`, oculta do
  `list_sessions` e não referenciada pela guidance do `delegate_task`. O que
  de fato não está no disco é a CLASSIFICAÇÃO estruturada (`error_kind` e
  os medidores), que vive só em `_SubSession` (memória de processo).
- "**O isolamento é real** (`delegate.py:50-56, 189-190`)" — confirmado,
  linhas exatas em wave9: `_CHILD_EXCLUDED_TOOLS` em `delegate.py:50-73`;
  `build_child_agent` com `memory_store=None`/`skill_store=None` em
  `delegate.py:181-196` (a citação `189-190` do T0 aponta dentro dessa
  faixa — confere).
- "**O pai já recebe resultado, faults, status, rollup, `workflow_run_state`,
  `workflow_audit_events` e notices de recovery**" — confirmado PARA O
  CANAL 1 (nó `workflow` aninhado, via `fold_nested`); **não é o quadro
  completo para o CANAL 2** (`delegate_task`), onde o "resultado" que o pai
  recebe é deliberadamente empobrecido por `_summary()` em relação ao que o
  `core` já produz.
