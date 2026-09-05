# Dogfood Wave 9 — T17 RUN 3 (#85, E8: catalog substitution) — 2026-09-05

Branch sob teste: `integration/wave9` no MESMO worktree `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend`, agora em `a66af2e` ("fix(providers): the error payload accessor tolerates both SDK shapes (E8, #85)"), sobre `5513980` ("test(providers): RED — the SDK unwraps the error layer; the fixture did not (E8, #85)") e `429f4c6` (fix 1, o fingerprint) — confirmado com `git log --oneline -5` antes de iniciar. Profile `lohra-dogfood-w75`. Slug bogus NOVO usado em todo o run 3: `nonexistent-vendor/e8c-xyz` (nunca usado nas duas rodadas anteriores). Total 3 turnos de chat + 2 scripts de diagnóstico read-only, ~$0.70 (api_equivalent) nos turnos de chat. Raw em `T17c-a-raw.json`/`T17c-b-raw.json`/`T17c-c-raw.json` (+ stderr, + prompts `prompt-T17c-*.txt`).

## Resultado — **PASS completo para (a), (b) e (c)**

O fix 2 corrige exatamente a causa raiz apontada no meu diagnóstico da rodada anterior: `_error_payload(exc)` agora faz `body.get("error", body)` (desce o envelope quando existe — shape anthropic — e fica no lugar quando já está achatado — shape openai real), e `_error_code_of(exc)` prefere o atributo `.code` nativo do SDK (`getattr(exc, "code", None)`) antes de cair no payload. Confirmado por leitura do código-fonte em `lohra/providers/errors.py` (linhas 180-206) antes de rodar qualquer teste.

### T17c(a) — sem tier map → PAUSE após UM leaf — **PASS**

Spec: `a` (`provider: openrouter`, `model: nonexistent-vendor/e8c-xyz`, `retries: 2`, "Say hello."), `b` (`depends_on: [a]`, `provider: openrouter`, `model: deepseek/deepseek-chat`, "Say world.").

- **PASS** — `"error_kind": "model_not_found"` (era `null` nas duas rodadas anteriores).
- **PASS** — `"leaf_respawns": 0`, exatamente UM leaf — o log do CLI mostra só um "leaf error" (nenhum "(attempt N/3)"), contra as 3 tentativas das rodadas anteriores.
- **PASS** — `cause` nomeia "the provider has no model by that name": `"a: leaf error: the provider has no model by that name (Error code: 400 - {...}); no retry on the same route can repair a slug that does not exist — name a model the catalog lists, or a \`tier:\` the operator mapped — run paused (route_fault): ..."`.
- **PASS** — `b` nunca agendado: `"outputs": {"a": null}`, `progress.nodes` mostra `b: pending`.
- **PASS** — `status: paused`, `pause_reason: route_fault`.

Payload verbatim (`route` block):
```json
{"node_id": "a", "provider": "openrouter", "model": "nonexistent-vendor/e8c-xyz",
 "error_kind": "model_not_found",
 "cause": "a: leaf error: the provider has no model by that name (Error code: 400 - {'error': {'message': 'nonexistent-vendor/e8c-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAs...",
 "last_error": "Error code: 400 - {'error': {'message': 'nonexistent-vendor/e8c-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'}",
 "envelope": "no_envelope"}
```

### T17c(b) — tier map presente → substituição, advisory, complete — **PASS**

Mesma spec + `a` com `tier: "small"`. Tier map criado em `~/.lohra/profiles/lohra-dogfood-w75/workflow_tiers.json` (small/medium/big -> `openrouter`/`deepseek/deepseek-chat`, o único modelo precificado no `pricing.json` do profile), removido ao final e confirmado ausente.

- **PASS** — `status: "complete"`.
- **PASS** — advisory com "substituted by": `"advisory_faults": ["a: model 'nonexistent-vendor/e8c-xyz' does not exist on 'openrouter'; substituted by 'deepseek/deepseek-chat' from the operator's tier map — fix the spec, or map the tier you meant in ~/.lohra/workflow_tiers.json"]`.
- **PASS** — `"leaf_respawns": 1`.
- **PASS** — outputs de `a` e `b`: `"outputs": {"a": "Hello! How can I assist you today?", "b": "World."}`.
- **PASS** — `workflow audit <run_id>` (run real `ae5431cd51fb4cc6ab9e1737efcf7b19`) mostra evento `node.rerouted`:
  ```json
  {"event_type": "node.rerouted", "data": {"node_id": "a",
    "from": {"provider": "openrouter", "model": "nonexistent-vendor/e8c-xyz"},
    "to": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
    "channel": "catalog"}}
  ```
  e o bloco `routing`: `{"rerouted": 1, "reroutes": [{"seq": 8, "node_id": "a", "from": {...}, "to": {...}, "channel": "catalog"}]}`.
- **PASS** — template certificado (`~/.lohra/profiles/lohra-dogfood-w75/workflows/templates/e8-t17c-b-tier-map.json`, run `complete` + `meta.name`) lista `meta.model_substitutions`:
  ```json
  "model_substitutions": [{"node": "a", "from": "nonexistent-vendor/e8c-xyz", "to": "deepseek/deepseek-chat"}]
  ```
  e `"rerouted_nodes": ["a"]`.
- **PASS com ressalva de literalidade** — insight na cópia read-only de `state.db` (`state-copy-run3.db`): uma linha NOVA em `workflow_insight_candidates`:
  ```
  fingerprint=ccfe5c71c664c08f123554abd5862597
  kind=candidate, status=model_substituted, mechanism=validation, responsibility=agency, confidence=1.0, hits=1
  summary="authored workflow node named a model the provider does not have: 'nonexistent-vendor/e8c-xyz' -> substituted by 'deepseek/deepseek-chat' from the operator's tier map"
  ```
  `mechanism: validation` e `responsibility: agency` batem EXATAMENTE com a expectativa. A string literal `rule:model_not_found` NÃO aparece em texto puro em nenhuma coluna (o `payload_json` desta linha está vazio/NULL) — ela entra como um dos dois elementos de `model_substitution_signals()` (`SIGNAL_SPEC_SHAPE`, `"rule:model_not_found"`) que compõem o HASH do `fingerprint`, não um campo legível. Reportado como PASS pela evidência disponível (mechanism/responsibility/status/summary todos corretos e não havia nenhuma linha assim nas duas rodadas anteriores), mas a string exata pedida não é literalmente lida em lugar nenhum do banco — só inferida da lógica de `service.py::model_substitution_signals`.

### T17c(c) — controle, sem roteamento — **PASS completo** (inalterado nas 3 rodadas)

- `status: complete`, rodou no modelo de sessão (`openai-codex/gpt-5.6-sol`), `faults: []`, `advisory_faults: []`, `outputs: {"c": "CONTROL"}`.
- Certificado como template (`e8-t17c-c-control.json`).

## `diag_classify2.py` rodado de novo contra o worktree (pedido explícito do coordenador)

Script reutilizado sem alterações desde a rodada 2 (ainda usa o slug antigo `e8b-xyz` — deliberadamente inalterado, só para reproduzir o MESMO shape bruto de antes e confirmar que ele é estável/determinístico independente do slug):

```
str(exc): Error code: 400 - {'error': {'message': 'nonexistent-vendor/e8b-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'}
response type: <class 'httpx.Response'>
raw text: {"error":{"message":"nonexistent-vendor/e8b-xyz is not a valid model ID","code":400},"user_id":"user_2vUa21XXyB8B3uLDkFAsGmMOwVh"}
body attr: {'message': 'nonexistent-vendor/e8b-xyz is not a valid model ID', 'code': 400}
```

Confirma que o shape bruto do OpenRouter (via SDK `openai`) continua o mesmo capturado nas rodadas 1 e 2 — `exc.body` é o dict interno JÁ desembrulhado, sem chave `"error"`. O que mudou é só o lado de `lohra/providers/errors.py`, que agora lê esse shape corretamente (`_error_payload`/`_error_code_of`), como provado por um segundo script (`diag_classify3.py`, novo nesta rodada, chamando `classify_provider_error` direto com o slug novo `e8c-xyz`):

```
class: openai BadRequestError
status_code attr: 400
_status_of: 400
body attr: {'message': 'nonexistent-vendor/e8c-xyz is not a valid model ID', 'code': 400}
_error_payload: {'message': 'nonexistent-vendor/e8c-xyz is not a valid model ID', 'code': 400}
_error_code_of: 400
fingerprints table key present: True
classify_provider_error -> model_not_found
```

## Custo / evidência

| Run | status | leaf_respawns | custo (api_equivalent) |
|---|---|---|---|
| T17c(a) `d65ace0e` | paused[route_fault], error_kind=model_not_found | 0 | $0.325153 |
| T17c(b) `ae5431cd` | complete | 1 | $0.119855 |
| T17c(c) `2f5288e5` | complete | 0 | $0.250460 |
| **Total (turnos de chat)** | | | **$0.695468** |

Tier map criado em `~/.lohra/profiles/lohra-dogfood-w75/workflow_tiers.json` para (b), confirmado removido ao final (`ls` → "No such file or directory"). Nenhum arquivo do repositório foi alterado; `git status` no worktree segue limpo.

## SURPRESAS

1. **O fix 2 resolve completamente o problema identificado nas duas rodadas anteriores.** As duas correções eram necessárias e complementares: fix 1 (`429f4c6`) adicionou o fingerprint de mensagem para o shape sem código estrutural; fix 2 (`a66af2e`) corrigiu o acessor que lia esse fingerprint (e o `code`) de um shape de `.body` que nunca existiu de verdade para o SDK `openai` real. Sem o fix 2, o fix 1 nunca disparava — exatamente o que as rodadas 1 e 2 deste dogfood mostraram ao vivo, byte a byte.
2. Único ponto de literalidade não confirmado: a string `rule:model_not_found` citada na expectativa do coordenador não aparece em texto puro em nenhuma linha do `state.db` — ela é um componente do hash do `fingerprint`, não um campo legível. Tudo o que É legível (`mechanism=validation`, `responsibility=agency`, `status=model_substituted`, `summary` nomeando a substituição) bate perfeitamente e é NOVO nesta rodada (as duas rodadas anteriores não geraram nenhuma linha assim). Reportado como PASS com essa ressalva, não como FAIL.
3. Certificação por `meta.name` seguiu funcionando de forma consistente nas 3 rodadas: só runs `complete` viram template, com `meta.model_substitutions`/`rerouted_nodes` aparecendo exatamente quando (e só quando) uma substituição real e bem-sucedida ocorreu.
4. Custo total das 3 rodadas de T17 (original + rerun + run 3): aproximadamente $1.61 + $0.96 + $0.70 ≈ **$3.27** em turnos de chat, para provar e depois confirmar a correção de um mecanismo que nunca tinha sido exercitado contra o provedor real antes deste dogfood.
