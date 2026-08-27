# Backlog — Workflow Harness: paridade operacional com dynamic workflows

> Origem: avaliação de prior art `pi-dynamic-workflows` (QuintinShaw, MIT, TS) em 2026-08-25,
> cruzada com inventário código-nível da Lohra. Regra de corte: só entrou item que fecha um
> **bug, dívida nomeada ou risco não-verificado** — não "eles têm e nós não".
> A arquitetura declarativa (spec inerte, sem executar código autorado) **não muda** — o próprio
> pi admite que `vm` "is not a security sandbox"; a Lohra já é superior em substrato, cache
> content-addressed, sandbox de leaf e `forcing_fallbacks`.

## Tier 1 — bug/dívida (mexer primeiro)

### WF-1 · Exaustão de quota vira cascata de `null` e envenena o loop J
- **Problema:** os SDKs absorvem 429 transitório (2 retries default, `lohra/agent/client.py:150,198,247`),
  mas exaustão sustentada de janela (caso Fase 10/subscription) fura: erro → `orchestration/core.py:265`
  `status="error"` → nó `null` (`workflow/engine.py:257`) → `null_rate` sobe → `workflow/library.py:58-61`
  grava prior em `insights.md` culpando a *forma* do workflow por um problema de quota.
- **Prior art:** `errors.ts:140-149` + `agent.ts:104-114` (classifica pelo stop-reason, nunca pelo texto),
  `workflow-manager.ts:994-999` (pausa o run inteiro), `usage-limit-scheduler.ts` (delay = resetHint − elapsed,
  backoff exp., min 60s / max 6h / 5 tentativas, cold-start rearm por `updatedAt`).
- **Aceite:** taxonomia `quota_exhausted` distinta de erro comum; run vai a `paused` (não completa com nulls);
  `record_outcome` pulado em pause-por-quota (como já ocorre em cancel, `service.py:117-124`);
  auto-resume via `resume_run_id` (a metade cara — cache content-addressed — já existe).

### WF-2 · Leaf zumbi no timeout
- **Problema:** `LEAF_TIMEOUT = 120.0` fixo (`workflow/strategies.py:29`); no timeout `engine.py:256`
  só desiste de esperar — não chama `core.cancel(sub_id)`, não retenta. Leaf segue rodando e segura
  1 dos 4 workers do pool.
- **Prior art:** `workflow.ts:1713-1745` — aborta ANTES do timeout vencer a corrida e espera o promise
  perdedor assentar antes de retentar (fix #109: retries empilhando sessões vivas).
- **Nota do review adversarial:** severidade rebaixada a MEDIUM — os SDKs têm read-timeout de 600s
  (defaults verificados ao vivo), então o worker fica preso por minutos, não para sempre; ainda vale
  o hardening cancel-on-timeout.
- **Aceite:** timeout → `core.cancel` + status honesto; `timeout`/`retries` configuráveis por nó;
  teste: leaf lento não ocupa worker após timeout.

### WF-3 · Token budget prometido e nunca entregue (Milestone F)
- **Problema:** `workflow/budget.py:1-6` diz "the token budget … land in Milestone F" — nunca chegou.
  `Budget` só conta spawns; tokens são agregados post-hoc e não gateiam nada.
- **Prior art:** gate soft pré-spawn (`workflow.ts:645-649`), sub-budget por fase (`507-522`),
  acúmulo através de pause/resume (`474-480` — pausar não zera o gasto).
- **Aceite:** campo `token_budget` no `run_workflow`; check pré-spawn (soft: em-voo pode estourar,
  contrato Claude Code); estouro → `paused`/fault explícito, nunca cap silencioso; cumulativo no resume.

### WF-4 · `loop_until_dry` confunde falha com ausência
- **Problema:** `strategies.py:180` — `output in (None, "", [], {})`: leaf MORTO (`None`) conta como
  rodada seca e encerra o loop cedo. `[]` = "não achei nada"; `None` = "não consegui rodar".
- **Aceite:** `None` não incrementa `empty_streak` (conta como falha, com retry bounded ou fault);
  teste discriminador dos dois casos.

## Tier 2 — custo/benefício bom

### WF-5 · Tiers de modelo + templates portáveis
- Templates da library gravam `model: <slug>` literal → não portáveis entre profiles/providers.
- Prior art: `small`/`medium`/`big` (`model-tier-config.ts`), auto-rank dos modelos autenticados.
- Aceite: nó aceita `tier`; resolução em runtime; template salvo com tier, não slug.

### WF-6 · Gate semântico (retry com feedback de conteúdo)
- Hoje o steer de correção cobre só FORMA (JSON schema, `engine.py:262-270`).
- Prior art: `gate(thunk, validator)` (`workflow.ts:1276-1293`) — feedback do validador realimenta
  a próxima tentativa. Encaixa como node-type declarativo: `body` + `validator` (leaf) + `attempts`.

### WF-7 · Saída vazia = falha recuperável
- Prior art: `AGENT_EMPTY_OUTPUT` recuperável (`agent.ts:1078-1084`) — modelos batem nisso em 1º attempt.
- Aceite: leaf com saída whitespace-only re-spawna (bounded) em vez de virar `null`.

### WF-8 · `judge_panel`: preflight do fan-out completo
- `strategies.py:149` faz `check_fanout(judges)` DENTRO do loop → pode estourar no meio com attempts
  já pagos; leaf de `synthesize` não é checado. Preflight: `attempts + attempts×judges + 1` antes de spawnar.

## Tier 3 — bom conceito, caro agora

- **WF-9** · Teste de contrato: `NODE_SPECS ≡ STRATEGIES ≡ docs` (mata o branch morto `engine.py:298-305`).
- **WF-10** · `checkpoint` humano journaled (gate de aprovação dentro do DAG, cacheável no resume).
- **WF-11** · Isolamento por git-worktree por leaf — só quando leaves editarem repo de verdade
  (hoje todos compartilham `runs/<id>/work`, `service.py:91` — dois leaves no mesmo arquivo colidem).

---

## Achados do review multi-agente (2026-08-25) — confirmados por verificação adversarial

> Fonte: `docs/reviews/2026-08-25-workflow-harness-review.md` (15 agentes; dimensão resume/cache
> ficou SEM cobertura — finder morreu em erro de API). Suíte verde (959 passed), mas os 6 confirmados
> vivem em caminhos de degradação sem teste.

### Tier 1 (entram na frente do WF-2/WF-3)

- **WF-12 · HIGH — container `${ref}` não resolvido em parallel/judge_panel** (`strategies.py:99,140`):
  `branches`/`attempts` com `${ref}` viram `[]` silencioso → run "complete", null_rate 0, e a library
  certifica o workflow quebrado como template. A mensagem do validador (`schema.py:202-203`) ainda
  ensina o autor a usar exatamente esse `${ref}`. Fix: `refs.resolve_value` no container (como o
  pipeline já faz em `strategies.py:201`); não-lista → fault.
- **WF-13 · HIGH — exceção no on_done do pipeline é engolida** (`core.py:282-285`): `finish()` nunca
  roda, `done.wait` segura a thread wf-run por 1800s e o estouro não vira fault. Fix: try/except no
  on_done → `finish(i, None)` + fault; fault no timeout do `done.wait`; flag `expired` sob lock
  descartando retardatários (fecha também o lost-update de `strategies.py:275` × `engine.py:267`).
- **WF-14 · MED-HIGH — cascata de null vira a string "None"/"null" no prompt downstream**
  (`refs.py:68`, `engine.py:215`): resposta confiante do leaf é cacheada como completion e a cascata
  não incrementa null_count. `required`/`min_success_ratio` (spec §7.4) são aceitos pelo parser e
  nunca lidos. Fix: ref → None detectado antes do spawn nulla o nó (ou registra fault).
- **WF-15 · MED — erro de provider descartado** (`engine.py:257` ← `core.py:295-297`): a causa
  (auth/429) não chega a rollup/prior; run 100% morta = "complete" com faults vazio. Fix: propagar
  `sub.output` como fault; derivar "degraded" de null_count>0. (Pré-requisito natural do WF-1.)
- **WF-16 · MED — exemplos didáticos nunca chegam ao autor** (`schema.py:44`): `SpecIssue.example`
  existe mas `ValidationError.message` não o inclui — quebra a promessa "corrected example, so the
  agent can fix its own spec". Fix: formatar `i.example` na mensagem + RUN_GUIDANCE enumerar os
  7 node-types (`tools.py:17` hoje omite pipeline/workflow).

### Rodada final do review (15/15 — dimensão resume/cache incluída)

- **WF-17 · CRITICAL — resume de run AINDA VIVA clobbera o RunState** (`service.py:108`):
  `self._runs[run_id] = state` sem checar liveness — run original fica órfã/incancelável e dois
  engines dividem o mesmo `working_root` e as mesmas linhas do `workflow_node_cache`.
  Fix: em `start()`, sob o lock, rejeitar `resume_run_id` cujo estado não seja terminal.
- **WF-18 · HIGH — `verify` falha ABERTO** (`strategies.py:124`): todos os skeptics mortos ⇒
  `counted=0` ⇒ `survived=True` sem fault — o gate de rigor aprova exatamente sob o modo de falha
  (429 sustentado) que ele existe para pegar. Fix: `counted == 0` (ou < quorum) ⇒ fail-closed + fault.
- **WF-19 · HIGH — cancel não cancela** (`core.py:214`): interrupt só cooperativo (não acorda LLM
  em voo), `shutdown(wait=True)` bloqueia a thread do pai, steers pendentes relançam turno pós-cancel,
  engine segue criando sessões órfãs pós-shutdown. Fix: flag de cancel checada por nó +
  `shutdown(cancel_futures=True)` assíncrono + checar interrupt antes de consumir steers. (→ M4)
- **WF-20 · MEDIUM — cache key do pipeline omite a identidade do item** (`strategies.py:229`):
  stage cujo prompt não interpola `${item}` colapsa N itens numa célula; no resume todos os itens
  recebem o output de um só. Fix: incluir `items[index]` cru no hash da célula.
- **Recalibragem WF-2:** verificação final subiu de volta a **HIGH** — worker zumbi + `shutdown(wait=True)`
  bloqueando a thread de tool do pai via `workflow_cancel`; critical apenas no caso stream-gotejante.

### Suspeitas não-verificadas (triar antes de virar item)

`schema.py:170` (roots de contexto não reservados como node ids) · `strategies.py:164,311` (schemas
de synthesize/stages fora do resolve_schema/validador; stages prompt-only) · `engine.py:294` (campos
aceitos e mortos; "failed" inalcançável) · `core.py:233,261` (eviction × on_done; janela de steer)
· `service.py:109,121` (resume de run viva clobbera estado; race cancel × prior) · `strategies.py:171`
(max_rounds sem teto bypassa lifetime).

---

## Achados do dogfood ao vivo (2026-08-26, run 0a9afc35 — a própria Lohra avaliando M5-M7)

> A Lohra headless autorou e rodou um workflow avaliando as propostas pendentes. Além de convergir
> com o backlog existente (WF-5/8/9/10 redescobertos independentemente — validação), trouxe dois
> pré-requisitos de implementação NOVOS, verificados no código:

- **M5-a · cache não persiste custo por célula** (`cache.py:43-46` — só `output_json`): um token
  budget cumulativo no resume não consegue contabilizar células já pagas. Persistir tokens_in/out
  na linha do cache junto do output.
- **M5-b · Budget zerado a cada start** (`service.py:136` — resume incluído): o gasto não atravessa
  o resume; carregar o acumulado do run anterior ao re-armar (mesmo racional do pi: pausar não
  zera o spend).
- **M6-a · nota de implementação**: progresso mid-run exige snapshots publicados do engine para o
  service (`RunResult` é interno até o fim — `engine.py:374-385`); não é só expor campo novo na tool.
- **WF-21 · `fs_allow` não distingue leitura de escrita** (`sandbox.py:40-42`, achado NOSSO no
  dogfood): a policy que permite leaves LEREM um repo também permite ESCREVEREM nele. Entradas
  read-only (ex.: `{"path": ..., "mode": "ro"}`) ou split `fs_read_allow`/`fs_write_allow`.

---

## Achados do dogfood do M6 (2026-08-26, run `d4cf109c` — assistindo o próprio run)

> Todas as features do M6 funcionaram ao vivo (snapshots mid-run, itens de pipeline settled/total,
> list com spend parcial, pause manual honesto — "pausing" → em-voo pousou e foi cobrado → `paused`
> com hint, resume preservando células). Três achados de ergonomia:

- **WF-22 · resume exige re-passar o spec** — `run_workflow(resume_run_id=...)` sem `spec` é
  recusado, embora o `RunState` guarde `spec_dict` desde o M4 (o auto-resume de quota já o usa).
  O agente naturalmente tentou só com o run_id (call 14 do envelope) e o erro didático o salvou,
  mas é fricção pura. Fix: `spec` opcional quando `resume_run_id` presente → usa o persistido.
- **WF-23 · run retomado subreporta o custo total no fechamento** — status final/rollup mostram só
  o ÚLTIMO segmento (`tokens_in: 1218/861` ≈ 2k) enquanto o ledger `workflow_run_spend` tem a
  verdade (28.047+2.681 = 30.7k). Sem `token_budget` setado, nenhum bloco cumulativo aparece.
  Afeta também o que `record_outcome` grava. Fix: status/rollup terminal mesclam o ledger
  (`spent` cumulativo primeiro-classe, com ou sem budget).
- **Nota (comportamento do modelo, não bug do harness):** 2 specs recusados antes do aceito —
  o modelo emitiu meta+schemas e NUNCA o `nodes` (schemas verbosos provavelmente estouraram o
  orçamento do tool-call). Recuperou consultando um template. Mitigação barata na skill: "schemas
  enxutos; prefira schema_ref; o spec inteiro precisa caber na chamada".
