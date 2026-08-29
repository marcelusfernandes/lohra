# Auditoria dos nodes do DAG — contrato de evidência

Status: **OBS-01 a OBS-05 concluídas na branch do épico**
Milestone: Wave 4 — Auditoria e observabilidade dos nodes do DAG  
Issue fundadora: #19

Este documento define o que a Lohra pode legitimamente chamar de auditoria de
workflow. As seções registram, em ordem, as escolhas de correlação,
armazenamento e consulta que sobreviveram à campanha adversarial de OBS-05.

A conclusão de OBS-01 é:

> A auditoria deve ser uma trilha durável de eventos observáveis, minimizados e
> rotulados por proveniência. Estado operacional é uma projeção dessa trilha;
> payloads crus são evidência separada e protegida. Reasoning privado e estado
> opaco de replay do provider ficam fora do contrato. Uma justificativa
> explicitamente produzida pelo agente pode ser exibida apenas como auto-relato
> opcional e não verificado.

## 1. Método e honestidade da investigação

### 1.1 Baseline revalidado

A investigação leu os caminhos de provider, agent loop, gateway, orchestration,
workflow e persistência no estado da branch `feat/lohra-epic-obs`.

O baseline confirmado é fragmentado:

- o gateway emite lifecycle de mensagem, deltas, tool start/complete, erro e
  fork (`backend/lohra/gateway/session.py:83-99, 122-131, 157-163, 187-198`);
- o workflow expõe `plan`, `node`, `items`, `fault` e `done`, além de progresso,
  faults, custos e resultados (`backend/lohra/workflow/events.py:49-56, 139-171`,
  `backend/lohra/workflow/progress.py:83-103`,
  `backend/lohra/workflow/rollup.py:70-120` e
  `backend/lohra/workflow/service.py:621-664`);
- a orchestration guarda os frames da sub-sessão apenas em
  `_SubSession.events`, uma lista process-local não limitada por bytes ou
  eventos, omitida por `collect()` (`backend/lohra/orchestration/core.py:72-79, 225-250, 288-303, 318`);
- `workflow_run_state.progress_json` persiste a fotografia mais recente, não a
  sequência histórica que a produziu (`backend/lohra/workflow/runstate_store.py:90-108, 176-222`);
- sessões bem-sucedidas persistem mensagens e custos, mas turnos com erro ou
  interrupção persistem custo e descartam as mensagens daquele turno
  (`backend/lohra/gateway/session.py:134-171`).

Isso já oferece observabilidade útil, porém não uma trilha causal completa.

### 1.2 Experimentos executados

A investigação tentou reconstruir as respostas a “o que ocorreu?” usando apenas
os artefatos atuais nos seguintes caminhos:

1. lifecycle de mensagem e tools no gateway;
2. sub-sessão e `collect_session` na orchestration;
3. node progress, faults, custos, cache e resume no workflow;
4. persistência e leitura cross-process;
5. provider reasoning, usage e replay state;
6. exposição de prompts, arquivos, tool args/results, web e MCP.

O resultado foi comparado entre cinco inventários independentes e uma síntese.
Um `verify` adversarial posterior produziu dois vereditos e um timeout: um
veredito sustentou a separação de proveniência; outro apontou corretamente que
uma trilha minimizada não responde, sozinha, “por que o modelo pensou isso?”. O
contrato incorpora essa objeção: ele responde causa operacional e evidência
observável, não causalidade mental privada. O run terminou `degraded` por esse
timeout, portanto não é apresentado como consenso de três revisores.

Run de investigação: `d359d7843794446bac92e2370d9551c8`.

## 2. Perguntas do produto

### 2.1 A auditoria deve responder

- Qual run, node e unidade de fan-out estavam envolvidos?
- Qual ação observável foi tentada: chamada de modelo, tool, cache/replay,
  retry, compaction, fork, persistência, cancelamento ou transição terminal?
- Quem declarou, executou e reportou cada parte da ação?
- Quando a ação começou e terminou, e qual é sua ordem causal?
- Qual foi o resultado: sucesso, falha, interrupção, timeout, rejeição,
  parcial, replay ou desconhecido por lacuna?
- Qual tool, provider, modelo, transport e política estavam em vigor?
- Quais métricas foram reportadas, derivadas, estimadas, não suportadas ou
  indisponíveis?
- Qual evidência observável sustenta a saída, ou por que essa evidência foi
  redigida, truncada, descartada, expirada ou nunca esteve disponível?
- Por que uma operação **falhou operacionalmente**, quando a causa é
  observável: erro estruturado, policy denial, timeout, quota, cancelamento,
  persistência ou lacuna da própria auditoria?
- Que justificativa explícita o agente ofereceu, se uma foi solicitada, sem
  apresentá-la como prova de causalidade interna?

### 2.2 A auditoria não deve afirmar responder

- Qual chain-of-thought ou pensamento token a token levou o modelo à ação.
- O que estado opaco, assinado, encrypted ou redacted do provider contém.
- Se uma justificativa do agente é verdadeira, completa ou corresponde
  fielmente ao processo interno que a gerou.
- Se ausência de telemetria significa valor zero.
- O conteúdo cru de prompts, secrets, arquivos, argumentos, resultados ou
  respostas por default.
- “Por que o modelo pensou isso?” como causalidade mental. O produto pode
  apresentar eventos causais observados e um auto-relato explícito, nunca
  fundi-los nessa resposta.

## 3. Taxonomia de proveniência e disponibilidade

Todo campo apresentado ao operador ou agente deve declarar uma destas classes.
Elas são ortogonais ao nível de sensibilidade. **Estas classes são requisitos
do contrato futuro, não uma descrição de enums ou schemas já implementados no
runtime atual.**

| Classe | Significado | Regra |
| --- | --- | --- |
| `observed` | O runtime observou a ação ou transição diretamente. | Nomear componente observador e instante. |
| `provider_reported` | O provider reportou status, usage ou identificador. | Preservar provider/transport; não inventar paridade. |
| `tool_reported` | A tool retornou um resultado ou erro. | Não tratar conteúdo como verdadeiro só por ter sido retornado. |
| `agent_declared` | Texto produzido deliberadamente pelo agente, inclusive justificativa. | Rotular como auto-relato não verificado. |
| `operator_declared` | Decisão ou input explícito do operador. | Preservar autoria sem chamá-la de observação do runtime. |
| `derived` | O runtime calculou duração, agregado, estado ou relação a partir de fatos. | Informar regra/versão e fatos de origem. |
| `inferred` | Uma interpretação não garantida pelos fatos disponíveis. | Não usar como fato auditável; tornar a incerteza explícita. |
| `redacted` | O dado existiu, mas foi removido por política. | Informar política/versão e, quando seguro, classe/tamanho. |
| `truncated` | Só parte limitada foi preservada. | Informar limite, tamanho conhecido e lado removido. |
| `dropped` | O evento/payload foi descartado por limite ou falha. | Emitir marcador de lacuna; nunca desaparecer silenciosamente. |
| `unavailable` | O dado nunca foi oferecido ou não pôde ser obtido. | Diferenciar de `redacted`, `dropped` e valor zero. |
| `excluded_private_state` | Reasoning privado ou replay state proibido no audit log. | Registrar no máximo presença/tipo/tamanho, se necessário; nunca conteúdo ou digest. |

`observed` não significa “verdade sobre o mundo”: significa somente que o
runtime observou aquela ação ou resposta. Um resultado MCP observado continua
sendo conteúdo não confiável do MCP.

## 4. Diferenças de provider confirmadas

A forma canônica atual não apaga as diferenças de origem:

| Caminho | Reasoning/replay | Streaming | Usage relevante |
| --- | --- | --- | --- |
| Anthropic Messages | thinking plaintext pode virar `reasoning`; blocos thinking/redacted e signatures podem permanecer em `provider_data` para replay. | Há callback de thinking. | Input/output e cache read/write; não há equivalência garantida de reasoning tokens. |
| OpenAI Responses/Codex | summary legível e `encrypted_content` são coisas distintas; ambos podem ser preservados para continuidade. | O client reconstrói output items; o callback de reasoning aceito não é encaminhado no caminho inspecionado. | Input/output, cache read e reasoning quando reportados; `store=false` não significa ausência de retenção local. |
| Chat Completions compatível | `reasoning_content` pode existir; replay e retenção variam. | Deltas de reasoning podem ir ao callback e não aparecer na resposta final montada. | Campos de cache/reasoning são provider-dependentes e stream usage pode faltar. |

Consequências contratuais:

- reasoning e `provider_data` não são fonte de auditoria;
- callback visibility não define completude da auditoria;
- cada meter precisa de origem e estado `reported`, `derived`, `estimated`,
  `unsupported` ou `unavailable`;
- `unsupported` e `unavailable` nunca são serializados semanticamente como
  zero observado.

## 5. Threat model e limites

### 5.1 Evidência de risco atual

- `tool.start` e `tool.complete` expõem args/result crus no gateway;
- prompts, system prompt, tool calls/results, reasoning e provider replay data
  podem ser persistidos no SessionDB;
- specs, args, checkpoints e node outputs podem ser persistidos nas tabelas de
  workflow;
- FTS indexa conteúdo e pode retornar snippets;
- `read_file`, web e MCP podem inserir dados privados ou hostis no contexto;
- `_sanitize_text` trata surrogates Unicode, não secrets;
- summaries de compaction/delegação são transformações do modelo, não
  redaction ou declassificação determinística;
- um turno falho pode emitir deltas/tools e depois não deixar transcript
  durável. A issue #25 registra a consequência de aprendizado dessa lacuna.

### 5.2 Regras de privacidade

O evento de auditoria default deve ser metadata-first e bounded. Ele não deve
copiar:

- prompt ou resposta completos;
- conteúdo de arquivo;
- tool args/results crus;
- URLs com query/fragment ou comandos completos sem política específica;
- reasoning, thinking, reasoning summaries usados como reasoning,
  `reasoning_content`, signatures, `encrypted_content` ou provider replay data;
- exception prose sem sanitização e limite.

Quando conteúdo permitido for necessário como evidência, o evento deve
referenciar um artefato separado. Reasoning privado e replay state continuam
proibidos mesmo nesse store: separação física não os transforma em evidência.
A referência não pode ser uma bearer capability e deve continuar inteligível
quando o artefato expirar: tipo, tamanho, sensibilidade, proveniência, política,
estado de retenção e motivo da indisponibilidade permanecem no evento.

OBS-01 não afirma que a Lohra já possui autorização por tenant, encryption at
rest, retenção ou deletion adequadas. A revisão completa desses controles ficou
**inconclusiva** e é requisito de OBS-03/04, não fato atual.

### 5.3 Volume e backpressure

- Deltas de texto não pertencem, individualmente, ao log default.
- Start/outcome de ações semanticamente relevantes pertencem.
- Truncation, sampling, queue overflow, sink failure e rate limiting precisam
  ser eventos/lacunas observáveis, não drops invisíveis.
- Um sink lento não pode bloquear indefinidamente o worker nem alterar a
  semântica do workflow.
- Limites numéricos de bytes, eventos e retenção serão medidos e escolhidos em
  OBS-03; inventá-los aqui seria transformar hipótese em contrato prematuro.

## 6. Alternativas refutadas e preservadas

### A. Apenas status operacional

**Benefício:** pequeno, barato e já parcialmente implementado.  
**Resultado:** rejeitado como auditoria; preservado como projeção operacional.
Não reconstrói tentativas, cache/replay, tools, falhas de persistência ou ordem
causal e `collect()` mantém apenas o output mais recente.

### B. Persistir todos os eventos crus

**Benefício:** alta fidelidade local quando a captura funciona.  
**Resultado:** refutado como default. Os frames atuais são inconsistentes,
process-local, não versionados e contêm argumentos, resultados e erros
sensíveis. Persisti-los aumentaria exposição sem provar completude.

### C. Eventos minimizados e rotulados por proveniência

**Benefício:** responde ator/ação/tempo/outcome/linhagem sem exigir payload cru.  
**Resultado:** hipótese adotada, com condições. Precisa de identidade causal,
ordering, limites, redaction, indicadores de lacuna e leitura autorizada; essas
condições ainda serão testadas em OBS-02–05.

### D. C mais justificativa explícita

**Benefício:** ajuda o humano em decisões que pedem explicação.  
**Resultado:** permitida somente como artefato opcional `agent_declared`,
bounded e sanitizado. Como o runtime atual não possui o sanitizador
determinístico necessário, a implementação deve omiti-la até que esse gate
exista e seja testado. Não substitui telemetria, não é declassificação e não
prova o reasoning real.

## 7. Hipóteses e classificação

| Hipótese | Falsificador usado | Resultado | Classificação |
| --- | --- | --- | --- |
| A observabilidade atual já forma uma trilha unificada. | Reconstruir ações, ordem e falhas só com artefatos duráveis atuais. | Snapshots, transcripts e frames têm retenção e identidade incompatíveis; falhas deixam lacunas. | **Refutada** |
| Não existe observabilidade útil hoje. | Encontrar contratos/testes de lifecycle, tools, progress, faults e custos. | Esses caminhos existem e são úteis como status. | **Refutada** |
| Auditoria útil não precisa de chain-of-thought. | Encontrar pergunta operacional obrigatória respondível apenas por reasoning privado. | Ações, tools, outcomes, políticas e evidência são observáveis sem reasoning. “Por que pensou?” foi declarado fora do contrato. | **Confirmada** |
| Justificativa explícita pode substituir reasoning. | Provar fidelidade causal, segurança e completude do texto produzido. | Não há essa prova; summaries podem omitir ou repetir secrets. | **Reformulada**: auto-relato opcional e não verificado |
| O contrato precisa distinguir observado, declarado, inferido, redigido e indisponível. | Demonstrar que uma origem/estado único representa provider reports, derivação, drops e private state sem ambiguidade. | Foram necessárias classes adicionais, como reported, derived, truncated, dropped e excluded. | **Confirmada e ampliada** |
| Providers oferecem telemetria equivalente após normalização. | Comparar callbacks, replay state e meters dos três transports. | Semântica e disponibilidade diferem materialmente. | **Refutada** |
| Raw event log é o default mais fiel e seguro. | Verificar schema, completude, boundedness e conteúdo sensível dos frames. | Não é bounded nem seguro e continua incompleto. | **Refutada** |
| Os controles atuais de autorização, encryption e retenção são adequados. | Revisão end-to-end de deployment, permissões, keys, backup e deletion. | A investigação não cobriu evidência suficiente. | **Inconclusiva** |
| Secrets são deterministicamente redigidos no ingest ou read. | Inspecionar sanitização, gravação, history e FTS. | Dados são persistidos/retornados crus em múltiplos caminhos. | **Refutada** |

## 8. Contrato conceitual para as próximas issues

Uma implementação só poderá se chamar auditoria de node se:

1. usar vocabulário fechado e versionado;
2. correlacionar run, node, unidade de fan-out, stage, attempt, turn e
   sub-session sem inferência pós-hoc ambígua;
3. representar ação e outcome, inclusive `partial`, `unknown` e `audit_gap`;
4. preservar ordem causal e declarar o limite de qualquer ordenação global;
5. diferenciar execução nova, cache lookup, cache hit e replay;
6. rotular proveniência de cada afirmação e meter;
7. ser bounded em bytes, eventos, retenção e custo de consulta;
8. representar redaction, truncation, drop, expiry e indisponibilidade;
9. sobreviver ao boundary de processo definido pelo produto;
10. permitir consulta read-only sem criar client ou chamar provider;
11. excluir private reasoning e replay state por construção;
12. não copiar payloads crus para o evento default;
13. tornar falha do próprio caminho de auditoria visível;
14. não mudar resultado, liveness ou custo contabilizado do workflow por causa
    de observação lenta;
15. manter justificativa explícita separada da evidência observada.

Este é um contrato de propriedades, não a aprovação antecipada de uma tabela,
um callback ou uma API. OBS-02 deve tentar refutá-lo com os casos causais;
OBS-03, com privacidade, volume e crash; OBS-04, com consultas reais; OBS-05,
com cenários adversariais end-to-end.

## 9. Questões deixadas deliberadamente abertas

- O contexto causal deve viajar no spawn, viver em registry lateral ou ser
  derivado por callbacks?
- Qual ordenação é garantida entre threads/processos e qual é apenas causal?
- Append-only SQLite, ring buffer, snapshot enriquecido ou combinação?
- Quais descritores são seguros por tool?
- Quais limites e políticas de retenção são sustentados por benchmark?
- Audit sink failure deve falhar a execução ou produzir uma lacuna durável?
- Qual superfície separa metadata de artefatos protegidos?
- Como representar legado anterior ao contrato?
- Como #25 consumirá falhas observáveis sem transformar bug de infra em
  “aprendizado” do agente?

## 10. OBS-02 — correlação causal das subexecuções

### 10.1 Hipótese e discriminadores

A hipótese inicial da issue #20 era que a atividade de subagentes poderia ser
correlacionada ao node de origem por uma entre três famílias: contexto explícito
no spawn, registry lateral ou derivação posterior. Ela foi testada contra
fixtures herméticas, sem provider externo, cobrindo:

- dois runs concorrentes com o mesmo spec/node e términos fora de ordem;
- pipeline concorrente com item e stage;
- retry por novo spawn e correção de schema por `steer()` na mesma sub-sessão;
- workflow aninhado;
- cache hit e nova execução após mudança content-addressed;
- callback assíncrono do pipeline, inclusive antes de um registry lateral ser
  populado;
- matriz de roles e coordenadas de todos os node types que criam leaves.

O falsificador principal foi: dadas somente as informações disponíveis à
alternativa, reconstruir sem heurística a tupla `(run, segmento, node path,
cell, fan-out, item, stage, attempt, sub-session, turn)` quando nomes de node e
ordem temporal colidem.

### 10.2 Resultado das alternativas

| Alternativa | Evidência | Resultado |
| --- | --- | --- |
| Contexto explícito no spawn | A identidade é congelada antes de a task entrar no pool; callbacks fora de ordem carregam o mesmo valor; retries e nesting ganham coordenadas no ponto que conhece sua semântica. | **Vencedora**, com o core tratando o valor como opaco. |
| Registry lateral `sub_id -> node` | O baseline `_leaf_node` servia ao custo, mas perdia item, stage, attempt, segmento e nesting; popular o registry depois de `spawn()` introduziria janela callback-before-registration. Duplicá-lo em cada estratégia repetiria o contexto explícito com mais estado mutável e cleanup. | **Refutada** como fonte de verdade; maps derivados podem existir como índice/projeção. |
| Derivação por callback/ordem | `on_done(sub_id)` não traz item/stage/attempt; término fora de ordem invalida posição temporal; cache hit não produz callback; dois runs podem ter o mesmo node/cell. Closures do pipeline conhecem parte da identidade, mas não formam um contrato uniforme. | **Refutada** para causalidade auditável; preservada apenas para métricas derivadas rotuladas. |

A hipótese foi, portanto, **confirmada e estreitada**: contexto explícito elimina
a ambiguidade somente se for criado pela camada de workflow e transportado
opacamente pela orchestration. Colocar o schema de workflow dentro do core seria
um acoplamento desnecessário e foi rejeitado.

### 10.3 Contrato implementado

`CausalContext` é imutável e contém:

- `run_id`, estável entre resumes;
- `segment_id`, novo em cada stretch executado;
- `node_path`, que namespaceia nodes de workflows aninhados;
- `cell_id`, identidade causal efêmera no core; na fronteira durável ela é substituída por um pseudônimo derivado somente das coordenadas estruturais (run/node/item/stage/branch/role), nunca do hash content-addressed do cache;
- `role`, distinguindo agent, branch, skeptic, judge, round, gate etc.;
- `item_index`, `stage_index` e `branch_path`, quando aplicáveis;
- `attempt`, incrementado tanto no novo spawn de retry quanto no turno corretivo
  da mesma sub-sessão;
- `turn`, zero no spawn e incrementado no `steer()` corretivo da mesma sub-sessão.

O `OrchestrationCore` aceita `causal_context` em `spawn()` e `steer()` sem
importar `lohra.workflow`, preserva o valor atual e uma janela dos 64 contextos
mais recentes, e devolve essa metadata em `collect()`. Entradas mais antigas são
descartadas com contador explícito `causal_history_dropped`; esse histórico é
diagnóstico process-local, não o ledger canônico. O `sub_id` continua sendo gerado pelo core e,
junto dos `message.start` ordenados da sub-sessão, completa a coordenada de
sub-session/turn. Um steer injetado enquanto um turno já está busy pertence ao
turno em curso; a correção de schema usada pelo workflow acontece após
`collect()`, portanto abre um novo turno e recebe novo `attempt` explicitamente.

O serviço injeta o `run_id` durável no engine. Engines aninhados compartilham
run/segment e acrescentam o node `workflow` ao `node_path`. Todas as estratégias
que criam leaves rotulam sua função no ponto do spawn; o fallback genérico
existe apenas para consumidores internos que não forneçam coordenadas mais
ricas.

### 10.4 Cache, replay e ordering

Cache lookup/hit/replay não é execução de leaf. Um hit preserva a mesma
correlação estrutural auditável, mas o digest content-addressed usado internamente pelo
cache não cruza a fronteira de persistência; não cria sub-sessão e não fabrica `sub_id`, `turn` ou `attempt`; OBS-03
deverá persistir os eventos de cache com a identidade da cell e outcome
`replay`. Se uma célula mudou ou não completou e precisa rodar após resume, ela
mantém `run_id`, recebe novo `segment_id` e um novo `sub_id` real.

Esta decisão não promete ordem global por relógio. Ela preserva relações
causais locais: spawn precede eventos da sub-sessão; turnos da mesma sub-sessão
são ordenados; parent node/path precede sua leaf; replay referencia a cell sem
simular execução. OBS-03 deverá adicionar sequência durável por run/sink e
marcadores de lacuna, sem reinterpretar timestamps como causalidade total.

### 10.5 Evidência executável e limites

Os discriminadores vivem em
`backend/tests/test_workflow_causality.py`. Eles demonstram concorrência,
callback fora de ordem e antes de registry lateral, retry fresh, duas correções
sucessivas por `steer`, nesting, cache/replay, fallback de cell e a matriz de roles
dos node types. A suíte completa permaneceu verde.

OBS-02 deliberadamente **não** implementa schema de evento, sink, retenção,
redaction ou query. `causal_history` é process-local, limitado a 64 entradas e
não é chamado de auditoria durável; ele prova o transporte e permite que OBS-03
capture cada turno sem derivação ambígua, enquanto o contador de descarte torna
a janela honesta. Cancelamento/timeout não requer coordenada nova: é
outcome da tentativa em curso e será modelado no vocabulário de eventos da
próxima issue.


## 11. OBS-03 — persistência, minimização e retenção

### 11.1 Diagnóstico revalidado e alternativas

A hipótese de #21 foi **confirmada e reformulada**. A lista process-local de
frames realmente era ilimitada, sem consumidor, continha deltas e payloads crus
e não sobrevivia ao processo; ela foi removida. Porém, "persistir os frames" foi
refutado como remédio: aumentaria simultaneamente volume e exposição sem criar
um schema estável. A solução validada é híbrida: ledger SQLite append-only e
limitado como evidência canônica, `progress_json` como projeção operacional e
uma fila process-local limitada apenas para desacoplar o produtor.

O teste hermético
`backend/tests/test_workflow_audit_resilience.py::test_snapshot_ring_and_append_strategies_have_distinct_evidence`
executa as três estratégias sobre cinco ações reais no `SessionDB`:

| Estratégia | Resultado observado | Decisão |
| --- | --- | --- |
| Snapshot em `workflow_run_state` | preserva somente `last_action=4`; tentativas e ordem anterior são irrecuperáveis | **Refutada** como auditoria; mantida como projeção barata |
| Ring process-local de três posições | preserva somente `[2, 3, 4]` e some no restart | **Refutada** como trilha canônica; fila/ring serve só para transporte quente |
| Eventos SQLite transacionais | reabre com os turns `[0, 1, 2, 3, 4]` em sequência | **Confirmada**, desde que limitada, minimizada e com gaps explícitos |

JSONL separado foi preservado como candidato futuro para artefatos protegidos,
não para o ledger default: ele introduziria divergência transacional entre dois
stores e pioraria consulta cross-run. Reconstrução a partir de transcript/cache
foi refutada porque esses artefatos omitem tentativa, drop, lifecycle e falhas
que nunca chegaram ao estado terminal.

### 11.2 Schema persistido e classes de dados

`workflow_audit_events` contém uma sequência `seq` por run, identidade causal
OBS-02, tipo fechado pelo produtor, proveniência, JSON sanitizado e instante de
ingestão. `run_id`, segmento, node, sub-session, tipo e tempo são colunas de
consulta; há índices SQL para run/seq, node e sub-session. Cell, role,
item/stage/branch, attempt e turn permanecem no payload JSON sanitizado —
consultáveis após leitura, mas deliberadamente não anunciados como indexados. A sequência é alocada na mesma transação SQLite que o
append e a poda; timestamp não participa da ordem causal. Um contador durável de
touch ordena retenção entre runs mesmo se o relógio civil regredir.

Persistido por default:

- fronteiras `segment.started`/`segment.completed`;
- lifecycle minimizado de node e leaf;
- presença e tamanho limitado do id da tool; nome literal somente quando pertence
  ao vocabulário fechado de tools builtin e foi realmente oferecido ao agent
  naquele turno; nomes dinâmicos/MCP ou model-generated viram metadata de
  presença/tamanho ou `unknown_tool`, sem copiar o valor;
- cache miss/store/replay/unavailable, sem fingir sub-sessão em replay;
- faults e erros somente como classe/estado e tamanho, sem exception prose;
- identidade causal e markers de gap/unavailable/truncation.

A fronteira do sink usa allow-list de campos de metadata, não apenas deny-list de
nomes sensíveis; campos desconhecidos viram `excluded_by_policy`. O append SQLite
reaplica a mesma sanitização independentemente do produtor. Campos semânticos
(`event_type`, `provenance`, status, reason, source e state) usam vocabulários
fechados ou um marker canônico; ids e nomes de tool fora do vocabulário builtin
viram apenas metadata de presença e tamanho. Markers de
redaction são revalidados campo a campo, portanto um payload não consegue forjar
`state=observed` para contrabandear uma chave arbitrária.

Explicitamente **redigido**: tool args/results. **Excluído por política**:
prompt, resposta, deltas, conteúdo de arquivo, comando, URL completa, reasoning,
thinking, summaries usados como reasoning, signatures, `provider_data` e
`encrypted_content`. Não há hash desses valores: um digest ainda permitiria
confirmação por dicionário e não provaria conteúdo. Para strings é registrado no
máximo número de caracteres; para bytes, bytes; para coleções, apenas cardinalidade
do nível superior. Objetos opacos não são serializados nem atravessados e ficam
`unavailable`.

Não existe sampling no ledger v1. Evento acima do limite é substituído por um
marker `audit.truncated` reserializado e medido novamente; até sob Unicode
adversarial ele respeita o teto estrito em bytes. Conserva `run_id` quando cabe
e usa `$unavailable` no fallback ASCII mínimo. Overflow da fila e falha
recuperada do sink viram `audit.gap` com contagem. Após `SIGKILL`, a cauda ainda na RAM é por definição incognoscível; o
resume registra `process_crash`, `dropped_count=null` e
`count_state=unavailable`, em vez de inventar zero. `process_crash` é
**reservado ao processo que realmente morreu** — uma linha `running` cuja lease
ninguém segura. Um `audit_segment_id` que ficou aberto prova apenas que o append
terminal não assentou, o que também acontece num processo VIVO cujo sink falhou
(SQLITE_BUSY no timeout de 50ms da conexão de auditoria, overflow de fila);
nesse caso a causa não é observável e o gap sai como `unavailable`. O campo
`recovered_process` do `segment.started` reporta só a liveness do processo. Retenção por tempo/eventos e
eviction de run produzem, respectivamente, gap com fronteira ou tombstone
`audit.unavailable`. A ORDEM de eviction conhece liveness: uma run `running`/`paused` em `workflow_run_state` — tipicamente uma pausada em `checkpoint`, que espera um humano ENTRE processos e não emite eventos enquanto isso — é evitada antes das runs terminadas, e a run que está apendando nunca se auto-despeja. O cap continua **duro**: liveness reordena quem sai primeiro, nunca isenta ninguém. Se um run evicto reaparece por resume, o tombstone restaura
o próximo `seq` e materializa um gap de prefixo: a história anterior não é
silenciosamente renumerada como uma trilha nova. Como tombstones também são
bounded, a compactação registra um marcador global (`$compacted`) — mas esse
marcador **não** é atribuível a nenhum run id, e por isso nunca fabrica um gap.
**Residual aceito (nomeado):** depois do horizonte de compactação, um run evicto
cujo tombstone já foi compactado reaparece por resume lendo como uma trilha
nova (falso negativo raro: evicto **e** compactado **e** resumido). A
alternativa — declarar `tombstone_compaction/count=null` para toda identidade
sem tombstone — não distingue "compactado" de "nunca visto" e faz **toda** run
nova depois do horizonte nascer com um gap fantasma de tamanho desconhecido,
destruindo justamente o discriminador que o ledger promete. O erro é aceito na
direção de esquecer, que é a mesma direção de todo o desenho de retenção. JSON inválido numa row vira
`corrupt_payload`, preservando a posição da sequência.

**Risco residual nomeado — identidade de node autorada.** `node_path` (e a
coluna de consulta `node_id`) é o único campo de identidade cujo CONTEÚDO é
escolhido pelo agente: o spec do workflow é autorado por LLM. Ele é gravado
verbatim, porque o ledger não consegue responder "qual node" sem ele, e é
bounded (8 elementos × 64 chars). Isso **não** viola a §5.2 (que proíbe
prompt/args/results/URL/reasoning, não identificadores), mas é um canal
agent-controlled de ~512 bytes por evento para estado durável compartilhado —
aceito explicitamente e pinado por teste
(`test_authored_node_ids_are_persisted_verbatim_by_design`).

### 11.3 Bounds de produto e backpressure

Os defaults adotados são deliberadamente pequenos e mensuráveis:

| Recurso | Limite | Pior caso derivado |
| --- | ---: | ---: |
| Evento serializado | 2 KiB | payload maior é substituído, nunca cortado como JSON inválido |
| Eventos retidos por run | 2.048 | no máximo 4 MiB de JSON por run |
| Runs/tombstones recentes | 64/64 + 1 marcador de compactação | no máximo 256 MiB de JSON do ledger, antes de overhead SQLite |
| Fila do writer | 256 eventos | `put_nowait`; produtor nunca espera o SQLite |
| Buckets de gaps/drops | 256 (255 atribuídos + 1 agregado) | overflow vira `$audit/drop_bucket_overflow`; estado auxiliar não cresce por run id |
| Histórico causal process-local | 64 contextos/sub-session | mantém a janela recente e conta descartes; ledger durável não depende dela |
| Retenção temporal | 30 dias | sweep em todo append; runs inativos também expiram |
| Deltas | zero | excluídos na ingestão, não "comprimidos" depois |

O orçamento de 2 KiB comporta os eventos metadata-only medidos (tipicamente
~0,5 KiB) sem autorizar payload arbitrário. O teto de 2.048 preserva centenas de
leaves com lifecycle de node/leaf/cache e mantém a consulta de um run na ordem de
1 MiB típica. O pior caso global de 256 MiB é um teto, não uma expectativa; o
sweep temporal pode reduzir antes. OBS-04 ainda deve medir paginação e
autorização da superfície de leitura — `audit_events` é API interna nesta issue.

Microbenchmark local hermético em diretório temporário, Python 3.13/macOS, com
4.000 `tool.complete` cujos args e results tinham 1 MiB cada:

| Carga | produtor | mediana / p99 de `record_gateway` | Resultado |
| --- | ---: | ---: | --- |
| burst sem pausa | 0,180 s | 10,7 / 18,8 µs | 3.743 drops declarados por `queue_overflow`; 259 rows/markers, DB 352 KiB |
| ritmo de 0,5 ms/evento | 3,320 s | 22,8 / 164,3 µs | zero overflow; 2.048 eventos retidos + gap de 1.952 por retenção, DB 1,65 MiB |

O payload de 2 MiB por ação não apareceu no banco e não foi serializado pelo
produtor; a medição de tamanho é O(1). O burst prova, de propósito, que a fila
prefere perder detalhe e declarar a lacuna a bloquear o workflow. Eventos
aceitos e markers recebem ordinal monotônico no produtor; o writer mescla os
dois por esse ordinal, de modo que um gap não salta à frente de eventos mais
antigos já enfileirados. Gaps explícitos (`process_crash`) usam o estado de
controle limitado, não competem pela fila comum e preservam `count=null`. Não se usa esse
número como promessa universal de latência: CI, filesystem e contenção mudam; os
testes de contrato verificam não bloqueio e bounds, não cronômetro frágil.

Também foi medido o caminho end-to-end real do harness: um único node
`parallel` com 64 leaves, pool 8, clients falsos sem latência e sete bancos
novos por variante. Sem auditoria, as medianas foram 32,9 ms wall / 28,2 ms CPU;
com auditoria e flush, 47,5 ms / 40,4 ms. O custo absoluto pessimista foi 14,5 ms
wall e 12,2 ms CPU para 134 eventos (~108/~91 µs por evento), com 120 KiB
adicionais ao SQLite. A alta variação percentual (~44%) é esperada porque a
"resposta do provider" falso custa zero; numa leaf real, segundos de rede/modelo
não são acelerados pela auditoria. O discriminador hermético correspondente
executa 64 leaves e exige exatamente 134 eventos, zero gap e lifecycle completo.


Um microbenchmark SQLite anterior com 5.000 eventos comparou append ilimitado,
append com ring durável de 2.048 rows e snapshot: respectivamente 0,149 s
(~33,6k eventos/s, 2,46 MiB), 0,177 s (~28,2k/s, 1,02 MiB) e 0,069 s
(~72,5k/s, 12 KiB). O snapshot venceu custo e perdeu a propriedade auditável; o
ring durável pagou ~19% de tempo para tornar espaço limitado. Logo desempenho
não refutou o ledger, mas refutou a ideia de obter histórico pelo custo de um
snapshot.

A migração é estritamente aditiva: quatro tabelas e três índices
`CREATE ... IF NOT EXISTS`, sem alterar ou reescrever tabelas preexistentes. Um
discriminador abre um SQLite legado com uma tabela-sentinela, inicializa
`SessionDB` e comprova simultaneamente a preservação do dado antigo e a criação
do schema de auditoria.

### 11.4 Crash, concorrência e integridade

Os discriminadores herméticos cobrem:

- quatro threads concorrentes, 200 appends, e quatro processos/connections
  independentes, 100 appends, ambos com sequência densa;
- processo filho morto por `kill()` tanto no append direto quanto no pipeline
  assíncrono com cauda na fila; `PRAGMA integrity_check=ok`, prefixo denso e
  `process_crash/count=null` após reopen;
- reopen do banco preserva a trilha; resume por cache no mesmo serviço abre novo
  segmento sem leaf fictícia no hit (restart integrado do serviço fica em OBS-05);
- regressão do relógio sem eviction do run recém-tocado;
- expiração de run inativo, limite de runs, tombstone explícito, compactação
  bounded dos tombstones e retomada posterior sem reiniciar `seq` silenciosamente
  nem esconder o prefixo perdido;
- sink bloqueado: produtor conclui enquanto a fila enche, depois grava o gap; a
  conexão/lock de auditoria tem `busy_timeout` de 50 ms e é separada do lock
  geral do `SessionDB`, evitando convoy nas operações normais;
- sink falhando e recuperando: ação perdida vira `sink_failure` contado;
- JSON corrompido: posição retorna como indisponível, não como ausência;
- canários em prompt, resposta, args, result, reasoning, replay state, nome de
  tool desconhecida e marker de redaction forjado ausentes do DB, WAL e
  estruturas consultáveis;
- cancelamento visível no término do segmento, sem fabricar eventos para nodes
  que nunca foram executados; checkpoint sem resposta emite `node.paused`, não
  `node.failed`;
- marcador durável `audit_segment_id` fechado atomicamente pelo append de
  `segment.completed`; resume de uma cauda terminal não fechada declara
  `process_crash/count=null`;
- evento, fila, retenção e crescimento do banco limitados por contrato.

O ledger não promete uma ordem global entre runs. Dentro de um run, o lock de
escrita SQLite serializa os produtores e a transação une reserva de sequência,
append e poda. Segmentos distinguem cada stretch; node/cell/sub-session/turn
preservam relações causais sem usar relógio. A retenção temporal remove somente
um prefixo completo de `seq`, mesmo sob regressão do relógio, para que nenhum
buraco intermediário seja representado como gap de prefixo. Um cache hit é
`replayed`, sem `sub_id`. Um crash pode perder somente a cauda ainda não
commitada; o próximo
segmento declara a quantidade desconhecida.

### 11.5 Limites e resultados negativos preservados

- Falha permanente do próprio arquivo SQLite não pode gravar no mesmo arquivo a
  prova de que ele falhou. O runtime loga a falha, não muda o resultado do
  workflow. `AuditTrail.shutdown()` retorna `False` se não drenar;
  `WorkflowService.shutdown()` registra esse fato e retorna `None`. Um marker
  durável só existe se o sink recuperar. Dizer mais seria circular.
- `SIGKILL` não permite contar eventos que estavam apenas na fila; por isso a
  contagem fica indisponível, nunca zero.
- O ledger detecta lacunas e corrupção de payload, não oferece assinatura,
  hash-chain ou prova contra um atacante com escrita direta no arquivo.
- Encryption at rest, ACL multi-tenant, backup seguro e propagação de deletion
  continuam **inconclusivos**. O arquivo herda a proteção do state DB/profile;
  não há alegação de isolamento por tenant.
- Artefatos com conteúdo, justificativa `agent_declared` e raw debugging seguem
  fora do escopo. OBS-04 não expõe conteúdo cru pela consulta.
- O estado durável preexistente (messages, specs, args, outputs, FTS e cache)
  continua com políticas próprias; esta mudança minimiza o novo ledger, mas não
  retroativamente declassifica nem apaga esses stores.
- Split-brain de owner/lease e TOCTOU de cancelamento são riscos do run-state,
  não resolvidos escondidamente por OBS-03; a trilha registra o observado, não
  substitui o conserto dessas invariantes.

### 11.6 Classificação

| Hipótese de #21 | Resultado |
| --- | --- |
| Snapshot + histórico limitado responde sem cada delta | **Confirmada e estreitada**: snapshot é projeção; ledger limitado guarda ações; deltas ficam fora |
| Níveis de detalhe devem incluir conteúdo | **Reformulada**: metadata/lifecycle no default; conteúdo e private state excluídos, não apenas ocultos na leitura |
| Redaction no ingest pode destruir evidência necessária | **Refutada para o contrato default**: identidade, ação, outcome, tamanho e proveniência responderam às perguntas operacionais sem conteúdo |
| `progress_json`, append-only e histórico existente são equivalentes | **Refutada** por restart/reconstrução hermética |
| Integridade exige gaps/drops detectáveis, não impedir toda alteração | **Confirmada**, com sequência transacional, gaps, tombstones e corrupção explícita; tamper-evidence permanece fora |
| Auditoria necessariamente bloqueia o workflow sob sink lento | **Refutada**: fila limitada + writer isolado mantêm o produtor não bloqueante |

Desfecho legítimo escolhido: **trilha durável, sanitizada e limitada, em camadas**.
OBS-03 está concluída no nível de armazenamento interno. A superfície de consulta e autorização foi concluída em OBS-04; a campanha
adversarial integrada e seus limites foram concluídos em OBS-05.

## 12. OBS-04 — consulta read-only compartilhada

A hipótese de ampliar `workflow_status` com histórico detalhado foi **refutada**:
status é um rollup compacto e frequentemente consultado; repetir uma trilha
paginada em cada poll aumentaria latência, payload e contexto sem melhorar o
estado atual. O detalhe causal ficou numa superfície dedicada, sobre um único
read model do `SessionDB`:

```text
workflow_audit(run_id, node_id?, event_type?, sub_id?, segment_id?, attempt?,
               after_seq=0, snapshot_seq?, limit=50)
lohra workflow audit RUN_ID [os mesmos filtros e cursores]
```

A tool é registrada apenas no agente pai e é excluída de subagentes. A CLI e a
tool chamam `SessionDB.audit_query`; nenhuma delas constrói client, consulta
provider ou gera resumo. O retorno declara `policy.mode=metadata_only`,
`provider_calls=none` e `summary_generated=false`. O boundary de leitura reaplica
a allow-list/bounding do writer: payload JSON legado, válido porém inseguro,
também não atravessa a consulta.

### 12.1 Cursor, snapshot e filtros

`seq` é a ordem durável do run e, portanto, o cursor; timestamp não ordena a
causalidade. A primeira página devolve `snapshot_seq`; repeti-lo nas próximas
páginas congela tanto eventos quanto disclosures de integridade, mesmo que outro
processo continue anexando eventos. `has_more` significa somente “há mais eventos
que satisfazem o filtro neste snapshot”; nunca é usado como sinônimo de perda.
`limit_requested`, `limit_effective` e `limit_clamped` tornam o limite de 100
explícito. A leitura do conjunto retido inteiro é intencional e bounded pelo ring
de 2.048 eventos por run: integridade não pode depender do filtro nem da página.

Os filtros (`node_id`, `event_type`, `sub_id`, `segment_id`, `attempt`) operam
somente sobre metadata. `integrity.scope=retained_snapshot` agrega, fora dos
filtros, markers de redaction/exclusion/truncation, gaps e indisponibilidade.
Retenção, tombstone, run nunca auditado e payload corrompido permanecem fatos
visíveis; um filtro vazio não os transforma em “execução limpa”. Não há alegação
de ACL multi-tenant: a fronteira de autorização disponível é o arquivo/profile
do `SessionDB`.

### 12.2 Quando usar cada superfície

| Superfície | Pergunta respondida |
| --- | --- |
| `workflow_status` | O run está onde, terminou como e quanto gastou? |
| `workflow_audit` | Qual sequência causal/segmento/attempt explica esse estado? |
| `lohra workflow audit` | A mesma investigação, cross-process, para operador/scripts |
| `lohra workflow watch` | Quais transições compactas estão acontecendo ao vivo? |

`watch` continua sendo o monitor live compacto; não duplica o ledger detalhado.
Para acompanhar um run ainda ativo, omitir `snapshot_seq` obtém a cauda commitada
mais recente; para percorrer uma visão consistente, fixar o valor retornado na
primeira página. Depois do run, a mesma consulta reabre o estado durável em outro
processo e produz o mesmo contrato metadata-only.

### 12.3 Classificação das hipóteses de #22

| Hipótese | Resultado |
| --- | --- |
| Estender `workflow_status` é a superfície natural | **Refutada**: mistura rollup/poll com histórico e repete payload |
| Tool e CLI devem ter implementações próprias | **Refutada**: um read model evita divergência semântica |
| Paginação por timestamp basta | **Refutada**: `seq` + `snapshot_seq` preserva ordem e snapshot |
| Filtros podem também filtrar disclosures de integridade | **Refutada**: esconderiam gaps e perdas |
| Sanitização apenas no ingest é suficiente | **Refutada defensivamente** por linha válida adulterada/legada |
| Consulta detalhada exige LLM/provider | **Refutada**: leitura SQLite determinística, zero client/tokens |
| Autorização equivale a ACL multi-tenant | **Inconclusiva/fora do produto atual**: isolamento é por DB/profile |

Desfecho: **tool e CLI dedicados, read-only, paginados por `seq`, snapshot
estável e integridade explícita, sobre a mesma API durável metadata-only**.

## 13. OBS-05 — campanha adversarial end-to-end

A campanha final foi derivada dos riscos que sobreviveram às quatro etapas
anteriores, e não de uma lista de classes do engine. O oráculo compara três
fontes independentes: resultado/estado durável do run, chamadas observadas pelo
client falso e a consulta pública `audit_query`. Cada cenário exige relações
causais (identidades e pares start/terminal), não uma ordem global inventada.

### 13.1 Matriz de riscos e discriminadores

| Risco confirmado/reformulado | Evidência executável | Propriedade exigida |
| --- | --- | --- |
| Fan-out termina fora de ordem e dois runs compartilham o serviço | pipeline em dois runs concorrentes sincronizados por barreira, com retorno rápido/lento determinístico, mais workflow aninhado | nenhum `run_id` ou `sub_id` cruza fronteiras; a coordenada completa run/node/cell é causal e cada leaf tem um único terminal |
| Retry pode confundir turn de correção com nova tentativa | schema inválido seguido de correção no mesmo filho e resposta vazia seguida de respawn | turns crescem no mesmo `sub_id`; retry fresco muda `sub_id` e incrementa `attempt` |
| Resume em outro processo lógico pode imitar execução | serviço A conclui, serviço B reabre o mesmo SQLite e faz resume | novo segmento contém `cache.replayed`, sem `leaf.started` fictício |
| Provider/transport muda o envelope bruto | fixtures independentes `anthropic_messages`, `chat_completions` e `responses`, incluindo tool e provider failure | cardinalidade e pares causais do lifecycle normalizado são equivalentes; reasoning, conteúdo, argumentos/result e estado privado nunca chegam ao ledger |
| Overflow ou evento grande pode parecer ausência limpa | fila de tamanho 2 com sink retido e payload acima do limite | `audit.gap` contado e `audit.truncated` continuam visíveis mesmo sob filtro que não seleciona esses eventos |
| Crash, cancel, checkpoint, retenção e corrupção quebram completude perfeita | campanha hermética de OBS-03/04, incluindo processo morto e query cross-process | prefixo denso ou lacuna explícita; pause não vira failure; disponibilidade/integridade não é escondida por filtros |
| Carga larga pode tornar o recurso impraticável | workflow real de 64 leaves, pool 8, provider falso sem latência | run conclui, 134 eventos esperados, zero gap no discriminador e bounds de fila/evento/runs preservados |

Os cinco cenários novos em `tests/test_workflow_audit_e2e.py` são sete casos de
pytest por causa dos três transports. Eles complementam — sem duplicar — os
discriminadores de crash, cancel, checkpoint, migração, contenção, retenção,
redaction e leitura cross-process em `test_workflow_audit*.py`.

### 13.2 Resultado da refutação

A suspeita de que concorrência exige ordem global foi **refutada**. A ordem de
`seq` é a ordem de commit do ledger, não a ordem semântica de todos os workers;
`run_id`, segmento, node, cell, sub-session, attempt e turn bastaram para
reconstruir uma história causal compatível com resultados fora de ordem. Não há
promessa de comparar causalmente dois runs independentes.

A suspeita mais perigosa — cache/resume parecer uma nova execução — também foi
**refutada no caminho real**, inclusive após destruir o primeiro
`WorkflowService`: o stretch retomado tem segmento próprio e `cache.replayed`,
sem nova leaf. Retry foi **reformulado** em duas operações distintas: correção de
schema preserva o filho/cell e avança attempt+turn; retry de resposta vazia cria
filho novo, preserva a cell, avança attempt e reinicia turn. Misturar as duas seria uma trilha falsa.

A equivalência entre providers foi confirmada apenas no nível que o runtime de
fato observa: envelopes normalizados dos três transports produzem os mesmos
fatos de lifecycle/tool/failure e excluem private state. Isso **não** afirma que
SDKs, redes ou streams reais tenham cronologia idêntica; diferenças anteriores à
normalização continuam fora da fronteira observável e devem aparecer como gap ou
falha se impedirem a captura.

### 13.3 Medição reproduzível

O comando abaixo recria bancos descartáveis, alterna variantes com e sem o sink
de auditoria e publica medianas, perdas e tamanho físico de SQLite/WAL/SHM em
JSON. Ele não contém threshold de CI: cronômetro de máquina compartilhada não é
contrato.

```bash
cd backend
python benchmarks/workflow_audit_overhead.py --samples 9 --warmups 2
```

Na medição de referência desta campanha (64 leaves, pool 8, 9 amostras), a
mediana sem auditoria foi **30,98 ms wall / 25,75 ms CPU / 252 KiB**; com
writer+flush foi **70,46 ms / 48,96 ms / 372 KiB**. A mediana dos deltas pareados
foi **38,14 ms wall, 23,21 ms CPU e 120 KiB**, ou ~285/~173 µs por cada um dos
134 eventos esperados;
as nove amostras tiveram zero gap e zero drop. Uma rodada exploratória separada
observou um `sink_failure` por contenção SQLite; o resultado não foi apagado nem
contado como amostra limpa: motivou o script a sempre reportar
`samples_with_gaps` e `dropped_total`. O discriminador automatizado permanece
mais estrito e exige 134/134, mas desempenho não depende de percentual.

Esses números são deliberadamente pessimistas quanto à participação percentual:
o provider falso responde em tempo zero, enquanto uma leaf real paga rede e
modelo. O resultado sustenta que o custo absoluto é limitado e observável; não
sustenta um SLA universal de latência.

### 13.4 Incertezas residuais

- Quota pause/autoresume é coberto pelo estado durável do harness, e pause/cache
  têm discriminadores de auditoria, mas uma campanha com quota real depende de
  provider externo e permanece **inconclusiva** quanto à cronologia anterior à
  normalização.
- `SIGKILL` só permite declarar cauda desconhecida (`count=null`); contar objetos
  que existiam apenas na memória seria fabricar evidência.
- O ledger é detectável quanto a gaps, truncamento e corrupção, não
  tamper-evident contra escrita direta no SQLite.
- ACL multi-tenant, encryption at rest, backup e deletion propagation continuam
  fora do contrato; isolamento é o do profile/arquivo.
- Sob contenção extrema, o sink pode perder eventos para não bloquear o workflow.
  Isso é uma degradação explícita (`sink_failure`/`audit.gap`), não completude.

### 13.5 Classificação das hipóteses de #23

| Hipótese | Resultado |
| --- | --- |
| Poucos cenários discriminadores cobrem melhor que muitos testes superficiais | **Confirmada**: cinco relações adversariais novas fecharam as fronteiras, apoiadas pelos testes unitários de bounds/crash |
| Causalidade, ordem parcial, deduplicação e gaps importam mais que ordem global | **Confirmada**; ordem global entre runs foi explicitamente rejeitada |
| Replay/resume/cache devem ser fatos próprios | **Confirmada end-to-end**, inclusive em novo serviço |
| Perda por crash ou limites deve deixar marker | **Confirmada dentro da fronteira recuperável**; crash tem quantidade desconhecida, overflow/sink failure têm contagem quando observável |
| Todos os providers podem prometer trilha bruta idêntica | **Refutada/reformulada**: a equivalência garantida começa no gateway normalizado |
| Auditoria pode ser simultaneamente não bloqueante e sempre completa | **Refutada**: disponibilidade do workflow vence; perda bounded é obrigatoriamente explícita |

Desfecho: **capacidade considerada auditável dentro do contrato metadata-only,
bounded e de ordem causal parcial**. Não houve falha que exigisse descartar a
arquitetura; as limitações acima são parte do contrato, não alegações escondidas
de completude perfeita.
