# Roadmap — Lohra rumo à paridade com os dynamic workflows do Claude Code

> Referência: a superfície REAL dos dynamic workflows do Claude Code (gabarito de 15 capacidades
> usado no review de 2026-08-25, `docs/reviews/2026-08-25-workflow-harness-review.md`).
> Scorecard atual: **7 yes · 7 partial · 1 no**. O pi-dynamic-workflows é prior art secundário.
>
> **Princípio-guia** (o contrato operacional que faz dynamic workflows funcionarem no Claude Code):
> 1. **Nunca degradar silenciosamente** — caps logados, fallbacks sinalizados, falha vira fault com causa.
> 2. **Nunca perder trabalho pago** — journal + resume reusam tudo que completou.
> 3. **Sempre poder olhar** — progresso vivo, status honesto mid-run, journal inspecionável.
>
> A Lohra já vence em segurança (spec inerte + sandbox leaf) e cache (content-addressed).
> O roadmap fecha o resto SEM regredir nisso.

Convenção: cada milestone é uma fatia commitável com TDD; review adversarial antes do commit
(ultracode). Itens WF-* referem-se ao `docs/BACKLOG.md`.

---

## M1 — "Nenhuma falha silenciosa" (correção · confirmados HIGH/MED)
Fecha os caminhos onde o harness mente sobre o próprio estado.
- [x] **WF-12** `${ref}` em `branches`/`attempts` resolvido via `refs.resolve_value`
      (como o pipeline já faz); não-lista → fault, nunca `[]` silencioso. (`strategies.py:99,140`)
- [x] **WF-14** ref que resolve para `None` NÃO vira a string "None"/"null" no prompt:
      nó downstream nulla com fault (`upstream null`). Ativar `required` (spec §7.4) que o parser
      aceita e ninguém lê. (`refs.py:68`, `engine.py:215,294`)
- [x] **WF-15** erro de provider propagado: `sub.output` (causa) vira fault no `RunResult`;
      status `degraded` quando `null_count>0` e `complete` só quando limpo. (`engine.py:257`)
- [x] **WF-4** `loop_until_dry`: `None` (leaf morto) ≠ `[]` (seco) — falha não incrementa
      `empty_streak`. (`strategies.py:180`)
- [x] **WF-18** `verify` fail-closed: skeptics todos mortos (ou < quorum) ⇒ não-sobrevive + fault.
      (`strategies.py:124`)
- Aceite global: run 100% morta NUNCA reporta `complete`; `library.py` não certifica template
  nem grava prior sem causa correta.

## M2 — Done-path do pipeline (correção · confirmados HIGH/MED)
- [x] **WF-13** try/except no `on_done` → `finish(i, None)` + fault (exceção nunca engolida
      deixando `done.wait` pendurado 1800s). (`core.py:282-285`)
- [x] Estouro do `done.wait(PIPELINE_TIMEOUT)` → fault explícito (não só warning).
- [x] Flag `expired` sob lock: retardatários pós-timeout não mutam `results` publicado
      (lost-update `strategies.py:275` × `engine.py:267`); devolver cópia.
- [x] **WF-17 (CRITICAL)** guard de liveness no `resume_run_id`: rejeitar resume de run não-terminal.
      (`service.py:108`)
- [x] **WF-20** cache key do pipeline inclui a identidade do item. (`strategies.py:229`)

## M3 — Superfície de autoria (o agente acerta o spec de primeira)
No Claude Code, a descrição da tool ensina os padrões e o erro de validação corrige o autor.
- [x] **WF-16** `SpecIssue.example` formatado no `ValidationError.message` (a promessa do
      docstring). (`schema.py:44`)
- [x] RUN_GUIDANCE enumera os **7** node-types com mini-exemplo (hoje omite pipeline/workflow)
      + nudge explícito de `workflow_templates`. (`tools.py:17`)
- [ ] Triar suspeitas de autoria: roots de contexto reserváveis como node ids (`schema.py:170`),
      schemas de synthesize/stages fora do validador (`strategies.py:164,311`).

## M3.5 — Skill `workflow-authoring` — FEITO
Gap identificado comparando com as instruções que o próprio Claude Code recebe: a tier-1 (RUN_GUIDANCE)
ensina sintaxe, mas ninguém ensina QUANDO usar cada nó, dimensionamento e armadilhas. A Lohra já tem o
substrato (SkillStore + progressive disclosure + index no prompt congelado) — falta o conteúdo.
- [x] Skill builtin `workflow-authoring` empacotada no repo, plugada via `extra_roots` com precedência
      MÍNIMA (projeto/home sobrescrevem); corpo lido sob demanda (Invariante #1 intacto).
- [x] Conteúdo: seleção de padrão por forma de tarefa · smell test de barreira (pipeline vs parallel)
      · dimensionamento · lifecycle completo (run → status → paused/quota → resume_run_id) ·
      anti-padrões (dedup-vs-seen, schema em leaf shape-crítico, ler faults/degraded) · 3-4 specs
      adaptáveis.
- [x] Anti-drift barato (lição do pi sem a máquina toda): teste de contrato — skill+RUN_GUIDANCE cobrem
      os 7 node-types; todo spec-exemplo da skill passa `validate_spec` round-trip (fecha WF-9 junto).
- [x] RUN_GUIDANCE: +2 linhas (mencionar `resume_run_id`; apontar a skill). Julgamento fica on-demand.
- **Sequência:** implementar APÓS o M4 — a skill documenta a superfície pós-M4 (timeout/retries por nó,
  paused, auto-resume).

## M4 — Resiliência de quota + timeout (capability #11) — FEITO
- [x] **WF-1** taxonomia `quota_exhausted` (classificar pelo erro do SDK, nunca por texto de
      prosa); run → `paused` (não cascata de null); `record_outcome` pulado (como cancel).
- [x] Auto-resume: scheduler simples (delay do retry-after ou backoff exp., min 60s/max 6h,
      N tentativas) reusando `resume_run_id` — a metade cara (cache) já existe.
- [x] **WF-2** (HIGH, recalibrado) cancel-on-timeout do leaf + `timeout`/`retries` por nó.
- [x] **WF-19** cancel real: flag por nó no engine + `shutdown(cancel_futures=True)` assíncrono +
      interrupt antes de consumir steers. (`core.py:214`)
- [x] **WF-7** saída vazia = falha recuperável com re-spawn bounded.

## M5 — Token budget real — FEITO
- [x] `token_budget` no `run_workflow`; gate soft pré-spawn (em-voo pode estourar — contrato CC).
- [x] Estouro → `paused` + fault, nunca cap silencioso; cumulativo no resume.
- [x] Pré-requisitos achados pelo dogfood (backlog M5-a/M5-b): persistir custo por célula no cache
      (`cache.py:43-46` só guarda output) e carregar o acumulado no re-arm (`service.py:136` zera).
- [x] Expor `{total, spent, remaining}` no `workflow_status`.

## M6 — Operabilidade — FEITO
- [x] `workflow_status` mid-run com progresso real (nós done/running/pending, tokens até agora)
      — hoje devolve rollup vazio antes do fim (`rollup.py:21-22`); exige snapshots engine→service
      (M6-a do backlog: `RunResult` é interno até o run acabar).
- [x] `workflow_list` + `workflow_pause` (pause = não spawnar novos nós; leaves em voo terminam).
- [x] Notificação de conclusão no turno do agente (evento no gateway/CLI).

## M7 — Ergonomia estendida — FEITO (WF-11 adiado)
- [x] **WF-5** tiers de modelo (`small`/`medium`/`big`) + templates portáveis (sem slug travado).
- [x] **WF-6** node `gate` (retry semântico com feedback de validador-leaf).
- [x] **WF-8** preflight completo do fan-out no `judge_panel`.
- [x] **WF-9** teste de contrato `NODE_SPECS ≡ STRATEGIES ≡ docs`.
- [x] **WF-10** `checkpoint` humano journaled (node próprio: pausa reason checkpoint + resume com
      `checkpoint_answers`, resposta cacheada).
- [ ] **WF-11** worktree por leaf — **ADIADO deliberadamente**: leaves são read-mostly hoje e o
      ro/rw do WF-21 cobre o risco imediato; volta ao jogo quando workflows editarem repo de verdade.
- [x] Nó `completeness_check` nomeado (hoje só via agent genérico).
- [x] **WF-21** `fs_allow` read-only vs read-write na policy do sandbox (achado do dogfood).

---

## Status
- 2026-08-25 · review multi-agente concluído (15 agentes; resume/cache re-rodada à parte);
  suíte 959 passed; branch de trabalho `feat/workflow-cc-parity`.
- 2026-08-26 · **M1+M2+M3 implementados** (workflow ultracode: scout sonnet → 3 implementadores
  opus TDD → 2 revisores sonnet → fixer → gate). Review pegou 1 gap real (loop_until_dry fora do
  WF-14) e foi corrigido. Suíte **993 passed**, ruff limpo, 92% cobertura. Commits `404c19f` (M1+M2)
  e `e251d7d` (M3). Restam M4 (quota/cancel), M5 (token budget), M6 (operabilidade), M7 (ergonomia)
  e a triagem das suspeitas não-verificadas do review.
- 2026-08-26 · **M4 implementado** (workflow: scout sonnet → 2 opus TDD sequenciais → 2 sonnet review
  → fixer → gate). Review pegou CRITICAL de fiação (request_cancel morto no caminho real) — a mesma
  classe de bug da rodada M1-M3, pega pelo mesmo mecanismo. Suíte **1050 passed**, 93% cobertura.
  A capability #11 (rate-limit → pausa) sai de "no". Falta M3.5 (skill), M5, M6, M7.
- 2026-08-26 · **M3.5 implementado** (opus + 2 review sonnet, zero findings, gate GREEN). Skill builtin
  404 linhas + tier builtin (projeto > home > builtin) + COW no update + 14 testes de contrato
  anti-drift. Suíte **1064 passed**. Não-verificado nomeado: caminho congelado (PyInstaller) do
  builtin_root. Falta M5 (token budget), M6 (operabilidade), M7 (ergonomia).
- 2026-08-26 · **M5 implementado** (scout sonnet → opus TDD → 2 review sonnet → fixer → gate GREEN).
  Review rendeu: CRITICAL (nós-barreira spawnam antes da cobrança → gate_fanout estimado) + 2 HIGH
  (parciais pagos preservados no estouro). Bônus: {total,spent,remaining} já aparece MID-RUN (adianta
  parte do M6). Suíte **1102 passed**. Falta M6 (operabilidade) e M7 (ergonomia).
- 2026-08-26 · **Dogfood do M5 ao vivo** (run `64eb6771`, watcher SQLite em tempo real): budget 30k →
  barreira paga 47.389 → consolidador recusado → `paused` (`resume_at: null`, sem auto-resume) →
  tentativa com o mesmo valor levou a recusa raise-only COM sugestão ("e.g. token_budget: 94778") →
  agente retomou com exatamente o sugerido → 3 células pagas replayaram do cache (2º trecho: só
  7.595) → `complete`, spend cumulativo 54.984. Residual documentado confirmado: barreira estoura
  antes de pausar (estimativa 2k/leaf vs ~16k reais — em-voo pago não é morto). Bônus: agente reusou
  o template do dogfood anterior; no run de 50k a análise citou o overrun do próprio run como evidência.
- 2026-08-26 · **M6 implementado** (670c746; suíte **1135 passed**). Progresso mid-run por nó (engine
  vivo), workflow_list/workflow_pause (3º reason do PauseSignal), notificação via inbox de steer.
  Review pegou pela 4ª vez o construído-mas-não-fiado (dashboard com session_id=None → owner morto);
  fix mudou o contrato do AgentFactory (recebe session_id) e de quebra materializou o parent linkage
  adiado da Fase 7. Falta só M7 (ergonomia + WF-21).
- 2026-08-26 · **M7 implementado — ROADMAP M1→M7 COMPLETO** (d930368; suíte **1218 passed**, 93%).
  Node-types 7→10 (gate, completeness_check, checkpoint); tiers de operador; WF-8/9/21/22/23
  fechados; WF-11 adiado com racional. Review pegou sutileza fina: resposta humana ''/null de
  checkpoint engolida pelo gate de empty-output do WF-7 (cache_answer ≠ cache_store).
  Único não-verificado remanescente nomeado: builtin_root sob PyInstaller (freeze não re-buildado).
