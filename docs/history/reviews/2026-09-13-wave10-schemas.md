# Wave 10 — schemas nomeados nos payloads de rigor (#87)

Data: 2026-09-13. Issue: [#87](https://github.com/marcelusfernandes/lohra/issues/87).
Objetivo da rodada: [#100](https://github.com/marcelusfernandes/lohra/issues/100).
Investigação sobre `e2077c2123650cca56105daa0947e2a543696007`; implementação na
branch `codex/task-87`, iniciada em `72a240b85ffc6b8b5cf13dad7430a7756c9ebf5d`.

## Veredito da hipótese

A assimetria persistia, mas a falha silenciosa descrita originalmente já não
era alcançável pelo caminho validado. O review de #82 adicionou uma recusa
`schema_type` para todo schema não objeto em `judge_panel.synthesize` e
`loop_until_dry.body`; `schema_ref` era recusado como `nested_unknown_field`.
Um nome registrado corretamente também era recusado. A issue não tinha comentários.

As estratégias ainda liam o valor cru: `run_judge_panel` passava
`synth.get("schema")` para `collect_with_schema`; `run_loop_until_dry` passava
`body.get("schema")`. O resolver compartilhado já suportava objeto inline,
nome em `schema`, `schema_ref` e os nomes builtin.

Experimento inicial com specs mínimos completos, sem provider:

| Entrada nas duas formas | Validador antes da mudança |
|---|---|
| `schema: "RESULT"`, nome registrado | `schema_type` |
| `schema_ref: "RESULT"`, nome registrado | `nested_unknown_field` |
| `schema: {type: object}` | `WorkflowSpec` |

Um segundo probe construiu `WorkflowSpec` diretamente, contornando o validador
apenas para investigar o reader, com `SessionDB(":memory:")` e client roteirizado:

| Entrada | Loop (uma rodada) | Judge panel |
|---|---|---|
| String de nome | `[]`, fault `round 0 died`, 2 correções | `None`, sem faults, 2 correções |
| `schema_ref` | Lista contendo JSON como string | JSON como string |
| Objeto inline | Lista contendo objeto parseado | Objeto parseado |

O erro do primeiro caso era `schema error: 'str' object has no attribute 'get'`,
capturado por `validation.parse_and_validate`. Isso confirma o mecanismo antigo,
sem alegar que ele ainda escapava de `validate_spec`.

## Medição de cache antes da intervenção

Leitura local de `~/.lohra/profiles/*/state.db`, sem executar workflows ou escrever
nesses bancos. Consulta às tabelas `workflow_run_state` (`run_id`, `spec_json`) e
`workflow_node_cache` (`run_id`, `node_id`, `status`, `content_hash`). Para cada
spec persistida, os nós de topo foram classificados por `type`, e suas células
associadas por `(run_id, node_id)`.

- 32 bancos de profiles, 62 specs persistidas, 95 células no total.
- Nenhuma spec atual continha `judge_panel` ou `loop_until_dry`.
- 84 células atribuíveis a outros tipos: 71 agent, 8 parallel, 3 checkpoint,
  1 completeness_check e 1 verify.
- 6 células sem nó de topo correspondente na spec atual; podem incluir células
  aninhadas. Outras 5 não tinham spec atual persistida. Não foi inferido seu tipo.
- Banco default `~/.lohra/state.db`: zero células.

Resultado: **zero células afetadas confirmadas, 11 sem atribuição suficiente**.
Não é prova de inexistência histórica desses tipos, nem censo de todos os
templates da library. O desenho preserva os hashes legados independentemente
desse resultado parcial.

27 bancos abriram com SQLite URI `mode=ro`. Cinco bancos de profiles e o default
recusaram a leitura nesse modo; não tinham `-wal`, `-shm` nem journal, e foram
lidos com `mode=ro&immutable=1`. Para esse fallback, tamanho e `mtime_ns` foram
conferidos antes/depois e a ausência de WAL reconfirmada. Nenhum mudou durante a
leitura. Nenhum banco foi aberto para escrita.

## Intervenção e identidade

As duas estratégias agora resolvem schemas com `engine.resolve_schema`.
`NESTED_SHAPES` admite `schema_ref` nelas; o validador usa o mesmo resolver para
recusar nomes inexistentes e tipos inválidos, com nó, campo e exemplo didático.
Também mantém a exclusão mútua `schema`/`schema_ref`. O guard fica restrito às duas
formas desta issue; não amplia contratos de validação de stages ou gates.

O hash do loop já possuía uma posição para o schema: essa posição passa a conter
a definição resolvida. No panel, somente um `synthesize` com nome/ref é normalizado
para uma cópia com `schema` resolvido e sem `schema_ref`. O payload inline e a
ausência de schema mantêm exatamente a composição anterior. Não há mutação da
spec, migração do banco ou bump global de versão de cache. Definição alterada sob
o mesmo nome produz nova célula; nomes equivalentes resolvidos para o mesmo
conteúdo seguem o contrato de identidade por conteúdo.

`cache_preview` já executa a estratégia real e seu engine de leitura já oferece
`resolve_schema`, portanto não precisou de uma segunda fórmula. O teste de
compatibilidade grava células usando **as fórmulas anteriores**, e exige que o
engine atual e o preview as reutilizem sem spawn. Os testes de alteração de
schema fazem uma execução real com client roteirizado, reaproveitam a célula,
trocam a definição mantendo o nome e verificam invalidação seguida de nova saída.

## RED e validação

Todos os comandos abaixo foram executados no diretório `backend` da worktree,
com Python explícito e sem chamadas LLM. Primeiro, somente o novo teste de
aceitação foi executado, antes da mudança de produção:

```bash
PYTHONPATH=. /Users/marcelusfernandes/.pyenv/versions/3.12.10/bin/python3 -m pytest tests/test_workflow_rigor_schemas.py -k named_schema_validates_before_any_leaf --no-cov -q
```

Resultado RED: **4 failed, 38 deselected**, exit 1: duas recusas `schema_type`
e duas `nested_unknown_field`. Depois da intervenção, a nova suíte passou;
acrescentado o caso de objeto inline indevido em `schema_ref`, ela tem 44 casos.

Validação de regressão:

```bash
PYTHONPATH=. /Users/marcelusfernandes/.pyenv/versions/3.12.10/bin/python3 -m pytest -o addopts='' tests/test_workflow_rigor_schemas.py tests/test_workflow_schema.py tests/test_workflow_field_consumers.py tests/test_workflow_rigor.py tests/test_workflow_cache_identity.py tests/test_workflow_cache.py tests/test_workflow_cache_preview.py tests/test_workflow_rigor_routing.py tests/test_workflow_loop_budget.py tests/test_workflow_authoring_skill.py tests/test_workflow_validation.py --cov=lohra.workflow.schema_nested --cov=lohra.workflow.strategies --cov-report=term-missing -q
/Users/marcelusfernandes/.pyenv/versions/3.12.10/bin/python3 -m ruff check .
```

Resultado: **440 testes passando**, Ruff limpo. A cobertura da suíte focada foi
100% em `schema_nested.py` e 75% no módulo completo `strategies.py`; não é uma
medição da suíte integrada. Os testes cobrem as quatro combinações de forma/sintaxe,
nomes builtin e schemas vazios, falhas didáticas, correção de resposta inválida,
replay, mudança de definição, ausência de mutação e hashes legados. A skill builtin
continua com 799 linhas, dentro do limite de 800. `parallel.branches` e
`judge_panel.attempts` continuam sem schema por desenho.

Limites: validação offline; não houve dogfood com LLM nem review independente
nesta lane. A revisão e a publicação pertencem ao coordenador. A lacuna
preexistente de nomes inválidos em outras formas embutidas não foi ampliada nem
refatorada nesta issue.
