# Wave 8 — investigação read-only sobre a run real `42abc3eb…` (lohra-notion-v4) — 2026-09-02

Três investigadores independentes (opus, read-only: código main@0.0.18 + `~/.lohra/profiles/lohra-notion-v4/state.db`
lido com `sqlite3 -readonly` + mtimes do projeto), um por trilha da Wave 8. Nenhum workflow rodou; nenhum arquivo do
repo ou do banco foi alterado. As conclusões foram publicadas como comentários nas issues #42, #43, #44 e #45.

## Síntese para o dono
- **#44 (cache)**: o cache está CORRETO — 0 reexecuções incorretas em 6 segmentos, provado por recomputação real das
  chaves (6/7 hashes reproduzidos byte-idênticos). O problema é de explicabilidade: `cache.missed` sem causa, ledger
  sem identidade de conteúdo, e uma invalidação legítima (pivô de `model` em `final_certification`) já enfileirada
  que vai re-pagar ~2,13M tokens em silêncio no próximo resume. Épico mais barato e de maior valor: **preview de
  blast radius pré-resume** (zero LLM).
- **#42-A / #45 (recurso compartilhado)**: o zumbi do cancel é FATO (agiu 156 s depois do sucessor começar), o dano
  não se materializou; a causa raiz não é topologia, é `cancel` cooperativo que não alcança o dispatch da resposta em
  voo (`loop.py:426`). 3 de 5 artefatos declarados por células foram mutados depois da gravação, por um leaf VIVO
  (caso #45); prejuízo zero porque o spec tinha zero `${ref}` (controle negativo confirmado). Épicos: checar interrupt
  antes do dispatch (S), texto honesto do recurso (S), manifesto de artefato validado no replay (S/M, decisão do dono
  sobre mismatch), guidance de autoria (S). Não fazer `working_root` por nó nem `reads/writes` declarativos.
- **#43 (fallback de rota)**: o pivô do v4 foi HUMANO verbatim (a fronteira SUP-01 funcionou). O custo real foi
  degradar em silêncio: 55,3% dos tokens fora de célula, 4 nós rodando após o comprometimento. Re-run same-route
  recuperou 3 nós; cross-route recuperou 1 e não recuperou outro. Opções para o dono: A (manual + fault didático),
  B (envelope do operador em arquivo, antes do run), C (pausar `route_fault` em vez de degradar). Recomendação:
  **C primeiro, B depois**; B depende de USD por rota (#46) e de contador durável (fatia da #36).
- **Dado transversal**: `workflow_insight_candidates` vazia neste run — evidência direta para a Wave 9 (#50).

## Épicos propostos (ordem sugerida de ataque)
| # | Épico | Issue | Tam. | Nota |
|---|---|---|---|---|
| 1 | Checar interrupt ANTES de despachar tool calls | #42/#8 | S | fecha o único ponto onde o zumbi agiu |
| 2 | Preview de blast radius pré-resume (recomputa chaves × cache) | #44 | S | zero LLM; teria previsto 2,13M |
| 3 | `reason` no `cache.missed` + identidade da spec no `segment.started` | #44 | S | explicabilidade mínima |
| 4 | Re-spawn same-route em falha terminal gateado pelo `retries` já autorado | #43 | S | recuperou 3/4 nós no v4 |
| 5 | Texto honesto da quiescência (`_ALIVE_HINT`, spec §8.3 `work-{fence}`) | #42/#45 | S | só texto + anti-drift |
| 6 | Pausa `route_fault` em vez de degradar (após 4) | #43 | M | zero autoridade nova |
| 7 | Manifesto de artefato validado no replay | #45 | S/M | decisão do dono: mismatch → re-spawn/fault/notice |
| 8 | Envelope de rotas do operador (`workflow_routes.json`) | #43 | M | depende de #46 e de contador durável (#36) |
| 9 | Cancel que alcança a chamada em voo | #42 | M/L | complemento da #48 (timeout ≠ abort) |
| 10 | Experimento: fan-out intra-nó sobre recurso compartilhado | #45 | S | única forma de decidir H2/H3 |

---

# Relatório 1 — #44 cache replay/miss

# W8-T4 (#44) — Explicabilidade de cache replay/miss sob resume e pivô
Investigação READ-ONLY. `main` @ 0.0.18. Banco só via `sqlite3 -readonly "file:...?mode=ro"`. Nada editado/commitado; nenhum workflow com LLM rodado.

## Veredito de uma linha
O cache está **correto** — 0 reexecuções incorretas em 6 segmentos. Mas o ledger **não consegue, por construção**, distinguir replay de miss-por-mudança, e há **uma invalidação legítima já enfileirada e invisível** (`final_certification`, ~2,1M tokens) que só aparecerá como um `cache.missed` mudo no próximo resume.

## Método (4 passos, o 1º foi descartado)
1. **DESCARTADO:** classificar pelo `cell_id` do audit. `audit.py:468-478` monta `cell_id` como sha256 da identidade **estrutural** (run_id, role, node_path, branch_path, item/stage index), **excluindo deliberadamente o content hash**. É constante por (nó, role) — o `cache.missed` e o `cache.replayed` do mesmo nó têm cell_id byte-idêntico. Qualquer inferência "mesmo cell_id ⇒ mesma identidade" é nula.
2. **Ordenação (não precisa de hash):** (b)/(c)/(e) exigem `cache.stored` ANTES e `cache.missed` DEPOIS no mesmo `node_id`. Nenhum par existe.
3. **Recomputação real da chave:** importei `content_hash` do próprio pacote e recalculei as 8 células a partir da spec persistida. **6 dos 7 hashes gravados reproduzem byte-idêntico** → o método se auto-valida.
4. **Bissecção de campo único** no 7º (o que não reproduz) + corroboração independente pelos `leaf.started` do ledger.

**Buraco do `audit.gap` fechado:** há `audit.gap {reason: process_crash, dropped_count: null}` no seq 1242. O único nó em voo ali era `repair_and_finalize`, que **nunca teve linha** em `workflow_node_cache` — a tabela (não o ledger) é a fonte da verdade sobre stores. Nada perdido pode esconder (b)/(c)/(e).

## Fatos do banco — run `42abc3eb28e64e1ba8505a132ef8e1f8`
Status `running` / lease morta / seg6 sem `segment.completed` → o próximo resume será outro `recovered_process`.

| | |
|---|---|
| eventos de audit | 1286 (seq 1–1286), 6 segmentos + 1 `audit.gap` |
| `cache.replayed` / `cache.missed` / `cache.stored` | **23 / 18 / 7** |
| células em `workflow_node_cache` | 7 linhas, **7 node_id distintos, 7 hashes distintos** |
| tokens_in somados das células | 2.222.413 |
| `workflow_run_spend.tokens_in` | 5.028.075 |

**Replays por segmento:** 0, 2, 3, 6, 6, 6.
→ segmentos 1–5 = **17**. A inspeção de 2026-09-01 registrada na issue está **CORRETA e é anterior ao segmento 6** (ec54b6b4, 02:54:02), que soma os outros 6. "7 células" bate exatamente.

**Aritmética que responde à suspeita original:** só **44,2%** do input gasto virou célula cacheável; **≥55,8% foi para leaves que morreram** (repair ×5, eval ×2, implement ×2, adversarial ×1, final_cert ×1) e **0% para reexecutar célula completa idêntica**. (Piso — os dois ledgers subcontam.)

## 1. Como a cell key é composta hoje
`engine.py:445-447` — `cell_hash(*parts) = content_hash(spec.meta.name, spec.meta.version, *parts)`; `_spec_id` setado em `engine.py:1029`. `cache.py:24-27` = sha256 sobre JSON canônico. Escopo = **run** (PK `(run_id, content_hash)`, `db.py:86-92`); cross-run OFF (§6.3).

| node type | partes da chave | file:line |
|---|---|---|
| `agent` | id, "agent", prompt resolvido, schema, model, effort, provider, timeout, retries, [max_iterations†] | `strategies.py:218-222` |
| `parallel` | id, "parallel", prompts | `strategies.py:323` |
| `verify` | id, "verify", finding, skeptics, lenses, kill, [routing†] | `strategies.py:363-366` |
| `judge_panel` | id, "judge_panel", prompts, judges, synth, [routing†] | `strategies.py:430-433` |
| `loop_until_dry` | id, "loop_until_dry", 1º prompt, schema, stop_after_k, max_rounds, [routing†] | `strategies.py:549-552` |
| `pipeline` | id, stage_idx, **item**, prompt, schema, [stage.max_iterations†] | `strategies.py:785-792` |
| `gate` | id, "gate", prompt, schema, validator, attempts, [routing†] | `gates.py:121-124` |
| `completeness_check` | id, "completeness_check", task, results, [routing†] | `gates.py:204-207` |
| `checkpoint` | id, "checkpoint", prompt | `gates.py:239` |
| `workflow` (nested) | **célula própria: NENHUMA** | `strategies.py:959-987` |

† **condicional** — só entra se o nó declarar o campo (`_routing_identity` `strategies.py:152-166`; `max_iterations` :221). Racional explícito: a chave é persistida, e um `None` à direita re-keyaria em massa toda linha gravada antes do knob existir.

**Gravam célula: 9 dos 10 node types.** `workflow` não tem célula própria — `nested_engine()` (`engine.py:383-407`) compartilha o `NodeCache`, mas `run()` sobrescreve `_spec_id` com o meta da spec **aninhada**. Logo as células filhas são namespaceadas pelo sub-template, **não** pelo pai (um bump de `version` no pai NÃO re-keya o sub-DAG), e `node_scope` **não** entra na chave.

**FORA da chave** (neutro, sem juízo): `tool_less`, `required`, `phase`, `depends_on`, `label`, `fs_allow`/`egress_allow`, o **nome** do `tier` (só a resolução entra), e `args` do run (chegam via prompt resolvido).

**Namespace:** `(meta.name, meta.version)` prefixa TODA célula. Mudar qualquer um re-keya inclusive nós intocados — H1 é factual.

## 2. Decomposição (a)–(e) do run real

**23 replays = (a).** Um replay É a prova de hit: a identidade corrente casou com uma linha gravada.

**18 misses = 8 primeiras-buscas + 10 re-misses, TODOS (d).** Zero (b), zero (c), zero (e).

| nó | miss | replay | store | leitura |
|---|---|---|---|---|
| evidence_architecture | 1 | 5 | 1 | cold start → replay limpo |
| eval_contract_design | 3 | 3 | 1 | 2 leaves morreram (segs 1,2) antes do sucesso no seg3 |
| security_risk_review | 1 | 5 | 1 | cold start → replay limpo |
| implement_package | 3 | 3 | 1 | pausa (seg1) + leaf error codex (seg2) |
| test_and_harden | 1 | 4 | 1 | cold start → replay limpo |
| adversarial_audit | 2 | 3 | 1 | leaf error anthropic (seg2) |
| repair_and_finalize | **5** | **0** | **0** | **nunca completou** — pausa, 2 leaf errors, 2 interrupted |
| final_certification | 2 | 0 | 1 | leaf interrupted (seg3) → sucesso (seg4) |

**Prova de (e) = ∅, dois caminhos independentes:**
- Ordenação: nenhum `stored`→`missed` posterior no mesmo nó (max(missed.seq) < min(stored.seq) em todos os 7).
- Tabela: 7 linhas / 7 node_id / 7 hashes → **nenhum nó foi persistido sob duas identidades**.

**Causa por miss, do próprio ledger** (`node.paused` ×2, `leaf.failed` ×5, `node.failed` ×7):
`implement_package` seg1 = pausa · `repair_and_finalize` seg2 = pausa · `implement_package` seg2 = leaf error (gpt-5.6-sol) · `adversarial_audit` seg2 = leaf error (claude-opus-5) · `repair_and_finalize` seg3 = leaf error (gpt-5.6-sol) · `final_certification` seg3 = leaf **interrupted** (deepseek) · `repair_and_finalize` seg4 = leaf **interrupted** (gpt-5.6-sol) · seg5 = **inferido (d)** pela ausência de linha na tabela (eventos de desfecho perdidos no crash).

**Os pivôs realmente ocorreram** (`leaf.started` carrega model/provider — evidência direta):
- **Pivô 1**, entre seg2 e seg3: `adversarial_audit` anthropic/claude-opus-5 → openai-codex/gpt-5.6-sol. **Blast radius = 0**: o nó tinha falhado, não havia célula a invalidar. História limpa (d)→(a).
- **Pivô 2**, no lançamento do seg5 (02:30:05): `repair_and_finalize` gpt-5.6-sol → z-ai/glm-5.3-flash (visto em `leaf.started` segs 5 e 6). **Blast radius observado = 0** — o nó nunca teve célula.

**A ARMADILHA — (b) previsto, ainda não observado:**
O mesmo Pivô 2 trocou `final_certification` deepseek-v4-pro-0813 → z-ai/glm-5.3-flash. Esse nó **tem** célula (gravada no seg4 sob deepseek). Recomputação:

```
hash gravado (seg4, deepseek)  be29202653d7c31f54b81a5ddaf7357cee0947e61bfd8c5a412d8f3414da4fe1
hash da spec atual (glm)       1ecceb88f10f9767ba3ec217bb645cb4d2a84246e6c056b07ee4fff7aae7e05c
```
Bissecção de campo único: **exatamente 1** explicação — `model`. Prompt, provider, effort, timeout, retries, max_iterations, meta.name e meta.version idênticos. Corroborado pelos `leaf.started` (segs 3 e 4 rodaram deepseek).

Segs 5 e 6 pararam em `repair_and_finalize` e nunca chegaram nele. **No próximo resume esse (b) vira um `cache.missed` mudo** e re-paga 355.212 in / 31.607 out (eixos do budget) + 1.722.112 cache_read + 17.451 reasoning ≈ **2,13M tokens**. É invalidação **legítima e correta** — o operador trocou o modelo. O defeito é que **nada avisa**.

**Origem da spec glm — não atribuível.** A sessão supervisora (única, 358c4cc4, 97 msgs) termina em 02:27:33; o último `run_workflow` que consigo ler (msg 507) ainda trazia codex/deepseek. O seg5 subiu às 02:30:05 já com glm. Consistente com um turno de pivô perdido no `process_crash` (persistência de turno é transacional) **ou** um resume externo por CLI. Não escolho entre os dois.

## 3. O que o operador/agente enxerga hoje

| superfície | mostra cache? | evidência |
|---|---|---|
| **audit ledger** (`lohra workflow audit`, tool `workflow_audit`) | Único lugar. `cache.replayed`/`missed`/`stored`/`unavailable` | `engine.py:506-522` |
| ...mas o payload | **`"data": {}`** — miss sem causa, replay sem economia | verificado nas 48 linhas do run |
| ...e o `cell_id` | `audit:<sha256 estrutural>` — **não joina** com `workflow_node_cache` | `audit.py:471-478` |
| **liveview / events / watch** | **ZERO menções a cache** (grep vazio) | `events.py`, `liveview.py`, `liveview_tui.py`, `watch.py` |
| **progress / `workflow list|status`** | Diz "complete", **nunca "replayed"** | `progress_json`: 6 complete, sem marca de replay |
| **`_emit_node`** | `COMPLETE` idêntico p/ replayed e executado | `engine.py:1051` |
| **`nodes` do status (`node_costs`)** | Célula replayada **retorna antes de `account_leaf`** → nunca aparece | `costs.py:32-37`, `COST_SCOPE = "nodes_executed_in_this_stretch"` |
| **rollup / envelope** | `cache_read`/`cache_write` = **prompt cache do provider**, não o node cache | `rollup.py:110-116` |
| **`spend.py:80/103`** | Economia só agregada, sem atribuição por nó | — |

**Onde falta o "porquê":** (i) `cache.missed` não tem campo de causa; (ii) o ledger apaga o content hash, então (a)/(b)/(c) são indistinguíveis post-hoc; (iii) `segment.started` carrega só `{resume, recovered_process}` — **sem name/version da spec**, então (c) é inauditável; (iv) `workflow.fault` guarda só a contagem de caracteres da causa (`{"cause":{"state":"redacted","characters":61}}`); (v) o run guarda **UMA** spec — `launch_spec` (`service.py:362`) faz a spec explícita vencer e `save` sobrescreve `spec_json` (`runstate_store.py:282`): **o pivô destrói a identidade sob a qual as células foram escritas**. Essa é a causa-raiz estrutural da não-explicabilidade; (vi) o único sinal de replay hoje é implícito — `progress` diz 6 completos e `nodes` lista 0 — e wall-clock.

## 4. Hipóteses

- **H0 (só telemetria) — CONFIRMADA**, e mais forte que o enunciado. A chave está correta nos 6 segmentos (0 de (e), por dois métodos independentes). Falta causa de miss **e** previsão: já existe um (b) legítimo enfileirado (2,13M tokens) que nenhuma superfície anuncia.
- **H1 (namespace global amplo) — FACTUAL, NÃO EXERCITADA.** `meta.name`+`version` prefixam toda célula (`engine.py:447`). Nas specs recuperadas `version` foi sempre `"4.0"` → nunca disparou neste run. Só mensurável com DAG sintético.
- **H2 (campo imaterial) — NÃO REFUTADA, NÃO PROVADA.** Candidatos: `timeout` e `retries`, incondicionais na chave (`strategies.py:215-217`, com racional escrito de compat retroativa). Nunca mudaram neste run → sem evidência. Só discriminador sintético; **não remover**.
- **H3 (bug de replay) — REFUTADA para os segmentos observados.** Nenhum `(e)`. O crescimento de tokens é leaf morta (≥55,8% do input), não replay.
- **H4 (novo, o INVERSO de (e)): replay obsoleto.** `tool_less` **não** está na chave, mas muda como o leaf roda (`strategies.py:198-200`: força saída estruturada por synthetic tool). Ligá-lo num resume replaia a resposta antiga não-forçada. **Hipótese, não fato** — precisa do discriminador D4.

**REGRA DA ISSUE RESPEITADA: nenhuma mudança de cache key proposta.** Os itens abaixo são **testes**, não fatos.

### Discriminadores propostos (testes, não veredictos)
- **D1 — pivô de model em nó COM célula:** grava célula, resume trocando só `model` → deve dar miss, e o teste afirma que a economia perdida é reportada. *(Já positivamente demonstrado offline em `final_certification`; falta virar teste.)*
- **D2 — `meta.version` bump:** re-keya nós intocados? Mede o blast radius de H1.
- **D3 — `timeout`/`retries` isolados:** quantifica H2. Se re-keyar, o teste **documenta**; remoção exige prova de segurança separada.
- **D4 — `tool_less` flip:** se replaiar a resposta antiga, H4 confirmada.
- **D5 — dois nós `workflow` → mesmo `ref`, args não interpolados nos prompts:** as células colidem (mesmo `_spec_id` aninhado, `node_scope` fora da chave) e o 2º nó nunca roda? Mesma classe do comentário de `${item}` em `strategies.py:777-779`.
- **D6 — pipeline:** `node_id` da célula é o **nó cru** (`strategies.py:796`), compartilhado por todos os (item, stage). Qualquer heurística "existe linha com hash diferente ⇒ identidade mudou" é ambígua ali.

## Épicos propostos (ordenados por valor/custo)

**E1 (S) — Preview de blast radius pré-resume.** Antes de aceitar um resume com spec explícita: recomputar as chaves da spec nova, diferenciar contra `workflow_node_cache`, e reportar `{replaia: N células, invalida: M células, custo a re-pagar: X tokens/USD}` lendo `workflow_node_cost`. **Zero LLM, computável hoje** — `recompute.py` desta investigação é o protótipo, e teria previsto os 2,13M do `final_certification`. Responde à "previsão do trabalho refeito" do H0 diretamente. *Arquivos:* `workflow/cache.py` (helper de diff), `workflow/service.py` (`start`, junto de `spec_warnings`/`lint_warnings`), `workflow/tools.py`.

**E2 (S) — `reason` no `cache.missed`.** Determinável no lookup, em `engine.cache_lookup`: consultar se o run tem linha para esse `node_id` → nenhuma = `never_completed`; existe com hash diferente = `identity_changed`. Separa (d) de (b/c/e), que é 100% do que faltou aqui. **Cuidado D6:** em fan-out o `node_id` é compartilhado → ou chavear por (node_id, item, stage) ou emitir `identity_changed_or_sibling`. *Arquivos:* `workflow/engine.py:506-522`, `state/db.py` (um `cache_peek_by_node`), `workflow/audit.py:139`.

**E3 (S) — Identidade da spec no `segment.started` + na linha da célula.** Carimbar `(meta.name, meta.version)` (e um digest da spec) no payload de segmento e/ou na linha do cache. É o **único** jeito de separar (c) de (b) post-hoc, e o único jeito de auditar um pivô depois que `spec_json` foi sobrescrito. Metadata pura — respeita `audit.py:468-470`. *Arquivos:* `workflow/engine.py:471-486`, `workflow/runstate_store.py`, `state/db.py`.

**E4 (M) — Replay visível no status/liveview/progress.** `progress` distingue `complete` de `replayed`; `workflow_status` reporta `{cells_replayed, tokens_saved}` de `workflow_node_cost`; liveview imprime replay na hora (hoje: zero menções). Fecha o buraco "6 completos, 0 nodes com custo". *Arquivos:* `workflow/progress.py`, `workflow/events.py`, `workflow/liveview.py`, `workflow/service.py:1076-1079`, `workflow/costs.py`.

**E5 (M) — Vetor de componentes da chave (nomear O CAMPO).** Sidecar com o hash de cada componente separadamente (nunca o conteúdo) → o miss diz `model mudou`, não só `identity_changed`. É a forma que `audit.py:468-470` permite. Depende de E2/E3.

**E6 (S, investigativo) — Suíte D1..D6.** DAG sintético, leaves fake, zero provider.

## O que NÃO fazer
1. **Não mexer na cache key.** Nenhuma reexecução incorreta foi encontrada; a regra da issue está satisfeita e continua valendo.
2. **Não remover `timeout`/`retries`/`max_iterations`/routing** da chave por "imaterialidade" — H2 não tem evidência, e `strategies.py:211-217` documenta a invalidação em massa que a mudança causaria em linhas já persistidas.
3. **Não tornar `_routing_identity`/`max_iterations` incondicionais** — é exatamente a regressão que os comentários existentes previnem.
4. **Não ligar reuso cross-run** (§6.3) — é hazard de corretude, não telemetria.
5. **Não persistir prompt/conteúdo resolvido no ledger** para "explicar" o miss — viola `audit.py:468-470` (metadata-only). Explicabilidade tem que ser por hash/estrutura.
6. **Não tratar `cache_read`/`cache_write` do rollup como node cache** — é o prompt cache do provider. A colisão de nomes já é armadilha de leitura.
7. **Não usar o `cell_id` do audit como identidade de conteúdo** — nem em análise, nem em código futuro. Foi o meu 1º método e é falso.
8. **Não concluir nada sobre H1/H2 a partir deste run** — nunca foram exercitados.

---

# Relatório 2 — #42-A / #45 artefatos e recurso compartilhado


Read-only (audit `workflow_audit_events` 1286 eventos/7 segmentos, `workflow_node_cache` 7 células, `workflow_run_state`, filesystem do projeto com 79 arquivos via mtime). Caveat: `spec_json` persistido é pós-pivô (modelo do `repair_and_finalize` diverge do audit); valores do spec só usados com corroboração independente.

## Linha do tempo (segmento `6afbe1c2…`, 2026-08-31)
- 22:44:21.394 `leaf.started repair_and_finalize` (gpt-5.6-sol/openai-codex) → ~55 `terminal` até 23:14:05.
- 23:08:00.869–01.012 `terminal` (4.289 chars) ⇄ mtime de `.orchestration/evidence_architecture.md` = 23:08:01.000906 (leaf VIVO e legítimo — caso #45).
- **23:14:21.400** `workflow.fault` causa redigida 57 chars = `repair_and_finalize: leaf timeout after 1800s (cancelled)` (string inteira em `pause_payload_json.prior_faults`; 1800,005 s cronometrados → `engine._timed_out`).
- 23:14:21.414 `node.failed repair_and_finalize` e **no mesmo milissegundo** `node.started final_certification` (deepseek-v4-pro/openrouter).
- **23:16:57.864–.885** `tool.started/completed terminal` DO ZUMBI (args 17.635 chars, 21 ms, resultado 56 chars) — **+156,45 s** após o sucessor começar; 23:16:57.887 `leaf.failed repair_and_finalize status: interrupted`.
- 23:24:24 `final_certification` complete (PASS, 35 testes/probes reais), segmento `degraded`.
- 23:30:05 e 23:54:02 resumes: 6 células replayadas cada vez.

## #42-A — o que sobra
- Zumbi escreve depois do cancel: **provado** (dois eventos do audit com o sub_id do zumbi posteriores ao sucessor). Causa mecânica: `agent/loop.py:286` checa interrupt no topo da iteração; `loop.py:426 _execute_tool_calls` não checa; `core.cancel` (`core.py:449`) é só `interrupt()`. O provider respondeu ~172 s depois com um tool_call e o loop despachou.
- Dano **não observado** neste run: único arquivo com mtime na janela do sucessor é o do próprio certificador. O comando de 23:16:57 tem assinatura de escrita, mas args redigidos → residual, não fato.
- Quiescência de 5 s de hoje: cairia em STILL RUNNING (156 s). Correto não aumentar o cap. `_ALIVE_HINT` "shared working_root may be mutated" está mal-endereçado: os 6 `work-N` estão VAZIOS; o recurso compartilhado era o projeto do usuário via `terminal`.
- H0 parcialmente confirmada (certificador independente recuperou confiança; segmento ficou degraded). H1 `requires_success` redundante com `required` (0.0.18). H2 dependência por recurso: engine é estritamente sequencial (`engine.py:1021`) — ordenação por recurso não compra nada; o que falta é cancel que atinge quiescência. H3 indecidível (working_root nunca usado).

## #45 — handles
- Célula = `output_json` (texto final do leaf); cell_hash (`strategies.py:218-222`) e valor não conhecem filesystem. Ex.: `evidence_architecture` cacheia prosa "…25.091 bytes, ~441 linhas…"; `security_risk_review` cacheou 45 chars sem resposta ("All PoCs confirmed. Now writing the review.").
- **3 de 5** artefatos declarados por células foram mutados DEPOIS da gravação (evidence_architecture 20:06→23:08; test_and_harden 21:06→23:13; eval_contract_design 21:35→23:13), pelo `repair_and_finalize` vivo; arquivo hoje 25.583 B/445 linhas vs célula 25.091 B/441. Células replayadas 2× afirmando o que não era mais verdade. Consequência real: nenhuma — zero `${ref}` no spec (controle negativo confirmado).
- `working_root` é por AQUISIÇÃO (`work-{fence}`, `service.py:476-479`), nunca entregue ao leaf; replay cross-fence seria negado pelo sandbox (fail-closed) — mas `terminal` fura tudo. Drift de doc: spec §8.3 diz `runs/<run_id>/work`.
- H0 guidance: não refutada, mais barata. H1 manifesto `{path,sha256,bytes}`: menor primitive honesta. H2 handle de 1ª classe: sem evidência (zero bytes em edges). H3 redução hierárquica: indecidível (sem fan-in).
- Recurso por nó: **zero** noção hoje (NODE_SPECS sem reads/writes; único sha256 é o do cache).

## Épicos propostos
- **E1 (S, alto)** checar interrupt ANTES de despachar tool calls em `loop.py:426` (cuidado: `messages.append` já rodou — descartar turno assistant ou anexar tool_result "interrupted"; precedente `forced_name`). Teste: cancel durante a chamada → `tool_dispatch` spy `call_count == 0`, sem `tool_use` pendente.
- **E2 (S, texto)** `_ALIVE_HINT` e docstring de `_timed_out` nomeando fs scope/fs_allow/shell; spec §8.3 `work-{fence}`; contrato anti-drift.
- **E3 (M/L)** cancel que alcança a chamada em voo (abort por request / streaming em leaves); complemento da #48 (timeout ≠ abort). Teste: stub bloqueante + cancel → terminal em < quiescence_timeout, `clean=True`.
- **E4 (S/M)** manifesto de artefato validado no replay (`validation.py` schema nomeado + hook em `cache_lookup`); cache key intocada. **Decisão do dono**: mismatch → re-spawn / fault / notice.
- **E5 (S)** guidance de autoria: certificador não escreve; devolver manifesto, não prosa; sem path absoluto; `depends_on` sem `${ref}` não é fail-closed.
- **E6 (S, experimento)** fan-out intra-nó sobre recurso compartilhado, com e sem shell — única forma de decidir H2/H3.

## Não fazer
Não aumentar `CANCEL_QUIESCENCE_TIMEOUT`; não criar `requires_success`; não fazer `working_root` por nó; não adicionar `reads:/writes:` declarativos sem enforcement; não promover H2 da #45; não tratar o run como certificação falsa; não confundir os dois fenômenos (23:08 leaf vivo = #45; 23:16 zumbi = #42).

## Não provado
Natureza do comando de 23:16:57 (args redigidos); autoria das escritas de 23:26:47 (gap de audit; candidato: o agente supervisor, não sandboxed); `timeout: 1800` (sustentado por prior_faults + cronômetro); H3 e fan-out intra-nó (sem dado).

---

# Relatório 3 — #43 fallback pré-autorizado de rota


Read-only: código main@0.0.18, `workflow_audit_events` (6 esticadas, 1286 eventos), `workflow_node_cost`/`workflow_run_spend`, tabela `messages` (spec original id=93, pivô humano id=506/507, último status id=524).

## Sumário
1. Não existe recuperação cross-route hoje: falha terminal de leaf por credencial/saldo/5xx → `classify_provider_error` = None (`errors.py:90-109`) → `note_leaf_failure` (`engine.py:778-812`) → fault + nó null → run segue `degraded`. Só quota pausa+auto-resume (mesma rota). `retries` (`strategies.py:239-269`) só cobre saída vazia — no run v4 os 8 nós tinham `retries: 1` e nenhuma das 4 falhas de provider foi retentada.
2. "Saldo insuficiente" é indistinguível de 400 genérico: erro real da Anthropic = `invalid_request_error` HTTP 400 sem `code` conhecido; `errors.py:8-11` proíbe regex sobre prosa. Gatilho honesto tem de ser agnóstico de classe.
3. Pivô do v4 foi HUMANO verbatim (`messages.id=506`: "Autorizo aumentar o token_budget… NÃO use Anthropic… RE-ROTEIE adversarial_audit para gpt-5.6-sol"; id=507: a agente chamou exatamente isso, resto do spec byte-idêntico). Latência falha→retomada ≈ 10m42s. Fronteira SUP-01 §6.2 funcionou.
4. Custo real: 7 células sobreviventes = 2.383.516 tokens; `workflow_run_spend` = 5.329.386 → **2.945.870 (55,3%) fora de célula** (tentativas vazias, re-runs, faults, timeouts). Re-run same-route recuperou 3 nós; cross-route recuperou 1 (adversarial_audit) e NÃO recuperou outro (repair_and_finalize: codex×2 → glm×2, zero `leaf.completed`). Fallback ≠ conserto.
5. Restrição dura = cell hash: nós `agent` têm rota na chave (`strategies.py:218-222`); nós de rigor/gate só se declararem campo de routing (`_routing_identity` `strategies.py:152-166` devolve `()`) → fallback abaixo do hash = cache poisoning.

## Mecanismos existentes
- `workflow_tiers.json` (tier → 1 destino; adoção real ZERO — nenhum arquivo em ~/.lohra; v4 usou rotas explícitas). Cadeia = M (6 pontos de resolução).
- `ClientPool` = gate de CREDENCIAL, não de billing ("tem chave" ≠ "está no envelope"); único gate de autoridade é `_build_subscription` (openai-codex exige `subscription_active`).
- `pricing/` tem tudo para orçar por rota, mas nenhum consumidor compara rotas; `workflow_node_cost`/`run_spend` sem coluna de rota; openrouter é preço dinâmico. Dependência dura da #46.
- `checkpoint`/pausa durável: pronta para uma `pause_reason` nova (`route_fault`).

## Opções para o dono
- **A** manter manual + fault didático (S): sem risco; não recupera tokens; latência não era o gargalo.
- **B** envelope do operador `~/.lohra/workflow_routes.json` (cadeia ordenada + `max_usd_per_cell` fail-closed + `max_fallbacks_per_run`), autorizado ANTES do run; harness escolhe dentro; evento `node.rerouted` (M). Resolveria o caso do v4 sem intervenção; não salvaria `repair_and_finalize`. Só age em nós com campo de routing. Gate de subscription permanece.
- **C** pausar `route_fault` em vez de degradar, após re-spawn same-route bounded (M): zero autoridade nova; ataca o custo medido (4 nós rodando após o comprometimento). Risco: falso positivo em 5xx transitório → mitigado pelo re-spawn prévio.
- **Recomendação: C primeiro, depois B**; A (fault didático) é pré-requisito dos dois. Não pendurar só em `tier`.

## SUP-01/#36
Fallback executado pelo harness dentro de envelope de operador é CONFIGURAÇÃO, não contorno (§6.3 governa decisões do agente; §6.2 já prevê "pricing metadata ou preauthorization humana"; SUP-04 idem). Teste de 3 pernas §6.3 item 5: reversível ✅ (célula nova); orçada ⚠️ só com USD por rota (#46); registrada ⚠️ parcial (só audit com LOHRA_AUDIT; faults sem linha de re-rota). Cadeia de N rotas = N adaptações na chave → envelope precisa do próprio bound; contador durável = fatia da #36.

## Épicos
E1 (S) re-spawn same-route em falha TERMINAL gateado pelo `retries` já autorado (quota/admin/timeout excluídos; hash inalterado) · E2 (M) envelope `workflow_routes.json` (`workflow/routes.py` espelhando tiers; testes: spec não cria envelope; nó de rigor sem routing recusa; codex continua gateado; cadeia finita) · E3 (M) pausa `route_fault` sem auto-resume · E4 (S) registro honesto de rota (fault + `node.rerouted`) · E5 (M, investigativo) timeout com incerteza de cobrança · E6 (M, bloqueante para E2) contador durável de fallbacks (fatia da #36).

## Não fazer
Regex sobre prosa; fallback abaixo do cell_hash; fallback para rota mais cara sem pré-autorização; spec definindo envelope; auto-escalar para subscription; fallback em quota; fallback em nó de rigor sem routing; cadeia ilimitada; prometer routing em parallel/stages (schema não tem).

## Não provado
`workflow_run_state` ainda `running` (6 segment.started × 4 completed); re-rotas para glm e budget 6M→8M sem `run_workflow` persistido (turno não salvo); `workflow_insight_candidates` vazia (dado para Wave 9); fault [6] é `str(exc)` cru — classificação TIMEOUT não estava aplicada no build do run; sem bancada de providers dublados.
