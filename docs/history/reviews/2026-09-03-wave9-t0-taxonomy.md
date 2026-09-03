# Wave 9 · T0 (#54) — Taxonomia do aprendizado, escopo, validade e esquecimento

**Investigação READ-ONLY.** Nenhum arquivo do repo ou de `~/.lohra` foi alterado; nenhum LLM foi executado. *Verificado no fecho:* `git status --porcelain` → saída vazia (working tree limpo). O único write foi este relatório, no scratchpad.
**Data:** 2026-09-03 · **Repo:** `/Users/marcelusfernandes/Desktop/playground-ai/lohra` (main, 0.0.21)
**Natureza deste documento:** PROPOSTA. Nenhuma decisão foi tomada em nome do dono.

**Como as contagens foram obtidas:** `sqlite3` via Python em `mode=ro` (o `state.db` do HOME foi *copiado* para o scratchpad porque o `mode=ro` sobre o original devolveu `unable to open database file`; a cópia é byte-idêntica, 86.376.448 bytes) + `ls`/`cat`/`grep`. Fontes de código citadas por `arquivo:linha`.

---

## 0. Sumário executivo

1. **A afirmação do STATUS precisa de uma correção e duas confirmações.**
   - ❌ *"0 insight candidates"* — **existe 1** (profile `lohra-dogfood-w75`), classe `invalid_spec`/`validation`/`agency`, conf 1.0.
   - ✅ *"1 memória em meses"* — confirmado: **172 sessões de topo** no HOME (168 cli + 4 gateway; as outras 286 são `orchestration`, e filho não escreve memória) → **1 entrada** em `MEMORY.md`.
   - ✅ *"priors boilerplate"* — confirmado e agravado: **16/16** priors legados terminam com a **mesma** frase, e em **15/16** o conselho é da **direção errada** (§1.5).
2. **O escopo já é profile-max por construção** (`lohra_home()`), e nada atravessa profile. O guardrail #1 da issue **já está satisfeito**; não é gap.
3. **Detecção de contradição não existe em canal nenhum.** O único caminho de invalidação é o agente chamar `memory replace|remove` por substring.
4. **A única memória que existe é exatamente a classe que a nota do #54 alerta** (fato ambiental sem escopo nem validade) — e ela foi salva **porque a guidance manda salvar** (`memory/tool.py:16-21`). A política do produtor contradiz a hipótese do épico.
5. **O fingerprint de candidate hasheia texto livre** (`state/insights.py:74-76`) → a condição de falsificação da #50 ("fingerprint depende de texto livre instável") **já é verdadeira hoje**, e não há contador de recorrência.
6. **`recent_insights` devolve só `row["summary"]`** (`workflow/service.py:1233`) → a hipótese central da #51 está **confirmada por código**.

---

## 1. Inventário por canal

### 1.1 Visão dos 6 canais

| Canal | Store | Produz **automaticamente** (harness) | Produz **por ação do agente** | Consumidor | Escopo |
|---|---|---|---|---|---|
| **Memória** (`MEMORY.md`/`USER.md`) | arquivo `§`-delimitado, `memory/store.py` | **nada** | tool `memory` add/replace/remove | prompt congelado (tier *context*), 1×/sessão | profile |
| **Skills** (`SKILL.md`) | dir, `skills/store.py` | **nada** | tool `skill_manage` create/update/delete | índice no prompt congelado + `skill_view` | profile (home) · projeto · builtin |
| **Candidates** (`workflow_insight_candidates`) | SQLite, `state/insights.py` | **1 gatilho só**: spec explícita do agente rejeitada por `validate_spec` (`workflow/service.py:751-772`) | — (indireto: autorar spec inválida) | `workflow_templates` (modo list), campo `insights` | profile |
| **Templates** (`workflows/templates/*.json`) | arquivos, `workflow/library.py` | **sim**: run `complete` + `null_rate ≤ 0.2` + não-`prior_degraded` | — | `workflow_templates` list/get | profile |
| **Priors legados** (`workflows/insights.md`) | arquivo | **DESATIVADO** (`library.py:84`, escrita e leitura) | — | ninguém (`recent_insights` legado → `[]`) | profile |
| **Notices** (`durable_notices` + `notice_trail`) | SQLite, `state/notices.py`, `state/notice_trail.py` | **sim**: turno morto, fim de workflow, recovery de processo | — | overlay request-only no *tail* do turno seguinte (`agent/notices_overlay.py`) | **sessão** (owner=session_id, claim por lineage) |

**Fato de fronteira:** só a sessão de topo escreve memória/skill. `agent/delegate.py:50-56` exclui `memory`, `skill_view`, `skill_manage` (além de `session_search`, cron, orquestração) de subagentes — e leaves de workflow herdam a mesma exclusão. Portanto **"produzido automaticamente" para memória e skills é literalmente zero**, por design.

### 1.2 Contagens reais por profile

| Profile | sessões | MEMORY.md | USER.md | skills | templates | priors legados | candidates | notices | trail |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **HOME** (`~/.lohra`) | 458 | **1 entrada** | 1 entrada | **1** | **31** | 14 | **0** | 13 | 2 |
| `lohra-lohra` | 112 | 0 | 0 | 0 | 13 | 2 | *(tabela ausente)* | *(ausente)* | *(ausente)* |
| `lohra-dogfood-w75` | 32 | 0 | 0 | 0 | 3 | — | **1** | 16 | 2 |
| `lohra-notion-v3sol` | 21 | 0 | 0 | 0 | 0 | — | 0 | 11 | *(ausente)* |
| `lohra-notion-v4` | 20 | 0 | 0 | 0 | 0 | — | 0 | 11 | 0 |
| `sup03-glm-service-tiny` | 17 | 0 | 0 | 0 | 0 | — | *(ausente)* | *(ausente)* | *(ausente)* |
| `lohra-notion-v2` | 9 | 0 | 0 | 0 | 0 | — | 0 | 2 | *(ausente)* |
| `work` | 5 | 0 | **1 entrada** | 0 | 0 | — | *(ausente)* | *(ausente)* | *(ausente)* |
| `lohra-codex1-lohra-e2e` | 4 | 0 | 0 | 0 | 0 | — | *(ausente)* | *(ausente)* | *(ausente)* |
| `impl-glm`, `lohra-notion` | 2 cada | 0 | 0 | 0 | 0 | — | 0 | 0 | *(ausente)* |
| **18 profiles restantes** (`sup03-glm*`, `sup01-glm`, `lohra-ts-oracle`, `lohra-notion-v3`) | 1 cada | 0 | 0 | 0 | 0 | — | 0/ausente | 0/ausente | *(ausente)* |
| **TOTAL** | **700** | **1** | **2** | **1** | **47** | **16** | **1** | **53** | **4** |

*(700 = 458 HOME + 242 nos profiles. Destas, só as de **topo** podem escrever memória/skill: no HOME são **172** — 168 `cli` + 4 `gateway`; as 286 restantes são `source=orchestration`, e `delegate.py:50-56` exclui `memory`/`skill_*` do filho.)*

*(tabela ausente)* = o schema é criado sob demanda; bancos antigos não têm a tabela. Isso **não** é "zero medido" — é "não medível", exatamente a ressalva que a nota da #50 faz.

### 1.3 Runs de workflow (denominador)

`workflow_run_state` (durável a partir da WF-29) em todos os profiles:

```
complete 38 · paused 9 · degraded 6 · failed 6 · cancelled 3 · running 3   → 65 runs
```

**47 templates para 38 runs `complete` registrados** — a conversão **não é calculável**, porque `_save_template` grava em `{_safe_name(meta.name)}.json` (`library.py:104-112`): a biblioteca é *"a última spec limpa por nome"*, **não** um histórico. Vários templates são anteriores à persistência de `workflow_run_state`.

### 1.4 Amostragem e classificação das 57 notices (o único dataset grande)

Todos os textos de `durable_notices` + `notice_trail` (57 linhas), bucketizados por causa:

| Classe da causa | n | %  |
|---|---:|---:|
| operacional / "workflow X finished: …" (nem falha) | 33 | 58% |
| environment / rejeição externa (slug de modelo, 400 do provider) | 7 | 12% |
| environment / transporte-timeout ("overloaded", "read operation timed out") | 5 | 9% |
| humano / "turn interrupted" | 5 | 9% |
| infra / morte por sinal (SIGTERM) | 3 | 5% |
| environment / credencial-saldo (401 token_expired, credit balance too low) | 2 | 4% |
| harness / `max_iterations (20) reached` | 1 | 2% |
| environment / 5xx do provider | 1 | 2% |

**Zero notices atribuíveis a agência sob a taxonomia atual.** Mesmo as 7 de slug de modelo inexistente caem em `external_rejection` + `provider_side` → `environment` (`failure_taxonomy.py:169-175`), embora quem escolheu o slug tenha sido o autor — **essa é a aresta real da taxonomia** e é o achado mais útil para a #50 (§2.4).

### 1.5 Os 16 priors legados — não só boilerplate, mas **direção errada**

Classificação dos 16 (14 em HOME + 2 em `lohra-lohra`):

| causa real | n |
|---|---:|
| rejeição externa (modelo inexistente / 400 invalid_request) | **10** |
| timeout de leaf *(um deles, `obs01`, é na verdade perda de processo + timeout)* | 2 |
| config de tier ausente em `workflow_tiers.json` (**decisão do operador**) | 2 |
| quota esgotada | 1 |
| **falha de validação de structured output** — `orquestrador-notion-markdown-openrouter`: `null_rate 100%, 2 validation-retr(ies), ~362687 tokens`, sem texto de fault | **1** |

**16/16** terminam com a frase idêntica: `Revise: add a verify stage / schemas / tighter fan-out.`

**15/16 dessas causas não se corrigem com verify/schema/fan-out.** O canal legado não é apenas ruído: ele **prescreve mudança de spec para falhas de provider, de operador e de infra**.

**A exceção, nomeada honestamente:** `orquestrador-notion-markdown-openrouter` é o **único** caso em que "add schemas" é conselho da direção **certa** — é justamente uma falha de validação de saída estruturada. Um acerto em dezesseis, por coincidência de frase fixa, não é um canal de aprendizado. A recomendação de manter o legado **congelado como resíduo** (§5) se sustenta em 15/16.

*(Nota de método: os dois itens antes não classificados foram inspecionados linha a linha. `[sup-01]` tem a mensagem truncada em `"is not suppo…"`, então o padrão `not supported` não casou — é rejeição externa, o que leva aquele balde de 9 para 10. O outro é a exceção acima.)*

### 1.6 O que os artefatos vivos dizem sobre si mesmos

- **A única memória** (para **172** sessões de topo no HOME): `"No ambiente local do projeto Lohra, o comando rg não está instalado; usar grep, find ou Python para buscas."` — fato **ambiental**, **sem escopo** (vale só naquela máquina/projeto), **sem validade** (instalar `rg` a torna falsa em silêncio). É literalmente o cenário da nota de abertura do #54. E foi salva **corretamente segundo a política vigente**: `MEMORY_GUIDANCE` (`memory/tool.py:16-21`) manda *"Save proactively when … you learn a convention or **environment quirk**"*.
- **A única skill:** `run-tests-lohra` (14/jun) declara `"Baseline atual: ~265 testes passando … cobertura ~91%"`. A suíte hoje passa de 2451. Procedimento estável **misturado com fato temporal**, sem nenhum campo de proveniência ou validade (o frontmatter só tem `name`/`description`/`version`).
- **Quantas skills são "auto-geradas"? NÃO É PROVÁVEL.** Não há campo de proveniência no `SKILL.md`. Não dá para distinguir uma skill escrita pela Lohra de uma escrita pelo usuário. Registrar como limitação de medição, não como zero.
- **1/47 templates** carrega `meta.leaf_respawns` (o carimbo é da 0.0.21). O restante não tem timestamp, `run_id`, provider/model nem profile em `meta`.

### 1.7 Drift entre doc e código (achado colateral)

`docs/ARCHITECTURE.md:43` e `docs/specs/03-memory-skills-state.md:133` descrevem *"self-improvement = agente forkado em daemon thread, whitelisted a só memory + skill tools"*.
`git log --oneline -S "self_improve"` → **vazio**. `grep -rn -i whitelist backend/lohra` → **vazio**. Nenhuma `daemon=True` corresponde (as 6 existentes são cron, MCP, audit, gateway e server). O candidato mais provável sob outro nome, `agent/aux.py` (`AuxClient`), é o cliente auxiliar de **compaction summary + título de sessão** — não escreve memória nem skill.
**Leitura mais bem suportada:** especificado, **nunca construído** — não "removido". Não afirmo mais que isso: a busca cobriu os identificadores óbvios, não todas as grafias possíveis.
**Consequência para a wave:** não existe hoje nenhum produtor automático de memória/skill. Qualquer trilha que assuma que existe está partindo de premissa falsa.

---

## 2. Matriz de taxonomia

### 2.1 Classes × dimensões, mapeadas nos canais **existentes**

| Classe de aprendizado | Canal hoje | Escopo hoje | Proveniência hoje | Contradição — como detectar | Expiração hoje | Veredito |
|---|---|---|---|---|---|---|
| **Bug de runtime da Lohra** (defeito do harness) | **NENHUM** | — | — | — | — | ⚠️ **sem canal** — hoje vira issue de produto por via humana |
| **Fato temporal** (modelo existe? saldo? rota? `rg` instalado?) | Memória (indevidamente) | profile | agente | **inexistente** | **nunca** | ⚠️ **canal errado** — é a única memória que existe |
| **Preferência estável do usuário** | `USER.md` | profile | agente/humano | inexistente | nunca | ✅ canal adequado; falta invalidação |
| **Padrão autoral** (como autorar boa spec) | Templates (positivo) + candidates (negativo) | profile | harness | inexistente | templates: nunca · candidates: cap 200 LRU | 🟡 canal existe, **sem validade e sem contador** |
| **Evidência operacional** (turno morreu, run terminou) | Notices + trail | **sessão** | harness | n/a (fato pontual, não regra) | **TTL 7 d / 30 d** | ✅ **o único canal com ciclo de vida completo** |
| **Procedimento reutilizável** | Skills | profile/projeto/builtin | agente | inexistente | nunca | 🟡 canal existe; mistura procedimento com fato temporal |

### 2.2 Escopo — estado real

| Escopo | Quem vive nele |
|---|---|
| **sessão** | notices (`owner_id = session_id`, claim pela cadeia root→tip) |
| **profile** | memória, skills(home), candidates, templates, priors legados — **teto por construção**: tudo passa por `lohra_home()` (`memory/paths.py`), que re-rooteia sob `~/.lohra/profiles/<nome>/` |
| **projeto** | **só leitura** (AGENTS.md/CLAUDE.md via `project/discover.py`) + skills de projeto (`.claude/skills`, `.lohra/skills`) |
| **home/global** | nada compartilhado entre profiles |
| **cross-profile** | **inexistente** |

➡️ **A hipótese "profile deve ser o limite máximo padrão" já é o comportamento implementado.** O guardrail *"nenhum compartilhamento implícito entre profiles"* está satisfeito. Isso não precisa de épico — precisa de **teste anti-drift**.

### 2.3 Proveniência — estado real

| Canal | Carrega proveniência? |
|---|---|
| Candidates | ✅ o mais rico: `kind`, `status`, `mechanism`, `responsibility`, `confidence`, `signals` — e o veredito é **recomputado na escrita** (`insights.py:117-127`), então um chamador **não consegue** declarar `agency` |
| Notices | 🟡 owner + fingerprint + timestamps; a *causa* fica em prosa dentro de `text` |
| Templates | ❌ só `meta.name`/`description` (+`leaf_respawns` em 1/47) |
| Memória / Skills | ❌ nenhuma. Texto puro |

**Ponto crítico:** o único canal com proveniência estruturada é também o único que **a descarta na entrega** — `recent_insights` (`service.py:1231-1233`) devolve `[row["summary"] …]`.

### 2.4 Contradição — o buraco mais fundo

**Nenhum canal detecta contradição.** Não há, em canal algum: campo de asserção verificável, timestamp de última confirmação, contra-evidência, nem re-verificação.
Único caminho: o agente perceber e chamar `memory replace|remove` por substring único.
Simulação pedida pelo épico ("uma lição antes válida que se torna falsa"): instalar `rg` torna a única memória falsa — e **nenhum ponto do sistema detectaria**. A memória entra no prompt congelado a cada sessão nova, para sempre.

### 2.5 Expiração — estado real

| Canal | Política |
|---|---|
| Memória | **nunca** (só limite de 2200/1375 chars) |
| Skills | **nunca** |
| Templates | **nunca** (sobrescrito por nome) |
| Candidates | **cap 200 LRU**, não TTL (`insights.py:59`, `_evict_overflow`) |
| Notices | **TTL 7 d + cap 32/owner** (`notices.py:68-69`) |
| Trail | **TTL 30 d + cap 64** (`notice_trail.py:59-60`) |
| Priors legados | congelado; nunca lido nem escrito |

➡️ **Os canais mais duráveis (memória/skills) são os únicos sem qualquer política de validade.** A wave inverteu o esforço histórico: tudo que ganhou ciclo de vida foi o efêmero.

### 2.6 Classes **sem canal** hoje (marcadas conforme pedido)

1. **Bug de runtime da Lohra** — a taxonomia mapeia `harness_internal → infrastructure`, e `infrastructure` **não é learnable** (`failure_taxonomy.py:88-91`). Correto para não poluir memória; mas então nada captura o defeito. *(Ex.: `max_iterations (20) reached` — 1 notice real, some em 7 dias.)*
2. **Fato temporal com TTL** — não existe store de fatos com validade. Hoje ou vira memória permanente (errado) ou se perde.
3. **Contra-evidência / resultado negativo** — "isto foi tentado e não funcionou" não tem canal. O guardrail do #54 pede resultado negativo como cidadão de primeira classe; hoje não é.
4. **Contador de recorrência escopado** — nenhuma tabela conta "quantas vezes". `INSERT OR IGNORE` (`insights.py:145`) descarta a repetição **e** não atualiza `updated_at`.

---

## 3. Lista "nunca memorizar" (proposta)

Classes que **não devem** virar memória/skill/insight automaticamente:

| # | Classe | Razão | Onde deveria ir |
|---|---|---|---|
| 1 | **Disponibilidade/slug/preço de modelo e provider** | temporal por natureza; falsifica sem aviso; 7/57 notices já são disso | consulta ao vivo, ou cache com TTL curto — nunca memória |
| 2 | **Saldo, quota, rate-limit, credencial** | muda em minutos; 2/57 notices | notice (TTL 7 d) — já está certo |
| 3 | **Erro de transporte / 5xx / timeout** | não é atribuível a decisão; 6/57 notices | notice; jamais lição |
| 4 | **Cancelamento e interrupção humana** | não diz **quem** cancelou; a taxonomia já devolve `unknown` (`failure_taxonomy.py:152-156`) | notice |
| 5 | **Config do operador ausente** (ex.: tier sem mapping) | é decisão do operador, não do agente; 2/16 priors legados prescreveram spec-fix para isso | erro didático ao operador |
| 6 | **Presença/ausência de binário local** (`rg`, etc.) | ambiental, por máquina, sem escopo — **é a única memória que existe hoje** | detecção ao vivo, ou memória **de projeto com escopo explícito** |
| 7 | **Progresso de tarefa, TODOs, logs de trabalho** | já proibido pela guidance (`memory/tool.py:19-20`) | sessão |
| 8 | **Texto de "lição" produzido por um filho** | alegação do modelo; schema valida forma, não verdade (premissa da #53) | bundle tipado do harness, se algo |
| 9 | **Baseline numérico dentro de skill** ("~265 testes") | fato temporal escondido em procedimento; **já apodreceu** na única skill existente | fora do corpo da skill |
| 10 | **Qualquer coisa correlacionada no tempo mas sem sinal tipado** | é o falso-positivo que a #50 existe para evitar | não persistir |

**Regra unificadora proposta:** *só persiste como lição o que (a) tem mecanismo tipado, (b) tem responsabilidade `agency` recomputada na escrita, (c) tem escopo declarado, e (d) tem regra de invalidação.* Hoje **(a)+(b) existem** no `InsightStore`; **(c)+(d) não existem em canal nenhum**.

---

## 4. Hipótese nula por trilha

### #50 — Gatilhos causais e baseline de recorrência
**Veredito proposto: hipótese nula PARCIALMENTE SUSTENTADA — mas há problema real, e não é o que o título sugere.**

- **Nula sustentada quanto a "faltam gatilhos".** 1 candidate em 65 runs (12 degraded/failed) é **consistente com o gate funcionando como projetado**: das 57 notices, **0** são atribuíveis a agência. Não há um estoque de falhas de autoria sendo perdido — há um estoque de falhas de *ambiente*, que corretamente não entram.
- **Problema real, diferente: o substrato de recorrência não existe.**
  - `_fingerprint(kind, responsibility, mechanism, summary_normalizado)` (`insights.py:74-76`) inclui **texto livre** — o único candidate real contém o node id `b` e a mensagem completa. O **mesmo** defeito em outro nó gera **outra linha**.
  - `INSERT OR IGNORE` (`insights.py:145`) → duplicata é no-op, `updated_at` **não** avança, e **não há coluna de contagem**. **É impossível medir recorrência hoje**, mesmo de lição byte-idêntica.
  - Ou seja: a condição de falsificação que a própria issue lista ("fingerprint depende de texto livre instável") **já é verdadeira no store atual**.
- **Aresta a decidir (não decido):** modelo inexistente cai em `environment` (7 notices + 9 priors legados), mas o slug foi escolhido pelo autor. É `environment` (o provider rejeitou) ou `agency` (a spec pediu)? A resposta muda a #50 inteira. Recomendo tratar como **pergunta explícita para o dono**, não como bug.

### #51 — Candidates/templates: preservar evidência e medir consumo
**Veredito proposto: hipótese "perde evidência" CONFIRMADA POR CÓDIGO; hipótese "não é consumido" NÃO OBSERVÁVEL.**

- ✅ **Confirmado estruturalmente:** `service.py:1233` devolve só `summary`. Classe, mecanismo, responsabilidade, confiança, status e `payload_json` **são descartados** antes de chegar ao agente. O consumidor único é `workflow_templates` no modo list (`tools.py:557-558`).
- ⚠️ **Não confirmado empiricamente:** com **1** candidate existente, não há evidência de que alguém o consumiu — nem de que não. **Não há telemetria de funil** (produzido→servido→usado). Registrar como *"estrutural, não observado"*.
- ✅ **A decisão sobre o legado tem evidência forte:** §1.5 mostra que os priors não são só boilerplate — em **15/16** o conselho é de direção errada, e o único acerto vem da frase fixa calhar de servir, não de um mecanismo causal. **Manter congelado como resíduo** é a leitura que os dados suportam.
- 🟡 **Templates:** sem proveniência, sem validade, sobrescritos por nome. A hipótese *"templates positivos têm mais utilidade e menos risco"* é **plausível e barata de melhorar**, e é o alvo de menor risco da wave.

### #52 — Momento da dead-turn notice
**Veredito proposto: hipótese nula FORTEMENTE SUSTENTADA — recomendo fechar sem nudge.**

- Das **57** notices, **0** carregam causa atribuível a agência **sob a taxonomia atual**. Um nudge de "salvar em memória" estaria, em **100%** dos casos observados, convidando a memorizar **fato ambiental, infra ou ação humana** — exatamente a lista §3.
  *Ressalva de fidelidade:* 7/57 são a aresta de §2.4 (slug de modelo escolhido pelo autor, rejeitado pelo provider). Se o dono decidir que essa classe é `agency`, o denominador do #52 muda de 0/57 para 7/57 — ainda 88% de ambiente, mas a conclusão deixa de ser categórica. **O veredito do #52 é, portanto, condicional à decisão de §2.4.**
- 33/57 (58%) nem sequer são falhas ("workflow X finished: complete").
- O custo em tokens do texto extra seria pago **em todo** turno com notice, para um retorno cuja base empírica é zero.
- A propriedade que a issue quer preservar (*"notice nunca vira insight automaticamente"*) já está implementada: `build_turn_notice` é declaradamente operacional (`notices_overlay.py:95-105`) e o gate do insight store recomputa o veredito.

### #53 — Pai ← filho
**Veredito proposto: NÃO DECIDÍVEL com dado armazenado. Não fabrico veredito.**

- A comparação "o que o filho observou × o que o pai recebeu" exige **runs ao vivo com instrumentação nos dois lados**. Nada no disco preserva a visão do filho separada da do pai.
- O que **é** verificável hoje: o isolamento é real (`delegate.py:50-56`, `189-190`), e o pai já recebe resultado, faults, status, rollup, `workflow_run_state`, `workflow_audit_events` e notices de recovery.
- A hipótese nula da própria issue ("os canais existentes já bastam") é **a mais barata de testar** e ainda não foi testada. Recomendo o menor experimento possível (§5) antes de qualquer primitive.

---

## 5. Épicos propostos

### Com evidência — propostos

| id | Épico | Tam. | Arquivos | Discriminador (o que prova que funcionou) |
|---|---|---|---|---|
| **E1** | **Fingerprint causal + contador de recorrência.** Trocar o basis do hash de texto livre por `(kind, responsibility, mechanism, código estável do SpecIssue)`; `ON CONFLICT DO UPDATE SET hits=hits+1, updated_at=?` no lugar de `INSERT OR IGNORE`. | **S** | `backend/lohra/state/insights.py:74-76,145` | Duas specs inválidas **pelo mesmo motivo em nós de nomes diferentes** → **1 linha, hits=2**. Motivos diferentes → 2 linhas. Hoje: 2 linhas, hits inexistente. |
| **E2** | **Entregar evidência estruturada em vez de summary.** `recent_insights` devolve linhas com `mechanism`, `responsibility`, `confidence`, `status`, `hits`, `created_at`. | **S** | `backend/lohra/workflow/service.py:1231-1233`, `workflow/tools.py:557-558` | Teste de contrato: a saída de `workflow_templates` contém os campos de proveniência; regressão se voltar a ser `list[str]`. |
| **E3** | **Reescrever a guidance de memória com a taxonomia.** Remover o convite explícito a "environment quirk"; exigir fato **estável** e **escopado**; nomear as classes proibidas (§3). Idem para a guidance de skill (fato temporal fora do corpo). | **S** | `backend/lohra/memory/tool.py:16-21`, `backend/lohra/skills/tool.py:23-28` | Discriminador **textual + anti-drift**: teste que falha se a guidance voltar a instruir salvar fato ambiental. Efeito real só medível ao vivo — declarar como tal. |
| **E4** | **Proveniência e validade no template.** Carimbar `meta`: `run_id`, `created_at`, `provider`/`model`, `profile`, `null_rate`, `leaf_respawns`. Listar a idade. Não sobrescrever silenciosamente. | **S/M** | `backend/lohra/workflow/library.py:104-112,120-142` | Um template salvo hoje expõe idade e run de origem no `workflow_templates`; template legado sem carimbo aparece como **"proveniência ausente"**, nunca com valor default (mesma doutrina do `leaf_respawns`). |
| **E5** | **Campo de invalidação na memória.** Cada entrada ganha classe + escopo + condição de invalidação (ou `permanente`). Migração: entradas legadas ficam `proveniência: ausente` — **sem autoridade retroativa** (guardrail do #54). | **M** | `backend/lohra/memory/store.py`, `memory/tool.py` | Uma entrada com condição declarada pode ser invalidada por evento; a entrada legada do `rg` aparece marcada como sem proveniência. Invariante #1 preservado (snapshot congelado inalterado) — teste de bytes idênticos no prompt. |
| **E6** | **Teste anti-drift do isolamento de escopo.** Congelar em contrato: nada atravessa profile; subagente/leaf não escreve memória/skill. | **S** | `backend/lohra/memory/paths.py`, `agent/delegate.py:50-56` | Teste falha se um novo canal escrever fora de `lohra_home()` ou se `memory`/`skill_*` sair da denylist do filho. |
| **E7** | **Experimento mínimo da #53** (antes de qualquer primitive). Instrumentar 3–5 runs reais com falha e diffar o que o filho observou × o que o pai recebeu pelos canais atuais. | **M** | `backend/lohra/workflow/` (só instrumentação), doc de evidência | Saída: lista de itens **provadamente ausentes** no pai. **Lista vazia ⇒ fechar a #53 como "nenhuma mudança necessária"** — desfecho legítimo e explicitamente previsto pela issue. |

**Ordem sugerida (proposta, não decisão):** E1 → E2 (destravam a medição de que as outras trilhas dependem) · E3 (menor custo, maior alavanca no problema de fato observado) · E6 · E4 · E5 · E7.

### O que **NÃO** fazer

1. **Não adicionar nudge de memória no overlay da dead-turn notice (#52).** 0/57 notices têm causa de agência; o nudge convidaria a memorizar ambiente em 100% dos casos observados, com custo de tokens em todo turno.
2. **Não reativar nem migrar `insights.md`.** 16/16 entradas repetem a frase fixa e **15/16** dão conselho da direção errada (a exceção, §1.5, acerta por coincidência, não por mecanismo). Manter congelado como resíduo de rollback/audit.
3. **Não ampliar os gatilhos de candidate por correlação temporal** (budget, timeout, checkpoint, reroute) antes de E1. Sem fingerprint causal e sem contador, ampliar o gate só produz linhas não-medíveis — e a #50 lista isso como condição de falsificação.
4. **Não criar campo livre `lesson` do filho para o pai (#53).** Premissa explicitamente fora de escopo na própria issue; e E7 pode mostrar que não há perda.
5. **Não introduzir compartilhamento cross-profile.** Guardrail do #54 e comportamento atual; qualquer mudança é decisão de produto separada.
6. **Não gerar skill automaticamente a partir de falha.** Já descartado como inseguro na SUP-05 (H7): o `SkillStore` sobrescreve arquivos sem CAS/revisão/rollback/lock cross-process. Nada mudou desde então.
7. **Não medir sucesso da wave por taxa bruta de escrita.** A própria #52 diz que pode contar negativamente — e os dados dão razão: a única memória existente é a classe errada.

---

## 6. Limites desta investigação (o que não posso provar)

- **Consumo**: não há telemetria de funil em canal nenhum. "Ninguém consumiu o candidate" **não** foi verificado — foi **não observado**.
- **Skills auto-geradas**: **não medível** (sem campo de proveniência no `SKILL.md`).
- **Conversão run→template**: **não calculável** (sobrescrita por nome + templates anteriores à persistência de `workflow_run_state`).
- **Profiles com tabela ausente**: **21 dos 28** não têm `workflow_insight_candidates` (só 7 têm: `impl-glm`, `lohra-dogfood-w75`, `lohra-notion`, `-v2`, `-v3`, `-v3sol`, `-v4`). Isso é "não medível", nunca "zero medido".
- **`self-improvement` forkado**: a busca (`git log -S self_improve`, `grep -i whitelist`, `daemon=True`) cobriu os identificadores óbvios, não todas as grafias. Verifiquei também o candidato mais provável sob outro nome — `agent/aux.py` (`AuxClient`) é **compaction summary + título de sessão**, não um agente de memória/skill. Leitura mais bem suportada: **especificado, nunca construído**. Uma busca por `--grep` em mensagens de commit ficou pendente (Bash indisponível no fecho); a afirmação está hedgeada de acordo.
- **#53**: não decidível a partir de dado armazenado. Nenhum veredito fabricado.
- Tudo aqui é **proposta**. As decisões — inclusive a aresta "slug de modelo inexistente é `environment` ou `agency`" — são do dono.
