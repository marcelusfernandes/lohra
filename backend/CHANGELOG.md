# Changelog

Todas as mudanças notáveis da Lohra, por versão publicada no PyPI
(`pip install lohra`). Formato inspirado em [Keep a Changelog](https://keepachangelog.com/);
versões seguem SemVer (fase 0.0.x: qualquer release pode conter mudanças incompatíveis).

## [Não publicado]

## [0.0.19] — 2026-09-02

Prelúdio da Wave 8: as duas decisões do dono que faltavam viraram código, e a Wave 8 abriu como investigação
read-only sobre a run real `42abc3eb…` (três relatórios, épicos propostos por issue —
`docs/history/reviews/2026-09-02-wave8-investigation.md`). Integração linear (sem merge commits).

### Adicionado
- teto de token budget pré-autorizado pelo OPERADOR para runs de workflow (issue #47, parte 2): `--token-budget-cap`
  (chat) > `LOHRA_TOKEN_BUDGET_CAP` > sem teto; efetivo = `min(spec, cap)`, e cap sozinho quando o agente não pede
  nada (fecha "agente sem teto roda ilimitado"); no resume o `token_budget` novo também é clampado; o aceite do
  `run_workflow` diz a proveniência (`token_budget: {total, source, operator_cap}`); pausa e recusa sob teto vinculante
  apontam o OPERADOR como remédio (fecha o loop clamp→pausa→clamp). Sem teto: byte-idêntico. `workflow/operator_budget.py`.

### Mudado
- `min_success_ratio` removido do schema (issue #15, metade restante): o engine nunca o aplicou e o spec deixou a
  semântica ambígua; agora rejeitado com erro DIDÁTICO (`min_success_ratio_removed`, nunca `unknown_field`) que
  nomeia o substituto — `gate`/`completeness_check` marcado `required: true` lendo o fan-out.
- use-lohra ensina o teto do operador para orquestradores headless; workflow-authoring e spec 07 sem o campo removido.
- bump 0.0.19

## [0.0.18] — 2026-09-01

Rodada "Wave 7.5": cinco fatias implementadas em worktrees paralelas (agentes opus/sonnet coordenados), cada uma
verificada no código antes de virar trabalho; as duas de maior risco (#4 e #8/#42) passaram por review adversarial
independente antes do merge. Detalhe em `docs/history/2026-09-01-wave7.5-parallel-slices.md`. **Validado ao vivo**
via Codex headless dirigindo a Lohra (6 testes, `docs/history/reviews/2026-09-02-dogfood-codex-wave7.5.md`).

### Segurança
- leaf sandbox nega `terminal` e `mcp_*` por default nos leaves de workflow (issue #4, F01-A) — exfil via shell
  (`cat ~/.lohra/.env`, `curl -d @…`) e via MCP estava aberta até em run tainted. Opt-in SÓ do operador
  (`allow_terminal`/`mcp_allow` em `~/.lohra/workflow_policy.json`, ou `LOHRA_LEAF_ALLOW_TERMINAL` /
  `LOHRA_LEAF_MCP_ALLOW`), nunca do spec; taint nega sempre; definitions do leaf deixam de anunciar o que o
  dispatch recusa. Template salvo que pedia `terminal` em leaf (ex.: `slow-experiment`) agora exige o opt-in.

### Adicionado
- `required: true` real (issue #15, resposta à #42-A): nó indispensável que resolve `null` aborta o run (`failed`),
  nós restantes registram fault `skipped` sem serem nulados; fora do `cell_hash`; aninhado sobe ao pai.
  `min_success_ratio` segue aceito-e-ignorado — spec ambíguo, racional em `docs/specs/07` §7.4.
- quiescência após cancel (issues #8 / #42-B): `_timed_out` e a barreira do pipeline esperam (teto curto,
  `LOHRA_CANCEL_QUIESCENCE_S`, default 5s) o leaf cancelado assentar; o fault diz "settled in Xs" ou
  "STILL RUNNING … shared working_root may be mutated".
- lint de autoria (issue #49): DAG de 2+ nós sem nenhuma aresta valida mas devolve `warnings` no aceite do run e
  no plano do live view.
- `LOHRA_PROVIDER_READ_TIMEOUT` (issue #48): timeout HTTP dos SDKs configurável pelo operador (default byte-idêntico);
  timeout classificado (`error_kind="timeout"`) e fault didático que nomeia os dois níveis (HTTP × `timeout:` do nó).
- envelope `--json` ganha `workflows` (issue #47): runs deste turno que ficaram `paused` (com `pause_reason`) ou
  que ainda rodavam e foram cancelados na saída (`cancelled_on_exit`). Ausente quando não há nada a dizer.

### Corrigido
- `depends_on` malformado (id desconhecido, string, item não-string, `null`) era descartado em silêncio e podia
  reordenar o DAG (issue #2, F12) — agora rejeitado com exemplo.
- `lohra workflow watch` fazia loop infinito em run pausado por budget (issue #47) — sai na pausa sem
  auto-resume, segue observando pausa por quota; `pause_reason` em `list`/`watch`.
- `cancel()` sobre run já terminada (complete/degraded/failed) reescrevia o veredito e respondia ok
  (candidato ii do dogfood-codex) — recusado nas duas portas; `paused` segue cancelável, `cancelled` idempotente.
- vocabulário do audit aceita `skipped` (sem isso o estado do nó pulado era redigido como `excluded_by_policy`).

### Mudado
- use-lohra ensina o campo `workflows` do envelope e o comportamento do `watch` em pausa (anti-drift cobre a cópia .codex).
- workflow-authoring documenta o que um leaf pode e não pode (sem shell/MCP), `required` real e seus limites.
- bump 0.0.18

## [0.0.17] — 2026-08-31

### Adicionado
- rastro de acks das notices — tombstones bounded + lohra notices (issue #39)
- morte por sinal publica dead-turn notice — sinal vira exceção, epílogo normal (issue #40)

### Corrigido
- unwinding de sinal não joina o pool de tools — morte pronta (review adversarial da wave 7)

### Mudado
- use-lohra cobre morte por sinal e lohra notices (wave 7)
- bump 0.0.17

## [0.0.16] — 2026-08-31

### Corrigido
- audit sink — migração faltante mascarada de contenção + busy_timeout do operador, warnings agregados, drain final (issue #34)

### Mudado
- bump 0.0.16

## [0.0.15] — 2026-08-31

### Adicionado
- --provider explícito sobrepõe a subscription sob preference=auto (issue #35)
- workflow watch/audit aceitam o prefixo curto que a list imprime (issue #24)

### Mudado
- use-lohra ensina cost/usage_total do envelope + override --provider; cópia .codex entra no anti-drift
- bump 0.0.15

## [0.0.14] — 2026-08-31

### Corrigido
- **A compactação preflight passou a conhecer a janela REAL do modelo** (issue #38).
  Antes, `Agent.context_window` era 200.000 hardcoded e nenhum callsite passava
  outro valor: todo modelo de todo provider rodava com 200k assumidos, e um
  modelo de janela menor (o caso real: `deepseek/deepseek-v4-pro` via OpenRouter,
  turno 9 do épico Wave 6) morria com `stop_reason: length` vindo direto do
  provider, sem o preflight sequer disparar — o mecanismo de defesa existia e
  nunca era consultado.
  - `ProviderProfile` ganhou `default_context_window` + `get_context_window(model)`
    (mesmo shape de `default_max_tokens`/`get_max_tokens`). Os perfis builtin
    declaram: anthropic 200k e openai 128k são os fatos publicados das famílias;
    os demais levam **piso conservador** (openrouter 32k — a rota serve centenas
    de modelos, de 8k a 1M); ollama não faz claim (é local).
  - O catálogo passou a preservar o `context_length` que a fonte publica
    (a OpenRouter publica) e a persistir o que aprendeu em
    `~/.lohra/model_windows.json` — escrita atômica, merge por provider,
    best-effort (json corrompido ou home read-only degradam para "não sei").
    Nenhuma chamada de rede nova: quem alimenta o cache é o `lohra models` que
    o operador já roda.
  - `Agent.resolve_context_window()` resolve na ordem **override explícito >
    cache do catálogo > piso do perfil > 200k** (o valor antigo, agora só último
    recurso), a cada decisão de compactação — assim uma troca de `agent.model`
    por sub-sessão reacompanha a janela sozinha. "Não sei" degrada para o
    conservador, nunca para o otimista.
  - Efeito prático: rode `lohra models` uma vez para o preflight conhecer a
    janela exata de cada modelo da sua rota; sem isso vale o piso do perfil.
  - **Granularidade por modelo E por rota** (`model_windows` + longest-prefix
    match): um mesmo provider serve modelos de janelas muito diferentes, e a
    janela depende também da ROTA. Verificado em fonte primária (2026-08-31):
    Anthropic key-based dá 1M a Opus/Sonnet 5, Fable/Mythos 5 e Opus/Sonnet 4.6+,
    e 200k a Opus/Sonnet/Haiku 4.5 e anteriores. O `gpt-5.5` tem **1M na API
    direta mas 400k no backend Codex/subscription** (a rota que a Lohra usa) —
    então sob subscription toda a família gpt-5* assume 400k, e assumir 1M ali
    mataria o turno por `length`. Valor exato por modelo é seguro nos dois
    sentidos; só um piso amplo demais era perigoso.

## [0.0.13] — 2026-08-30

### Adicionado
- **Wave 6 — supervisão ativa dos workflows em voo** (milestone 8, 6 issues; épico liderado pela própria Lohra, gate por avaliador independente):
  - **Doutrina de supervisão** (SUP-01): o ciclo vigiar→diagnosticar→adaptar→retomar com fronteira explícita agente×humano por categoria de causa e freios com valores justificados (1 contorno por `(run, causa, target)`, 3/run, K=2 de não-progresso, allowance min(6k, 25%)), posicionada no guidance das tools e na skill builtin, protegida por 37 testes de contrato anti-drift. As mensagens do harness foram alinhadas à fronteira (o erro de budget não sugere mais o valor do novo teto; o checkpoint exige resposta humana verbatim).
  - **Contrato de leitura de run em voo** (SUP-02): `workflow_status` declara proveniência e custo (`source`, `provider_calls: none`, tokens não atribuídos separadamente); doutrina de leitura hierárquica (status para decisão run-level, audit sob demanda, sem polling cego, "silêncio significa desconhecido"); benchmarks reproduzíveis em duas cadências em `docs/history/evidence/`.
  - **Steering de leaf em voo** (SUP-03): injeção endereçada por identidade quíntupla (`run/sub/segment/attempt/turn`), só ocorrência viva e exata, lida entre iterações; freios (1/leaf, 3/run durável, correções cumulativas com o retry de schema, 4.000 chars); lifecycle auditável sem persistir o conteúdo; prompt congelado provado por identidade de objeto.
  - **Pivô com reuso de cache** (SUP-04): spec adaptado no mesmo `resume_run_id` re-paga só as células alteradas; fronteira monetária respeitada (provider/rota/budget = humano); interação com o autoresume documentada; nós rigorosos não têm cache parcial (refutado com evidência).
  - **Aprendizado no momento do erro** (SUP-05): classificador de falhas aprendíveis fail-closed, notas operacionais duráveis com claim/ack at-least-once, dedup, caps e expiração; dead-turn notices para turnos mortos E para os descartes invisíveis (falha de persistência, lock de compactação perdido); recovery de runs órfãos cercada por fence; notifier durável de workflow na CLI (paridade dashboard).
  - **Gate E2E** (SUP-06): matriz de integração cross-feature (morte no meio do pivô, steering×cancel, flood de notices, dedup sob concorrência, duas decisões no mesmo run — nenhum bug de produção encontrado) + probes adversariais ao vivo com artefatos preservados; veredito CONFIRMADA com lacunas nomeadas (evidência real dos gatilhos de enforcement → issue #36).

### Corrigido
- **Persistência de turno é transacional** (`save_messages`): uma falha no meio do lote faz rollback do turno inteiro (CLI, gateway e fork de compactação) — nunca mais transcript parcial quebrando a alternância no resume.
- **Recusa cercada do ledger aborta o launch inteiro** (fencing pós-persist): um resume que perde a cerca entre a linha e o ledger não deixa engine, notice, PLAN nem lease para trás.
- **O settle de um run sobrevive a erro de escrita do ledger** — core shutdown, fechamento de segmento, linha terminal e release da lease sempre rodam.
- **Flake 5-de-6 em recovery-notice desativado**: o helper de teste agora bloqueia a leaf de verdade (a lease "lapsada" era renovada pelas escritas da thread ainda viva).

## [0.0.12] — 2026-08-29

### Corrigido
- **O que uma PAUSA cancela não é falha da forma** — uma pausa de quota mata de
  propósito os leaves em voo (todos tomariam 429 também), e cada um deles caía
  no rollup como fault de leaf (`leaf cancelled/interrupted: no detail`). O
  `carried_faults()` só descontava a UMA fault que a própria pausa escreveu,
  então `prior_degraded` virava `True` e o run — depois de auto-resumir e
  terminar **impecavelmente limpo** — ensinava um insight PROBLEMÁTICO à library
  em vez de certificar o template. Agora essas faults são marcadas como
  administrativas (`RunResult.pause_faults`, alimentado pelo `note_leaf_failure`
  quando o leaf termina `cancelled`/`interrupted` **com o run pausado e não
  cancelado**) e o `carried_faults()` as desconta junto com a principal.
  **Continuam reportadas** em `faults`/`faults_total` — o desconto é só no
  veredito, o fail-closed do relato não mudou. Cancelamento do USUÁRIO segue
  pulando o `record_outcome` como sempre (o run sela `cancelled`, outra rota).
- **O timeout do `pipeline` devolve os slots dos leaves que nunca rodaram** — o
  `_expire()` marca `_expired` e só então cancela o backlog, e o `on_done` de um
  enfileirado cancelado retorna no guard de straggler **antes** do
  `account_leaf` — então a reserva de lifetime vazava e todo run que estourou a
  barreira voltava com menos lifetime do que realmente gastou. Agora o próprio
  `_cancel_running` liquida os que o pool descartou da FILA
  (`cancel → "queued"`), que é o único caso em que o slot não comprou nada; leaf
  ainda dentro de uma chamada de provider continua cobrado pelo seu done-path.
- **Lifetime esgotado no meio de um `pipeline` é FAULT, não null silencioso** —
  `N itens x M stages` pode passar do lifetime declarado, e a recusa cai dentro
  do `_advance`, num worker de `on_done` (a exceção nunca chega à thread do nó,
  então o handler que conta `FanoutRejected` para todos os outros node-types não
  a vê). Era só `logger.warning` + item `None`: o run selava **`complete`**, sem
  fault e sem `cap_trip` — e um `pipeline` truncado que lê limpo é exatamente o
  que a `library` certifica como template reusável. Agora registra fault nomeado
  (`engine.record_fault`) e conta o cap trip (`engine.count_cap_trip`), então o
  status degrada, igual ao caminho `FanoutRejected` dos demais nós (M1/M2, §12).
- **Um processo fenced-out para de RESPONDER pela run, não só de trabalhar
  nela** — o `_abort_fenced_run` abortava o engine mas deixava o `RunState`
  obsoleto legível em `_runs`: o `status()` do processo velho reportava a
  própria stretch morta **mascarando a linha do dono novo**, e o `cancel()`,
  que faz curto-circuito num state vivo, devolvia `{"ok": true}` para uma run
  sobre a qual ele não tem mais nenhuma reivindicação (falso-positivo). Agora o
  state é marcado `fenced` e o `_get` — a costura única de `status`, `cancel`,
  `pause`, `resume` e `run_owner` — passa a devolver `None`, então todos caem no
  caminho cross-process que já existia e é honesto (o `cancel` responde o
  tri-estado "outro processo está dentro dessa run, a lease vence em ~Ns"). O
  `workflow_list` também deixa de listar a stretch abandonada — listá-la
  escondia a linha do dono novo pelo dedup. A entrada **continua** em `_runs`:
  o `finally` da thread da run ainda a usa, e o guard de clash do `start` tem
  que seguir recusando enquanto uma thread retardatária drena.
- **A fresta do `shutdown(wait=False)` fechada** — o snapshot dos filhos era
  lido sob o lock, o lock era solto, e só então o pool era encerrado: um
  `spawn()` que caísse NESSA janela era submetido a um pool prestes a dropá-lo
  (`cancel_futures`) e ficava fora do snapshot que liquida os descartados —
  ninguém marcava terminal, ninguém disparava o `on_done`, e o hang da issue #8
  renascia uma corrida depois. Agora há uma **segunda varredura** de
  `_children` DEPOIS do teardown do pool: como o `ThreadPoolExecutor` guarda
  `submit` e `shutdown` com o mesmo lock, um spawn posterior é recusado de vez
  (o chamador vê a recusa) e um anterior está no segundo snapshot — não existe
  terceiro estado.
- **Cancelar um leaf ainda ENFILEIRADO agora encerra na hora (issue #8)** — o
  `on_done` de uma sub-sessão só disparava de dentro do `_run`, e um future que
  o `cancel()` vencia nunca chega a rodar `_run`. Resultado: quem encadeia
  trabalho no `on_done` (o scheduler de `pipeline`) ficava esperando a própria
  barreira — **até 1800s (`PIPELINE_TIMEOUT`)** — por um turno que jamais ia
  acontecer, e o run segurava sua lease no banco todo esse tempo depois de o
  `workflow_cancel` já ter respondido `{"ok": true}`. Agora tanto `cancel()`
  quanto `shutdown(wait=False)` (o caminho do cancel de verdade, que dropa os
  enfileirados no nível do pool e nem passava pelo `cancel()`) liquidam cada
  sub-sessão descartada pelo mesmo caminho fire-once.
  - **Status novo `cancelled`** — "nunca rodou" deixou de ser indistinguível de
    `interrupted` ("rodou e foi parado no meio"): são custos diferentes, e o
    primeiro é o único que uma contabilidade pode devolver honestamente.
  - `on_done` agora é reivindicado sob lock e invocado fora dele: com três
    threads podendo alcançá-lo (o worker do pool, `cancel`, `shutdown`), o
    check-then-set solto que existia era janela real de disparo duplo.
- **O lifetime de leaves do workflow virou reserva atômica (issue #14)** — o
  saldo era consultado num lugar (`lifetime_remaining`, em `_advance`) e cobrado
  em outro (`charge`, depois do `core.spawn`): duas aquisições do mesmo lock
  separadas por I/O real (escrita no banco, `GatewaySession`, submit no pool).
  Os workers concorrentes de `on_done` do `pipeline` liam todos o mesmo saldo
  antigo e todos decidiam "pode", então um run spawnava mais leaves do que o
  lifetime que ele mesmo declarava. Agora `Budget.reserve()` checa e incrementa
  numa **única seção crítica**, dentro do funil de spawn do engine (o mesmo
  ponto único por onde `_gate_tokens` já passa), então **todos** os node-types
  ficam cobertos. Medido: 2 spawns antes, 1 spawn + 1 recusa depois.
  - **Refund só para leaf que NUNCA rodou** — spawn que levantou, e sub-sessão
    que o pool descartou da fila (o `cancelled` da issue #8 acima; antes dela o
    refund nem teria como disparar). Leaf que rodou e falhou **continua
    cobrado**: `token_budget` é `None` por padrão, então o lifetime é o único
    limite duro da maioria dos runs, e devolver falhas reais deixaria uma forma
    que sempre falha spawnar sem fim. Exatamente uma vez por leaf.
  - O eixo de **tokens não foi tocado** (o gate é soft por design: leaf em voo é
    trabalho já pago).
- **Perder a lease do run agora ABORTA a execução (issue #8, elo com a #12)** —
  o fencing da 0.0.11 fez a escrita de um dono obsoleto falhar fechado, mas
  nada o impedia de continuar **executando**: ele seguia agendando nós,
  segurando workers de orquestração e **queimando tokens** num run que outro
  processo já tinha assumido; o heartbeat apenas parava de bater. Agora ele
  avisa o dono (`on_lease_lost`, disparado fora do lock do heartbeat e no
  máximo uma vez), que para o engine (`request_cancel` + `shutdown(wait=False)`)
  — chamadas **em memória**, nunca por escrita no banco: um processo fenced-out
  não pode rotear controle por um caminho que o próprio fence recusa. Nada é
  persistido: o desfecho do run é do dono novo.

## [0.0.11] — 2026-08-29

### Corrigido (segurança)
- **Gateway: toda rota REST `/api*` exige o token de sessão** (`X-Lohra-Session-Token`)
  — middleware cobre rotas atuais E futuras; Bearer/query não autenticam REST;
  WebSocket e `--insecure` preservados. Antes, as rotas REST não checavam auth.
- **Agent loop: tool calls incompletos são rejeitados** antes do dispatch — um
  stream truncado não constrói mais executor de zero workers (crash) nem
  reescreve silenciosamente a resposta; deltas de tool órfãos num stream que
  termina em texto são descartados com warning, preservando o conteúdo.

### Corrigido
- **Fencing de ownership dos workflows (issue #12)** — a lease de run (WF-29)
  arbitrava quem pode **começar** um run, nunca quem pode **escrever**. As cinco
  famílias de escrita feitas sob ownership (node cache, custo por célula, ledger
  do run, linha durável, ledger de auditoria) eram `INSERT OR REPLACE`
  incondicionais, então um dono obsoleto — congelado, ou apenas mais lento que o
  TTL, com o heartbeat já sabendo que perdeu a lease e só parando de bater — ainda
  escrevia por cima do processo que assumiu o run, **em silêncio** (o fault
  `recovered after process loss` do novo dono, por exemplo, sumia da linha
  durável). Agora cada aquisição da lease carrega um **fence** monotônico por run
  (`workflow_run_fence`, incrementado na mesma transação do INSERT vencedor) e
  toda escrita o apresenta: escrita de dono obsoleto é **recusada e logada**
  (`warning` nomeando o run), degradando sem corromper e sem levantar exceção nas
  threads de pool/sink.
  - **Aditivo**: tabela nova (`CREATE TABLE IF NOT EXISTS`); fence `NULL` nunca
    recusa, então base antiga e caminhos legitimamente sem dono (cancelar um run
    que ninguém segura) escrevem exatamente como antes.
  - **Memória de fences bounded não vira licença** — o store lembra os fences de
    até 1024 runs; quando a eviction tirava um da memória, o lookup devolvia
    `None`, indistinguível de "esse run não tem fence" (base pré-#12) — e a
    escrita voltava a ser **unfenced**, com o evento obsoleto aterrissando no
    ledger do novo dono. Os dois casos agora são distintos: `None` só significa
    "esse run nunca teve fence" (a linha durável em `workflow_run_fence` é a
    fonte, não a memória do processo), e "não consigo apresentar o fence"
    (evicto, ou store que nunca foi dono) **recusa** com warning nomeando o run.
    Degrada para recusa, nunca para unfenced; fail-closed também quando a
    própria leitura do fence falha. E a eviction (oldest-first) nunca tira o
    fence de um run que o store ainda segura — o mais velho é justamente o run
    LONGO, e um processo que cicla mil runs curtos enquanto ele roda passaria a
    recusar os eventos de auditoria do próprio run vivo.
  - **Cancelar deixou de ser check-e-depois-write** — `mark_cancelled` lia a
    lease numa transação e escrevia `cancelled` em outra: um dono que adquirisse
    o run dentro dessa janela tinha a linha `running` dele substituída por
    `cancelled` enquanto ainda trabalhava (o run lia como parado com processo
    dentro). A condição "ninguém segura uma lease viva" agora viaja no **mesmo
    statement** da escrita (mesmo padrão do `_fenced_write`), e o retorno virou
    tri-estado (`cancelled` | `missing` | `busy`): com lease viva o serviço
    responde `busy` (poll com `workflow_status`) em vez de escrever — o cancel
    de run vivo neste processo continua pelo caminho cooperativo
    (`engine.request_cancel`).
  - **Célula e custo viraram uma escrita só** — eram dois commits com dois
    guards: um dono novo que adquirisse entre eles deixava a célula gravada e o
    custo recusado, e uma célula cacheada sem custo **replaya de graça** no
    resume (o budget não é cobrado por trabalho que o run pagou, e um loop de
    resume gasta acima do teto). Agora `cache_put_with_cost` grava as duas
    linhas na mesma transação, atrás de um guard só: precificada ou ausente.
  - **O que o run já gastou é lido SOB ownership** — `seed_spend`/ceiling eram
    lidos ANTES do acquire, então um resume semeava o budget de uma contabilidade
    que o dono anterior ainda estava fechando. Movidos para depois do acquire; a
    recusa por budget já gasto devolve a lease em vez de segurá-la até o TTL.
  - **Working root por AQUISIÇÃO** (`runs/<run_id>/work-<fence>`) — o fence
    protege o estado SQLite, não o filesystem: os leaves do dono obsoleto
    continuavam escrevendo no `runs/<run_id>/work` COMPARTILHADO que o dono novo
    lia como scratch próprio. Agora cada aquisição nasce num diretório limpo e o
    obsoleto suja só o dele. **Custo nomeado**: scratch não é reaproveitado entre
    stretches — um resume começa com working root vazio (nada depende disso hoje:
    o path é fronteira de sandbox, nenhum prompt/nó/engine entrega o caminho ao
    leaf).
  - **Efeitos colaterais terminais gateados pela escrita cercada** —
    `record_outcome` (que **publica**: template reusável e prior de insight lido
    por toda autoria futura) e a notificação `done` (que steera a sessão dona)
    rodavam incondicionalmente na thread do run: o dono obsoleto acordava e
    sobrescrevia o template que o dono novo tinha acabado de corrigir. Agora só
    executam se a escrita TERMINAL cercada foi **aceita** — o retorno do
    `_persist_state`, sinal que já existia. O evento `done` do live view segue
    sem gate (é a visão local do próprio stretch; não publica nada e não steera
    ninguém).
  - **Fora de escopo, nomeado** (issue #8): nada aborta a *execução* do dono
    obsoleto — ele roda até o fim, apenas não escreve mais nada.

## [0.0.10] — 2026-08-29

### Adicionado
- **Auditoria dos nodes do DAG (épico OBS, Wave 4)** — uma trilha durável,
  **metadata-only**, do que cada nó do workflow realmente fez.
  - `lohra workflow audit <run_id>` — consulta da trilha de um run, JSON no
    stdout, **sem provider e sem tokens** (`--node/--event/--sub-id/--segment-id/
    --attempt/--after-seq/--snapshot-seq/--limit`, limit clampado em 100).
  - Tool `workflow_audit` (agent-facing, interceptada com `SessionDB`),
    **excluída de subagentes/leaves** e gateada pela allow-list do `lohra serve`.
  - Ledger durável por run: identidade causal (`run_id`/`segment_id`/`node_path`/
    `role`/`item`/`stage`/`branch`/`attempt`/`turn`), 20 tipos de evento,
    paginação por `seq` durável com `snapshot_seq` estável.
  - **Esquema aditivo** (padrão `_ADDED_COLUMNS`: base antiga abre e lê NULL):
    coluna `workflow_run_state.audit_segment_id` + 4 tabelas novas
    (`workflow_audit_events/_state/_tombstones/_order`). Conexão SQLite dedicada
    para o sink (`busy_timeout=50`) — sink travado vira `audit.gap` explícito,
    nunca convoy do lock geral.
  - **Política**: nada de conteúdo/prompt/resposta/reasoning/`provider_data` é
    gravado (só marker + tamanho); toda perda é declarada como `audit.gap`/
    `audit.truncated`/`audit.unavailable`. A ordem de eviction conhece liveness
    (uma run pausada em checkpoint não perde o trilho para runs mais novas).
  - **Controles de operador**: `LOHRA_AUDIT=off` desliga a trilha inteira
    (restaura o caminho sem auditoria byte-idêntico) e `LOHRA_AUDIT_MAX_EVENTS`
    levanta o teto por run. Demais limites: fila 256, 2 KiB/evento, 2.048
    eventos/run, 64 runs, retenção 30 dias.

### Corrigido
- **Auditoria: fim de run não inventa mais um `audit.gap`.** A linha terminal e
  a lease saíam ANTES de o `segment.completed` chegar ao ledger; um resume
  dentro dessa janela lia o segmento como perdido e gravava um
  `audit.gap`/`unavailable`/`count=null` **permanente** sem nenhuma falha real.
  Agora o core assenta, o segmento fecha (espera limitada de 1 s pelo sink) e só
  então a linha terminal é publicada e a lease devolvida — um resume que chega
  no meio encontra a run ocupada, nunca um gap fabricado. Um sink que se recusa
  a aceitar mantém o marker, que é a leitura honesta.
- **Auditoria: `LOHRA_AUDIT=off` não deixa rastro.** O marker de segmento era
  persistido mesmo com a trilha desligada e nenhum evento existia para limpá-lo,
  então o primeiro resume feito depois de religar a auditoria nascia declarando
  um gap fantasma.
- **Auditoria: `leaf.started`/`leaf.completed` nomeiam o provider e o modelo**
  que rodaram o node (§2.1) — lido do agent VIVO no instante do frame, não da
  atribuição de custo da sub-sessão (que cai para `None` quando um steer troca
  o modelo). Crítico em leaves cross-provider, onde o leaf pode rodar num
  provider que o orquestrador nem toca. Identidade de configuração, limitada a
  128 chars como qualquer outro identificador; `transport` segue **não
  disponível nesta wave**.
- **Auditoria: allow-list alinhada ao vocabulário real dos produtores.**
  `status="interrupted"`, `reason="lookup_failed"|"store_failed"` e
  `source="human_checkpoint"` viravam `excluded_by_policy` — a trilha perdia o
  desfecho, a causa e a autoria humana. Um teste de contrato varre os
  produtores (scan AST escopado nos call sites) e falha se um valor emitido
  ficar de fora.
- Transport Responses não envia mais `max_output_tokens` — o backend
  Codex/ChatGPT o rejeita com 400 "Unsupported parameter", o que fazia o
  preflight de compactação (AuxClient, `max_tokens=1024`) 400ar **todo** turno
  sob subscription.
- Falha na compactação preflight degrada (segue sem comprimir) em vez de matar o
  turno inteiro antes de o agente ver qualquer coisa — e é tentada **uma vez por
  turno**, não a cada round-trip do tool-loop.

## [0.0.9] — 2026-08-28

### Adicionado
- **Roteamento nos nós de rigor**: `verify`, `judge_panel`, `loop_until_dry`,
  `gate` e `completeness_check` aceitam `model`/`tier`/`effort`/`provider` no nó
  (um configure por nó, aplicado a todos os leaves que ele spawna) — "todos os
  nós no openrouter" deixou de ser inautorável. Cache identity só muda quando um
  knob é declarado (runs antigas seguem replayando).
- **Tiers com superfície de operador**: `lohra tiers suggest` propõe um
  small/medium/big a partir do catálogo REAL (deny-list de modelos não-chat;
  apresenta e confirma — nunca escreve sozinho, nunca prompta fora de TTY) e
  `lohra tiers list` mostra o mapa; `lohra models` exibe o tier map (chave
  `tiers` aditiva no `--json`); `lohra doctor` ganha o remedy.
- **Custo acumulado por sessão**: cada turno soma tokens (com split de cache),
  round-trips e custo real/bruto na linha da sessão (preço do momento, nunca
  recalculado; `cost.partial` quando nem toda chamada teve preço). Envelope
  `--json` ganha o bloco `session`; o chat humano imprime `session total`.
- **Custo por agente/nó: bruto × real, com o cache visível** — a contabilidade de
  tokens passou a ser honesta ponta a ponta.
  - **Uma convenção só (normalização disjunta)**: na fronteira de CADA transport,
    `input_tokens` = tokens de prompt **não** cacheados, em todos os providers. A
    OpenAI (chat_completions e responses) reporta `cached_tokens` como
    SUBCONJUNTO do prompt; agora é subtraído na entrada. A Anthropic já era
    disjunta. Invariante testado por transport, com o fixture no shape real de
    cada API: `input_tokens + cache_read_tokens + cache_write_tokens` == o total
    de prompt do provider. Uma fórmula de custo, não uma por medidor.
  - **Bruto × real**: `CostEstimate` expõe `usd` (real: cada medidor no seu
    preço), `gross_usd` (como-se-sem-cache) e `saved_usd` (a diferença — negativa
    quando o *write* premium dominou, nunca maquiada de desconto). Preço de cache
    ausente no snapshot → cobra input cheio **com nota**, jamais um desconto
    inventado. `reasoning_tokens` NÃO é precificado (é breakdown de output;
    cobrá-lo de novo seria contar duas vezes).
  - **Preço do operador**: `~/.lohra/pricing.json` (por profile, via `lohra_home()`)
    sobrepõe o snapshot datado por modelo e permite modelos fora dele — inclusive
    dar preço a openrouter/ollama. Loader fail-safe no padrão `load_tiers`
    (ausente/lixo/entrada malformada → sem override, nunca levanta). Todo custo
    exibido carrega `source` (`snapshot <data>` | `pricing.json`): defasagem
    nunca é silenciosa.
  - **Propagação pelo workflow**: o split (cache read/write + reasoning) deixa de
    morrer em `_SubSession._finalize` e atravessa `collect()` → `account_leaf` →
    `RunResult` → cache de célula → ledger do run. `workflow_node_cost` e
    `workflow_run_spend` ganharam colunas ADITIVAS e NULLABLE (padrão
    `_ADDED_COLUMNS`): base antiga abre e lê 0. Rollup e `workflow_status`
    mostram **por nó** `X in (Y cached) + Z out` mais o custo real/bruto quando o
    (provider, model) daquele nó tem preço — e o total em dinheiro diz sobre
    QUANTOS nós foi somado.
  - **Budget inalterado** (decisão explícita): segue cobrando input+output (agora
    uniformemente não-cacheado). Cache é coluna de RELATÓRIO, não eixo de
    orçamento.
  - `lohra chat` humano fecha o turno com a linha de custo quando há preço; o
    envelope `--json` estende o campo `cost` (compat: `usd`/`basis` seguem lá).
  - `lohra serve`: o endpoint `/v1/responses` parou de hardcodar
    `cached_tokens: 0` / `cache_write_tokens: 0` — repassa o usage real,
    **re-inclusivo** na saída (a wire shape da OpenAI conta cached DENTRO de
    `prompt_tokens`), e passou a somar o turno inteiro em vez da última chamada.
  - **Ocupação da janela ≠ tokens cobrados**: o preflight de compactação lê a
    ocupação real (`input + cache_read + cache_write + output`), não só a fatia
    não-cacheada. Sob a convenção disjunta ler `input_tokens` sozinho invertia o
    sinal — quanto mais longo o histórico, mais dele a OpenAI cacheia, MENOR o
    número — e da 2ª iteração de um turno agêntico em diante a compactação
    simplesmente não disparava.
  - **Especificidade entre tabelas**: o preço mais específico vence, venha do
    snapshot ou do `pricing.json`. Um override de prefixo curto (`gpt-5`) não
    reprecifica um modelo que o snapshot conhece exatamente (`gpt-5.6-sol`); um
    override na mesma chave continua ganhando, e agora alcança também o modelo
    API-equivalente de uma subscription. `basis` (a NATUREZA da cobrança:
    `api_equivalent` de uma assinatura não é conta por token) deixou de ser
    apagado por um override — override muda o `source`, nunca o `basis`. (Um
    override em `ollama` agora também mantém `basis: local`: o preço veio do
    operador, a natureza da cobrança — não há conta de API — não mudou.)
  - **Proveniência no total, não só por nó**: o custo agregado do run carrega
    `sources`/`bases` (um run cross-provider pode somar list price real com
    dinheiro notional de assinatura) e `scope` — o dinheiro é do STRETCH atual,
    ao lado de um `tokens_spent_total` cumulativo; célula replayada do cache não
    reentra em `node_costs`.
  - **Atribuição fail-closed por sub-sessão**: uma sub-sessão retomada
    (`delegate_task` com `resume_id`) sob outro modelo acumula os tokens dos dois
    turnos; o `(provider, model)` agora é retido (None) em vez de reatribuído ao
    último — tokens continuam reportados, o dinheiro do modelo errado não.
- **Roteamento de modelo nos 5 nós de rigor** — `verify`, `judge_panel`,
  `loop_until_dry`, `gate` e `completeness_check` aceitam `model`/`tier`/`effort`/
  `provider` no nível do nó (antes só `agent` aceitava, e o rigor sempre caía no
  modelo da sessão — "rodar este DAG inteiro no openrouter" era inautorável). Uma
  resolução por NÓ, aplicada a TODOS os leaves que ele spawna (os skeptics; os
  attempts, os judges e a síntese; cada rodada do loop; o draft e o reviewer do
  gate). Modelos diferentes por GRUPO dentro de um nó seguem não suportados;
  `parallel` e os `stages` de pipeline continuam sem knobs de roteamento. Cache
  persistida preservada: a resolução só entra na identidade da célula quando o nó
  declara algum dos 4 campos, então resume de run antiga continua acertando.
- Providers diretos **xai** (Grok, alias `grok`), **glm** (Zhipu/Z.ai, aliases
  `zhipu`/`zai`) e **kimi** (Moonshot, alias `moonshot`) — OpenAI-compat; catálogo,
  chat e roteamento por nó de DAG funcionam automaticamente. 8 → 11 providers
  builtin. Endpoints, env vars e fallbacks validados contra as docs oficiais
  (pesquisa online 2026-08-28): grok-4.6/4.3, glm-5.3/5.3-flash (host
  internacional api.z.ai), kimi-k3/k2.6.

### Alterado
- **Os números por turno vão mudar — são mais honestos, não uma regressão.** Duas
  correções em direções opostas: (a) sub-sessões/leaves passam a contabilizar
  `usage_total` (TODAS as chamadas do turno) em vez de só a última, então o
  acúmulo AUMENTA num turno com tool round-trips; (b) `input_tokens` em providers
  OpenAI-compat passa a excluir o que veio do cache, então o input cacheado
  DIMINUI (e reaparece em `cache_read_tokens`). Somados, os medidores continuam
  batendo com o total do provider.
- **Um `token_budget` já configurado compra MAIS trabalho em providers
  OpenAI-compat.** O budget continua estruturalmente inalterado (decisão travada:
  dois eixos, `input + output`) — mas o `input` que ele cobra agora é só a fatia
  não-cacheada, e `est_leaf_cost` (a média medida do próprio run, que gateia o
  fan-out estimado) encolhe junto. Não é só o eixo de RELATO que mudou: um teto
  de segurança ajustado à mão pelo operador antes desta versão ficou mais
  permissivo, e pode querer ser reapertado.
- A suíte passou a rodar contra um `$LOHRA_HOME` privado por teste. Antes, três
  testes de custo liam o `~/.lohra/pricing.json` REAL — quem usasse a feature que
  esta fatia entrega via a suíte quebrar na própria máquina.

### Pendências nomeadas (fora do escopo desta fatia)
- `AuxClient.complete()` ainda descarta `response.usage`: compactação e geração
  de título consomem tokens do aux model sem entrar em contabilidade nenhuma.
- `cache_control` segue sem ser setado em lugar nenhum — o prompt caching da
  Anthropic é opt-in, então `cache_read_tokens` provavelmente é sempre 0 ali
  hoje (o Invariante #1 é pré-requisito, não suficiente).
- As colunas fantasma de `sessions` (`cache_read_tokens`, `estimated_cost_usd`,
  `actual_cost_usd`, …) continuam sem nenhum escritor.
- O rollup cross-process (`durable_rollup`, run de outro processo) reporta o
  total persistido, mas ainda não o custo POR NÓ — precisaria do JOIN
  `workflow_node_cost` × `workflow_node_cache` pelo `content_hash`. Uma resume no
  MESMO processo tem a limitação gêmea: a célula replayada retorna antes do
  `account_leaf`, então nunca entra em `node_costs`. Nesta fatia isso está
  ROTULADO (`cost.scope`), não resolvido — o JOIN fecharia os dois de uma vez.

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
