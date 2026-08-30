# Investigação SUP-01 — Supervisão ativa dos workflows em voo

Data: 2026-08-30

Baseline: `b9eb5f5` (HEAD da branch `feat/lohra-epic-sup`)

Branch: `feat/lohra-epic-sup`

Esta investigação usou análise estática do guidance das tools, da skill builtin
de autoria, e dos sinais que o harness emite para o agente-orquestrador. Dois
subagentes OpenRouter analisaram código em paralelo. Nenhum provider foi chamado
em loops de experimentação.

## Hipótese inicial

A issue #27 formula: "Sem uma fronteira definida entre a decisão que é da agente
e a que é do operador, o produto oscila entre dois fracassos simétricos — a babá
humana a cada fault mecânico, que é o comportamento de hoje, e a autonomia sem
freio." A evidência citada é `cfbf4b3` e a bateria OpenRouter de 2026-08-28: o
agente "reportava e parava" diante de `paused`/`degraded`.

## Revalidação da evidência inicial no estado atual

O código evoluiu significativamente desde `cfbf4b3`. No baseline atual:

1. **A doutrina de supervisão JÁ EXISTE**, e é extensa. O guidance de
   `run_workflow` (`RUN_GUIDANCE`, 12.668 caracteres) contém o loop
   `watch → diagnose → adapt → resume`, freios (per-key 1 tentativa, 3/run, K=2
   global), registro de workaround, fronteira agente×humano por categoria de
   causa, e regras de pivô. A skill `workflow-authoring` (SKILL.md, ~680 linhas)
   tem seção "Active supervision" com a tabela completa, os freios e o circuit
   breaker comportamental.

2. **Testes anti-drift JÁ EXISTEM** — `test_workflow_supervision_doctrine.py`
   com ~30 contratos que pinam cada limite e fronteira contra o código e a skill.

3. **Os sinais de pause JÁ são explícitos** — `pause_fields()` injeta um `hint`
   imperativo no status reply para `token_budget`, `checkpoint` e
   `user_requested`, dizendo exatamente que tool chamar e com que parâmetros.

A evidência original de `cfbf4b3` é **parcialmente obsoleta**: a doutrina foi
definida e testada. Mas o comportamento de "reportar e parar" ainda pode ocorrer
por um defeito de **posicionamento** (ver abaixo).

## Experimentos

### Experimento 1: Inventário de visibilidade da doutrina por superfície

**Condição de falsificação:** se toda a doutrina de ação estivesse visível no
`workflow_status` (a tool que o agente chama para supervisionar), o agente não
precisaria de orientação extra ao receber `paused`/`degraded`.

**Método:** grep exaustivo em `tools.py` e `SKILL.md`. Mediu-se o que está
anexado a `_STATUS_SCHEMA.description` (a tool description de `workflow_status`)
vs `RUN_GUIDANCE` (description de `run_workflow`) vs SKILL.md.

**Resultado:**

| Sinal de ação | Em `workflow_status`? | Em `run_workflow`? | Na SKILL.md? |
|---|---|---|---|
| `paused` → decisão por `reason` (quota/budget/checkpoint) | ✅ Completa | ✅ | ✅ |
| `stale` → "resume under supervision brakes" | ⚠️ Invoca sem definir | ✅ (definidos) | ✅ |
| Loop `watch → diagnose → adapt → resume` | ❌ | ✅ | ✅ |
| Freios (per-key, K=2, 3/run) | ❌ | ✅ | ✅ |
| Registro pré/pós workaround | ❌ | ✅ | ✅ |
| Correção de model slug | ❌ | ✅ | ✅ |
| **`degraded`** → "leia `faults` antes de `outputs`" | ❌ **zero ocorrências** | ❌ **zero ocorrências** | ✅ §6 |
| `failed` → o que fazer | ❌ | ❌ | ✅ §6 |
| Pointer para a skill | ❌ | ✅ ("load the workflow-authoring skill first") | — |

**Achado principal:** `degraded` e `failed` têm ZERO cobertura em QUALQUER tool
surface. Quando o agente chama `workflow_status` e recebe `status: "degraded"`,
não há uma palavra de guidance visível. O `_STATUS_SCHEMA.description` fala de
`paused`, `running`/`stale`, e observabilidade — mas `degraded` e `failed` não
aparecem.

Para `paused`, a doutrina está parcialmente no ponto: as decisões binárias
(quota/budget/checkpoint) estão duplicadas no `_STATUS_SCHEMA`. Mas os freios
(K=2, per-key, registro de workaround) só estão em `RUN_GUIDANCE` — que é a
tool description de OUTRA ferramenta. O agente que só chama `workflow_status`
não os vê.

**Veredicto:** a doutrina é correta em conteúdo mas sofre de um defeito de
**posicionamento**: está na tool errada para o momento da supervisão.

### Experimento 2: Teste de descoberta da doutrina no ponto da ação

**Condição de falsificação:** se o agente consegue descobrir "o que fazer com um
`degraded`" sem nunca ter chamado `run_workflow` nesta sessão, o posicionamento
é suficiente.

**Método:** simulação do fluxo de ferramentas. Um agente que herda um run (nunca
autorou, só supervisiona) chama `workflow_list` → vê `degraded` → chama
`workflow_status(run_id)` → lê a resposta. O tool description de
`workflow_status` é o ÚNICO texto de guidance injetado nesse ponto.

**Resultado:** o agente vê `status: "degraded"`, `faults`, `null_rate`, `outputs`
com alguns `null` — mas ZERO instrução. A palavra "degraded" não aparece no
description. O agente precisa INFERIR o que fazer com dados brutos.

**Veredicto: confirmado.** O agente fica sem guidance exatamente nos dois
status que mais exigem julgamento: `degraded` (outputs parcialmente `null`) e
`failed` (tudo `null`).

### Experimento 3: Alternativas comparadas

**Alternativa A — Doutrina só em texto (guidance + skill), sem enforcement.**
É o que existe hoje, com o defeito de posicionamento corrigido: adicionar
`degraded`/`failed` ao `_STATUS_SCHEMA` e um pointer para a skill. **Prós:**
zero mudança no harness, backwards-compatível, a doutrina já provou funcionar
quando o agente a lê (os freios são comportamentais e o agente os segue quando
os vê). **Contras:** ainda depende de o agente interpretar prosa corretamente;
não há garantia mecânica.

**Alternativa B — Enforcement no harness.**
Circuit breaker com estados CLOSED/OPEN/HALF-OPEN implementados no código,
contador de tentativas por `(run_id, cause, target)`, detecção de não-progresso
por fingerprint. **Prós:** garantia mecânica, impossível o agente violar.
**Contras:** rígido demais para um agente que precisa julgar; o harness não tem
como distinguir "o agente reformulou a mesma pergunta" de "o agente tentou uma
abordagem genuinamente diferente com o mesmo target"; implementar isso
corretamente é um projeto de várias issues (SUP-02..06); e a doutrina em texto
já cobre os casos observados.

**Decisão:** Alternativa A para SUP-01. O defeito é de posicionamento, não de
conteúdo. A correção é barata (3 linhas no `_STATUS_SCHEMA`), testável, e
resolve o problema observado. A alternativa B é material para SUP-02..06 se os
testes ao vivo mostrarem que a doutrina em texto é insuficiente.

## Correção implementada

### 1. `_STATUS_SCHEMA.description` — adicionar `degraded`, `failed` e pointer

No `_STATUS_SCHEMA.description` (em `tools.py`), após o bloco de `paused`,
adicionar:

```
status 'degraded' means at least one node nulled or a fault exists:
read 'faults' (and 'faults_total' on a resumed run) before trusting
'outputs'; say which parts are missing instead of writing around holes.
status 'failed' means every node nulled — re-author, do not paper over it.
For the full supervision doctrine (the loop, the brakes, what is yours
to fix vs the human's), load the workflow-authoring skill.
```

### 2. Testes anti-drift adicionados

- `test_status_schema_covers_degraded_status_with_action` — `degraded`
  nomeado com instrução de ação (ler `faults` antes de confiar nos outputs).
- `test_status_schema_covers_degraded_faults_total_on_resumed_runs` —
  `faults_total` nomeado para runs retomados.
- `test_status_schema_covers_failed_status_with_action` — `failed`
  exige re-autoria, não "paper over".
- `test_status_schema_points_to_workflow_authoring_skill_for_full_doctrine` —
  pointer para a skill de supervisão no ponto da ação.

### 3. Nada muda na skill, no RUN_GUIDANCE, ou no harness

A skill e o `RUN_GUIDANCE` já estão corretos. Apenas duplicamos as instruções
essenciais de `degraded`/`failed` no ponto onde o agente as lê durante a
supervisão, mais um pointer para a fonte completa.

## Classificação final

**Reformulada.** A hipótese inicial ("a fronteira entre agente e humano não está
definida") é imprecisa. A fronteira ESTÁ definida — na skill e no guidance de
`run_workflow` — mas não está visível no ponto onde o agente supervisiona
(`workflow_status`). O defeito é de **posicionamento**: `degraded` e `failed`
não têm instrução em nenhuma tool surface, e os freios de adaptação só são
visíveis em `run_workflow` (outra ferramenta).

A evidência de "reportar e parar" diante de `paused`/`degraded` é consistente
com este defeito: sem guidance visível, o agente não sabe que pode agir, e
reporta.

Corrigir o posicionamento (3 linhas no `_STATUS_SCHEMA`) é o escopo completo de
SUP-01. As hipóteses de enforcement no harness (circuit breaker, detecção de
não-progresso) são material para SUP-02..06, dependendo dos resultados ao vivo
após esta correção.

## Evidências mínimas atendidas

- [x] Baseline, hipótese, condição de falsificação, experimento e resultado
      registrados.
- [x] Fronteira explícita agente×humano com causa que classifica cada lado:
      já existe na skill e no `RUN_GUIDANCE`; a correção a torna visível no
      ponto da supervisão.
- [x] Freios definidos com valores: per-key 1 tentativa, 3/run, K=2 global,
      planejamento allowance 6k tokens/25% — já existem, sem alteração.
- [x] Demonstração de que a doutrina sobrevive a testes anti-drift: os ~30
      testes existentes + 4 novos passam.
- [x] Alternativa comparada: doutrina em texto (implementada) vs enforcement no
      harness (adiado para SUP-02..06).
- [x] Classificação: **reformulada** (defeito de posicionamento, não de conteúdo).

## Próximos passos

SUP-02..06 podem explorar enforcement no harness (circuit breaker, detecção de
não-progresso) se os testes ao vivo com a doutrina corrigida mostrarem
insuficiência.