# Dogfood via Codex CLI — 2026-08-26 (M7 ao vivo)

> Operador: usuário no Codex CLI (skill `use-lohra`), profile `lohra-lohra` (teste 1) e sessões avulsas.
> Log mantido pelo Claude Code conforme os resultados chegam.

## Teste 1 — checkpoint (humano no loop) · runs `cee5d97b` → `23582ea2`
- **Turno 1: PASSOU** — run pausou certinho no checkpoint.
- **Turno 2 (resume com a resposta): BUG REAL → WF-24.** Faults observados:
  `analyze: upstream null: args.source` → cascata até `plan`. Causa confirmada no código:
  `service.py:384/410` usa `args or {}` — o resume manual reidrata o **spec** (`_launch_spec`)
  mas NÃO os **args**, mesmo com `RunState.args` persistido (o auto-resume de quota na linha
  ~522 passa `state.args` — assimetria pura).
- **O que funcionou mesmo assim:** o fail-closed do M1 nomeou a causa exata em cada fault
  (nenhuma mentira), e o agente da Lohra se RECUPEROU sozinho — detectou a cascata, reconstruiu
  o nó final como run novo (`23582ea2`, model claude-opus-4-8 · cross-provider!) e entregou o
  plano. Resiliência de agente compensando bug de harness.
- **Dano secundário → WF-25:** o run falho gravou 2 priors no `insights.md` do profile culpando
  a FORMA ("Revise: add a verify stage") por um bug de reidratação — mesma classe "lição errada"
  do quota pré-M4. O texto do prior é boilerplate genérico; deveria citar os faults reais.

## Teste 2 — node `gate` · run `215951b9` · PASSOU (fraco)
- Aprovou no attempt 1 (o haiku já continha "cache") — mecânica rodou, mas o loop de
  reprovação→feedback não foi exercitado. `tokens_spent_total` presente (WF-23 visível).
- Re-teste sugerido: validador com critério que o 1º draft dificilmente atende
  (ex.: "só aprove se contiver a palavra 'idempotente' E tiver exatamente 3 versos").

## Teste 3 — tiers sem mapping · run `177c3227` · PASSOU PERFEITO
- Fault didático nomeando `~/.lohra/workflow_tiers.json`, nó rodou no model default,
  status `degraded`, resumo em 3 linhas correto. O caminho honesto do erro, como desenhado.
- Nota: o prior gravado para esse degraded também é lição-errada (config do operador ≠ shape) — WF-25.

## Teste 4 — assistir/pausar/retomar sem spec · run `21ae6b96` · PASSOU + 3 achados
- **M6 ao vivo ✅**: 3 snapshots mid-run sem wait (running, tokens 0→15.510), `workflow_list` com
  spend parcial, pause com fault informativo + `resume_at: null`, resume **só com
  `{resume_run_id}`** (WF-22 funcionando — zero re-envio de spec).
- **WF-28 (MED-HIGH, suspeita do review CONFIRMADA ao vivo):** no pause o único nó (parallel,
  3 branches) já estava `done 1/1` com 70.851 tokens; o final fechou em **164.465** — o resume
  RE-PAGOU ~93,6k por trabalho pronto. Causa: só `agent` e stages de pipeline chamam
  `cache_store`; parallel/verify/judge_panel/loop não têm célula → resume re-executa. Viola
  "nunca perder trabalho pago" para 4 dos 10 node-types.
- **WF-26 (LOW-MED):** faults são por-segmento — o fault do pause (e qualquer fault real do 1º
  trecho) some do status final (`faults: []`); um degraded do trecho 1 leria como run limpa.
  Mesmo tratamento do WF-23: cumulativo ao lado do segmento.
- **WF-27 (LOW):** parallel numa run com 3 branches mostrou `total: 1` o tempo todo — sem
  `items {done,total}` como o pipeline tem; o operador não vê o fan-out andando. Publicar
  branches settled/total (mesma mecânica do note_items).

## Backlog derivado
- **WF-24 (HIGH):** resume manual deve reidratar `args` persistidos (explícito vence, senão
  `RunState.args`) — espelhar o `_launch_spec` do spec. Teste: checkpoint → resume só com
  `resume_run_id`+`checkpoint_answers` → refs `${args.*}` intactas.
- **WF-25 (MED):** `_record_prior` deve citar os faults reais do run (causa) em vez do conselho
  boilerplate "add a verify stage"; e run degraded APENAS por warning de config do operador
  (tier sem mapping) não deveria virar prior de shape.
- **WF-26 (LOW-MED):** faults cumulativos (ou "prior segment faults") no status final de run retomado.
- **WF-27 (LOW):** progress do parallel publica branches settled/total (como pipeline items).
- **WF-28 (MED-HIGH):** cell-cache para parallel/verify/judge_panel/loop — evidência ao vivo:
  resume re-pagou 93,6k tokens de um nó já concluído (run 21ae6b96).

## Desfecho (commit `9d2feb9`, suíte 1248 passed)
Todos os 5 itens corrigidos no mesmo dia, com review adversarial rendendo mais 3 findings válidos:
- branch `""` de parallel seria congelada no cache como completion (gate per-elemento);
- `record_outcome` só via o último trecho (degraded do 1º trecho viraria template);
- prior não citava faults de trechos anteriores — resolvido separando citação (pause_fault
  identificável) de veredito (pause não bloqueia template).
Re-rodar a bateria no Codex agora deve fechar o teste 1 sem auto-recuperação do agente e o
teste 4 sem re-pagamento no resume.

---

## Redux (2026-08-26, pós-fixes `9d2feb9` — bateria re-rodada pelo Claude Code)

> Tentativa via `codex exec` headless bloqueada por **política enterprise** do Codex do usuário
> (força approval humano + proíbe danger-full-access → sandbox read-only sem rede). Dogfood via
> codex nesta máquina exige humano aprovando (como o usuário fez). Bateria re-rodada direto.

| Turno | Resultado |
|---|---|
| A1 checkpoint | ✅ args `{"foco": ...}` passados, pausa em `approve` com payload completo |
| A2 resume só com run_id+answers | ❌ **WF-29 (novo, HIGH)** — "o processo perdeu o estado do run" |
| B1 parallel+pause | ✅ **WF-27 ao vivo**: `items {0→3}/3` nos snapshots; pause a 30.892 tokens |
| B2 resume só com run_id | ❌ mesma parede — erro didático: "a run this process never launched has nothing to replay" |
| B3 resume COM spec (cross-process) | ✅✅ `complete`, segmento **0 in / 0 out** (zero spawns — células do cache SQLite), `tokens_spent_total: 30.892` = o valor do pause |

**Veredito:** WF-28+WF-23 validados CROSS-PROCESS — o re-pagamento de 93,6k virou **zero**.
WF-24/27 corretos. O que sobrou é estrutural:

- **WF-29 (HIGH):** `RunState` (spec_dict/args/owner/payload de pause) vive só na memória do
  service — e cada turno do `lohra chat` é um processo novo. O resume manual "sem re-enviar nada"
  só funciona in-process; e o **checkpoint é por definição cross-process** (o humano responde
  depois). Fix: persistir o estado essencial do run no SessionDB (ledger e cell-cache JÁ são
  duráveis — falta só o estado); de quebra habilita o cold-start rearm do auto-resume de quota
  (timers também são in-memory — gap que o pi cobria com PersistedRunState + coldStartRearm).

## WF-29 entregue (`9f2d364`, suíte 1272 passed, 94% cobertura)
Estado do run agora é durável (workflow_run_state) com lease single-winner (padrão
compression_locks + LeaseHeartbeat próprio — o CRITICAL do review: renovação só em cache-write
deixava nó lento perder a lease) e cold-start rearm do quota. O checkpoint agora sobrevive ao
processo: resume noutro turno só com run_id+answers, células replayadas com 0 chamadas ao client
(teste com dois WorkflowService sobre o mesmo DB). Bônus fail-closed do implementador: taint OR'd
no resume. Pronto para a bateria ao vivo no Codex.

## Ato 1 do teste ao vivo (WF-29 em produção) — PASSOU, com direito a história
Run `4df13afb` (profile lohra-lohra): pausa no checkpoint com payload+hint → prompt 2 no profile
errado provou o ISOLAMENTO de profiles (erro honesto "nothing on disk names this run" — naquele
disco) → prompt corrigido: processo virgem viu o run pausado, retomou SÓ com run_id+answers,
`approve: "sim"` virou output, plan rodou, analyze de 3.903 tokens veio do cache (segmento:
1.372/343; total 4.120→5.835), faults_total preservou o histórico.
**E o meta-momento da jornada:** o nó `analyze` desse workflow revisou `lease_heartbeat.py`
(código de HOJE) e achou uma race REAL (stop×tick — timer re-armado imortal); o plano de fix
foi aprovado pelo humano via o próprio checkpoint e implementado como WF-30 (`RED→GREEN`,
suíte 1273). De quebra: skill use-lohra ensinava flag em posição inválida — corrigida.

## Ato 2 do teste ao vivo — PASSOU (o 93,6k→0 comprovado na máquina do usuário)
Run `c39da043` (profile lohra-lohra, agora na subscription): parallel 3 branches → snapshots
mid-run com `items {0→3}/3` → pause a **37.160** tokens → sessão/processo novos retomaram SÓ com
`resume_run_id` → `complete` com segmento **0 in / 0 out** (célula durável replayada, zero
chamadas ao provider), total preservado em 37.160, `faults_total` carregando o fault do pause.
O cenário que custou 93,6k re-pagos no primeiro dogfood agora custa exatamente zero.

**Candidatos a triar** (gerados pelo próprio review de 3 branches — NÃO verificados; o precedente
WF-30 justifica olhar): shutdown() solta leases sem pedir cancel (engine sobrevivente ×
novo dono); cancel() sem liveness/lock pode re-rotular run completa (lease-check→mark não
atômico); agendamento do pause antes do persist/release final pode fazer um auto-resume precoce
ser recusado como "still live" e estrangular o run (timer consumido).

## Ato 3 — PASSOU · Bateria ao vivo COMPLETA
Processo virgem listou 3 runs (todos de processos que nunca viu): `service-three-aspects`
complete 37.160 · `service-py-3aspect-parallel` complete 5.539 · `lease-heartbeat-improvement`
complete 5.835. Runs pré-WF-29 corretamente ausentes (sem linha durável — o sistema não inventa
memória). Placar final da bateria ao vivo via Codex: **checkpoint cross-process ✓ · isolamento
de profiles ✓ (por acidente) · resume 0-token ✓ · listagem durável ✓ · subscription no profile ✓
· WF-30 achado-aprovado-e-corrigido pelo próprio ciclo ✓**.
