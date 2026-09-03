# T11 — envelope de rotas do operador (dogfood direto, 2026-09-03)

O probe pelo Codex foi morto no teto de 10 min do harness no meio da 2ª tentativa (a 1ª foi recusada por PREÇO
DESCONHECIDO — OpenRouter é dinâmico — exatamente o fail-closed desenhado; o Codex escreveu o override em
`~/.lohra/profiles/lohra-dogfood-w75/pricing.json` e o envelope em `workflow_routes.json`). Com os arquivos no lugar,
rodei o cenário diretamente com `lohra chat --profile lohra-dogfood-w75 --json` (LOHRA_AUDIT=1).

## Parte 1 — envelope presente (`fallback: ["openrouter/deepseek/deepseek-chat"]`) → PASS
- run `d13315cb64e04c67be0be0a94dd6d7e2`: status **complete**, sem pausa; envelope `workflows` ausente (correto).
- faults: `(attempt 1/2)`, `(attempt 2/2)`, `re-spawns exhausted`, e
  `doomed: re-routed by operator envelope: openrouter/nonexistent-vendor/no-such-model-xyz -> openrouter/deepseek/deepseek-chat (never chosen by the harness beyond the operator's list)`.
- `leaf_respawns: 2` (1 retry declarado + 1 leaf da re-rota — gate M3).
- `node_costs`: `doomed` em `openrouter / deepseek/deepseek-chat`, custo `api_list_price` via `pricing.json`.
- audit: 3 `leaf.started` (2 na rota morta, 1 na nova) + 1 `node.rerouted` (`role: run.reroute`, `node_path: ["doomed"]`).

## Parte 2 — envelope esgotado (`fallback: []`) → PASS
- run `15b463cea72d44daa8c0143dd1aaab12`: **paused / route_fault**; envelope `workflows: [{status: paused, pause_reason: route_fault}]`.
- `route` traz `provider/model/last_error` e o campo `envelope` (motivo do não-contorno); hint íntegro com a forma do comando (`checkpoint_answers`).

Envelope restaurado ao conteúdo da parte 1 ao final. Extratos em `T11-extracted.json`.
