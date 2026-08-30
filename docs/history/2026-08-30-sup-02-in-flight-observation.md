# SUP-02 (#28) — Observação in-flight de workflows: contrato de leitura hierárquico

> Data: 2026-08-30 · Branch: `feat/lohra-epic-sup` · Predecessora:
> `docs/history/2026-08-29-sup-01-active-supervision.md` (SUP-01)
> Superfícies lidas: `backend/lohra/workflow/tools.py` (`workflow_status`,
> guidance), `backend/lohra/workflow/events.py` (push
> `plan/node/items/fault/done`), `backend/lohra/workflow/notify.py`
> (notificação terminal agent-facing), `backend/lohra/workflow/runstate_store.py`
> (linha durável, `durable_rollup`, `stale`), `backend/lohra/workflow/audit_query.py`
> (leitura read-only do ledger), `backend/lohra/workflow/causality.py`
> (identidade causal), `docs/specs/08-workflow-node-audit.md` (OBS-01–05).
> **Decisão fechada: a Opção C foi a escolhida** — proveniência mínima de
> observação no status + doutrina textual de leitura hierárquica. O contrato
> mínimo selecionado é registrado nesta investigação; o registro da
> implementação seguirá após os testes.

## 1. O problema

A SUP-01 fechou a **fronteira** da supervisão (quem decide o quê, quantas vezes,
com que teto) em texto. Faltava o outro lado do mesmo loop
(`watch → diagnose → adapt → resume`): **o que o orquestrador consegue de fato
observar enquanto o run está em voo, a que custo de contexto, e com que
confiança**. A doutrina manda assistir, diagnosticar e não repetir cegamente —
mas se a observação custar caro em contexto ou não distinguir "lento" de
"travado", a própria doutrina empurra para polls cegos ou para abandono.

Perguntas desta issue:

1. `workflow_status` (rollup) e `workflow_audit` (ledger durável, metadata-only)
   bastam para observação in-flight, ou falta um contrato de leitura
   intermediário?
2. Qual o custo de contexto real de observar por polling (status e cauda de
   audit), e por cadência?
3. A identidade causal (retry/cache/resume) está acessível ao observador, ou
   está ausente dele?
4. Push (eventos ao vivo) substitui polling, ou só o complementa?

## 2. Baseline

Medido antes de escrever a conclusão, sobre o baseline da branch em
`fa12740a4cf7e00a69a0a012ccd7a454d1975cb2`:

- **Superfícies existentes de leitura**: `workflow_status` (rollup compacto,
  live ou durável, com `progress` per-node) e
  `workflow_audit` (ledger SQLite metadata-only, paginado por `seq`, com
  `snapshot_seq` e disclosures de integridade run-wide). Ambas são leituras
  locais: **zero provider calls, zero tokens de workflow/provider por consulta**.
- **Push existente**: `EventEmitter` emite `plan/node/items/fault/done` a
  **um sink** — hoje o operador (terminal/dashboard). **Não é agent-facing para
  eventos intermediários**: o único canal agent-facing é a notificação terminal
  (`notify.py`), que **enfileira** uma linha quando o run para. A fila só é
  drenada por outra iteração/turn do loop: não acorda nem inicia um turno.
- **Estado durável**: `workflow_run_state` persiste status, reason, checkpoint,
  `resume_at`, attempts, `progress_json` e `audit_segment_id`; `stale`
  distingue um run `running` sem lease (processo perdido).
- **Gap de contemporaneidade**: um fault pode chegar ao sink live enquanto o
  `workflow_status` ainda não o mostra, porque o caminho live lê os faults do
  `RunResult` somente depois que o engine retorna. Status não é uma cauda de
  eventos e não deve ser tratado como tal.
- **O que nenhuma superfície tem**: deltas de mensagem, ação corrente do leaf
  ("o que ele está fazendo agora"), dados de pricing ou rota de cobrança
  detalhados. Qualquer conclusão que dependa desses campos é falsa por
  construção hoje.
- **Tokenizador**: `tiktoken o200k_base` foi usado nos benchmarks **apenas como
  estimador do payload que entraria no contexto** do supervisor. Não é
  contabilidade nem billing — os números de "tokens estimados" abaixo medem
  custo de contexto, não custo monetário.

## 3. Hipóteses

- **H1 (identidade leaf ausente — hipótese inicial)**: a identidade causal de
  um leaf (retry, cache/replay, resume) não está disponível ao observador
  in-flight; não há como correlacionar tentativas entre si nem com o cache de
  células, e a identidade precisaria ser criada.
- **H2 (custo de polling proibitivo ou trivial)**: ou o polling contínuo de
  status+audit inunda o contexto do supervisor, ou é barato o bastante para ser
  default indiscriminado. O benchmark decide.
- **H3 (push substitui polling)**: os eventos ao vivo existentes tornam o
  polling desnecessário para o agente.
- **H4 (status suficiente)**: `workflow_status` isoladamente basta para toda
  observação in-flight que a doutrina SUP-01 exige.
- **H5 (contradição estrutural)**: nenhuma combinação das superfícies atuais
  distingue um leaf **lento** de um leaf **travado** (wedged) enquanto ele não
  terminaliza — nem o status (só dá contadores), nem o audit (silêncio não
  prova ociosidade).

## 4. Condições de falsificação

- **H1**: estaria **integralmente falsificada** se a identidade causal já
  estivesse acessível ao observador com contrato bounded, sem conteúdo cru —
  inclusive correlação retry/cache/resume. A condição foi **parcialmente
  satisfeita**: o audit durável carrega identidade de execução (`run_id`,
  `segment_id`, `sub_id`, `attempt`, `turn`) e eventos de cache (`replayed`),
  mas não preserva correlação universal leaf↔cache/revisão. H1 foi, portanto,
  **parcialmente falsificada**, e o problema global foi **reformulado**: não é
  preciso criar identidade de execução; falta um contrato de leitura
  hierárquico e bounded, com a lacuna de cache explicitamente não prometida.
- **H2**: refutada em qualquer direção extrema pelas medidas do §6 — o payload
  mostrou-se proporcional à cadência (498 vs 162 tokens estimados de status;
  1861 vs 1011 de audit tail), nem inundação gratuita nem trivialidade.
- **H3**: falsificada se não existir evento intermediário agent-facing com
  identidade de leaf (`sub_id`/`attempt`) ou se a notificação terminal não
  acordar o agente. Não existe o primeiro; a segunda é apenas steer enfileirado,
  sem wake/start de turno. **Refutada com evidência mais forte.**
- **H4**: falsificada se surgir pergunta de diagnóstico in-flight que o rollup
  não responde. O caso "lento vs travado" a refuta. **Refutada para
  diagnóstico de leaf.**
- **H5**: seria refutada se qualquer superfície demonstrasse discriminar
  lentidão de travamento sem esperar terminalização. Nenhuma o faz; a
  contradição é registrada como **limitação permanente do contrato atual**, não
  como bug a consertar nesta issue. **Confirmada como limitação.**

## 5. Metodologia reproduzível dos benchmarks

Ambos os benchmarks do §6 foram executados em **Python 3.12, macOS local**, com:

- **`ScriptedClient` falso** — provider falso determinístico, sem rede;
- **leaf bloqueada por `threading.Event`** — a leaf só prossegue quando o
  observador solta o gate, o que torna o intervalo de observação controlado;
- **`SessionDB` file-backed** (arquivo em disco, não `:memory:`), com
  **`WorkflowService` real** por cima — o mesmo caminho de produção de estado;
- **consultas reais de leitura**: `service.status(run_id)` e
  `db.audit_query(run_id, after_seq=<cursor>)`, avançando o cursor `after_seq`
  a cada página (o cursor durável do ledger);
- **contagem de payload com `tiktoken o200k_base`**, como **estimador de
  contexto** do supervisor — não contabilidade, não billing.

O benchmark e a saída do **baseline pré-contrato** estão versionados em
`docs/history/evidence/sup-02-monitor-bench.py` e
`docs/history/evidence/sup-02-monitor-bench.json`. Para repetir a metodologia
(timings variam por host e o payload atual inclui os campos implementados nesta
issue; `tiktoken` é dependência opcional do experimento, não do produto):

```bash
python -m pip install 'tiktoken==0.9.0'
cd backend
python ../docs/history/evidence/sup-02-monitor-bench.py
```

A identidade retry/cache/resume do §8 foi verificada pela suíte existente:

```bash
cd backend
python -m pytest -q tests/test_workflow_audit_e2e.py --no-cov
# => 8 passed em 2.47s
```

## 6. Experimentos

### 6.1 Benchmark determinístico (file-backed)

Setup conforme §5: leaf fake bloqueada ~6 s, SQLite file-backed, leitura de
`workflow_status` + cauda de `workflow_audit` com cursor `after_seq`, cadência
fixa, tokens medidos com tiktoken o200k_base **como estimador de payload de
contexto** (não contabilidade).

| Métrica | Cadência 1 s | Cadência 3 s |
| --- | --- | --- |
| Polls no intervalo observado | 6 | 2 |
| Tokens estimados de `workflow_status` (total) | 498 | 162 |
| Tokens estimados da cauda de `workflow_audit` (total) | 1861 | 1011 |
| `workflow_status` — latência média / máx | 0,541 ms / 0,793 ms | 1,381 ms / 1,530 ms |
| `workflow_audit` — latência média / máx | 0,757 ms / 1,127 ms | 1,536 ms / 1,915 ms |
| Primeira observação acionável | ~1,002 s | ~3,013 s |
| Primeira página de audit | 4 eventos, depois **cinco páginas vazias** | 4 eventos, depois **uma página vazia** |

Ambas as consultas fizeram **zero provider calls** e, portanto, **zero tokens
de workflow/provider**; o custo model-visible é o payload estimado. Push
`node-running` chegou ~0,9 ms após o start — mas **apenas ao operador** e
**sem `sub_id`**. A notificação terminal existente é agent-facing apenas no
sentido de ser enfileirada para o parent: é terminal, não intermediária, e
**não acorda nem inicia um turno**.

Leitura honesta: **a cauda de audit incremental, mesmo vazia, continua custando
caro em JSON de policy/integrity** (a resposta carrega disclosures run-wide
mesmo sem eventos novos). O custo de contexto cresce com a cadência mesmo
quando nada mudou — exatamente o padrão que a doutrina SUP-01 quer evitar
(polls cegos).

> **Limitação**: 1 s e 3 s são **amostras numa única máquina**, não defaults
> universais. Não se recomenda nenhum default numérico de polling a partir
> deles.

Uma repetição pós-contrato, preservada em
`docs/history/evidence/sup-02-monitor-bench-post-contract.json`, mediu 702/240
tokens estimados de status e 1880/1076 de audit para 1 s/3 s. O aumento de
204/78 tokens estimados no status é o próprio bloco `observation`; ele não muda
a conclusão: custo de contexto cresce com a quantidade de leituras e não é
custo do ledger do run.

### 6.2 Experimento real em subscription (negativo, single-run)

Run único, subscription, modelo `gpt-5.6-sol`, com leaf que deveria dormir
~12 s e 4 polls nominais a cada 2 s. Resultado observado:

- `workflow_status` retornou: `running`, `complete`, `complete`, `complete` —
  ou seja, **3 dos 4 polls foram pós-terminal**.
- Envelope do parent: `input_tokens=1629`, `cache_read=31232`, `output=21`;
  spend da leaf: 1026.

**Negativo declarado**: a inferência do parent/leaf fez o sleep não estabilizar
a cadência; esta run **não isola o custo por poll** e **não serve de base
quantitativa**. O único fato que ela confirma é que **resultados de polls
entram no contexto/usage do orquestrador** — o que já era a hipótese de
partida.

Anomalia registrada sem diagnóstico causal: várias mensagens
`workflow audit append failed (OperationalError, retried)` e um shutdown
bounded sem drain nesta execução compartilhada. **Não reproduzida** no
benchmark file-backed do §6.1; registrada como anomalia não diagnosticada, no
padrão da nota de escopo da SUP-01 (§2) — não lida como causa nem como efeito
de nada aqui.

## 7. Negativos (consolidados)

1. O experimento real não isola custo por poll (§6.2) — nenhuma conclusão
   quantitativa por poll é sustentável a partir dele.
2. A cauda de audit vazia custa payloads substanciais de policy/integrity
   (§6.1) — "só olhar o audit" não é observação barata.
3. Push intermediário não alcança o agente: chega ao operador, sem `sub_id`, e
   o canal agent-facing é terminal-only.
4. Um fault live pode anteceder sua aparição em `workflow_status`; o status não
   é um feed de faults em voo.
5. Nenhuma superfície expõe deltas de mensagem, ação corrente, pricing ou rota
   de cobrança — qualquer recomendação que os presuma está fora do escopo por
   construção.
6. `OperationalError` de append de audit + shutdown sem drain em execução
   compartilhada: anomalia não reproduzida, fora do escopo, sem causalidade
   alegada.

## 8. Identidade retry / cache / resume

O estado real, medido contra o código e a suíte
`tests/test_workflow_audit_e2e.py` (8 passed em 2.47s, §5), que cobre
retry/cache/resume:

- `run_id` é **estável** no resume;
- `segment_id` é **novo** a cada stretch executado;
- `sub_id` é o **mesmo** apenas na mesma sub-sessão/turn de correção; retry
  fresco e resume criam um novo; cache hit **não tem** sub-sessão;
- `attempt` e `turn` diferem entre tentativas e turnos corretivos;
- `cell_id` estrutural é estável entre retries, **mas** o sanitizador público
  rehashes coordenadas estruturais e o evento de cache (`role=cache`) perde
  `item/stage` do pipeline.

**Conclusão reformulada (H1 parcialmente falsificada)**: a identidade de
**execução** não está ausente — o audit durável a tem. O problema de SUP-02
não era criar essa identidade, era expor a existente sob um **contrato de
leitura hierárquico e bounded**. E,
dado o rehash de coordenadas e a perda de item/stage no evento de cache, **não
se pode prometer identidade universal leaf↔cache nem identidade de revisão de
conteúdo** — essa promessa seria falsa contra o contrato de privacidade da
OBS-03 (o digest content-addressed não cruza a fronteira de persistência de
propósito).

## 9. Custos — três linhas separadas

O custo de observar in-flight tem **três componentes distintos**, que nenhum
número deste documento mistura:

1. **Tokens provider do workflow**: o que o run gasta em provider.
   Observação **não adiciona nada** a esta linha — status e audit fazem zero
   provider calls. No experimento real (§6.2), o spend da leaf foi 1026. O envelope do parent
   (input 1629, cache_read 31232, output 21) inclui o trabalho de supervisão e
   não permite isolar um contrafactual sem polls; por isso não é atribuído a
   esta linha nem usado como medida por consulta.
2. **Tokens/contexto do supervisor**: o que entra no contexto do orquestrador
   por observação. É a linha que o polling cobra — estimada com tiktoken
   (§6.1: 498/162 tokens de status; 1861/1011 de cauda de audit, por cadência).
   Esta linha é **model-visible** e aparece no usage agregado do turno do
   supervisor, mas não é atribuída separadamente ao payload nem contabilizada
   como gasto do run. O contrato declara
   `supervisor_context_tokens: not_separately_attributed` e
   `workflow_token_ledger_delta: 0` (§12).
3. **Custo/latência local**: CPU e tempo das leituras SQLite. Medido e
   pequeno (status 0,5–1,4 ms médio; audit 0,8–1,5 ms médio), mas **não é o
   gargalo** — o gargalo é a linha 2.

Nenhuma linha aqui é pricing: as superfícies não expõem preço nem rota de
cobrança, e este documento não alega que expõem.

## 10. Matriz decisão → campos

A matriz foi expandida para cobrir **todas as ações da doutrina SUP-01**
(§§6.2–6.3 lá). Colunas: **campos existentes** (o que as superfícies de fato
carregam — nada inventado), **fonte/proveniência** (superfície e origem) e
**ausências** (o que falta e que, por isso, força `unknown` ou escalada
humana). Onde a superfície não prova o fato, a coluna de ausências diz
`unknown` — não se preenche com inferência.

### 10.1 Observação (run-level)

| Decisão/ação | Campos existentes | Fonte/proveniência | Ausências → unknown/humano |
| --- | --- | --- | --- |
| O processo dono ainda parece vivo? | `status`, `stale`, `hint` | `workflow_status`; `stale` é derivado da lease, e `observation.source` distingue o caminho primário `local_registry` de `durable_store` | Indica registry/lease, não atividade do leaf; ação corrente indisponível |
| Por que pausou / quem decide? | `reason`, `resume_at`, `attempts`, `checkpoint`, `hint` | `workflow_status` (durável; `pause_fields`) | Suficiente para fronteiras SUP-01; não diz se houve progresso desde a pausa |
| Quanto gastou? | `tokens_spent_total`, `token_budget{total,spent,remaining}`, `node_costs` e, quando há pricing conhecido, `cost` | `workflow_status` | `cost` é best-effort do que já executou; não informa pricing de candidato, credential nem billing route |
| Onde está o run? | `progress{done,running,pending,total}` + lista per-node | `workflow_status` (live engine ou `progress_json` durável) | Sem ação corrente; **não distingue lento de travado** |
| Que leaf atingiu qual boundary, em que ordem? | `seq`, `event_type`, `node_id`, `sub_id`, `segment_id`, `attempt`, proveniência e outcomes sanitizados | `workflow_audit` (on demand) | Metadata-only: não contém prompt/resultado bruto nem ação corrente; custo não-trivial mesmo vazio |
| Este leaf é o mesmo de antes (retry)? | `run_id`/`segment_id`/`sub_id`/`attempt`/`turn` | `workflow_audit` | Não promete leaf↔cache universal |
| Este node completou por execução ou replay? | eventos de cache (`replayed`), sem sub_id | `workflow_audit` | Perde item/stage no evento de cache |
| O leaf está fazendo algo agora? | — | **nenhuma** | Sem deltas, sem ação corrente |
| Quanto custou em dinheiro / qual rota? | `cost` pode existir para execução observada com preço conhecido | `workflow_status` | Não revela pricing de rota candidata, credential/billing route nem garante cobertura monetária universal |

### 10.2 Contornos agent-owned (SUP-01 §6.2)

| Decisão/ação (SUP-01) | Campos existentes | Fonte/proveniência | Ausências → unknown/humano |
| --- | --- | --- | --- |
| Processo stale/orphan (`stale: true`) → resume | `status=running`, `stale`, `hint` (STALE_HINT) | `workflow_status` (durável) + lease do `RunStateStore` | Nenhuma para a decisão; o tempo restante da lease só aparece no erro `busy_error`, não como campo do status |
| Slug/model inválido → corrigir via `list_models`, **same provider + credential/billing route**, custo qualificado | Texto do fault; `provider`/`model` em `leaf.started`/`leaf.completed` (identidade de config, ≤128 chars) | `workflow_status` (`faults_total`) / `workflow_audit` (fault) + tool `list_models` (reachability + tier map) | **Status/audit NÃO têm catálogo, pricing, preauthorization, credential nem billing route.** A qualificação de custo (evidência de subscription fixed-price, ou pricing metadata/preauthorization provando custo não maior) não é observável pelas superfícies → sem ela, **humano**; rota de cobrança = **unknown** |
| Parâmetro opcional incompatível (ex. `effort`) → remover só se não solicitado e sem mudar o objetivo | O spec autorado e o pedido do usuário ficam no contexto do orquestrador; não há campo público de status para essa intenção | Contexto da conversa/spec original, não `workflow_status`/audit | Nenhuma superfície de leitura prova se o usuário **solicitou** o parâmetro nem se removê-lo muda o objetivo; sem esse contexto → **humano** |
| `max_iterations` insuficiente → **uma** elevação para `min(N+4, 128)`; N ≥ 128 → humano | N aparece no **texto do fault** ("max_iterations (N) reached") | `workflow_status` (`faults_total`) / `workflow_audit` (leaf fault) | **N não é campo estruturado** — vem do fault/contexto, e extraí-lo do texto é interpretação; o cap 128 é do harness (validação do spec), não campo do status; N ≥ 128 → **humano** |
| Quota (`quota_exhausted`) → respeitar `resume_at`, não competir com o auto-resume | `reason=quota_exhausted`, `resume_at`, `attempts`, `hint` | `workflow_status` (durável; `pause_payload_json`) | O status não expõe quanto resta do contador **compartilhado** de auto-resume nem o timer de cooldown vigente (`attempts` é da run; o cooldown vive no `autoresume.py`, não no rollup) — o agente compara `resume_at` com o relógio; `resume_at` null ou attempts esgotados → **humano** |
| Provider transitório **não-quota** → um resume após cooldown, sem auto-resume pendente | Texto do fault (classe do erro); ausência de `reason=quota_exhausted` | `workflow_status` / `workflow_audit` | **Não provado pelas superfícies se há auto-resume pendente nem quanto falta do cooldown** — o agendamento do auto-resume não é campo do rollup → **unknown**; competir com o timer é proibido, então a decisão exige esperar ou escalar |
| Checkpoint → relay do prompt; resposta **verbatim do humano** | `checkpoint{node_id, prompt, default?}` + `hint` | `workflow_status` (durável; `pause_payload_json`) | Nenhuma superfície verifica a verbatibilidade da resposta — o harness não autentica autoria humana; a garantia é contrato do orquestrador (SUP-01), não campo; resposta não fornecida por humano → **viol de fronteira** |
| `token_budget` (nunca elevar) | `token_budget{total, spent, remaining}`, `tokens_spent_total` | `workflow_status` (durável) | Qualquer aumento é **humano** por contrato (SUP-01); a superfície não distingue autoria (quem definiu o cap inicial) |

### 10.3 Escaladas humanas e freios (SUP-01 §6.2–6.3)

| Decisão/ação (SUP-01) | Campos existentes | Fonte/proveniência | Ausências → unknown/humano |
| --- | --- | --- | --- |
| Credentials / permissões / chaves | — | **nenhuma** | Nenhuma superfície as expõe (por design) → **sempre humano** |
| Provider / credential / billing route (mudar ou rota desconhecida) | `provider`/`model` no lifecycle do leaf (identidade de config) | `workflow_audit` | **Rota de cobrança = unknown** pelas superfícies; mudar provider/rota é **sempre humano** (SUP-01) |
| Escopo, objetivo ou semântica do run | O status mostra outputs/faults/progress, não o pedido humano nem uma autorização de mudança | Contexto da conversa e spec original do orquestrador | `spec_json`/`args_json` são persistência interna, não contrato público de `workflow_status`; mudar escopo/objetivo é **humano** |
| Ação irreversível | — | — | **Sempre humano** (contrato SUP-01); irreversibilidade é julgamento do orquestrador, não campo |
| Fingerprint de progresso (K=2) | `status`/`reason` + `progress{done,running,pending,total}` + estados per-node + `faults_total` normalizáveis; **`spent` fora do fingerprint** | `workflow_status` | O fingerprint é **definido em texto (SUP-01 §6.3), não em código** — dois observadores podem normalizar faults de forma ligeiramente diferente; o mínimo exigível (status/reason + progress + per-node) é objetivo |
| Limites de contorno: **1 por chave** `(run, causa, alvo)` / **3 por run**; registro pré/pós com custo incremental | Chave montável a partir de `run_id` + fault normalizado + node id | `workflow_status` + `workflow_audit` | **Não existe ledger de supervisão no harness** — a contagem de contornos e o registro pré/pós são disciplina do orquestrador (trace/log da conversa); nada mecaniza o cap por chave/run |
| Allowance **`min(6.000 tokens, 25% do budget original)`**; pre-estimate; caber no `remaining` | `token_budget{total,spent,remaining}` e custos reais agregados por node quando disponíveis | `workflow_status`; estimativa conservadora fica no trace do orquestrador | **Não há ledger do allowance nem custo anterior público por cell**; sem pre-estimate explícito → **unknown** e nenhum contorno com LLM. O gate do run é **soft** e pode overshoot |

## 11. Polling vs notification

- **Push existente**: barato e imediato (`node-running` ~0,9 ms), porém
  (a) vai ao operador, não ao agente; (b) sem `sub_id`/identidade de leaf.
- **Notificação terminal**: o callback enfileira um steer para o parent, mas
  **não acorda nem inicia um turno**; só será lido se outra iteração/turn drenar
  a fila. Também pode estar sem wiring, é omitido no cancel e pode falhar
  silenciosamente. É dica oportunista, não mecanismo de watch.
- **Polling**: toda informação relevante (status, cauda de audit) é obtível,
  mas custa contexto proporcional à cadência — mesmo quando nada mudou (§6.1).
  `workflow_status(wait=true)` é a única espera bloqueante embutida: usa timeout
  interno fixo de 600 s, não um deadline escolhido pelo caller.
- **Decisão**: **sem polling fixo cego e sem fingir wake-up**. Se o turno atual
  precisa ver a fronteira terminal, pode usar `wait=true`; fora disso, só
  reconsulta após uma espera bounded que o ambiente realmente consiga agendar.
  Sem scheduler/reentrada, declara que não há watcher ativo. Os números de 1 s/
  3 s são referência desta máquina, nunca default de produto.

## 12. Alternativas comparadas

**A — Apenas texto (doutrina, sem mudança de superfície).** Ensinar o
orquestrador a pollar com parcimônia e consultar audit só sob suspeita.
*Pró:* zero código. *Contra:* não muda o custo real de leitura (a cauda de
audit vazia segue cara) nem a contradição lento/travado; repete o limite que a
SUP-01 já confessou — texto compra disciplina, não contrato.

**B — Novo snapshot de leaf (superfície nova no harness).** Uma leitura
dedicada de "o que o leaf está fazendo agora". *Pró:* atacaria a ação corrente.
*Contra:* exige dados que o runtime não carrega (deltas/ação corrente não
existem nas superfícies); criaria mais um caminho de leitura para manter
sanitizado, sem se pagar nesta wave.

**C — Proveniência mínima + doutrina (ESCOLHIDA).** Contrato mínimo
selecionado: metadados de observação no status — **fonte**
(`local_registry | durable_store`), **`provider_calls: none`**,
**`supervisor_context_tokens: not_separately_attributed`** e
**`workflow_token_ledger_delta: 0`** — mais guidance/skill textual
ensinando o contrato de leitura hierárquico (status para fronteiras de run;
audit **apenas sob demanda** para diagnóstico de leaf; notificação terminal
como dica que não acorda turno, `wait=true` quando a fronteira precisa ser
observada no turno atual, e nenhum polling cego contínuo; sem
conteúdo cru; sem promessa de identidade leaf↔cache universal). **Sem nova
tool de leitura e sem snapshot de harness.** *Pró:* declara em contrato o que
hoje é implícito (de onde vem o status, que a leitura não chama provider, que
o custo de contexto do supervisor não é contabilizado como gasto do run);
fecha o caminho do polling cego por texto; é do tamanho da SUP-01 (texto +
campos declarativos, sem enforcement). *Contra:* não resolve a contradição
lento/travado — que é declarada como limitação permanente, não como bug desta
issue.

## 13. Classificação final

| Hipótese | Resultado |
| --- | --- |
| H1 (identidade leaf ausente) | **Parcialmente falsificada** — identidade de execução/retry/resume já existe no audit durável (`run_id`/`segment_id`/`sub_id`/`attempt`/`turn`), mas a condição original incluía correlação cache e ela não é universal. Problema global **reformulado**: leitura hierárquica e bounded, com leaf↔cache/revisão explicitamente não prometida |
| H2 (custo de polling proibitivo ou trivial) | **Reformulada**: custo proporcional à cadência; cauda de audit vazia continua cara em policy/integrity; status barato em latência, não-nulo em contexto |
| H3 (push substitui polling) | **Refutada**: push intermediário vai ao operador, sem `sub_id`; a notificação agent-facing é terminal, enfileirada e não acorda/inicia turno |
| H4 (status basta) | **Refutada para diagnóstico de leaf**: status basta para fronteiras de run (SUP-01); diagnóstico causal exige audit on demand |
| H5 (lento vs travado indistinguível) | **Confirmada como limitação do contrato**: status não distingue lento de wedged; silêncio do audit não prova ociosidade. Preservada como contradição, não consertada nesta issue |

## 14. Limitações

- 1 s/3 s são amostras em **uma** máquina; nenhum default numérico decorre
  delas.
- O experimento real (§6.2) é single-run e não isola custo por poll; só
  confirma que resultados de polls entram no contexto/usage.
- `OperationalError` de append e shutdown sem drain: anomalia não reproduzida,
  sem causalidade diagnosticada.
- Sem deltas de mensagem nem ação corrente em qualquer superfície; sem
  pricing/rota de cobrança — nada deste documento pode presumi-los.
- A contradição central permanece: **o status não distingue um leaf lento de um
  wedged, e o silêncio do audit não prova que o leaf está ocioso.** Toda a
  doutrina opera *dentro* dessa incerteza, não apesar dela.
- A §10 mapeia a doutrina SUP-01 sobre as superfícies **existentes**; onde a
  coluna de ausências diz `unknown` ou **humano**, é porque a superfície não
  prova o fato — não porque o fato seja falso.

## 15. Conclusão global — **REFORMULADA**

A hipótese inicial de SUP-02 — **identidade leaf ausente** — foi
**parcialmente falsificada pelo que já existia**: o audit durável carrega
identidade causal de execuções (`run_id`, `segment_id`, `sub_id`, `attempt` e
`turn`), mas não correlação universal leaf↔cache/revisão. A conclusão global é
**reformulada**, não uma falsificação total. `workflow_status` já basta para
as fronteiras de run que a SUP-01 contratou.
O problema real é **de contrato de leitura, em camadas**: hoje o observador
paga contexto proporcional à sua impaciência (a cauda de audit vazia custa
caro em policy/integrity), não sabe de onde vem o status que lê, e nenhum texto
o impede de pollar cego. A expansão da §10 mostra o mesmo padrão linha a linha:
**o status sustenta a triagem run-level; o audit localiza boundaries de leaves
sob demanda; e fatos decisivos (custo de rota candidata, intenção, cooldown,
verbatibilidade e ledger de contornos) continuam `unknown` e, quando exigidos
pela SUP-01, escalam ao humano.**

**Desfecho: Opção C selecionada nesta investigação.** Contrato mínimo
selecionado: metadados de observação no status (fonte
`local_registry | durable_store`, `provider_calls: none`,
`supervisor_context_tokens: not_separately_attributed`,
`workflow_token_ledger_delta: 0`) + doutrina textual — status para
run, audit só sob suspeita, notificação terminal apenas oportunista,
`wait=true` para bloquear o turno atual (timeout interno fixo) e nenhuma
promessa de wake-up ou default numérico de polling; sem nova tool, sem snapshot,
sem conteúdo cru, sem
alegação de pricing/rota. A implementação e a validação ficam registradas
abaixo.

A Opção C preserva honestamente as duas coisas que nenhuma superfície atual
responde e que este documento se recusa a fingir responder: **se um leaf lento
está travado, e se um audit silencioso está ocioso.** Toda a doutrina opera
*dentro* dessa incerteza.


## 16. Implementação e validação

Contrato permanente implementado:

1. todo status bem-sucedido recebe `observation.source` (`local_registry` ou
   `durable_store`), `provider_calls: none`,
   `supervisor_context_tokens: not_separately_attributed` e
   `workflow_token_ledger_delta: 0`; erro de run inexistente não finge
   proveniência;
2. guidance, descrições de tools e skill ensinam status primeiro, audit sob
   demanda, paginação, notificação sem wake-up, espera bloqueante com timeout
   interno e as limitações de identidade;
3. testes anti-drift cobrem os dois caminhos de status, cópia não compartilhada,
   erro, custo/proveniência, guidance e skill (inclusive teto de 800 linhas).

Varredura dos consumidores de `run_id` herdada da #24: `workflow_status` apenas
serializa aditivamente o novo bloco; `cancel` opera pelo id e não interpreta o
rollup; resume reidrata `DurableRun`, não o payload de status; `watch` lê o
run-state durável diretamente; `list` usa sua projeção própria. Nenhum desses
quatro contratos foi alterado.

Validação final em 2026-08-30:

- `tests/test_workflow_supervision_reading.py`: **24 passed**;
- todas as suites `tests/test_workflow_*.py`: **855 passed**;
- `env -u OPENROUTER_API_KEY python -m pytest -q`: **2179 passed**, cobertura
  total **95%**;
- `ruff check .`: **limpo**;
- `git diff --check`: **limpo**.

Resultado negativo preservado: a primeira execução da suite completa herdou
`OPENROUTER_API_KEY` do ambiente de investigação e 13 testes herméticos de
onboarding/provider usaram OpenRouter de verdade; um teste posterior de thread
de audit também encontrou o writer deixado por essa execução contaminada. A
mesma suite, com **somente essa credencial externa removida**, passou inteira.
Isso não foi mascarado como falha de produto nem motivou mudança de código.
