# Medição W9-E5 — esquecimento/invalidação de insights (issue #86)

Read-only. Nenhuma escrita em nenhum `state.db`; todas as conexões abriram com
`PRAGMA query_only=1` e só executaram `SELECT`. Nenhum `PRAGMA wal_checkpoint`
foi chamado.

## 0. Estado do checkout

`/Users/marcelusfernandes/Desktop/playground-ai/lohra` está em
`integration/wave10.1`, não `integration/wave9`. O código foi lido em
`/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-e4` (branch
`feat/w9-e4`), que já tem E1 (`hits`/`last_summary`) integrado.

## 1. Código lido — achados que mudam a pergunta

### 1.1 `is_learnable` é o filtro real, não `status`

`backend/lohra/workflow/failure_taxonomy.py`:

```python
@property
def is_learnable(self) -> bool:
    """Only high-confidence AGENCY observations may feed learning."""
    return self.responsibility is Responsibility.AGENCY
```

`InsightStore.record()` chama `classify_failure(...)` e, se
`observation.is_learnable` for `False`, **retorna sem gravar nada**
(`backend/lohra/state/insights.py`, método `record`). Ou seja: a tabela
`workflow_insight_candidates` só pode conter linhas com
`responsibility = 'agency'`. Uma linha `environment` ou `unknown` é
estruturalmente impossível hoje — não é que elas existam e fiquem sem
política de expiração; elas **nunca são escritas**.

### 1.2 Só existe UM call-site de `.insights.record(...)` em todo o backend

```
grep -rn "\.insights\.record(" backend/lohra/
backend/lohra/workflow/service.py:940:            self._db.insights.record(
```

É `_record_spec_candidate` (`service.py`, dispara quando `validate_spec`
rejeita um spec autorado). Ele grava sempre:

- `kind="candidate"` (nunca `"insight"`)
- `status="invalid_spec"`
- `mechanism="validation"`
- `signals=(SIGNAL_SPEC_SHAPE, *rule_signals)`
- `confidence=1.0`
- `summary="authored workflow spec rejected by validate_spec: {error.message}"`

Como `mechanism="validation"` + `SIGNAL_SPEC_SHAPE` + `confidence>=0.8` é a
ÚNICA combinação em `_resolve()` que produz `AGENCY` sem depender de
`SIGNAL_PROVIDER_SIDE`/`SIGNAL_HARNESS_INTERNAL`, e é exatamente o que este
call-site sempre passa, toda linha gravada tem
`(kind, status, mechanism, responsibility) = (candidate, invalid_spec,
validation, agency)`, sempre. Não há nenhum outro produtor.

**Consequência direta para a pergunta final do épico**: `recent_insights`
(20 mais recentes) **nunca serviu, e não pode servir hoje**, nada além de
candidatos `invalid_spec` — não por falta de volume, mas porque não existe
segundo call-site. Isso é mais forte que "0 aproveitados" do T0: é
estrutural, não uma amostra pequena.

### 1.3 Consequência para as 3 políticas simuladas

- **P1** (expirar `environment` com >30 dias sem recorrência): universo
  vazio por construção — não há, nem pode haver hoje, linha `environment`
  na tabela. A política é sensata *se* E3/E8 abrirem um segundo call-site
  que grave `environment`/`unknown`; sobre os dados atuais ela não teria
  efeito nenhum (0 em todos os profiles).
- **P3** (refutar `agency` que cita um modelo, contra o catálogo): também
  vazio por construção *nestes dados* — o único call-site nunca produz
  `agency` sobre "modelo não existe" (isso classificaria como
  `EXTERNAL_REJECTION` + `SIGNAL_PROVIDER_SIDE` → `ENVIRONMENT`, que não é
  gravável). `_validate_tier` (schema.py) rejeita apenas *tiers* abstratos
  (`small`/`medium`/`big`), nunca um slug de modelo concreto — então mesmo
  um segundo call-site de validação de spec não geraria isso hoje.
- **P2** (`hits < 2` após N=20 runs) é a única das três com universo
  não-vazio nos dados atuais, porque não depende de `responsibility`.

Catálogo usado para P3 (mesmo vazio): `~/.lohra/model_windows.json` — cache
`{provider: {model_id: janela}}`, não achei `catalog*.json` separado.

## 2. Varredura de todos os `state.db`

`~/.lohra/state.db` (root) + `~/.lohra/profiles/*/state.db` = 32 bancos.

| Profile | tem tabela? | rows |
|---|---|---|
| (root) | sim | 0 |
| impl-glm | sim | 0 |
| lohra-dogfood-w75 | sim | **2** |
| lohra-notion | sim | 0 |
| lohra-notion-v2 | sim | 0 |
| lohra-notion-v3 | sim | 0 |
| lohra-notion-v3sol | sim | 0 |
| lohra-notion-v4 | sim | 0 |
| lohra-codex1-lohra-e2e | **não** | — |
| lohra-lohra | **não** | — |
| lohra-ts-dogfood | **não** | — |
| lohra-ts-mrc-testes | **não** | — |
| lohra-ts-oracle | **não** | — |
| sup01-glm | **não** | — |
| sup03-glm (+ 10 variantes: engine-limit, impl-limits, impl-limits2, service-core, service-core3, service-steer, service-tiny, tests, 2-8) | **não** | — |
| ts | **não** | — |
| work | **não** | — |

`SessionDB.__init__` cria a tabela `workflow_insight_candidates`
incondicionalmente (`self.insights = InsightStore(path)` roda sempre, não é
lazy) — "não tem tabela" significa que aquele profile não foi reaberto por
uma build do backend que já inclui o InsightStore (build anterior ao
SUP-05), não que o recurso esteja desligado para ele. 24 de 32 profiles
estão nessa situação — inclusive `work`, que parece ser o profile de uso
diário mais pesado.

**Total de linhas em TODOS os bancos: 2.** Ambas no mesmo profile
(`lohra-dogfood-w75`), ambas `kind=candidate`, `status=invalid_spec`,
`mechanism=validation`, `responsibility=agency`, `confidence=1.0`.

## 3. `hits`/`last_summary` — legado

Nenhum dos 8 bancos com a tabela tem as colunas `hits`/`last_summary` —
schema anterior ao E1 (fingerprint sobre `summary` normalizado, `INSERT OR
IGNORE`, sem contador). Isso confirma a premissa da issue: **toda linha
existente hoje é legada**, `hits` não apenas `NULL` como o campo nem existe
na tabela ainda. Ao aplicar o E1 (`ALTER TABLE ... ADD COLUMN`, aditivo e
idempotente — visto em `insights.py` do worktree `w9-e4`), as 2 linhas
herdariam `hits IS NULL` (nunca `0`, por doutrina do próprio código: "a
legacy row's hits reads as NULL forever, never coerced to 0 or 1").

## 4. Fingerprints — legado vs. estrutural (E1)

Fingerprint atual (pré-E1): hash de `(kind, responsibility, mechanism,
summary_normalizado)` — é PK, então `linhas distintas == fingerprints
distintos` sempre (2 == 2 em `lohra-dogfood-w75`).

Fingerprint estrutural do E1: hash de `(kind, responsibility, mechanism,
signals_ordenados)`. `signals` **não é persistido como coluna** — só
alimenta o fingerprint no momento da escrita e o `payload_json` está `NULL`
nas 2 linhas (o call-site não passa `payload=`). Não dá para recuperar
`signals` exatamente; é possível reconstruir o **primeiro** `rule` citado
porque o formato de `summary` embute `[{issue.rule}]` (`schema.py,
_render_issue`):

| # | idade (dias) | rule extraído do summary | agrupamento grosseiro (kind,mechanism,resp) |
|---|---|---|---|
| 1 | 0,1 | `field_value` | candidate\|validation\|agency |
| 2 | 3,6 | `depends_on_type` | candidate\|validation\|agency |

Agrupando só por `(kind, mechanism, responsibility)` (sem `signals`), as 2
linhas caem no MESMO grupo grosseiro — um limite superior ingênuo de "1
grupo" sugeriria que fundiriam. Mas isso ignora `signals`: como
`error.issues` tem regras diferentes (`field_value` ≠ `depends_on_type`),
os `signals` (`rule:field_value` vs `rule:depends_on_type`) são diferentes,
então sob o fingerprint estrutural do E1 as 2 linhas **continuariam
distintas** (0 merges), não 1. A estimativa correta com os dados disponíveis
é: **0 de 2 linhas se fundiriam**, mesmo que o agrupamento grosseiro (que
ignora `signals` por falta de coluna) sugerisse o oposto — registrar esse
gap de método é o ponto: qualquer estimativa de merge que não reconstrua
`signals` superestima a fusão.

## 5. Distribuição de idade / recência

Só há dados em `lohra-dogfood-w75` (as 2 linhas):

| # | idade (dias) | > 7 dias? | > 30 dias? |
|---|---|---|---|
| 1 | 0,1 | não | não |
| 2 | 3,6 | não | não |

Nenhuma linha em nenhum banco tem mais de 30 dias — a base é jovem demais
para o P1 (expiração por idade) produzir qualquer efeito hoje, mesmo
ignorando o problema estrutural da seção 1.3.

## 6. Os 20 mais recentes (o que `recent_insights` serviria)

Só `lohra-dogfood-w75` tem o que servir; os outros 7 bancos com tabela
serviriam lista vazia.

| # | idade (dias) | status | mechanism | responsibility | confidence | resumo (truncado) |
|---|---|---|---|---|---|---|
| 1 | 0,1 | invalid_spec | validation | agency | 1,0 | "…[field_value] cp .default: a checkpoint with 'accept' is a HUMAN gate;" |
| 2 | 3,6 | invalid_spec | validation | agency | 1,0 | "…[depends_on_type] b .depends_on: 'depends_on' must be a list of node i…" |

- Mais velhas que 30 dias: **0 de 2** (0%).
- `invalid_spec` boilerplate: **2 de 2 (100%)**.

## 7. Simulação das 3 políticas — por profile e total

| Profile | linhas | P1 (environment >30d, hits<2) | P2 (hits<2 após ≥20 runs) | P3 (agency cita modelo inexistente) |
|---|---|---|---|---|
| (root) | 0 | 0 | 0 | 0 |
| impl-glm | 0 | 0 | 0 | 0 |
| lohra-dogfood-w75 | 2 | 0 | **1** | 0 |
| lohra-notion | 0 | 0 | 0 | 0 |
| lohra-notion-v2 | 0 | 0 | 0 | 0 |
| lohra-notion-v3 | 0 | 0 | 0 | 0 |
| lohra-notion-v3sol | 0 | 0 | 0 | 0 |
| lohra-notion-v4 | 0 | 0 | 0 | 0 |
| **Total** | **2** | **0** | **1** | **0** |

Detalhe do P2 em `lohra-dogfood-w75` (o único caso não-trivial): usei
`workflow_run_state`/`workflow_run_spend` (26 `run_id` distintos no
profile) e contei, por linha, quantos `run_id` distintos têm
`updated_at` posterior ao `updated_at` da linha de insight:

| # | idade (dias) | `hits` | runs distintos desde então | P2 esconderia? |
|---|---|---|---|---|
| 1 | 3,6 | NULL (coluna nem existe) | 20 | **sim** (≥20, hits<2) |
| 2 | 0,1 | NULL | 1 | não (<20 runs ainda) |

P1 e P3 dão 0 em todo lugar não por coincidência de amostra pequena, mas
porque o universo que eles mirariam (`responsibility=environment` para P1;
`agency` citando um modelo para P3) é vazio por construção — ver §1.3.

## 8. Resposta à pergunta final do épico

**`recent_insights` (20 mais recentes) já serviu algo além de candidatos
`invalid_spec` nesses bancos?** Não — nem uma vez, em nenhum dos 32 bancos.
E não é uma observação empírica frágil (poderia mudar com mais dados): é
garantida pelo código atual, porque existe exatamente um call-site de
escrita e ele sempre grava `status="invalid_spec"`. Isso é a evidência do
T0 ("0 insight candidates aproveitados / priors boilerplate") confirmada
com números: 2 candidatos existem (não 0), mas os 2 são exatamente o
boilerplate que o T0 descreveu — nenhum progrediu de `candidate` para
`insight`, nenhum tem `responsibility` diferente de `agency`, nenhum tem
`mechanism` diferente de `validation`.

## 9. O que isso implica para o desenho de E5 (não pedido, mas decorre direto da medição)

- Uma política de esquecimento por `responsibility=environment` (P1) ou por
  refutação de modelo em `agency` (P3) não tem hoje **nenhum** dado para
  atuar — não é "a janela de 30 dias está errada", é que essas classes
  nunca chegam à tabela. Implementar P1/P3 antes de abrir um segundo
  call-site (que grave `environment`/`unknown`, presumivelmente vindo de
  `route_fault.py` ou de uma falha de execução real, não de validação de
  spec) seria código morto — testável só com fixtures sintéticas, nunca
  com dado real de produção.
- P2 (recorrência via `hits`) é a única política com sinal real hoje, e
  ainda assim sobre 2 linhas em 1 profile de 32.
- O gargalo de volume não é a política de expiração — é a falta de um
  segundo produtor de insights. `_record_spec_candidate` é o único emissor
  desde que o recurso existe.
