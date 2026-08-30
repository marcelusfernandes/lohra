# SUP-05 (#31) — memória causal e continuidade entre processos

**Data:** 2026-08-30
**Branch:** `feat/lohra-epic-sup`
**Baseline:** `7a92d1f` (docs SUP-04) · **HEAD:** `115f5a5`
**Commits da issue:** `12a1a1c` (taxonomia + insight store), `a902d31` (notice store + overlay no loop), `4a77329` (entrega entre turnos + canal durável do notifier), `3a5679b` (spec inválida + recovery notice), `a8525c4` (cap de notices protege lease ativo), `39a95a0` (taxonomia fail-closed em causalidade ambígua), `7e1e3fb` (aposenta o aprendizado legado não-gateado), `9ac5b9e` (proveniência só de spec explícita), `29b9049` (notifier entrega o fato uma única vez), `cb5d745` (relê pós-fence dos fatos de recovery + abort limpo se a persistência cercada for recusada — `workflow/service.py` + `tests/test_workflow_recovery_fencing.py`), `d1bf6d9` (E2E real da cadeia de recovery com três subprocessos de SO), `115f5a5` (estabiliza as corridas do teste de fencing). Todos commitados; nada staged.
**Classificação final:** **REFORMULADA** — a amnésia foi confirmada; "fault = aprendizado" foi refutado; o aprendizado legado por texto livre foi APOSENTADO (não consertado); o que foi entregue é um canal durável bounded de notices, um store de candidates SQLite com gate causal estreito (apenas spec explicitamente enviada e invalidada) e o template library para runs limpos.

## Pergunta

Como preservar um erro de turno ou workflow entre processos sem contaminar o aprendizado da Lohra com falhas de infraestrutura/ambiente e sem alterar o system prompt congelado?

A propriedade de segurança herdada de SUP-01 é: a Lohra só aprende como escolha própria o que é causalmente atribuível a uma decisão dela. Fatos operacionais podem ser persistidos e entregues, mas não viram procedimento por isso.

## Baseline revalidado

Estado inicial: `7a92d1f`.

1. `WorkflowEngine.record_fault()` grava prosa em `RunResult.faults`, live view e audit. Não carrega responsabilidade causal.
2. `library.record_outcome()` rodava no fim de runs não pausados/não cancelados e, para runs problemáticos, fazia read-modify-write de `insights.md` com `threading.Lock`, dedup por linha inteira e cap de 200 linhas.
3. `GatewaySession` e CLI descartam corretamente o transcript de turno com erro/interrupção para não persistir uma alternância inválida; preservam só custo. Nenhum fato do erro chega ao próximo turno.
4. A inbox de steer é memória do processo, sem TTL/cap durável. A notificação de workflow tinha só o canal live process-local (`enqueue_steer`) e o rollup para poll — nenhum canal durável: após um crash, a conclusão do run se perde.
5. Runs têm estado, lease e owner duráveis. Um `running` sem lease vivo é detectável como órfão, mas só quando alguém consulta a superfície de workflow (e, para virar recovery notice, só quando alguém resume o run).
6. `Agent.system_prompt()` congela um snapshot por instância. A inbox entra nas mensagens de cauda, não no snapshot.
7. `classify_provider_error()` reconhece quota estruturalmente (`error_kind` no result do turno). Os demais erros não têm uma taxonomia causal; texto livre seria o único sinal depois que a exceção atravessa o loop.

### Experimento B1 — turno morto some

Script local com `GatewaySession`, DB em memória e client determinístico: primeira chamada levanta `RuntimeError("injected provider failure")`; segunda completa.

Resultado:

```text
after_error_persisted_messages=0
second_call_roles=['user']
second_call_contents=['second request']
has_error_context=False
```

O descarte do transcript está correto; a perda do fato operacional está confirmada.

### Experimento B2 — lock de thread perde writers entre processos

16 processos sincronizados escreveram priors distintos via `_record_prior()` no mesmo `insights.md`, cinco vezes.

```text
expected=16 actual=6 lost=10
expected=16 actual=7 lost=9
expected=16 actual=6 lost=10
expected=16 actual=3 lost=13
expected=16 actual=1 lost=15
```

Todos os filhos terminaram com exit code 0. O mecanismo textual não é cross-process — e em vez de ser consertado, foi aposentado (abaixo).

### Resultado negativo preservado — experimento inválido

A primeira versão do script multiprocessing não tinha `if __name__ == "__main__"` sob o start method `spawn` do macOS. Todos os filhos falharam no bootstrap e o pai imprimiu falsos `actual=0`. Esse resultado foi descartado, não usado como evidência, e o experimento foi repetido corretamente acima.

## Hipóteses e falsificação

| Hipótese | Condição de falsificação | Resultado |
|---|---|---|
| H1. Todo fault pode virar insight no instante em que ocorre. | Um fault real não ser atribuível a uma escolha da agente. | **Refutada.** Quota, credencial, transporte, audit SQLite, processo perdido e 5xx são contraexemplos. |
| H2. Tipo/status HTTP separa infra, ambiente e agência. | O mesmo tipo/status admitir causas de classes distintas. | **Refutada e provada por contrato** (`test_workflow_failure_taxonomy.py`: `_same_status_and_mechanism_split_between_agency_and_environment`, `_same_timeout_mechanism_splits_between_environment_and_infrastructure`) — o mesmo (mecanismo, status) cai em lados opostos com evidência distinta. |
| H3. O arquivo textual atual pode ser tornado seguro só com o lock existente. | Writers cross-process perderem updates. | **Refutada por B2.** Resposta: o mecanismo foi aposentado, não consertado (`7e1e3fb`). |
| H4. A inbox atual basta para continuidade. | Reinício perder inbox ou uma abertura gerar cauda persistida/alternância imprópria. | **Refutada.** É process-local e settle ocorre antes do commit do turno. |
| H5. Notices operacionais e aprendizado podem compartilhar store/política. | Um fato necessário para continuidade não ser learnable. | **Refutada.** Dois stores separados na implementação: a notice nunca é classificada como insight (`build_turn_notice` é operacional; o gate do insight store recomputa o veredito a partir dos campos brutos). |
| H6. Uma cauda request-only preserva Invariante #1. | Notice alterar snapshot/system persistido ou entrar no transcript canônico. | **Validada.** O overlay é embutido DENTRO da última user message, sobre cópia, reaplicado a cada API call do turno, e nunca entra na história canônica nem no system: `test_notice_turn_integration.py::_notice_reaches_provider_inside_user_message_and_is_acked` (uma única user message, fora do system, fora do transcript), `_no_notices_means_byte_identical_request`, e `test_workflow_recovery_notice.py::_the_new_session_receives_the_recovery_notice_through_the_volatile_tail`. |
| H7. Insight textual é o artefato mínimo correto; skill automática é melhor. | Insight não expressar procedimento ou skill não suportar concorrência/revisão segura. | **Reformulada.** Registro estruturado bounded serve como observação; skill automática no fault foi **descartada como insegura** (o `SkillStore` atual sobrescreve arquivos, sem CAS/revisões/rollback/lock cross-process). Promoção para skill fica como ação explícita posterior. |

## Taxonomia implementada (`workflow/failure_taxonomy.py`, commits 12a1a1c + 39a95a0)

Dois eixos independentes, fail-closed nos dois:

- **mecanismo** (`Mechanism`): `validation`, `transport`, `timeout`, `external_rejection`, `resource`, `cancellation`, `unknown`. Mecanismo não reconhecido degrada para `unknown` (`_missing_`), nunca é um chute.
- **responsabilidade** (`Responsibility`): `infrastructure`, `environment`, `agency`, `unknown`.

A responsabilidade é decidida SOMENTE por mecanismo + sinais de evidência (`spec_shape`, `provider_side`, `harness_internal`) — o status nunca participa da decisão. `39a95a0` fechou duas brechas de entrada: confiança não-finita (`NaN`/`inf`) degrada para `0.0` em vez de ser clampada; e sinais de evidência CONFLITANTES (mais de um sinal conhecido distinto na mesma observação) retornam `unknown` — causalidade ambígua é underdetermined, nunca resolvida por regra de precedência. Só `agency` com confiança ≥ `AGENCY_CONFIDENCE_MIN = 0.8` é `learnable` (`FailureObservation.is_learnable`); abaixo do piso degrada para `unknown` — não existe "agência com ressalvas". Normalização bounded (sinais 8×128 chars, summary 500, confidence clamped) e `FailureObservation` imutável.

Mapeamento implementado (todo ramo indecidível retorna `unknown`):

| Mecanismo | Evidência | Responsabilidade |
|---|---|---|
| `validation` | `spec_shape`, conf ≥ 0.8 | agency (learnable) |
| `validation` | `spec_shape`, conf < 0.8 | unknown — baixa confiança não é meia-agência |
| `validation` | `provider_side` | environment |
| `transport`/`timeout` | `harness_internal` | infrastructure |
| `transport`/`timeout` | `provider_side` | environment |
| `external_rejection` | `provider_side` | environment |
| `resource` | `harness_internal` | infrastructure |
| `cancellation` | — (sempre) | unknown — cancelar não diz quem cancelou |
| qualquer | sinais conhecidos conflitantes (≥2 distintos) | unknown — evidência ambígua não é precedência |
| qualquer | sem sinais / mecanismo unknown | unknown |

Falso positivo impedido por construção: um chamador não consegue declarar `responsibility='agency'` para o store — o veredito é **recomputado** de (mecanismo, sinais, confiança) no limite de escrita. Falso negativo aceito: sem o sinal estrutural certo, a observação fica `unknown` e não é aprendida; melhor não aprender do que promover um workaround falso, e não há regex de mensagem para esconder a limitação.

**Limitação real preservada:** a taxonomia é estrutural e estreita. Quota/401/429/5xx/credencial não têm mecanismo alimentando o store — o único produtor hoje é a spec explicitamente enviada e invalidada (abaixo). Não há propagação causal geral de erros de provider para aprendizado; lacuna deliberada, documentada, não um atalho escondido.

## Insight store (`state/insights.py`, commit 12a1a1c)

`InsightStore` — tabela `workflow_insight_candidates` no arquivo compartilhado da SessionDB, conexão própria (mesmo padrão do audit sink: writers são threads de leaf e processos inteiros; não convoya o lock geral; WAL com busy_timeout 5000; exposto como `SessionDB.insights`).

Invariantes impostas no limite de escrita, cada uma com teste:

- **learnable gate com veredito recomputado** — `record()` roda `classify_failure` de novo e recusa (`False`) tudo que não é agency high-confidence (`_store_recomputes_verdict_caller_cannot_assert_agency`; variantes não-learnable recusadas em `_non_learnable_variants_are_refused`).
- **dedup semântica** — fingerprint SHA-256 de `(kind, responsibility, mechanism, texto normalizado)`, `INSERT OR IGNORE`: mesma lição em palavras diferentes cai uma vez (`_same_lesson_different_words_is_deduplicated`); kind/mecanismo distintos não colidem (`_fingerprint_includes_mechanism_and_kind`); duplicata exata é no-op.
- **cap 200, oldest-first, na MESMA transação do insert** — nenhuma janela com a tabela ilimitada (`_cap_200_evicts_oldest_first`).
- **transação curta** — um `BEGIN IMMEDIATE` cobre leitura de fingerprint + insert + evicção; dois processos nunca podem ambos observar "ausente" e ambos ganhar.
- **texto bounded** — clip no limite do schema (`_text_is_bounded`); listagem newest-first e bounded.

Cross-process provado com subprocessos reais: N processos escrevendo a mesma lição viram UMA linha (`_concurrent_processes_writing_same_lesson_land_one_row`), writers distintos não perdem updates (`_concurrent_distinct_writers_have_no_lost_update`), integridade SQLite pós-concorrência (`_sqlite_integrity_after_concurrent_writes`) e persistência através de processos (`_sessiondb_persists_across_processes`). Este é o mecanismo que o B2 mostrou faltar ao `insights.md` — que foi aposentado em vez de estendido.

## Aprendizado legado aposentado (`workflow/library.py`, commit 7e1e3fb)

O caminho textual ungated foi **desligado, não blindado**:

- `record_outcome()`: veredito problemático (degradado, status ≠ complete, null_rate acima do piso) não escreve **nenhum artefato da library** — nem prior textual legado, nem template; só loga. Os outros planos de persistência (candidates de insight e notices duráveis) são fluxos independentes, com stores e gates próprios, e não passam por `record_outcome`. Provas: `test_workflow_library.py::test_quota_timeout_and_process_loss_outcomes_write_nothing`, `test_degraded_run_teaches_the_library_nothing`, `test_high_null_rate_complete_run_is_not_a_template`, `test_a_problematic_run_leaves_a_preexisting_legacy_insights_file_untouched`.
- `insights.md` é **read-only por omissão**: nunca mais escrito, nunca mais exposto como leitura ativa. Bytes preexistentes ficam intactos no disco para rollback/audit; nenhum caminho os lê nem os sobrescreve.
- `library.recent_insights()` virou hook de compatibilidade que retorna `[]`; a superfície ativa é `WorkflowService.recent_insights()`, que expõe **apenas os candidates do SQLite** (`db.insights.list(limit=20)`, summaries causalmente gateados) — é isso que o rollup da tool mostra (`workflow/tools.py`: `insights=self._service.recent_insights()`).
- O mecanismo que **sobrevive** em `record_outcome`: run limpo, validado e com null-rate baixo continua salvando **template reutilizável** (`_save_template`), como sempre (§12.3) — `test_clean_run_saved_as_template`, `test_template_name_is_sanitized`.

## Notice store (`state/notices.py`, commits a902d31 + a8525c4)

`DurableNoticeStore` — tabela `durable_notices` no mesmo arquivo, conexão própria, exposto como `SessionDB.notices`. Fatos operacionais por sessão; nunca aprendizado.

- **owner = session_id** — `publish`/`claim` recusam ownerless (`None`/`""`/lista vazia); sem fallback "todos", que seria injeção profile-global (`_publish_refuses_ownerless`, `_ownerless_owner_ids_are_refused`).
- **dedup por fingerprint textual** — texto normalizado (whitespace colapsado, casefold); republicar o mesmo fato é no-op (`_duplicate_text_is_deduplicated_by_fingerprint`); mesmo texto sob owners diferentes são fatos distintos (`_same_text_different_owner_is_a_separate_fact`).
- **hard cap por owner (32), oldest-first, na transação do insert** — um owner não desloca o outro (`_cap_evicts_oldest_per_owner`, `_cap_is_per_owner_not_global`). Correção do rascunho (`a8525c4`): a evicção é **lease-safe** — são evictáveis só rows pendentes (`lease_token IS NULL`) ou com lease JÁ EXPIRADO (`lease_expires_at <= now`, que o próximo claim trataria como pendente); lease ATIVO nunca é evictado (fato em voo não se perde — quebraria at-least-once após crash do claimer), e a row recém-inserida também é intocável. **Se as evictáveis não bastam para voltar ao cap, o publish inteiro é REVERTIDO atomicamente e retorna `False`** — o cap não é excedido e nada em voo é perdido (`test_publish_overflow_with_active_lease_is_refused_not_evicted`). Lease expirado libera espaço para o próximo publish (`test_expired_lease_frees_cap_space_for_new_publish`), e dedup sob pressão de cap não apaga row alugada (`test_dedup_survives_cap_pressure_even_when_duplicated_row_is_leased`).
- **TTL (7 dias default)** — a expiração é **purgada no claim**, dentro da mesma transação do select/lease (`DELETE WHERE expires_at <= now`): notice expirada é descartada na próxima entrega (`_expired_notice_is_dropped_on_claim`) e fresh sobrevive dentro do TTL (`_fresh_notice_survives_within_ttl`). O único caminho de descarte por TTL é o claim.
- **claim é lease single-winner** via `BEGIN IMMEDIATE` — o token devolvido é a prova de posse; segundo claim enquanto o lease vive não vê as rows (`_second_claim_while_lease_lives_sees_nothing`); lease expirado (crash do claimer) é recuperável — entrega **at-least-once**, nunca exactly-once (`_expired_lease_is_reclaimable_after_crash`, `_unexpired_lease_is_not_stolen_early`).
- **claim concorrido multi-processo** — exatamente um vencedor (`_multiprocess_claim_has_exactly_one_winner`) e notices distintas entregues uma vez cada (`_multiprocess_claim_of_distinct_notices_delivers_each_once`), com persistência entre processos (`_sessiondb_notices_persist_across_processes`).
- **claim bounded** — máx 8 notices e 4.096 chars por claim; para no primeiro row que estourar o orçamento (o resto fica pendente para o próximo claim).
- **ack/release** — ack só com o token correto, remove tudo ou por ids (liberando o lease do restante na mesma transação); release devolve tudo a pendente; token errado é no-op (`_ack_with_wrong_token_removes_nothing`, `_release_returns_rows_to_pending`).

## Entrega entre turnos: overlay request-only (commits a902d31 + 4a77329)

- **No loop** (`agent/loop.py`): `run_conversation(request_overlay=...)` recebe texto efêmero e o embute DENTRO da última user message via `_apply_request_overlay` — sobre cópia, reaplicado do zero a cada API call (sobrevive a compactação e steering), sem criar user message extra (user/user é rejeitado por providers), sem tocar o system prompt e sem entrar na história do result dict. Sem overlay, a request sai byte-idêntica.
- **Bridge puro** (`agent/notices_overlay.py`): `lineage_owners(db, session_id)` lê a cadeia root→tip da SessionDB (o store de notices fica SEM acoplamento a lineage); `claim_lineage_notices` faz o claim — falha do store = turno sem overlay, at-least-once re-entrega depois; `format_notice_overlay` monta o texto bounded (cap 4.096 chars; linhas que não couberem ficam pendentes); `build_turn_notice` monta o notice operacional de turno morto (`turn <status>; error_kind=...; error=...`, clip 500), TTL 24 h (`DEAD_TURN_TTL_SECONDS`).
- **GatewaySession e CLI**: no início do turno, claim das notices do lineage → overlay. Em erro, interrupção, fork de compactação perdido, lock de compactação perdido ou falha de persistência: **release** (não ack) das notices + persistência do turno NÃO ocorre (regra do baseline preservada) + publicação best-effort do notice operacional de turno morto (owner = a própria sessão, TTL 24 h). Só após persistência canônica limpa (incluído fork de compactação limpo) as notices são **ackadas**. Provas: `_notice_reaches_provider_inside_user_message_and_is_acked`, `_error_turn_releases_notice_and_publishes_operational_notice`, `_failed_turn_notice_is_available_on_the_next_turn` (o fato sobrevive para o turno seguinte), `_interrupted_turn_releases_and_publishes_operational_notice`, `_compacted_turn_acks_lineage_notice_after_clean_child_persist`, `_save_message_failure_releases_notice`, `_notice_from_parent_lineage_is_claimed_by_child_session` e os equivalentes de CLI em `test_notice_turn_integration.py`; regressões de exceção em `test_notice_turn_regressions.py` (exceção antes do result nunca acka; fork perdido faz release; lock de compactação perdido faz release; gateway `save_message` propagando exceção faz release).

## Notifier de terminação do workflow: UM fato, UMA entrega (commits 4a77329 + 29b9049)

`bind_workflow_notifier` (`agent/equip.py`) — corrige o rascunho, que descrevia dois canais com fallback legado:

- **Com `db` ligado, o canal durável é o ÚNICO canal**: toda terminação owned não-cancelada publica o summary em `db.notices` (TTL 7 d, dedup, hard cap por owner) sob o id da sessão dona — e a live inbox **não é escrita** (um segundo canal entregaria o mesmo fato duas vezes). A entrega cross-process é at-least-once por design: crash entre publish e ack pode re-entregar; dedup absorve republicação idêntica; re-entrega de fato já ackado é o custo aceito por não perder o fato. Provas: `test_workflow_durable_notifier.py::_the_durable_channel_replaces_the_live_one_when_db_is_bound`, `_an_owned_completion_publishes_a_durable_summary`, `_the_notice_is_durable_across_processes`, `_republishing_the_same_completion_is_a_dedup_noop`.
- **Live inbox é fallback SOMENTE sem `db`** (compatibilidade): `resolve_inbox(session_id)` → `enqueue_steer` como system-reminder na cauda da próxima iteração; process-local, at-least-once apenas dentro do processo (`_no_db_still_delivers_the_live_inbox_only`).
- **Owner vem COM o callback, do `RunState`**: a assinatura é `on_run_done(owner, run_id, status, summary)` — o owner é capturado no momento em que o write terminal cercado foi aceito, nunca re-derivado por um lookup tardio `service.run_owner()` (que um straggler poderia responder com a sessão ERRADA, o owner recuperante). Prova: `_owner_comes_from_the_run_state_not_a_late_lookup`.
- **`notify_done` tem um contrato único, sem retry por arity**: callback recebe `(owner, run_id, status, summary)`; um `TypeError` interno do sink NÃO é interpretado como mismatch de assinatura nem gera segunda chamada — a exceção é isolada (logada e engolida) com exatamente UMA invocação (`_notify_done_contract_single_invocation_and_exception_isolation`).
- **Fail-isolation por chamada, não por canal**: com `db`, o publish durável é best-effort próprio (`_a_failing_durable_store_never_breaks_the_run`); sem `db`, a inbox quebrada é no-op silencioso. Run ownerless e run cancelado publicam NADA, durável ou live (`_an_ownerless_run_never_publishes_a_notice`, `_a_cancelled_run_publishes_no_durable_summary`). O fence é herdado: o callback só dispara no trecho cujo write terminal cercado foi aceito (`_the_callback_only_fires_for_an_owned_stretch`).

## Spec inválida vira candidate — só com proveniência explícita (commits 3a5679b + 9ac5b9e)

`WorkflowService.start(agency_authored=True)` — setado SOMENTE pela superfície `run_workflow` do agente (`workflow/tools.py`), nunca por operador/testes. E, correção do rascunho (`9ac5b9e`), a flag sozinha não basta: o serviço exige **spec EXPLÍCITA na chamada** (`explicit_spec = spec_dict is not None`) — `if agency_authored and explicit_spec`. Um resume sem spec repete a spec PERSISTIDA do run, escrita por outro turno/outro autor: atribuí-la à agência agora seria atribuição falsa. **Resume sem spec explícita não registra nada, mesmo com `agency_authored=True`** (fail-closed no serviço; prova: `test_workflow_spec_candidate.py::_persisted_spec_replayed_on_resume_is_never_attributed`).

Na tool, `agency_authored = spec is not None`: resume puro passa `False` (`_tool_passes_agency_authored_false_on_pure_resume`); spec explícita em resume passa `True` (`_tool_passes_agency_authored_true_for_explicit_spec_on_resume`). Uma spec explicitamente enviada em shape non-object (lista/string/escalar) NÃO é recusada na porta — chega ao `validate_spec`, que a rejeita com o erro didático, e a falha de autoria registra a candidata como qualquer outra spec inválida (`_non_object_spec_reaches_service_and_records_candidate`, `_non_object_spec_via_service_directly_records_candidate`). O que continua recusado antes do serviço é só a ausência total de spec num run fresco.

Spec rejeitada nesse caminho grava, ANTES do retorno didático, um registro `kind='candidate'` em `db.insights` (mecanismo `validation`, sinal `spec_shape`, confiança 1.0 — a proveniência da superfície de autoria é a evidência estrutural; o gate do store recomputa o veredito de qualquer forma). Nunca promovido além de `candidate`; falha do store é logada e engolida — o autor lê o erro didático, nunca o side-channel. Provas: candidate no instante do fault com retorno didático intacto (`_invalid_spec_from_tool_records_candidate_and_keeps_didactic_return`, `_candidate_recorded_before_return_even_when_return_is_ok_path_error`), start direto sem a flag não registra nada (`_direct_start_without_agency_flag_records_nothing`), spec repetida dedup, store quebrado não afeta o retorno, spec válida/cancelada/sem erro de spec não registram nada.

## Recovery notice de run órfão — fatos lidos DEPOIS da cerca (commits 3a5679b + cb5d745)

Quando um resume recupera um run `running` órfão (lease morto) e ganha o fence, `WorkflowService._publish_recovery_notice` publica em `db.notices` um notice para o **owner ANTERIOR** (a sessão que perdeu o run), nunca para a sessão que recupera. O texto é função só do `run_id` (sem timestamps/contagens), então a dedup do store funde a recuperação repetida do MESMO run em uma row — e dois runs seguem sendo dois fatos. Ownerless (None/blank) não publica; falha do store custa o notice, nunca a recuperação. O notice diz que o processo parou, células completas foram replayadas do cache e o trabalho em voo foi perdido/re-tomado — manda inspecionar o status, não presume resume/pivô.

**Ordem do launch cercado (reforço `cb5d745`, `test_workflow_recovery_fencing.py`):** os fatos da recuperação são redefinidos DEPOIS de adquirir lease/fence, nunca do snapshot pré-acquire:

1. **Relê pós-acquire** — `prior`, `owner` e o veredito de `orphaned` são relidos SOB ownership. O snapshot pré-acquire pode ter sido tornado mentira pelo último write cercado do dono anterior (status, owner, marcador de audit mudaram) ou por um novo dono que assumiu o run. `orphaned` exige: prior existe, run não está live aqui, o lease JÁ estava livre ANTES do acquire (fato sobre o momento pré-acquire — depois de acquired, a pergunta não tem resposta) e status `running`. Prova: `_recovery_facts_are_reread_after_the_fence`; lease ainda não morto no snapshot não basta (`_a_not_yet_dead_lease_at_snapshot_is_not_enough`); o notice vai ao prior owner da LINHA pós-acquire (`_the_recovery_notice_goes_to_the_post_acquire_prior_owner`).
2. **Persist cercada decide o launch** — a primeira escrita é `_persist_state(state)` (a linha de onde um processo novo resumiria); só depois `_persist_spend`. **Se a persistência cercada for RECUSADA** (novo dono assumiu entre o acquire e a escrita), o launch aborta ANTES de leaf/engine/plan/audit-gap/recovery-notice: a entrada do registry é descartada, o core é desligado, o lease devolvido e o retorno é erro didático — nada fala pelo run e nada vaza (`_a_fenced_state_refusal_aborts_the_launch_cleanly`, `_a_fenced_refusal_on_a_recovery_publishes_no_notice`, `_a_fenced_refusal_leaves_the_winner_intact`, `_a_launch_that_raises_after_the_persist_aborts_cleanly`).
3. **Notice só após persistência cercada aceita** — o recovery notice dispara no caminho vencedor, depois de `_persist_state` aceito; um resume recusado (busy, clash, refusal) nunca alcança esse ponto. O audit gap (`process_crash` se órfão, `unavailable` se só segmento não-fechado) é gravado na mesma região, depois do notice.

Provas do notice em si em `test_workflow_recovery_notice.py`: um notice ao prior owner e nada ao novo (`_recovering_an_orphaned_run_publishes_one_notice_to_the_prior_owner`), TTL default, sem prior owner → nada, owner em branco → nada, resumer perdedor → nada, store quebrado não bloqueia a recuperação, recuperação repetida é um notice mas runs distintos são fatos distintos, texto determinístico, nenhum outro owner vê.

**E2E real com subprocessos de SO (`backend/tests/test_sup05_e2e_subprocess.py`, commit `d1bf6d9`):** a cadeia completa órfão→lease morto→recovery→notice→overlay é provada com TRÊS subprocessos Python reais (`sys.executable`), sem memória compartilhada, sobre UM SessionDB file-backed. **Processo A** inicia uma leaf bloqueada, sinaliza o início pela stdout e morre SEM shutdown (o harness o mata) — o lease fica no SQLite e expira sozinho com wall-clock real (`lease_ttl=1.0` + ~1.5 s de sleep; único custo de relógio do teste). **Processo B** reabre o DB, vê o run órfão (lease morto), recupera-o e publica a recovery notice DURÁVEL para o owner anterior. **O harness** reabre o DB e prova EXATAMENTE UMA row durável GLOBAL na tabela `durable_notices` (owner certo = prior owner, texto do run certo — nem duplicata, nem broadcast). **Processo C** (nova sessão, nova conexão) consome a notice no turno seguinte via overlay request-only: `GatewaySession.submit` entrega o overlay provider-facing EXATAMENTE UMA vez (dentro da única user message, `run_id` aparece 1×); o system prompt é BYTE-IDÊNTICO antes/depois do turno (snapshot do agent E coluna persistida `sessions.system_prompt`) e igual ao `system` realmente enviado ao provider; o transcript canônico contém SÓ a user message real; a persistência canônica antecede o ack; e a pendência zerou após o ack. Além deste E2E, cada elo isolado mantém seus testes composicionais (`test_workflow_recovery_notice.py`, `test_notice_turn_integration.py`, `_the_new_session_receives_the_recovery_notice_through_the_volatile_tail`).

## Caps e expiração

Valores implementados (constantes nos módulos, não configuração):

| Limite | Valor | Onde |
|---|---:|---|
| Summary de insight | 500 chars | `insights.py` (status 32, sinais 8×128, payload clipado) |
| Insights persistidos (cap, oldest-first) | 200 | evicção na transação do insert |
| Notice text | 500 chars | clip no publish (`MAX_TEXT_CHARS`) |
| Notices por owner (hard cap) | 32 | evicção lease-safe oldest-first NA TRANSAÇÃO do publish; publish revertido se evictáveis não bastarem |
| Notices por claim | 8 | `MAX_CLAIM` |
| Orçamento de chars por claim | 4.096 | `MAX_CLAIM_CHARS` (para no primeiro row que estourar) |
| Overlay do turno | 4.096 chars | `format_notice_overlay` (paridade com o claim) |
| TTL notice de turno morto | 24 h | `DEAD_TURN_TTL_SECONDS` |
| TTL default (completion/recovery) | 7 dias | `DEFAULT_TTL_SECONDS` |
| Lease de claim | 5 min | `DEFAULT_LEASE_SECONDS` |

**Semântica de overflow/expiração (correção do rascunho):** o publish NÃO remove expiradas primeiro para abrir espaço. Inserir além do cap evita o MAIS ANTIGO daquele owner dentro da transação do insert, MAS lease ativo nunca é evictado e a row recém-inserida é intocável; se as evictáveis não bastarem, o insert é REVERTIDO e o publish retorna `False` (o hard cap não é excedido e nada em voo se perde). Lease expirado volta a ser evictável (o próximo claim o trataria como pendente) e libera espaço. A expiração por TTL é purgada exclusivamente no claim (mesma transação do select/lease), onde leases mortos também são recolhidos. O dado fonte do workflow nunca é removido — só o aviso unsolicited expira.

## Fronteiras explícitas

- Não há regex sobre corpo de erro para inventar responsabilidade.
- Não há broadcast de órfão ownerless.
- Não há autoedição de skill — promoção de candidate para procedimento é ação explícita e auditável depois de adaptação settled com progresso.
- Não há aumento de token budget, troca de provider/billing/credencial ou resposta a checkpoint como "recuperação".
- O mecanismo de notice não compõe nem invalida o prompt congelado; sem notice, a request é byte-idêntica.
- A limitação preexistente de reidratação byte-idêntica do prompt entre processos não foi mascarada nem ampliada por esta issue.

## Limitações reais preservadas

- **Sem propagação causal geral:** a única fonte de aprendizado é a spec explicitamente enviada e invalidada. Erros de provider (quota, credencial, 5xx, timeout) não alimentam o insight store — permanecem `error_kind` no result e/ou notice operacional. Generalizar exigiria evidência estrutural que hoje não existe.
- **Sem workaround settled:** nada é promovido a política ativa; todo registro é `candidate`/observação.
- **SIGKILL de turno não é detectável:** o notice de turno morto só existe quando o erro/interrupção é capturado no processo (GatewaySession/CLI). Um processo morto violentamente não publica nada. A continuidade cross-process real cobre runs de workflow (lease/órfão) — e mesmo ali o recovery notice só dispara quando ALGUÉM resume o run; ninguém resumindo, ninguém é avisado.
- **Dedup textual de notices:** o fingerprint é do texto normalizado; dois fatos distintos com a mesma redação colidem em uma row (o store é um quadro de avisos, não um log). O notice de recovery mitiga isso sendo função do `run_id`.
- **Entrega at-least-once, não exactly-once:** um crash entre publish e ack pode re-entregar o mesmo fato; a dedup absorve republicação idêntica, e a re-entrega de fato já ackado é o custo aceito por não perder o fato.
- **Claim que falha custa a entrega daquele turno:** store quebrado = turno sem overlay (o turno segue; at-least-once re-entrega depois).

## Resultados da implementação

1. Mesmo mecanismo/status com evidências distintas classifica responsabilidades diferentes; sem evidência ou com evidência ambígua/conflitante retorna `unknown`, nunca `learnable` — **comprovado** (`test_workflow_failure_taxonomy.py`, 19 testes, incluindo os splits por status idêntico, o piso de confiança, sinais conflitantes e confiança não-finita).
2. Infra/ambiente/cancelamento não entram no store de aprendizado — **comprovado** (gate recomputado; `_non_learnable_variants_are_refused`, `_cancelled_status_is_never_attributed`, `_only_agency_is_ever_learnable`).
3. Agency high-confidence entra no instante do fault, antes do terminal, com dedup — **comprovado** para o único caso coberto (spec explicitamente enviada e invalidada): `test_workflow_spec_candidate.py` grava antes do retorno didático, dedup repete, resume sem spec explícita não registra nada; **fora desse caso não há gravação no fault** (limitação acima).
4. N processos gravando chaves iguais resultam em uma linha; chaves distintas não perdem updates; cap retém os mais novos — **comprovado com subprocessos reais** (`test_workflow_insight_store.py`).
5. Turno com erro continua sem transcript dangling, mas deixa notice durável — **comprovado** (`test_notice_turn_integration.py::_error_turn_releases_notice_and_publishes_operational_notice` + B1 continua válido).
6. Novo processo na mesma sessão recebe notice; falha na entrega não dá ack; sucesso persistido dá ack — **comprovado** (`_failed_turn_notice_is_available_on_the_next_turn`, `_save_message_failure_releases_notice`, `_notice_reaches_provider_inside_user_message_and_is_acked`, `_compacted_turn_acks_lineage_notice_after_clean_child_persist`).
7. Claim expirado é recuperável; duas conexões não claimam o mesmo registro simultaneamente — **comprovado** (`_expired_lease_is_reclaimable_after_crash`, `_second_claim_while_lease_lives_sees_nothing`, `_multiprocess_claim_has_exactly_one_winner`).
8. Notices longos, numerosos e expirados obedecem hard cap/TTL sem tocar lease em voo — **comprovado** (`_cap_evicts_oldest_per_owner`, `_publish_overflow_with_active_lease_is_refused_not_evicted`, `_expired_lease_frees_cap_space_for_new_publish`, `_text_is_bounded_at_the_schema_boundary`, `_expired_notice_is_dropped_on_claim`, `_claim_honors_limit`); expiração purgada no claim, não no publish.
9. Órfão só aparece após lease morrer, os fatos são relidos pós-fence e o notice só sai do lado do prior owner, no caminho vencedor com persistência cercada aceita — **comprovado** (`_recovering_an_orphaned_run_publishes_one_notice_to_the_prior_owner`, `_no_other_owner_sees_the_recovery_notice`, resumer perdedor nunca publica; `test_workflow_recovery_fencing.py` fecha as corridas).
10. System prompt (objeto/texto) e `sessions.system_prompt` ficam byte-idênticos; notice não aparece nas mensagens canônicas — **comprovado** (`_no_notices_means_byte_identical_request`, `_notice_reaches_provider_inside_user_message_and_is_acked`, `_the_new_session_receives_the_recovery_notice_through_the_volatile_tail`).
11. Terminação owned publica o summary UMA vez, por um único canal — durável com `db`, live inbox só sem `db` — com owner do RunState e sem retry de sink — **comprovado** (`test_workflow_durable_notifier.py::_the_durable_channel_replaces_the_live_one_when_db_is_bound`, `_owner_comes_from_the_run_state_not_a_late_lookup`, `_notify_done_contract_single_invocation_and_exception_isolation`).
12. Run problemático não escreve artefato nenhum da library; `insights.md` preexistente fica byte-idêntico; run limpo ainda salva template — **comprovado** (`test_workflow_library.py::test_quota_timeout_and_process_loss_outcomes_write_nothing`, `test_a_problematic_run_leaves_a_preexisting_legacy_insights_file_untouched`, `test_clean_run_saved_as_template`).

## Suíte final

Suíte de backend no fechamento (HEAD `115f5a5`): **2423 passed**, cobertura **95%**, **Ruff verde**.

**Resultado negativo útil — flake de fencing, corrigido:** uma rodada anterior da suíte expôs uma flake em `test_workflow_recovery_fencing.py`: o worker original podia terminar (e publicar) durante a corrida de recovery, contaminando os asserts do recuperador. O teste falhava porque o harness não garantia que o worker original continuasse bloqueado — era uma fraqueza do TESTE, não uma falha de produção. Corrigido por `115f5a5`: o responder do processo "perdido" agora é deterministicamente bloqueado numa `threading.Event` e só é liberado no teardown (`_unblock`), então a leaf original não termina nem publica nada durante a corrida. Preservado aqui como evidência de que a suíte detecta corridas reais.

Testes novos da issue, por arquivo (`def test_`): taxonomia 19, insight store 16, notice store 35 (`test_durable_notice_store.py`), notice turn integration 11, notice turn regressions 14, recovery notice 10, spec candidate 15, recovery fencing 7, durable notifier 12, E2E subprocess real 1 (`test_sup05_e2e_subprocess.py`).

## Classificação final

**REFORMULADA.**

A amnésia entre turnos/processos e o lost update do aprendizado textual foram confirmados (B1/B2) — e o aprendizado legado por texto livre foi **aposentado**, não consertado: `insights.md` nunca mais é escrito nem exposto, `record_outcome` problemático não escreve artefato nenhum da library (candidates/notices são planos independentes), e a única superfície de insight ativa são os candidates causalmente gateados do SQLite. O canal de continuidade é um único mecanismo durável bounded de notices (hard cap por owner que nunca evicta lease em voo, publish revertido atomicamente se não houver espaço seguro, TTL purgado no claim), entregue um fato por vez — durável quando há DB, live inbox apenas como fallback sem DB. O aprendizado automático ficou restrito ao único caso com evidência estrutural de alta confiança E proveniência explícita: a spec enviada nesta chamada e invalidada — resume que repete spec persistida não atribui nada à agência atual. A recuperação de órfãos relê prior/owner/status pós-fence e só publica notice depois que a persistência cercada é aceita; recusada, o launch aborta limpo, sem leaf, engine ou notice. Todo o resto de falha (quota, credencial, transporte, processo perdido) circula como notice operacional at-least-once e nunca é aprendido como escolha da Lohra. Procedimento ativo exige promoção explícita posterior; skill automática no fault segue descartada como insegura.

## Fechamento (turnos finais — coautoria declarada)

A investigação foi conduzida e majoritariamente implementada pela Lohra
(commits até `9968120`). Os ~30% finais foram fechados pelo supervisor do
épico após a quota da subscription interromper os turnos dela (429, reset em
6 dias) e o modelo substituto (DeepSeek v4 Pro) falhar 3× como orquestrador
(2 desorientações sob contexto compactado, 1 estouro de contexto). Registro
de coautoria, não de takeover: os testes RED dela definiram o que faltava.

### O que os turnos finais entregaram

- **Fencing pós-persist** (`3bf1a13`): o WIP de produção dela estava correto;
  a coreografia do teste era impossível (o acquire do perdedor nunca passa da
  lease viva do vencedor) e foi refeita para a janela real linha→ledger, com
  lapso de relógio.
- **Gap 1 — descartes invisíveis** (`1a1f6e1`): falha de persistência e lock
  de compactação perdido agora deixam dead-turn notice durável (owner = a
  sessão, TTL 24h, best-effort, claim liberada e nunca ackada sem
  persistência canônica).
- **Gap 2 — notifier durável na CLI** (`1a1f6e1`): `run_chat` liga
  `bind_workflow_notifier` com o SessionDB do estado (paridade dashboard).
  Limite honesto: no fim do turno o shutdown CANCELA runs em voo e run
  cancelada nunca notifica — a completion durável vale para runs que
  terminam DURANTE o turno.
- **Review adversarial (8 achados, nenhum ALTA)** e os fixes (`76dfbb1`):
  persistência do turno virou UMA transação (`save_messages` — CLI, gateway
  e fork de compactação; achado 1, provado empiricamente pelo revisor);
  o caminho que re-levanta imprime o session id (sem isso, o notice de uma
  sessão sem `--session` era inalcançável até expirar — achado 2); guarda
  `canonical_done` impede notice "error" FALSO quando a falha vem depois da
  persistência canônica (achado 5); o settle do run sobrevive a erro de
  escrita do ledger (achado 4); o helper `_blocked_service` passou a
  bloquear DE VERDADE, matando um flake 5-de-6 (a lease "lapsada" era
  renovada pelas escritas da thread ainda viva — achado 8).

### Evidência de primeira mão (o mecanismo operando em produção)

O banco real (`~/.lohra/state.db`) guarda 4 dead-turn notices de mortes REAIS
de turnos deste épico: 2× `max_iterations`, 1× `turn interrupted` e 1× o 400
de roteamento (`glm-5.3-flash` enviado ao backend Codex). A morte por 429 do
turno que interrompeu esta investigação NÃO está lá — consistente com
entrega + ack pelos turnos sucessores da linhagem, mas improvável post-hoc:
**o ack apaga a linha**, então entrega bem-sucedida e não-publicação são
indistinguíveis depois do fato. Limitação nomeada (candidata a follow-up:
rastro de acks).

### Limitações que permanecem (nomeadas, sem fix nesta issue)

- A linha aceita do perdedor não é revertida no abort cercado — por
  construção: quem perdeu a cerca não pode escrever a reversão (achado 3).
  Janela residual: vencedor que aborta entre acquire e a própria linha deixa
  a linha do perdedor como estado durável (falso órfão possível).
- No caminho que re-levanta, `record_turn` não roda (tokens do turno morto
  fora do total da sessão) e o contrato `--json` de 1 envelope não é
  honrado. Pré-existentes, agora documentados.
- `work-<fence>` vazio por aquisição abortada não é limpo (pendência antiga
  de acumulação de working dirs).

### Classificação final (mantida): **REFORMULADA**

O aprendizado no momento do erro se sustenta com a disciplina já provada
(classificador fail-closed por tipo, dedup, cap, expiração, entrega pela
cauda volátil sem tocar o prompt congelado) — e a parte "amnésia" se provou
tratável por notas operacionais duráveis, não por memória semântica.
