# Dogfood Wave 10.1 — T14 (#78), T15 (#67/#65), T16 (#77) — 2026-09-05

Branch sob teste: `integration/wave10.1`. Profile `lohra-dogfood-w75`. Total ~179 s, ~$1.96 (api_equivalent). Raw em `T14-*-raw.json`, `T15-1-raw.json`, `T16-1-raw.json` (+ prompts `prompt-T1x-*.txt`). Relatório redigido pelo coordenador a partir do relato literal do agente de dogfood (o agente não pôde gravar .md).

## T14 — checkpoint aninhado chaveado pela CHAMADA (#78) — PASS completo
- Template `child` certificado por um run real (não há API manual de registro; `library.record_outcome` certifica run `complete` com `meta.name`).
- Pai com `a`/`b` ambos `ref: child` (args staging/PROD): pausa 1 `{"node_id": "sub[a]:cp", "prompt": "Approve staging?", "template": "child"}`; fault `sub[child]: checkpoint 'cp' is waiting…` (por TEMPLATE — o split documentado).
- Resume com chave crua `{"cp": "sim"}` → recusa didática nomeando `sub[a]:cp`, zero leaves, $0.08.
- Resume `{"sub[a]:cp": "sim"}` → pausa 2 `sub[b]:cp` "Approve PROD?"; resume `{"sub[b]:cp": "sim"}` → `complete`, `b.do = "DONE for PROD."`.
- SURPRESA (limitação conhecida reproduzida ao vivo): `cache_preview` do último resume: `invalidated: sub[child]:cp, sub[child]:do (identity_changed_or_sibling)`, `tokens_to_repay: 998` — as células de runs aninhados são chaveadas pelo REF do template, então `a` e `b` colidem em identidade de célula; o guard de colisão forçou re-spawn (output correto, um leaf a mais). → issue de follow-up.
- Cosmético: `progress.nodes` marca `b` `complete` enquanto o run está pausado no checkpoint de `b` ("complete" = totalmente agendado).

## T15 — `write_file(mode="append")` em path compartilhado — BLOQUEADO pelo sandbox (esperado)
- Sem `workflow_policy.json` no profile → único root gravável é o `working_root` do run, cujo path absoluto nunca é entregue ao leaf (spec 07 §8.2); `notes.txt` relativo resolve fora do allow-set → 3 recusas. Nada gravado; advisory de path N/A.
- SURPRESA: a recusa do sandbox NUNCA vira `fault` — `faults` vazio, `status: complete`; só aparece parafraseada no texto final de cada leaf (3 paráfrases diferentes, nenhuma igual à string canônica do `sandbox.py`). → issue de follow-up.
- #67 (append) segue coberto só por testes (inclusive 2 threads sem perda).

## T16 — `parallel.retries` visível (#77) — PREMISSA DO PROBE INVÁLIDA (culpa do coordenador)
- `workflow_routes.json` do profile mapeia o slug bogus canônico → usado `other-xyz`.
- Rota por BRANCH não é autorável por desenho: `NESTED_SHAPES[("parallel","branches")] = {"prompt"}`; o validador (#82) recusou `provider`/`model` na branch com a mensagem didática nova, antes de spawnar. Não há caminho para matar UMA branch ao vivo sem tool; #77 fica coberto pelos testes de unidade/integração (100% de `parallel_retry.py`).
- Confirmação lateral: leaves de `parallel` rodam sempre no modelo da sessão (T15 rodou em `openai-codex/gpt-5.6-sol`, não no deepseek pedido nas branches — o pedido é ignorado porque não existe roteamento por branch, e agora é recusado).
