# Auditoria dos nodes do DAG — contrato de evidência

Status: **OBS-01 e OBS-02 concluídas; OBS-03–05 ainda investigativas**
Milestone: Wave 4 — Auditoria e observabilidade dos nodes do DAG  
Issue fundadora: #19

Este documento define o que a Lohra pode legitimamente chamar de auditoria de
workflow. Ele não escolhe ainda o mecanismo de correlação, armazenamento ou
consulta: essas escolhas pertencem, respectivamente, a OBS-02, OBS-03 e OBS-04
e devem sobreviver aos testes adversariais de OBS-05.

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
- `cell_id`, a identidade content-addressed já usada pelo cache;
- `role`, distinguindo agent, branch, skeptic, judge, round, gate etc.;
- `item_index`, `stage_index` e `branch_path`, quando aplicáveis;
- `attempt`, incrementado tanto no novo spawn de retry quanto no turno corretivo
  da mesma sub-sessão;
- `turn`, zero no spawn e incrementado no `steer()` corretivo da mesma sub-sessão.

O `OrchestrationCore` aceita `causal_context` em `spawn()` e `steer()` sem
importar `lohra.workflow`, preserva o valor e seu histórico de turnos, e devolve
essa metadata em `collect()`. O `sub_id` continua sendo gerado pelo core e,
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

Cache lookup/hit/replay não é execução de leaf. Um hit preserva o mesmo
`cell_id`, não cria sub-sessão e não fabrica `sub_id`, `turn` ou `attempt`; OBS-03
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
redaction ou query. `causal_history` ainda é process-local e não é chamado de
auditoria durável; ele prova o transporte e permite que OBS-03 capture cada
turno sem derivação ambígua. Cancelamento/timeout não requer coordenada nova: é
outcome da tentativa em curso e será modelado no vocabulário de eventos da
próxima issue.
