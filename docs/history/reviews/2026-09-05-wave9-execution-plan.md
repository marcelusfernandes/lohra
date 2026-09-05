# Wave 9 — plano de rodada (pós-decisão do dono, 2026-09-05)

Investigação READ-ONLY. Nenhum arquivo do repo foi alterado. `main` = 0.0.25 (a taxonomia
T0 foi escrita em 0.0.21/22; alguns números de linha citados lá já andaram — recito os
atuais e sinalizo onde divergem).

## 0. A decisão do dono (fonte: último comentário da #54, 2026-09-05)

> modelo inexistente escolhido pelo autor = **`agency`**. Complemento: a Lohra tem
> acesso ao catálogo de modelos, então não deveria cometer esse erro; **se cometer por
> instrução humana**, a Lohra deve **escolher um modelo existente adequado à tarefa**
> em vez de falhar/pausar. Isso adiciona comportamento à taxonomia: rota inexistente na
> autoria → substituição por catálogo com aviso (advisory + nota no rollup),
> classificada como `agency` para o loop de aprendizado.

Isso resolve a "aresta" que a própria taxonomia (§2.4, §4-#50, §4-#52) tinha devolvido
ao dono como pergunta aberta, e reabre o denominador do #52 (7/57 notices deixam de ser
`environment` puro).

---

## 1. Épicos E1–E7 (da taxonomia) — hipótese, evidência de código, experimento RED, tamanho, dependências

### E1 — Fingerprint causal + contador de recorrência
**Hipótese:** o hash de dedupe do candidate usa texto livre normalizado, então o mesmo
defeito em nós de nomes diferentes gera linhas distintas, e uma duplicata exata não
incrementa contagem nenhuma — não há substrato para medir recorrência.

**Evidência de código (confere no HEAD atual):**
- `backend/lohra/state/insights.py:74-76` — `_fingerprint` faz
  `"|".join((kind, responsibility, mechanism, _normalize(summary)))`; `summary` é texto
  livre (inclui o node id e a mensagem completa do `ValidationError`).
- `backend/lohra/state/insights.py:151-171` — `INSERT OR IGNORE INTO
  workflow_insight_candidates (...)`: colisão de fingerprint é no-op; não há coluna
  `hits`, e `updated_at` não avança na duplicata (só é setado uma vez, na criação).
- `backend/lohra/workflow/service.py:929-936` (`_record_spec_candidate`) é o único
  produtor automático hoje: `summary="authored workflow spec rejected by validate_spec:
  {error.message}"` — `error.message` é prosa de `ValidationError`, não um código
  estável do `SpecIssue`.

**Experimento RED:** duas specs inválidas pelo **mesmo motivo estrutural** (ex.: dois
nodes `tier: "xl"` inexistente) em nós de IDs diferentes → hoje produz **2 linhas**,
`hits` inexistente. Depois de E1: **1 linha**, `hits=2`. Duas specs com motivos
DIFERENTES devem continuar gerando 2 linhas — esse é o teste negativo que evita
colapsar causas distintas.

**Tamanho:** **S** — confinado a `state/insights.py` (mudar a base do hash para
`(kind, responsibility, mechanism, código_estável_do_SpecIssue)` + `ON CONFLICT DO
UPDATE SET hits=hits+1, updated_at=?`; precisa de uma migração de schema trivial
adicionando a coluna `hits`).

**Dependências:** nenhuma upstream. É pré-requisito conceitual (não de arquivo) para
E2 (expor `hits`) e soft-dependência para o épico novo E8 (que também chama
`insights.record`, mas usa a interface pública sem tocar `_fingerprint`).

---

### E2 — Entregar evidência estruturada em vez de summary
**Hipótese:** o único canal com proveniência estruturada rica descarta tudo antes de
entregar ao agente, então "candidates não são consumidos" pode ser um artefato da
interface de entrega, não da ausência de sinal.

**Evidência de código:**
- `backend/lohra/workflow/service.py:1471-1473` (`recent_insights`):
  ```python
  def recent_insights(self) -> list[str]:
      return [row["summary"] for row in self._db.insights.list(limit=20)]
  ```
  Retorna `list[str]` — descarta `mechanism`, `responsibility`, `confidence`, `status`,
  `created_at`/`updated_at` (e, com E1, `hits`).
- `backend/lohra/workflow/tools.py:610` — `insights=self._service.recent_insights()`,
  consumido pelo modo `list` de `workflow_templates` (linha exata do taxonomy doc
  citava `tools.py:557-558`; no HEAD atual é `:610` — drift de ~50 linhas por commits
  intermediários, mesmo arquivo/mesmo papel).

**Experimento RED:** teste de contrato sobre a saída de `workflow_templates` (modo
list): hoje `insights` é `list[str]`; depois de E2, cada item é um dict com pelo menos
`{summary, mechanism, responsibility, confidence, status, hits}`. Regressão: o teste
falha se `insights` voltar a ser `list[str]`.

**Tamanho:** **S** — dois arquivos, mudança de shape de retorno + o teste de contrato.

**Dependências:** nenhuma dura; mais valioso DEPOIS de E1 (para incluir `hits`), mas
funciona sem ele.

---

### E3 — Reescrever a guidance do produtor de memória (alinhar à taxonomia)
**Hipótese:** a única memória viva hoje é exatamente a classe que o #54 alerta (fato
ambiental sem escopo/validade) — e foi salva CORRETAMENTE segundo a guidance vigente,
que convida explicitamente a isso. A guidance do produtor contradiz o épico.

**Evidência de código:**
- `backend/lohra/memory/tool.py:16-21` (`MEMORY_GUIDANCE`): *"Save proactively when
  the user corrects you, shares a preference or habit, or you learn a convention or
  **environment quirk**."* — o termo "environment quirk" é o convite direto a salvar
  exatamente a classe #6 da lista "nunca memorizar" da taxonomia (§3).
- A memória real citada no relatório (`rg` não instalado) é um fato ambiental,
  não verificado independentemente aqui (dado do relatório T0, não reconferido nesta
  rodada — não copiei o `state.db`).

**Experimento RED:** teste textual + anti-drift: `MEMORY_GUIDANCE` não deve conter
`"environment quirk"` nem convidar a salvar disponibilidade/preço/slug de modelo,
saldo, quota, binário local — deve nomear as classes proibidas (§3 da taxonomia) e
exigir fato estável+escopado. O teste falha se a string proibida reaparecer.
Efeito real (menos memórias erradas) só é medível ao vivo — declarar isso no PR, não
fingir que o teste prova o efeito.

**Tamanho:** **S** — só `memory/tool.py` (+ o equivalente em `skills/tool.py` para o
mesmo tipo de contaminação, se existir uma guidance análoga lá).

**Dependências:** nenhuma. É a alavanca mais barata citada pelo próprio dono na #54.

---

### E4 — Proveniência e validade no template
**Hipótese:** templates de sucesso não carregam proveniência (run de origem, quando
foram gerados, provider/model, null_rate) e são sobrescritos silenciosamente por nome
— um template "funciona" hoje sem que o consumidor saiba de quando/onde.

**Evidência de código:**
- `backend/lohra/workflow/library.py:104-112` (citado pela taxonomia como local de
  `_save_template`; sobrescreve por `{_safe_name(meta.name)}.json`).
- `backend/lohra/workflow/library.py:75-95` mostra que o `meta` JÁ carrega alguns
  contadores advisory (`artifact_divergences`, `replay_divergences`, `budget_overrun`)
  seguindo a doutrina "ausência ≠ zero, nunca default silencioso" (mesma doutrina do
  `leaf_respawns` citado pela taxonomia) — ou seja, o PADRÃO para adicionar campos de
  proveniência advisory já existe e está sendo seguido para outras métricas; falta
  aplicá-lo a `run_id`/`created_at`/`provider`/`model`/`profile`.

**Experimento RED:** um template salvo hoje deve expor `provenance: absent` (nunca
omitir o campo nem inventar valor) para templates pré-E4; um template salvo depois de
E4 expõe `run_id`, `created_at`, `provider`/`model`, `profile`, idade calculada. Teste:
listar templates e checar que nenhum campo de proveniência tem default mascarando
ausência.

**Tamanho:** **S/M** (como a própria taxonomia já marcava) — um arquivo, mas mexe no
formato de `meta` que outros consumidores (`workflow_templates` get/list) leem.

**Dependências:** nenhuma; compõe com E2 (mesma filosofia "não descartar
proveniência na entrega").

---

### E5 — Campo de invalidação na memória
**Hipótese:** os canais mais duráveis (memória, skills) são os únicos sem qualquer
política de validade — o inverso do que a wave deveria priorizar.

**Evidência de código:**
- `backend/lohra/memory/store.py` / `memory/tool.py` — o schema de entrada de memória
  hoje é texto livre `§`-delimitado, sem campos de classe/escopo/condição de
  invalidação (confirmado pela ausência de qualquer parsing de metadata estruturada
  nesses arquivos além do texto).
- Depende de E3 ter definido as classes e o vocabulário antes de dar campos
  estruturados a elas — do contrário o épico está adicionando um schema sem uma
  taxonomia estabilizada para preenchê-lo.

**Experimento RED:** uma entrada nova com condição de invalidação declarada (ex.:
"válido até `rg` ser instalado") pode ser marcada invalidada por um evento
correspondente; uma entrada migrada do formato legado aparece como
`proveniência: ausente` (nunca com uma condição inventada — guardrail "sem autoridade
retroativa" do #54). Teste de bytes idênticos no prompt congelado (Invariante #1) para
garantir que a mudança de schema em disco não vaza para o prompt vivo.

**Tamanho:** **M** — dois arquivos (`memory/store.py`, `memory/tool.py`) + migração de
dados existentes + o teste de invariante #1.

**Dependências:** **depende de E3** (vocabulário de classes precisa existir primeiro).

---

### E6 — Teste anti-drift do isolamento de escopo
**Hipótese:** "profile é o teto de escopo, nada atravessa" já é o comportamento
implementado — não é um gap a fechar, é um invariante a CONGELAR em teste antes que uma
mudança futura (ex.: E5, E8) o quebre sem querer.

**Evidência de código:**
- `backend/lohra/agent/delegate.py:49-59` (`_CHILD_EXCLUDED_TOOLS`): denylist
  confirmada — `memory`, `skill_view`, `skill_manage`, `session_search`, `cronjob`,
  `vision_analyze`, `image_gen`, `spawn_session`, `delegate_task` fora do filho.
- `backend/lohra/catalog/tool.py:5-9` reforça o mesmo padrão para `list_models`: "it is
  excluded from subagents and `lohra serve`" — a doutrina de "autoria é decisão de
  quem orquestra, não do leaf" está espalhada por múltiplos tools, não só
  memory/skill; um teste anti-drift bom deveria cobrir a FAMÍLIA de tools
  author-time-only, não só memory/skill.

**Experimento RED:** teste que falha se (a) `memory`/`skill_manage`/`skill_view`/
`list_models` saírem de `_CHILD_EXCLUDED_TOOLS`, ou (b) um novo store escrever fora de
`lohra_home()`/`memory/paths.py`.

**Tamanho:** **S** — puramente aditivo (só testes), zero risco de regressão em
produção.

**Dependências:** nenhuma. Pode rodar em paralelo com qualquer outro épico — é só teste.

---

### E7 — Experimento mínimo da #53 (pai ← filho)
**Hipótese:** a hipótese nula da própria issue ("os canais existentes já bastam") é a
mais barata de testar e ainda não foi testada com dado ao vivo — o relatório T0 foi
explícito que isso **não é decidível com dado armazenado**.

**Evidência de código:**
- Isolamento confirmado: `agent/delegate.py:49-59` (acima).
- O pai já recebe: resultado, faults, status, rollup — via `workflow_run_state`,
  `workflow_audit_events`, e o dobramento de métricas de sub-runs em
  `engine.fold_nested` (`backend/lohra/workflow/engine.py:814-862` no HEAD atual —
  soma `null_count`, `tokens_*`, `faults` prefixados com `sub[{ref}]:`, etc.). Isso é
  para o nó `workflow` (template aninhado), que tem semântica de "run filho" diferente
  de um `delegate_task` (subagente); os dois merecem inventário separado no
  experimento, porque os canais de retorno não são os mesmos objeto a objeto.

**Experimento RED:** 3–5 runs reais com falha; diff entre o que o filho observou
(logs/estado interno da leaf) e o que o pai efetivamente recebe pelos canais atuais
(fault text, status, rollup, notices). Saída esperada é uma lista de itens
PROVADAMENTE ausentes — lista vazia é desfecho legítimo ("fechar como nenhuma mudança
necessária"), explicitamente previsto na issue.

**Tamanho:** **M** — não é escrita de código de produto, é instrumentação +
observação ao vivo (custa tempo de execução real, não linhas de diff).

**Dependências:** nenhuma técnica; mais informativo se rodado DEPOIS de E1/E2 (para já
poder distinguir, nos runs observados, o que seria um candidate causal legítimo do que
seria "lição" de prosa não sustentada).

---

## 2. Épico novo (E8) — decisão do dono: rota inexistente na autoria → substituição por catálogo

### O que existe hoje

1. **O catálogo existe e já é consultável ao vivo.** `backend/lohra/catalog/catalog.py`
   (`build_catalog`, `ProviderModels`) faz fetch real por provider (Anthropic, OpenAI,
   OpenRouter, Ollama local, subscription) com timeout, cap de bytes, e nunca levanta —
   degrada a `source="error"` por provider. Exposto ao agente via o tool
   `list_models` (`backend/lohra/catalog/tool.py:1-9`), que é **author-time only**:
   "naming a model is an AUTHORING-time decision the orchestrator makes, not something
   a leaf revisits" — excluído de subagentes e de `lohra serve` do mesmo jeito que
   `memory`/`skill_*` (mesma família do E6).
2. **`model:` (o slug livre) NÃO é validado em lugar nenhum na autoria.**
   `backend/lohra/workflow/schema.py` valida `tier` contra um conjunto FECHADO
   (`_validate_tier`, linha 393-411, contra `MODEL_TIERS = ("small","medium","big")`
   em `workflow/tiers.py:31`), mas não existe `_validate_model` — busquei
   explicitamente (`grep -n "_validate_" schema.py`) e a lista de 9 validadores não
   inclui nada para `model`. `model` é um dos 4 `ROUTING_FIELDS` declarados em
   `workflow/nodes.py:56-60` (`model`, `tier`, `effort`, `provider`) e é texto livre.
   `provider` É validado (mas só o NOME do provider, contra o registro —
   `providers/resolve.py:19-24`, `_canonicalize` levanta em provider desconhecido); o
   slug do modelo dentro daquele provider, não.
3. **A rejeição de um slug inexistente hoje só aparece na hora da chamada real ao
   provider**, dentro do loop compartilhado por toda invocação de LLM (top-level e
   leaf): `backend/lohra/agent/loop.py:430-446` — `agent.client.create(**kwargs)` com
   `model=agent.model`; a exceção é classificada por
   `classify_provider_error(exc)` (`backend/lohra/providers/errors.py:117-141`), que
   hoje só distingue estruturalmente `QUOTA_EXHAUSTED` / `AUTH_FAILED` / `TIMEOUT` — **não
   existe uma 4ª categoria estrutural para "modelo não existe"** (HTTP 404, ou um
   `code`/`param` do payload nomeando o modelo). Um slug inválido cai em `None`
   ("unclassified — an ordinary failure whose leaf dies alone").
4. **O `error_kind` resultante decide o que o engine faz** em
   `backend/lohra/workflow/engine.py:1601-1643`: hoje só há ramos para
   `QUOTA_EXHAUSTED` (pausa+auto-resume), `TIMEOUT` e `AUTH_FAILED` (pausa via
   `route_fault.py`); qualquer outro `error_kind` cai no comportamento genérico
   (fault + null nesse leaf, run segue `degraded`).
5. **`route_fault.py` não é o lugar certo para a substituição automática.** Seu
   propósito declarado é justamente o OPOSTO do que o dono pediu: "Zero new
   authority... a diferente provider, a different billing route, an unknown-or-higher
   cost or a refused credential is the HUMAN's call" (`route_fault.py:34-40`). O dono
   quer que, especificamente para slug inexistente, a Lohra corrija sozinha — então
   não é uma pausa-e-pergunta-ao-humano, é uma correção automática com aviso. É uma
   classe de comportamento nova, mais parecida com o respawn opt-in de
   `leaf_retry.py` (mesma família "re-tentar a MESMA cela com uma correção") do que
   com `route_fault`, mas `leaf_retry.py` também recusa explicitamente re-rotear: *"Re-
   routing is explicitly NOT here — every attempt runs on the route the spec
   authored"* (`leaf_retry.py:37-39`). Ou seja: **nenhum mecanismo atual autoriza
   trocar o modelo no meio de um run** — os dois existentes (`leaf_retry`,
   `route_fault`) excluem essa opção por design, cada um por um motivo diferente.

### Onde a substituição deveria acontecer

Não em `run_workflow` (schema.py `validate_spec`): essa função é **pura, offline, sem
rede** por design (é chamada em todo `run_workflow`/resume, síncrona, e testada como
determinística). Fazer uma chamada de catálogo ao vivo ali misturaria disponibilidade
de rede — que é `environment`, não `agency` — dentro de uma validação de FORMA, e uma
falha transiente do provider durante a validação da spec derrubaria a autoria por um
motivo que a própria taxonomia pede para nunca confundir (§ "responsibility é decidida
por mecanismo + evidência, nunca por status").

O ponto certo é **reativo, no dispatch do leaf** — o mesmo lugar que já decide o
destino de `AUTH_FAILED`/`TIMEOUT`/`QUOTA_EXHAUSTED`
(`workflow/engine.py:1601-1643`), alimentado por uma nova categoria estrutural em
`providers/errors.py` (`MODEL_NOT_FOUND` ou nome equivalente, por HTTP status 404 ou
código de payload — nunca regex sobre prosa, mesma doutrina do módulo). Isso preserva
a garantia "determinístico dentro da mesma rota" que `AUTH_FAILED` já usa (um slug
inexistente falha do mesmo jeito em toda tentativa naquela rota, então **não** vale a
pena um respawn cego — precisa trocar o modelo antes de tentar de novo).

### Desenho mínimo seguro

1. `providers/errors.py`: nova constante estrutural (ex. `MODEL_NOT_FOUND`),
   detectada por status HTTP (404, ou 400 com `code`/`param` nomeando o modelo) —
   sem regex sobre texto livre, seguindo o próprio contrato do módulo.
2. `workflow/engine.py` (no mesmo bloco que já trata `error_kind`): ao ver
   `MODEL_NOT_FOUND`, consultar o catálogo (cache existente em `catalog/windows.py`
   ou equivalente, para não bater rede em toda leaf) para o PROVIDER daquela rota,
   escolher um modelo existente compatível com o `tier`/`effort` declarado no node
   (nunca mais caro que o pedido, mesma doutrina do texto de `route_fault.py`: "never
   onto a costlier model"), e re-tentar essa ÚNICA cela **uma vez** com o modelo
   substituído.
3. **Nunca silencioso**: (a) o fault do leaf registra
   `"model {orig!r} not found for provider {p!r}; substituted {new!r}"`; (b) uma nota
   advisory sobe ao rollup do run, no mesmo padrão de `artifact_divergences` /
   `replay_divergences` / `budget_overrun` já existente em `workflow/library.py`
   (contador nunca omitido, nunca default mascarando ausência); (c) chamada a
   `state/insights.py`'s `InsightStore.record(kind="candidate", status=
   "model_substituted", mechanism="validation", signals=(SIGNAL_SPEC_SHAPE,),
   confidence=1.0, summary=...)` — reaproveitando EXATAMENTE o padrão já usado por
   `_record_spec_candidate` (`workflow/service.py:919-937`) para spec rejeitada, sem
   precisar mexer em `failure_taxonomy.py`: `mechanism=validation` +
   `SIGNAL_SPEC_SHAPE` + `confidence>=0.8` já mapeia para `Responsibility.AGENCY`
   (`failure_taxonomy.py:143-150`), exatamente o veredito que o dono pediu.
4. Se a substituição TAMBÉM falhar (provider fora do ar, catálogo vazio para aquele
   provider, nenhum modelo compatível com o tier): cair no comportamento genérico de
   hoje (fault + null, run segue `degraded`) — nunca insistir mais de uma vez, para não
   transformar "modelo não existe" num loop de retries caro.

### Tamanho

**M/L.** É o maior épico da rodada: toca 4 subsistemas (`providers/errors.py` — nova
categoria estrutural; `workflow/engine.py` — novo ramo de dispatch; `catalog/` — uma
função de "escolher substituto" que hoje não existe, mesmo com `build_catalog` já
pronto; `state/insights.py`/`workflow/library.py` — os dois pontos de registro
advisory). Ao contrário de E1-E6, não é uma correção localizada — é um mecanismo novo
de correção automática de rota, algo que os dois mecanismos existentes (`leaf_retry`,
`route_fault`) excluem hoje por design. Recomendo tratá-lo como M por trilha, mas medir
antes de comprometer: como só 7/57 notices amostradas são desse tipo, vale prototipar
com o `list_models`/`build_catalog` já existentes antes de desenhar o "escolher
substituto por tier" definitivo.

**Dependências:** nenhuma dura sobre E1-E7 (a chamada a `insights.record` usa a
interface pública, estável independente de E1 mexer em `_fingerprint` internamente).
Soft-dependência recomendada: rodar E1 antes, para que o candidate de substituição já
nasça com fingerprint estrutural em vez de texto livre (evita duplicar o trabalho de
troca de esquema duas vezes). Rodar E7 depois é mais informativo (o experimento pai←
filho pode incluir casos reais de substituição de modelo como um dos cenários
observados).

---

## 3. Ordem recomendada — primeira rodada de épicos S (máx. 4-5 fatias paralelas)

Épicos S candidatos a paralelizar: **E1, E2, E3, E6**, e opcionalmente **E4** (S/M).

| Fatia | Arquivos tocados | Conflito com |
|---|---|---|
| E1 | `state/insights.py` (+ migração de schema) | **E8** (dependência soft, não conflito de linha — E8 só CHAMA `record()`, não edita `_fingerprint`) |
| E2 | `workflow/service.py` (`recent_insights`), `workflow/tools.py` (linha do `insights=`) | nenhum dos S; toca os mesmos dois arquivos que E4 mexe de raspão (`library.py` é separado de `service.py`/`tools.py`, sem overlap real) |
| E3 | `memory/tool.py`, `skills/tool.py` | nenhum — arquivos exclusivos desta fatia |
| E6 | testes novos apenas (`agent/delegate.py`, `catalog/tool.py`, `memory/paths.py` — só LEITURA para asserção, não edição) | nenhum — é aditivo |
| E4 | `workflow/library.py` | nenhum dos S acima; mas caso E8 avance em paralelo e também precise carimbar `meta` (não deveria — E8 escreve no rollup/insights, não no template), checar antes de abrir a worktree |

**Recomendação:** rodar **E1, E2, E3, E6 em paralelo** (4 worktrees, zero overlap real
de arquivo — E1 e E2 tocam arquivos DIFERENTES apesar de ambos mexerem no funil de
insights) na integração da Wave 9. **E4** pode entrar como 5ª fatia se houver braço
disponível — não conflita com as outras quatro. Deixar **E5** para depois (depende de
E3 fechar primeiro, sequencial). **E7** e **E8** ficam fora da primeira rodada de S:
E7 é observação ao vivo (não é um "épico de código" paralelizável do mesmo jeito), e
E8 é o único M/L — merece sua própria fatia isolada, revisão adversarial (mexe em
classificação de erro + dispatch, é doutrina de concorrência/segurança do CLAUDE.md) e
não deveria competir por atenção de review com as 4-5 fatias S no mesmo ciclo.

---

## 4. O que a #52 precisa para fechar "sem nudge se a nula se mantiver"

**O que é "a nula":** o próprio corpo da issue #52 (W9-T3) declara: *"A hipótese nula é
válida: narrar e diagnosticar a notice já é o comportamento correto, e aprendizado deve
permanecer harness-side."* — ou seja, a hipótese nula é que o overlay atual de
dead-turn notice (`build_turn_notice`, `backend/lohra/agent/notices_overlay.py:86-105`,
que é declaradamente **operacional, nunca insight**) já é suficiente, e que NENHUM
nudge adicional de "salvar isso em memória" deve ser injetado nesse momento do turno.

O relatório T0 (`docs/history/reviews/2026-09-03-wave9-t0-taxonomy.md`, §4-#52) tinha
dado essa nula como **"fortemente sustentada — recomendo fechar sem nudge"**, mas com
uma ressalva explícita: *"o veredito do #52 é, portanto, condicional à decisão de
§2.4"* — a mesma pergunta ambiente-vs-agência que a #54 acabou de decidir. Isso muda o
denominador da amostra original de **0/57** notices atribuíveis a agência para **7/57**
(12%) — as notices de slug de modelo rejeitado, que agora são `agency` por decisão do
dono.

**O que falta, concretamente, para reabrir e fechar a #52 com confiança:**

1. **Reclassificar as 57 notices amostradas com a fronteira corrigida** (não fiz essa
   reclassificação nesta rodada — não copiei/consultei o `state.db`, que é o dado bruto
   por trás da tabela do T0). Confirmar se, mesmo a 12% de agência, o argumento de
   custo/benefício do nudge genérico ainda perde (a issue já antecipa isso: *"58% nem
   são falhas"*, e a maioria do resto seria `environment`/infra ainda).
2. **Esperar o E8 aterrissar antes de fechar**, porque ele muda a própria população: se
   a substituição automática por catálogo funcionar, a maioria dessas 7 notices de
   "slug inexistente" deixa de existir como NOTICE DE FALHA — vira um advisory de
   substituição bem-sucedida, não um turno morto. O que sobra para o #52 avaliar é só o
   resíduo (substituição que TAMBÉM falhou), uma fatia provavelmente menor que 7/57.
   Fechar o #52 ANTES de E8 estaria avaliando um cenário que a própria wave está prestes
   a mudar.
3. **A issue exige uma comparação controlada de 4 variantes sob o mesmo cenário e
   orçamento** (overlay atual / indicação neutra / evidência tipada quando o gate
   permitir / nenhuma intervenção), medindo precisão, falsas lições, validade futura,
   custo em tokens/latência e recorrência — nada disso foi rodado ainda; o T0 é
   amostragem retrospectiva de texto de notice, não um experimento controlado ao vivo.
   Sem isso, "fechar sem nudge" é uma inferência razoável mas não é a "Evidência mínima
   para concluir" que a própria issue pede.

**Conclusão desta rodada:** a nula segue plausível e é a aposta mais barata, mas fechar
a #52 agora seria prematuro em dois eixos — falta reclassificar a amostra com a
fronteira nova, e falta rodar (ou decidir explicitamente dispensar) o experimento
controlado que a issue pede. Recomendo sequenciar a #52 **depois** de E8 e de uma
reclassificação rápida da amostra existente (não precisa de runs novos para isso —
é reprocessar o `state.db` já citado no T0 com a taxonomia corrigida).

---

## 5. Resumo de decisões e riscos abertos

- Nenhum código foi alterado; nenhuma issue foi editada; nenhum PR foi aberto (task
  read-only, como pedido).
- Onde a taxonomia e o código de HEAD atual (0.0.25) divergem em NÚMERO DE LINHA (não
  em substância), sinalizei explicitamente: `service.py` (candidate: 929-936 vs. citado
  751-772; `recent_insights`: 1471 vs. citado 1233), `tools.py` (`insights=`: 610 vs.
  citado 557-558), `notices_overlay.py` (`build_turn_notice`: 86-105 vs. citado
  95-105). Substância confirmada em todos os casos — é drift de commits intermediários
  (Wave 8.x), não erro na taxonomia.
- Nada inventado: onde não pude confirmar (ex.: reclassificação exata das 57 notices
  com a fronteira nova, estado do `state.db`), digo explicitamente que não foi feito
  nesta rodada, em vez de estimar um número.
