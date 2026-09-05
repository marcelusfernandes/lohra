# E1 parte 2 — gatilhos causais de agência (censo READ-ONLY)

**Issue:** #50 (parte 2). **Natureza:** censo, nenhuma escrita no repo nem em `~/.lohra`.
**Worktree lida:** `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int` (branch `integration/wave9`, rebaseada sobre main 0.0.26 — E1 estrutural, E2, E3, E4, E6, E7a + Wave 10.1). `feat/w9-e8` foi lida à parte (ainda não integrada) só para o item E8.
**Bancos:** cópias byte-a-byte de `~/.lohra/state.db` + `~/.lohra/profiles/*/state.db` (32 arquivos) para o scratchpad; identificados por tamanho de arquivo (único por profile).

---

## 1. `failure_taxonomy.py` — mecanismos, sinais, `_resolve`, `is_learnable`

`backend/lohra/workflow/failure_taxonomy.py`

- **Mechanism** (linhas 51-65): `VALIDATION`, `TRANSPORT`, `TIMEOUT`, `EXTERNAL_REJECTION`, `RESOURCE`, `CANCELLATION`, `UNKNOWN` (default de qualquer valor não reconhecido, `_missing_`).
- **Signals** (opacos, vocabulário fechado só para os 3 conhecidos; extras tolerados mas não usados no `_resolve`): `SIGNAL_SPEC_SHAPE = "spec_shape"`, `SIGNAL_PROVIDER_SIDE = "provider_side"`, `SIGNAL_HARNESS_INTERNAL = "harness_internal"` (linhas 32-34).
- **`is_learnable`** (linha 92-94): `return self.responsibility is Responsibility.AGENCY` — só isso, sem checar confiança de novo (a confiança já foi consumida dentro de `_resolve`).
- **`_resolve` (linhas 137-177)** — mapeamento completo:
  - `>1` sinal conhecido presente simultaneamente → `UNKNOWN` (evidência conflitante, nunca precedência).
  - `CANCELLATION` → sempre `UNKNOWN` (não diz quem cancelou).
  - `UNKNOWN` (mecanismo) → `UNKNOWN`.
  - `VALIDATION` + `SIGNAL_SPEC_SHAPE` + `conf >= 0.8` → **`AGENCY`** (a única combinação que produz agência hoje). Com `conf < 0.8` → `UNKNOWN` (nunca "meia-agência").
  - `VALIDATION` + `SIGNAL_PROVIDER_SIDE` → `ENVIRONMENT`.
  - `VALIDATION` sem sinal → `UNKNOWN`.
  - `TRANSPORT`/`TIMEOUT` + `SIGNAL_HARNESS_INTERNAL` → `INFRASTRUCTURE`; + `SIGNAL_PROVIDER_SIDE` → `ENVIRONMENT`; sem sinal → `UNKNOWN`.
  - `EXTERNAL_REJECTION` + `SIGNAL_PROVIDER_SIDE` → `ENVIRONMENT`; sem sinal → `UNKNOWN`.
  - `RESOURCE` + `SIGNAL_HARNESS_INTERNAL` → `INFRASTRUCTURE`; sem sinal → `UNKNOWN`.

**Única combinação `(mechanism, signals)` que produz `AGENCY`:** `mechanism="validation"` + sinal `SIGNAL_SPEC_SHAPE` presente (sozinho ou acompanhado de sinais desconhecidos ao vocabulário, como `rule:<x>`) + `confidence >= 0.8`.

---

## 2. Produtores — `insights.record(` / `FailureObservation(` / `classify_failure(`

```
lohra/workflow/service.py:940        self._db.insights.record(   <- ÚNICO call site em `integration/wave9`
lohra/state/insights.py:181          observation = classify_failure(...)   <- dentro do próprio InsightStore.record
lohra/workflow/failure_taxonomy.py:118  return FailureObservation(...)     <- dentro de classify_failure
```

Confirmado por grep em `lohra/` inteiro (excluindo `tests/`): **nenhum outro chamador** de `insights.record` ou de `FailureObservation(` fora desses três arquivos.

O único produtor real é `_record_spec_candidate` (`service.py:921-949`), chamado de `service.py:546` **somente quando** `validate_spec` rejeita uma spec **explícita** (`agency_authored and explicit_spec`, `service.py:537-546` — um resume sem spec explícita nunca chega aqui, porque a spec persistida foi escrita por outro turno/autor):

```python
self._db.insights.record(
    kind="candidate",
    status="invalid_spec",
    mechanism="validation",
    signals=(SIGNAL_SPEC_SHAPE, *rule_signals),   # rule_signals = {"rule:<issue.rule>", ...}
    confidence=1.0,
    summary="authored workflow spec rejected by validate_spec: " + error.message,
)
```

Sinais fixos: sempre `SIGNAL_SPEC_SHAPE` + `rule:<rule>` (vocabulário fechado, nunca texto livre — E1 já corrigiu isso). `confidence=1.0` sempre. Sob `_resolve`, isso sempre cai em `VALIDATION + SIGNAL_SPEC_SHAPE + conf>=0.8` → **AGENCY**, sempre `is_learnable`.

Em `feat/w9-e8` (ainda **não integrada**), existe um **segundo** call site já escrito: `_record_substitution_candidates` (`service.py:989-1026` nessa branch), chamado de `service.py:1241` no fecho do run. Confirmado via `git show feat/w9-e8:backend/lohra/workflow/service.py`:

```python
self._db.insights.record(
    kind="candidate",
    status="model_substituted",
    mechanism="validation",
    signals=model_substitution_signals(),   # = (SIGNAL_SPEC_SHAPE, "rule:model_not_found")
    confidence=1.0,
    summary="authored workflow node named a model the provider does not have: "
            f"{entry['from']!r} -> substituted by {entry['to']!r} from the operator's tier map",
)
```

Mesma forma exata do produtor de E1 (`mechanism="validation"`, `SIGNAL_SPEC_SHAPE`, `conf=1.0`) → **AGENCY**. Essa é a decisão do dono citada no docstring: "a nonexistent model chosen by the AUTHOR is `agency`". Resolve a aresta que a taxonomia (§2.4 do doc de 09-03) tinha deixado em aberto — mas só depois que E8 for integrada.

---

## 3. Gatilhos observáveis que hoje NÃO viram candidate

### 3.1 Checkpoint rejeitado por humano (#74)
- **Decisão:** `lohra/workflow/gates.py:316` — `engine.record_fault(f"{node.id}: checkpoint rejected by human: {_quoted(answer)}")`, dentro de `run_checkpoint` (`gates.py:241-324`), no ramo em que `accept` está declarado e a resposta não bate (`checkpoint_accepts(answer, accept)` falso, `gates.py:303-316`).
- **`FailureObservation` que resultaria:** não existe combinação de `mechanism`/`signal` no vocabulário atual que descreva "um humano recusou o conteúdo". Não é `VALIDATION` (a spec não tem defeito de forma), não é `EXTERNAL_REJECTION` no sentido de provider, não é `CANCELLATION` (que a própria taxonomia já trata como `UNKNOWN` — "não diz quem cancelou"). Forçado pelo `_resolve` sem sinal correspondente → `UNKNOWN`.
- **`is_learnable`: NÃO** — hoje, e não de forma corrigível com um sinal existente. Precisaria de um mecanismo novo (algo como `HUMAN_REJECTION`) e um novo sinal, decisão de taxonomia que este censo não toma.
- **Atribuição é genuinamente ambígua:** o autor pode ter escrito uma pergunta ruim (agência) ou o humano pode estar exercendo julgamento legítimo sobre um resultado correto (nem defeito nenhum). O evento por si só não distingue os dois casos.

### 3.2 `required_failure` (nó `required: true` resolvendo null) e `completeness_check` `complete: false`
- **Decisão:** `lohra/workflow/engine.py:2369-2391`, método `_required_abort`. Dois ramos:
  - `output is None and node.required` (linha 2379-2381) → `result.required_failure = node.id`; fault de `lohra/workflow/required.py:31-36` (`required_fault`): `"{node_id}: required node resolved to null — run aborted..."`.
  - `node.required` com `completeness_gaps(node, output) is not None` (linha 2382-2385) → fault de `required.py:59-79` (`completeness_fault`): `"{node_id}: completeness check found gaps: {missing[:3]}... — run aborted..."`.
- **`FailureObservation` que resultaria:** nenhum. `required_failure` é um **sintoma**, não uma causa — ele registra que o autor marcou o nó como indispensável e ele voltou vazio/incompleto, mas **não diz por que** o nó voltou vazio (poderia ser qualquer mecanismo: provider rejeitou o leaf, timeout, cancelamento, ou de fato um defeito de spec). Atribuir agência aqui exigiria conhecer a causa raiz do null upstream, que este ponto do código não carrega.
- **`is_learnable`: NÃO**, e não seria nem com um novo mecanismo simples — a evidência para decidir responsabilidade está em outro lugar (a falha do nó upstream), não neste ponto de decisão.
- Exceção parcial: `completeness_check complete: false` é diferente em espécie — é o **próprio modelo crítico** dizendo que o trabalho está incompleto, o que é evidência sobre a qualidade do output, não sobre uma causa mecânica externa. Ainda assim não mapeia em `VALIDATION`/`EXTERNAL_REJECTION`/etc. sem inventar categoria nova.

### 3.3 Avisos de lint aceitos-e-ignorados (`nested_id_type_ignored`, #82) e lint warnings (#49)
- **Decisão:** `lohra/workflow/lint.py:31-42` (`lint_warnings`), chamado em `lohra/workflow/service.py:551` (`spec_warnings = lint_warnings(parsed)  # #49: warns, never blocks/nests`) — **depois** que `validate_spec` **aceitou** a spec (diferente do caminho de `_record_spec_candidate`, que só dispara em rejeição).
  - `_lint_disconnected` (`lint.py:44-70`) produz `SpecIssue("disconnected_dag", ...)`.
  - `_lint_nested_id_type` (`lint.py:73-105`) produz `SpecIssue("nested_id_type_ignored", ...)` — `id`/`type` num shape aninhado (branch/attempt/stage/body) que nunca é lido.
- Ambas reusam `SpecIssue` (o mesmo tipo que alimenta `error.issues` no caminho de rejeição), com `rule` no mesmo vocabulário fechado (`disconnected_dag`, `nested_id_type_ignored`) que `_record_spec_candidate` já usa para `rule:<rule>`.
- **`FailureObservation` que resultaria (se fosse wireado):** `mechanism="validation"`, `signals=(SIGNAL_SPEC_SHAPE, f"rule:{issue.rule}")`, `confidence` plausivelmente `<1.0` (é aviso, a spec RODOU — a evidência é mais fraca que uma rejeição hard) → sob `_resolve`, se `confidence >= 0.8`: **AGENCY**; abaixo de 0.8: `UNKNOWN`.
- **`is_learnable`: SIM, seria**, com a MESMA forma que `_record_spec_candidate` já usa hoje — é o gatilho estruturalmente mais próximo do produtor existente (mesmo `SpecIssue`, mesmo vocabulário `rule:`, mesmo mecanismo `validation`). A única decisão pendente é a confiança a atribuir a um aviso vs. uma rejeição.

### 3.4 Substituição de modelo (E8, #85) — já escrito
Coberto na Seção 2 acima. `mechanism="validation"`, `signals=(SIGNAL_SPEC_SHAPE, "rule:model_not_found")`, `confidence=1.0` → **AGENCY**, `is_learnable=True`. É o único dos seis gatilhos pedidos que **já tem produtor escrito** (em branch não integrada).

### 3.5 `route_fault` respondido por comando com uma NOVA rota (humano corrigiu a rota do autor)
- **Decisão:** `lohra/workflow/route_fault.py:429-495` (`apply_route_answer`), chamado de `lohra/workflow/service.py:498` dentro do fluxo de resume quando `answered.node_id is not None` (a resposta ao `route_fault` trouxe uma rota).
- O `route_fault` em si (a MORTE que pausou o run) é decidido em `route_fault.py` a partir de `AUTH_FAILED` (`providers/errors.py:36`) ou de uma série de re-spawns na mesma rota esgotada (`route_fault.py:13-30`, docstring do módulo) — **isso é explicitamente environment**: "the provider refused this route's credential" / re-spawns na mesma rota, nada que a análise estática da spec pudesse ter previsto.
- A CORREÇÃO (`apply_route_answer` aplicando uma rota nova) é uma ação humana/comando, não um sinal de que o autor errou ao escolher a rota original — o próprio módulo documenta que trocar de provider/credencial/custo é "the HUMAN's call", fora da autoridade do agente (`route_fault.py:34-40`).
- **`FailureObservation` que resultaria:** nenhuma boa opção no vocabulário atual. Não é `VALIDATION` (a rota era sintaticamente válida). É mecanicamente `EXTERNAL_REJECTION` (auth) ou uma série exaurida de retries (mais perto de `TIMEOUT`/`TRANSPORT` repetido) — ambos, com `SIGNAL_PROVIDER_SIDE`, resolvem em **`ENVIRONMENT`**, nunca `AGENCY`.
- **`is_learnable`: NÃO**, e a resposta correta aqui não é "faltou sinal" — é que a morte É environment por natureza (o texto do próprio módulo confirma isso), e "o humano teve que trocar a rota" não é evidência de erro autoral: pode ser simplesmente que a rota morreu por saldo/quota, algo imprevisível na hora de autorar.

### 3.6 Buraco de agregação (#72, `upstream null inside ${p}[i]`) — autor sem `retries` sobre um fan-out (#77)
- **Decisão:** `lohra/workflow/prompts.py:97-111` (`refuse_aggregate_hole`) e `lohra/workflow/prompts.py:70-90` (`strict_prompt`, que chama o anterior). Fault: `engine.record_fault(f"{node_id}: upstream null inside ${{{source}}}[{index}] (dead {AGGREGATION_ELEMENT[kind]} of {kind} {source!r})")` (linhas 106-109).
- **Pergunta do task:** a morte do elemento é `environment` (ex.: leaf caiu por timeout/rejeição do provider); a AUSÊNCIA de `retries` no nó que faz o reduce é uma decisão de autoria. É agência ou environment?
- **Resposta: `unknown` — a taxonomia não resolve isso hoje, por desenho.** `_resolve` decide responsabilidade a partir de MECANISMO + evidência do que aconteceu, nunca a partir de "o autor poderia ter mitigado isso e não mitigou". Não existe eixo de "mitigação ausente" no vocabulário (`SIGNAL_SPEC_SHAPE`/`PROVIDER_SIDE`/`HARNESS_INTERNAL` descrevem ONDE a falha ocorreu, não se ela era evitável por spec). Tratar "faltou retries" como agência exigiria um mecanismo novo inteiro (algo como "missing mitigation"), com um discriminador que hoje não existe em código nenhum — e correria o risco real de atribuir agência à VÍTIMA de uma falha de ambiente sempre que o autor não blindou o nó, o que praticamente toda spec author de boa-fé faz às vezes. Este é exatamente o tipo de contra-exemplo que a doutrina fail-closed da taxonomia (`UNKNOWN` sempre que subdeterminado) existe para recusar.

### 3.7 Dead-turn notices classificadas agência (#52, 6/57 — nonexistent-model)
- Notices vivem em canal separado (`durable_notices`/`notice_trail`, `agent/notices_overlay.py`), nunca em `workflow_insight_candidates` — não há hoje nenhum caminho de notice → candidate, e o doc de taxonomia (§1.4, §2.4) já mediu 0/57 notices atribuíveis a agência sob a taxonomia atual (as 7 de slug inexistente caíam em `EXTERNAL_REJECTION + SIGNAL_PROVIDER_SIDE → ENVIRONMENT`).
- **Achado deste censo:** com E8 integrada, esse cenário deixa de existir como MORTE — o slug inexistente passa a ser substituído pelo catálogo do operador ANTES do leaf rodar (`_record_substitution_candidates`, Seção 2/3.4), então o run não morre mais por isso e a notice de dead-turn correspondente não é mais gerada por essa causa. **Não é um gatilho a cablear** — é uma classe que E8 torna, na prática, extinta na origem. Cablear um produtor separado a partir da notice seria redundante e correria atrás de um evento que E8 já impede de acontecer.

---

## 4. Evidência de frequência nos 32 bancos do dogfood

Método: cópia byte-a-byte de `~/.lohra/state.db` (HOME) + 31 `~/.lohra/profiles/*/state.db` para o scratchpad; identificação de cada cópia por **tamanho de arquivo exato** (único por profile, confirmado — ex.: `lohra-dogfood-w75/state.db` = 7.933.952 bytes, batendo exatamente com a cópia correspondente). Busca por regex sobre o JSON serializado de `workflow_run_state`, `durable_notices`, `notice_trail`, `workflow_audit_events` e `messages` (a última carrega as transcrições completas, incluindo textos de fault/aviso que só aparecem no retorno de tool, não em coluna dedicada).

| Gatilho | Ocorrências (bruto, 5 tabelas, pode duplicar o mesmo evento entre camadas) | Onde |
|---|---:|---|
| checkpoint rejeitado por humano | 11 | só `lohra-dogfood-w75` |
| `required_failure` (null) | 3 | só `lohra-dogfood-w75` |
| `completeness_check complete:false` | 3 | só `lohra-dogfood-w75` |
| `nested_required_fault` | 0 | nenhum |
| buraco de agregação (`upstream null inside ${`) | 0 | nenhum |
| lint `nested_id_type_ignored` (#82) | 0 | nenhum (regra nasceu em 2026-09-05, hoje — sem tempo de uso real ainda) |
| lint `disconnected_dag` | 2 | só `lohra-dogfood-w75` |
| slug de modelo inexistente (texto `is not a valid model ID`) | 87 (8 em HOME, 77 em `lohra-dogfood-w75`, 2 em `lohra-lohra`) | concentrado em `lohra-dogfood-w75` |
| `route_fault` (pausa efetiva) | 2 runs pausados (`workflow_run_state.pause_reason='route_fault'`, nomes `harness-test` e `harness-route-fault-test`) | só `lohra-dogfood-w75` |

**Achado estrutural mais importante da medição:** praticamente **todas** as ocorrências de todos os seis gatilhos estão em **um único profile**, `lohra-dogfood-w75` — e os NOMES dos runs que as produzem (`required-timeout-harness-test`, `checkpoint-harness-test`, `harness-route-fault-test`, `disconnected-validator-test`, `doomed`) mostram que são **testes deliberados de QA/dogfood dessas próprias features** (SUP-05 / Wave 7.5), não uso orgânico de produção. `lohra-lohra` — o profile de maior volume real de trabalho (112 sessões, 20,7 MB) — tem **zero** ocorrências de checkpoint-rejected/required/completeness/aggregation/route_fault, e só 2 menções de "modelo inexistente" (também prováveis testes). O único hit orgânico plausível é o de `HOME` (8 menções, texto `z-ai/glm-99-ultra is not a valid model ID` — um slug real digitado errado por um usuário, não um teste sintético).

**Conclusão de frequência:** a base observável hoje **não é evidência de uso natural recorrente** — é evidência de que os mecanismos EXISTEM e produzem texto de fault distinguível quando exercitados propositalmente. Extrapolar "isto acontece muito" a partir de 32 bancos dominados por um profile de teste seria o mesmo erro metodológico que o doc de 09-03 já flagrou para os priors legados: contar sinal sintético como se fosse sinal de produção.

---

## 5. Recomendação — o que cablear primeiro, e o que NÃO cablear

### Cablear (ordem sugerida)

1. **Lint warnings (`nested_id_type_ignored` + `disconnected_dag`, §3.3).** É o único gatilho, dos seis pedidos, com forma **idêntica** ao produtor que já existe e já é `AGENCY` sob a taxonomia sem precisar de mecanismo novo: mesmo `SpecIssue.rule`, mesmo `mechanism="validation"`, mesmo `SIGNAL_SPEC_SHAPE`. Custo mínimo: em `service.py`, logo após `spec_warnings = lint_warnings(parsed)` (linha 551), chamar `self._db.insights.record(kind="candidate", status="lint_warning", mechanism="validation", signals=(SIGNAL_SPEC_SHAPE, f"rule:{w['rule']}"), confidence=<escolher, provavelmente 0.8-0.9>, summary=...)` por warning. Dedupe: o fingerprint estrutural de E1 já cobre isso — `(kind, responsibility, mechanism, sinais ordenados)` vira uma linha por `rule`, com `hits` incrementando a cada spec repetindo o mesmo aviso.
2. **Substituição de modelo (E8, §3.4).** Já está escrito e correto em `feat/w9-e8`; só falta integrar a branch. Não é um gatilho novo a projetar — é trabalho pronto esperando merge.

Esses dois juntos cobrem exatamente as duas classes onde a taxonomia JÁ resolve `AGENCY` sem ambiguidade (mesmo mecanismo/sinal do produtor existente) — não exigem inventar taxonomia nova, só estender o gate de onde ele já roda.

### NÃO cablear (atribuição indeterminada → envenenaria o store)

- **Checkpoint rejeitado por humano (§3.1).** Sem mecanismo/sinal que distinga "pergunta mal-formulada pelo autor" de "julgamento humano legítimo sobre um resultado correto". Cablear hoje forçaria uma atribuição que a própria taxonomia, por doutrina fail-closed, recusa fazer sem evidência.
- **`required_failure`/`completeness_check` (§3.2).** É sintoma, não causa — a responsabilidade real está no nó upstream que produziu o null, que este ponto do código não enxerga. Cablear aqui contaria a declaração do autor (`required: true`) como se fosse prova de erro do autor, quando na maioria das vezes é o autor **corretamente pedindo para o run parar** diante de uma falha de ambiente.
- **`route_fault` corrigido por humano (§3.5).** A morte é environment por desenho do próprio módulo (auth/quota, nunca algo detectável na autoria); a correção humana não é evidência retroativa de erro de spec.
- **Buraco de agregação sem `retries` (§3.6).** Veredito explícito: `unknown` — exigiria um eixo de taxonomia ("mitigação ausente") que não existe, e put-lo em prática penalizaria toda spec de boa-fé que não blindou cada fan-out contra uma falha de ambiente imprevisível.
- **Dead-turn notices de slug inexistente (§3.7).** Não cablear um segundo produtor aqui: E8 já extingue a causa na origem (substitui antes de morrer); um produtor via notice seria redundante e correria atrás de um evento que deixa de existir.

**Regra que emerge do censo:** as únicas classes seguras para aprender hoje são as que **compartilham a forma exata** do produtor existente — `mechanism="validation"` + `SIGNAL_SPEC_SHAPE` + `rule:<vocabulário fechado>` — porque só nelas a evidência de autoria é direta (a spec, como escrita, tem uma forma reconhecível como problemática). Todo gatilho que depende de um evento RUNTIME (uma morte de leaf, uma decisão humana, uma ausência de mitigação) carrega causas concorrentes que a taxonomia, corretamente, se recusa a resolver sem sinal novo — e inventar esse sinal por conveniência é exatamente o "aprender do ambiente como se fosse autoria" que a Wave 9 existe para evitar.
