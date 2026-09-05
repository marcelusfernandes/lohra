# Reanálise READ-ONLY — issue #52 (W9-T3, dead-turn notice) sob a decisão do dono de 2026-09-05

**Natureza:** investigação read-only. Nenhum arquivo do repo, de `~/.lohra` ou de qualquer `state.db` foi alterado. Os únicos writes foram cópias dos `state.db` para o scratchpad (necessário porque `sqlite3 -readonly` e `mode=ro` falharam com `unable to open database file` neste sandbox — o mesmo obstáculo que o T0 já registrou) e este relatório. Nenhuma query de escrita (`INSERT`/`UPDATE`/`DELETE`/`PRAGMA wal_checkpoint`) foi executada contra as cópias.

**Fontes:**
- `gh issue view 52 --repo marcelusfernandes/lohra --comments` — issue aberta sem comentários; corpo é a proposta original.
- `gh issue view 54 --repo marcelusfernandes/lohra --comments` — 4 comentários: resumo do T0, fechamento, e a **decisão do dono em 2026-09-05**.
- `docs/history/reviews/2026-09-03-wave9-t0-taxonomy.md` (relatório T0 completo, 284 linhas).
- `backend/lohra/state/notices.py`, `backend/lohra/agent/notices_overlay.py`, `backend/lohra/workflow/failure_taxonomy.py`, `backend/lohra/workflow/service.py:973`, `backend/lohra/workflow/notify.py:32` — código que produz e classifica as notices.
- Cópias de `~/.lohra/state.db` (HOME) e de `~/.lohra/profiles/{lohra-dogfood-w75,lohra-notion-v2,lohra-notion-v3sol,lohra-notion-v4}/state.db`, lidas com `sqlite3.connect()` padrão (rw) sobre a CÓPIA — nunca sobre o original — só com `SELECT`.

---

## Decisão do dono (2026-09-05) que muda o denominador

> Modelo inexistente escolhido pelo autor = **`agency`**. Complemento: se cometido por instrução humana (o humano pediu um modelo que não existe), a Lohra deve escolher um modelo existente adequado à tarefa em vez de falhar/pausar. Isso adiciona comportamento à taxonomia: **rota inexistente na autoria → substituição por catálogo com aviso (advisory + nota no rollup), classificada como `agency` para o loop de aprendizado.**

Essa decisão altera exatamente **uma** classe de causa dentro dos 57: rejeição do provider por *modelo/slug inexistente*. Não decide nada sobre as outras classes de `external_rejection` (ex.: parâmetro incompatível), timeout, credencial, sinal ou cancelamento humano — tratadas abaixo como **não cobertas** pela decisão.

---

## 1. Onde estão as 57 notices e como foram re-obtidas

O T0 (2026-09-03) contou **53 `durable_notices` + 4 `notice_trail` = 57**, distribuídos em 5 profiles: HOME (13+2), `lohra-dogfood-w75` (16+2), `lohra-notion-v3sol` (11+0), `lohra-notion-v4` (11+0), `lohra-notion-v2` (2+0). A cópia de `state.db` que o T0 usou era um scratchpad efêmero de outra sessão — **não existe mais**. Portanto os 57 exatos não são recuperáveis byte-a-byte; foram **reconstruídos**, com o seguinte método e o seguinte grau de confiança:

- **HOME, `v2`, `v3sol`, `v4` (39 dos 57): reconstrução EXATA.** Esses 4 profiles não tiveram nenhuma atividade nova depois de 2026-08-31/09-01 (confirmado pelos próprios timestamps das linhas — o notice mais recente do HOME é de 2026-09-01 23:16, e os 3 profiles `v2/v3sol/v4` só têm dados de 2026-08-31). Os totais batem exatamente com o T0 (13/2/11/0/11/0/2/0) e o texto de cada linha foi lido integralmente.
- **`lohra-dogfood-w75` (18 dos 57): reconstrução por evidência indireta, não por cópia direta.** Esse profile teve testes de dogfood adicionais em 2026-09-03 à tarde (14h–17h, fora da janela do T0) e em 2026-09-05 (relacionados à própria Wave 8.4/8.5/9 que fecharam depois do T0) — hoje tem 29 `durable_notices` + 24 `notice_trail` = 53 linhas físicas, não mais 18. Reconstrução: como `notice_trail` nunca sofre hard-delete dentro de 30 dias (TTL do trail) e o T0 não escreveu nada, **o total de fatos criados até o instante T do T0 é igual à contagem atual de linhas (live+trail) com `created_at` ≤ T**, função monótona em T. Ordenando as 53 linhas atuais por `created_at`, a contagem acumulada atinge exatamente **18** no evento de `2026-09-03 01:00:52` — e o próximo evento só ocorre em `2026-09-03 09:51:46` (~9h de silêncio). Esse é o único ponto de corte que reproduz os totais do T0 (16+2) com uma folga temporal clara, e a composição por classe de causa resultante bate **exatamente** com a tabela de buckets do §1.4 do T0 (verificação abaixo). Adoto essa fatia (os 18 eventos cronologicamente mais antigos) como o \"dogfood-w75 da era T0\".
- **Verificação cruzada (evidência de que a reconstrução está certa):** somando as 57 linhas reconstruídas por padrão de texto, os buckets batem 1:1 com a tabela do T0 (§1.4): operacional/\"finished\"+\"recovered\" = 33 (58%); rejeição externa 400 (modelo+parâmetro) = 7; timeout/overload = 5; `turn interrupted` = 5; SIGTERM = 3; credencial/saldo = 2; `max_iterations` = 1; 5xx genérico = 1. **57/57.** Isso não prova identidade byte-a-byte com o dataset original do T0, mas prova que a composição por causa é a mesma — o que é o que a reclassificação abaixo precisa.
- **TTL explica a divergência física:** notices \"turn error/interrupted/killed\" usam `DEAD_TURN_TTL_SECONDS = 24h` (`agent/notices_overlay.py:23`, aplicado em `cli.py:625-633`); já \"workflow X finished\" usa o TTL padrão de 7 dias (`workflow/service.py:973`, sem `ttl_seconds` explícito). Por isso os 6 \"nonexistent-model\" do dogfood, criados em 2026-09-02, já viraram tombstone `expired` por volta de 2026-09-05 — a primeira atividade do profile depois do TTL de 24h dispara a purga lazy.

**Cobertura declarada:** 57/57 localizadas e classificadas (39 por leitura direta, 18 por reconstrução cronológica com verificação de bucket).

---

## 2. Reclassificação de todas as 57 sob a decisão de 2026-09-05

| # | origem | id | owner | texto (≤120c) | classe antiga | classe nova | justificativa |
|---|---|---|---|---|---|---|---|
| 1 | home/live | 8 | `sup05livewf1` | workflow live-notice-proof (066df7b4) finished: complete, spent 713 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 2 | home/live | 9 | `sup06adversa` | workflow sup06-budget-trap (c798c0c9) finished: paused, spent 755 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 3 | home/live | 11 | `sup06slug178` | workflow sup06-slug-trap (50368983) finished: failed, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 4 | home/live | 12 | `sup06adv2178` | workflow sup06-budget-trap-2 (eb65f954) finished: paused, spent 417 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 5 | home/live | 13 | `sup06slug178` | workflow sup06-slug-trap (50368983) finished: complete, spent 700 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 6 | home/live | 14 | `855109e3a47b` | workflow fato-haiku (a8fdbbbd) finished: complete, spent 2618 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 7 | home/live | 15 | `8673357162ce` | workflow fato-vulcao-distico (db9504fd) finished: complete, spent 997 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 8 | home/live | 16 | `b8545ac941a3` | workflow fato-deserto-distico (e3d5a63a) finished: complete, spent 959 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 9 | home/live | 17 | `6a75992c6949` | workflow fato-gelo-minimo (83044869) finished: complete, spent 474 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 10 | home/live | 18 | `c4848c5ebee3` | workflow fato-neve-minimo (2c7cc96b) finished: complete, spent 470 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 11 | home/live | 19 | `4ef65b10f8eb` | workflow fato-chuva-minimo (0075c73c) finished: complete, spent 465 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 12 | home/live | 20 | `c36a9e7698db` | workflow fato-vento-minimo (732c6d33) finished: complete, spent 461 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 13 | home/live | 23 | `800f3bd217de` | turn error; error=Our servers are currently overloaded. Please try again later. | environment | environment | unchanged — sobrecarga do provider, fora da decisão do dono |
| 14 | home/trail | 1 | `dogfood-kill` | turn killed (SIGTERM); error=the process was terminated by a signal before the turn completed | infrastructure | infrastructure | unchanged — morte por sinal, fora da decisão do dono |
| 15 | home/trail | 2 | `dogfood-kill` | turn killed (SIGTERM); error=the process was terminated by a signal before the turn completed | infrastructure | infrastructure | unchanged — morte por sinal, fora da decisão do dono |
| 16 | dogfood/live | 1 | `555a5f4feb1f` | workflow required-timeout-harness-test (06c2cfe1) finished: failed, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 17 | dogfood/trail | 2 | `a018b1a5e55c` | turn error; error=Our servers are currently overloaded. Please try again later. | environment | environment | unchanged — sobrecarga do provider, fora da decisão do dono |
| 18 | dogfood/live | 3 | `a6a800e66efc` | workflow three-ocean-paragraphs (41f725ac) finished: paused, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 19 | dogfood/live | 4 | `112278769165` | workflow shell (f6516b21) finished: complete, spent 466 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 20 | dogfood/live | 5 | `57683c9752a9` | workflow shell (f0db0a22) finished: complete, spent 1047 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 21 | dogfood/trail | 6 | `c721710df58d` | turn error; error_kind=timeout; error=Request timed out. | environment | environment | unchanged — timeout de transporte, fora da decisão do dono |
| 22 | dogfood/live | 7 | `296c26928e76` | workflow disconnected-validator-test (78358734) finished: complete, spent 722 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 23 | dogfood/live | 8 | `72de9fccad0b` | workflow three-planet-paragraphs (7b320f80) finished: paused, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 24 | dogfood/trail | 9 | `11e9d80fd70b` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 25 | dogfood/trail | 10 | `fdc050d3351a` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 26 | dogfood/trail | 11 | `e7af02f1df60` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 27 | dogfood/live | 12 | `60c0328fca9e` | workflow doomed (9cd6635c) finished: failed, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 28 | dogfood/trail | 13 | `606d1e5da3cd` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 29 | dogfood/live | 14 | `2414dc6b89b9` | workflow harness-test (95ff5fb8) finished: failed, spent 0 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 30 | dogfood/trail | 15 | `1281d1ab4380` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 31 | dogfood/trail | 16 | `33cb3d465fe8` | turn error; error=Error code: 400 - {’error’: {’message’: ’nonexistent-vendor/no-such-model-xyz is not a valid model ID’ | environment | agency | decisão do dono 2026-09-05: modelo inexistente escolhido na autoria = agency (rota inexistente → catálogo deveria pegar) |
| 32 | dogfood/live | 17 | `e692b5e34d3f` | workflow harness-test (09745ca2) finished: paused, spent 363 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 33 | dogfood/live | 18 | `8af0ad6ab1e7` | workflow harness-test (09745ca2) finished: complete, spent 1024 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 34 | v3sol/live | 1 | `ff4d0503e113` | turn error; error=Request timed out. | environment | environment | unchanged — timeout de transporte, fora da decisão do dono |
| 35 | v3sol/live | 2 | `a835fee64c16` | workflow notion-pdf-automation-package-v3 (663fab4c) finished: paused, spent 179901 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 36 | v3sol/live | 3 | `cb2f44a5e1ff` | turn interrupted | unknown | unknown | unchanged — taxonomia não distingue quem cancelou (CANCELLATION -> UNKNOWN sempre) |
| 37 | v3sol/live | 4 | `86d33c7790e5` | turn interrupted | unknown | unknown | unchanged — taxonomia não distingue quem cancelou (CANCELLATION -> UNKNOWN sempre) |
| 38 | v3sol/live | 5 | `fbfb4b06667e` | turn interrupted | unknown | unknown | unchanged — taxonomia não distingue quem cancelou (CANCELLATION -> UNKNOWN sempre) |
| 39 | v3sol/live | 6 | `a835fee64c16` | workflow notion-pdf-automation-package-v3 (663fab4c) finished: degraded, spent 229834 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 40 | v3sol/live | 7 | `a835fee64c16` | workflow notion-pdf-automation-package-v3 (663fab4c) finished: complete, spent 248464 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 41 | v3sol/live | 8 | `1ed61087a206` | turn error; error=Error code: 400 - {’error’: {’message’: ’Provider returned error’, ’code’: 400, ’metadata’: {’raw’: ’d | environment | unknown | não é 'modelo inexistente' — é combinação de parâmetro (tool_choice+thinking) rejeitada pelo provider; decisão do dono não cobre este caso; poderia ser agency (spec_shape) mas não foi decidido — marco unknown, não force |
| 42 | v3sol/live | 9 | `78f591dfbed8` | workflow notion-pdf-automation-v3 (55c2b941) finished: degraded, spent 1987 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 43 | v3sol/live | 11 | `78f591dfbed8` | workflow notion-pdf-automation-v3 (55c2b941) finished: degraded, spent 113405 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 44 | v3sol/live | 12 | `78f591dfbed8` | workflow notion-pdf-automation-v3 (55c2b941) finished: degraded, spent 164925 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 45 | v4/live | 1 | `358c4cc4be3e` | workflow notion-pdf-automation-v4 (42abc3eb) finished: paused, spent 471092 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 46 | v4/live | 2 | `354edbaff4db` | turn error; error=An error occurred while processing your request. You can retry your request, or contact us through our | environment | environment | unchanged — erro genérico 5xx-like do provider |
| 47 | v4/live | 3 | `8bca1e225b9f` | turn error; error={’type’: ’error’, ’error’: {’details’: None, ’type’: ’invalid_request_error’, ’message’: ’Your credit  | environment | environment | unchanged — credencial/saldo, fora da decisão do dono |
| 48 | v4/live | 4 | `358c4cc4be3e` | workflow notion-pdf-automation-v4 (42abc3eb) finished: paused, spent 3225235 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 49 | v4/live | 5 | `c79d9e7e0381` | turn error; error=The read operation timed out | environment | environment | unchanged — timeout de transporte, fora da decisão do dono |
| 50 | v4/live | 6 | `111feb0450c2` | turn interrupted | unknown | unknown | unchanged — taxonomia não distingue quem cancelou (CANCELLATION -> UNKNOWN sempre) |
| 51 | v4/live | 7 | `358c4cc4be3e` | workflow notion-pdf-automation-v4 (42abc3eb) finished: degraded, spent 4942567 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 52 | v4/live | 8 | `32b6c17a49a1` | turn interrupted | unknown | unknown | unchanged — taxonomia não distingue quem cancelou (CANCELLATION -> UNKNOWN sempre) |
| 53 | v4/live | 9 | `358c4cc4be3e` | workflow notion-pdf-automation-v4 (42abc3eb) finished: degraded, spent 5329386 tokens | n/a (rollup de fim de workflow, não fault de turno) | n/a | unchanged — não é dead-turn fault; T0 já não classificava (58% 'nem falha') |
| 54 | v4/live | 10 | `358c4cc4be3e` | turn killed (SIGTERM); error=the process was terminated by a signal before the turn completed | infrastructure | infrastructure | unchanged — morte por sinal, fora da decisão do dono |
| 55 | v4/live | 11 | `358c4cc4be3e` | workflow run 42abc3eb28e64e1ba8505a132ef8e1f8 recovered: the process running it stopped before it finished; completed ce | n/a (recovery de processo, não fault de turno) | n/a | unchanged — evento de recovery, mesma classe operacional |
| 56 | v2/live | 1 | `3ed056f67ba0` | turn error; error=Error code: 401 - {’error’: {’message’: ’Provided authentication token is expired.’, ’type’: None, ’co | environment | environment | unchanged — credencial/saldo, fora da decisão do dono |
| 57 | v2/live | 2 | `beb9c4cfa0bc` | turn error; error=max_iterations (20) reached without a final response | infrastructure (não aprendível) | infrastructure (não aprendível) | unchanged — limite do harness, não decidido pela pergunta do dono |

**Resumo da mudança (classe antiga → classe nova):**

| Transição | n |
|---|---:|
| `environment` → `agency` | **6** |
| `environment` → `unknown` (não coberto pela decisão) | 1 |
| `environment` → `environment` (inalterado) | 8 |
| `infrastructure` → `infrastructure` (inalterado) | 3 |
| `infrastructure (não aprendível)` → inalterado (`max_iterations`) | 1 |
| `unknown` → `unknown` (inalterado, cancelamento humano) | 5 |
| `n/a` (rollup operacional) → inalterado | 33 |
| **Total** | **57** |

Os 6 reclassificados são **todos a mesma causa raiz**, replicada por 6 sessões de teste distintas do dogfood-w75: `Error code: 400 ... 'nonexistent-vendor/no-such-model-xyz is not a valid model ID'`. O 1º item marcado `unknown` (`v3sol`, item #41 na tabela) é uma rejeição 400 diferente — `tool_choice` incompatível com modo `thinking` — que **não** é \"modelo inexistente\"; a decisão do dono não fala disso, então não force.

---

## 3. Denominador que a issue precisa

- **\"Revisável pelo agente\" sob o novo limite (agency)** = **6/57 (10,5%)**. Sob o limite antigo era 0/57.
- **Dessas 6, quantas desaparecem quando E8 (#85, substituição por catálogo) estiver no ar?** **6/6 (100%)**. A causa das 6 é exatamente o alvo do E8: modelo/slug inexistente na spec. Com a substituição em vigor, o turno não morre mais por esse motivo — vira sucesso com aviso (advisory + nota no rollup), não um dead-turn notice. Ou seja: **a fatia inteira que a decisão do dono acabou de mover para `agency` é a mesma fatia que outra épico está prestes a fazer desaparecer como falha.** Isso é relevante para o desenho do #52: não vale a pena desenhar um nudge em cima de uma classe de causa que, estruturalmente, está prestes a deixar de gerar notice de falha.
- Consequência: mesmo depois da decisão do dono, o **estoque útil de notices \"agency\" observadas neste dataset, pós-E8, tende a ZERO** — não porque a taxonomia errou, mas porque a única fonte de agency observada era um bug de UX (falta de validação de catálogo) que outro épico corrige na origem.

---

## 4. \"A nula\" ainda se sustenta?

**Sim — e mais fortemente do que o T0 já mostrava, por um motivo adicional que a reclassificação expôs.**

Além de reclassificar a causa, verifiquei se alguma das 57 notices foi **de fato entregue e consumida** por um turno seguinte (campo `reason` do `notice_trail`: `acked` = foi reivindicada, consumida e confirmada por um turno vivo na mesma lineage; `expired` = nunca foi reivindicada, morreu pelo TTL sem que ninguém a lesse; `evicted` = descartada por estouro de cap).

Resultado: **das 57, só 1 tem `reason = acked`** — o item #14 da tabela (`home/trail id=1`, owner `dogfood-kill-w7`): `\"turn killed (SIGTERM); error=the process was terminated by a signal before the turn completed\"`, classe `infrastructure` (morte por sinal), **não** uma das 6 reclassificadas. As 6 notices de \"modelo inexistente\" (itens #24, #25, #26, #28, #30, #31 na tabela) têm todas `reason = expired`: nenhuma foi reivindicada por um turno seguinte na mesma lineage — cada uma nasceu de uma sessão de teste isolada (`owner_id` distinto por linha, sem continuação visível) e morreu pelo TTL de 24h sem que o mecanismo de overlay (`claim_lineage_notices`, que só entrega para owners da cadeia root→tip) chegasse a apresentá-la a ninguém.

**Portanto: não há, nas 57, nenhum caso em que uma notice reclassificada para `agency` chegou a ser vista por um próximo turno — logo não há evidência de que um nudge de memória naquele momento teria mudado a próxima ação do agente, porque o momento nunca aconteceu na prática para essas 6.** O único caso com entrega comprovada é de uma classe (`infrastructure`/SIGTERM) que a decisão do dono não tocou, e mesmo esse caso é um evento de teste de dogfood explicitamente desenhado para produzir SIGTERM (`dogfood-kill-w7`) — não uma falha orgânica em produção.

**Achado colateral não pedido, mas relevante para o desenho do #52:** o desenho de teste que gerou as 6 notices de \"modelo inexistente\" usa sessões de um turno só, sem continuação na mesma lineage — o overlay estrutural do #52 nunca teria a chance de disparar para esse padrão de teste. Se a wave quiser medir empiricamente o efeito de um nudge (ver §5), o cenário de teste precisa **forçar continuidade na mesma lineage**, coisa que o dogfood atual não faz.

---

## 5. O que falta para fechar #52 pela própria \"evidência mínima\"

A issue pede uma **comparação controlada de 4 variantes**, sob o mesmo cenário/orçamento: (1) overlay atual; (2) indicação neutra de evidência revisável; (3) evidência tipada/escopada; (4) nenhuma intervenção. Medindo: precisão/utilidade da decisão, falsas lições, validade na sessão seguinte, custo em tokens/latência, impacto na tarefa principal, recorrência futura.

**Isso é viável offline com provider fake — e a base já existe no repo.** `backend/tests/test_client.py` já tem o padrão (`FakeCompletions`, injetada em `OpenAIClient._client`) para scriptar respostas determinísticas por chamada, sem custo de API real. Falta compor isso com o restante do harness (`agent/loop.py`, `state/notices.py`, `agent/notices_overlay.py`) numa fixture de sessão viva.

**Menor experimento proposto:**

1. **Fixture:** uma sessão fake de 2 turnos na MESMA lineage — turno N falha por uma causa `agency` real e observável (ex.: `tool_choice` malformado ou spec rejeitada por `validate_spec`, não \"modelo inexistente\" — essa classe está prestes a sumir com o E8, não vale a pena testar em cima dela) publicando o dead-turn notice; turno N+1, na mesma lineage, reivindica (`claim_lineage_notices`) e recebe UMA das 4 variantes de overlay antes do prompt.
2. **4 variantes do texto do overlay** injetadas no turno N+1: (a) atual (`build_turn_notice`, só operacional); (b) + uma linha neutra apontando \"há evidência causal revisável, ver `workflow_templates`\"; (c) + o `FailureObservation` tipado (mechanism/responsibility/confidence) já existente em `insights.py`, sem prosa livre; (d) sem overlay algum (controle).
3. **N pequeno:** 8–12 episódios por variante (32–48 total) com um modelo real barato (ex. haiku-class) só no turno N+1 — os turnos N (a falha) não precisam de modelo real, são 100% scriptados via fake provider.
4. **Métricas por episódio:** (i) o agente escreveu em memória/skill? correta ou incorretamente (a notice é sempre `agency` real aqui, então \"correto\" = reconhecer e não promover a fato ambiental); (ii) tokens gastos no turno N+1 (delta por variante); (iii) o agente corrigiu o defeito real no retry (proxy de utilidade); (iv) se simular uma sessão seguinte reaproveitando a mesma lição (quando escrita), ela ainda é válida.
5. **Custo:** baixo — a única chamada real de LLM é o turno N+1 em 32–48 episódios curtos (a falha do turno N é 100% fake); no modelo haiku-class isso é da ordem de poucos dólares e minutos, sem tocar profile de produção.

Esse desenho resolve também o achado do §4: força deliberadamente a continuidade de lineage que o dogfood atual não tem, então mede o efeito do nudge no único cenário em que ele pode, estruturalmente, ter algum.

---

## Limites desta reanálise

- Os 57 não são recuperáveis byte-a-byte (a cópia do T0 era efêmera); a reconstrução é por composição de bucket verificada (57/57 bate com o T0), não por hash de arquivo.
- O corte de \"18 mais antigos\" do dogfood-w75 é inferência a partir de contagem monotônica + gap temporal de 9h, não uma marca explícita no dado; é a explicação mais simples e a única compatível com os totais do T0, mas não é prova formal.
- \"Nenhuma notice `agency` foi consumida\" é uma leitura do campo `reason` das 57 linhas reconstruídas; não cobre notices publicadas fora dessa amostra.
- O item classificado `unknown` (`tool_choice` incompatível) é uma leitura minha, não uma decisão do dono — sinalizado como tal, não misturado com os 6 confirmados.
