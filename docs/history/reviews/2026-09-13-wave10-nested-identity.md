# Wave 10 — identidade de células aninhadas (#90)

Data: 2026-09-13. Base pública: `72a240b85ffc6b8b5cf13dad7430a7756c9ebf5d`; branch `codex/task-90`. Implementação isolada da #87; integração entre lanes e revisão independente ficam com o coordenador.

## Problema demonstrado e correção da premissa

O T14 antigo comprovou uma atribuição incorreta no preview, não reexecução indevida de um leaf completo. Em `T14-4-resumeA-raw.json`, `a.cp`/`a.do` concluíram e `b.cp` estava nulo; `b.do` não executou. Em `T14-5-resumeB-raw.json`, A replaia duas células e economiza 998 tokens; B executa seu leaf pela primeira vez, por 1002 tokens. `_miss_reason` apenas explicava o lookup: não havia guard que causasse re-spawn. A [correção explícita](../evidence/wave10.1-dogfood/report-T14-T16.md) foi acrescentada sem alterar o relato original nem os arquivos raw.

Os oito testes RED de `test_workflow_nested_identity.py`, commit `a69509e`, falharam na base antes da implementação:

| Caso | Comportamento anterior | Contrato agora |
| --- | --- | --- |
| Dois irmãos, mesmo template e checkpoint idêntico | A aprova B pelo cache | B pausa para sua própria resposta |
| Raiz/filho com identidade de spec igual, ambas as ordens | Aprovação atravessa o nível | Cada checkpoint precisa da própria aprovação |
| Aprovação legada, só uma chamada na spec atual | Autoria presumida | Revalidação explícita |
| Aprovação legada na raiz, hash anterior | Resposta antiga aceita sem autoria | Hash preservado, hit exige scope observado |
| A concluído/B ainda novo | Preview cobra 998 de A como invalidação de B | Duas células nunca concluídas, zero cobrança atribuída ao irmão |
| Custos de duas chamadas | Uma entrada sobrescreve a outra | Dois donos; atribuição soma o gasto real |
| Reabertura de SQLite | Uma aprovação servia às duas chamadas | Duas células persistidas e dois replays |

## Implementação

`CellKeys` compartilha entre execução e preview a chave e a decisão de leitura. A raiz mantém `content_hash(*spec_identity, *parts)` byte por byte. Um filho usa `content_hash("invocation-v1", tuple(calling_nodes), legacy_hash)`. O scope é estruturado; labels nunca são parseados para compor a identidade. Células de pipeline conservam o id composto produzido por `stage_cell`, inclusive quando o id autorado contém `#`.

Uma coluna nullable `node_scope_json` é adicionada pelo mecanismo idempotente de schema: `NULL` é legado desconhecido; `[]` é raiz observada. Novas conclusões, inclusive respostas humanas, gravam scope no mesmo INSERT cercado da célula/custo/manifesto. Um dono obsoleto não grava metadado separado e uma falha no custo desfaz a célula inteira.

Checkpoint só replaia com scope e id correspondentes, inclusive na raiz. A spec atual não reconstrói autoria: pivôs podem remover irmãos ou trocar refs, e `harness_version` não testemunha a chamada. Sem prova, `cache.missed.reason=legacy_scope_unproven`, advisory e o campo `cache_compatibility` no checkpoint explicam a nova pergunta. Não se transfere uma aprovação por conveniência.

Para leaves aninhados antigos, todas as entradas de um manifesto não vazio precisam ter o owner explícito do harness e o id cru da linha precisa corresponder. JSON inválido, dono ausente, mistura de donos ou dono de irmão recusam o alias. Esse alias é somente leitura: `source_hash` aponta para a célula e custo originais, sem copiar cobranças. Rechecagem de arquivo e carimbos continuam aplicáveis. Um miss sem autoria não atribui o preço do irmão: preview usa `unknown` e, para leaves, `cost_unknown`. Células legadas comuns da raiz mantêm o replay anterior por conteúdo.

Relatórios de filhos passam a usar a chamada `sub[a]:x`/`sub[b]:x`; custos e detalhes de preview mantêm `template` em campo separado. Faults, required failure e route fault usam o mesmo prefixo para preservar os descontos administrativos. O peek de histórico também filtra scope persistido, além do label; a história legada da raiz permanece compatível. Nenhum relatório durável antigo é reescrito.

## Medição de compatibilidade, sem migração de dados reais

O levantamento prévio abriu SQLite exclusivamente por URI `mode=ro&immutable=1`, inspecionando schema/linhas como dados, nunca construindo `SessionDB` sobre profiles reais. Havia 33 bancos, 95 células: 90 relacionáveis à spec durável, 5 órfãs. Dentre as relacionáveis, 4 eram aninhadas (T14, profile `lohra-dogfood-w75`) e 86 de raiz. Os WAL inspecionados tinham zero bytes. Das 41 linhas em bancos que já possuíam `artifact_json`, nenhuma tinha manifesto preenchido. As 4 células aninhadas identificadas não possuíam prova de owner: a política conservadora exige revalidar 2 checkpoints e executar novamente 2 leaves se esse run for retomado. Os 5 registros órfãos não permitem classificação segura.

Nenhum banco real foi migrado, retomado ou modificado para validar esta mudança. Migração, abertura repetida, fence e rollback foram testados em bancos sintéticos. O efeito de compatibilidade é deliberado: aprovações legadas sem prova podem exigir novas perguntas, inclusive na raiz; não há promessa de replay de toda aprovação histórica.

## Validação e limites

- RED inicial: 8 falhas discriminadoras; após a implementação, os mesmos 8 passaram.
- Casos adicionais: manifesto real medido pelo harness, alias repetido sem nova linha/custo, 6 formas de autoria insuficiente, arquivo alterado, schema legado/reabertura, 4 scopes ausentes/malformados/incorretos, fence/rollback, pontuação nos ids, pipeline aninhado e motivo preservado no sanitizador do audit.
- Regressão direcionada: **449 passed em 13,71 s** (25 arquivos: cache, preview, checkpoints, nesting, pipeline, artefatos, custos, pivôs, causalidade, pausa/answer de rota, SQLite, autoria, token budget e fencing). Clientes fake e SQLite sintético; nenhum provider pago. Execução em `backend`, `PYTHONPATH=.`, `/Users/marcelusfernandes/.pyenv/versions/3.12.10/bin/python3 -m pytest -q --no-cov`. A rodada anterior de 323 casos mediu 100% de cobertura do novo `cell_identity.py`; nessa rodada a única falha era a expectativa antiga do label de rota, atualizada no contrato por chamada.
- `ruff check lohra tests`: limpo; `git diff --check`: limpo. Skill builtin `workflow-authoring`: 799 linhas, dentro do limite 800; exemplos e contratos de autoria testados.
- A suíte integrada completa com #87 é responsabilidade da coordenação. Nenhuma publicação, PR, merge ou alteração de versão nesta lane.

Follow-up concreto e preexistente: o protocolo textual de respostas e os labels humanos admitem que um id autorado na raiz `sub[a]:cp` coincida com a apresentação do checkpoint `cp` da chamada `a`. #78 já construía ambos com o mesmo texto. O cache novo e seu histórico distinguem scopes mesmo nesse caso, mas esta fatia não muda a sintaxe de `checkpoint_answers` nem promete desambiguar todo label/custo humano adversarial. Uma futura correção deve escolher escaping ou uma identidade estruturada para esse protocolo, com compatibilidade explícita das perguntas já persistidas. Também não se reconstrói autoria histórica de células cujo conteúdo mudou e já não casa com a chave legada.
