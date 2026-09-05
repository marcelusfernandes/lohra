# Dogfood Wave 9 — T17 RERUN (#85, E8: catalog substitution) — 2026-09-05

Branch sob teste: `integration/wave9` no MESMO worktree `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend`, agora em `429f4c6` ("fix(providers): a captured message fingerprint for gateways that send no code (E8, #85)"), sobre `17ed111` ("test(providers): RED — the OpenRouter body a live run really returns (E8, #85)") — confirmado com `git log --oneline -5` antes de iniciar. Profile `lohra-dogfood-w75`. Slug bogus NOVO usado em todo o rerun: `nonexistent-vendor/e8b-xyz` (o T17 original já tinha gravado `e8-xyz`/`no-such-model-xyz` em templates/rotas; `e8b-xyz` garante uma célula/rota nunca vista). Total 3 turnos de chat + 1 script de diagnóstico read-only, ~$0.96 (api_equivalent) nos turnos de chat. Raw em `T17b-a-raw.json`/`T17b-b-raw.json`/`T17b-c-raw.json` (+ stderr, + prompts `prompt-T17b-*.txt`).

## Setup

- Tier map: ausente antes do rerun (mesmo estado do T17 original). Criado para o subteste (b):
  ```json
  {
    "small": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
    "medium": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
    "big": {"provider": "openrouter", "model": "deepseek/deepseek-chat"}
  }
  ```
  em `~/.lohra/profiles/lohra-dogfood-w75/workflow_tiers.json`, removido logo após o subteste (b) — confirmado ausente ao final (`ls` → "No such file or directory").
- `workflow_routes.json` inalterado (mesma entrada do T17 original, não usada — `e8b-xyz` não colide com ela).

## Resultado — **FAIL** para (a) e (b), byte a byte idêntico ao T17 original; **PASS** para (c)

### T17b(a) — sem tier map, esperado PAUSE após UM leaf com `error_kind: model_not_found` — **FAIL**

Mesma spec do T17(a) original, só trocando o slug para `nonexistent-vendor/e8b-xyz`.

- **FAIL** — `error_kind`: esperado `"model_not_found"`, real `null` (idêntico ao antes do fix).
- **FAIL** — `leaf_respawns == 0` / UM leaf: real `leaf_respawns: 2`, 3 tentativas nos `faults` ("attempt 1/3", "2/3", "3/3") — o fix NÃO mudou o número de tentativas.
- **FAIL** — `cause` mencionando "the provider has no model by that name": real continua `"a: leaf failed on the same route after 3 attempt(s); re-spawns exhausted"` — o texto genérico do #43, não o texto novo do #85.
- **PASS parcial** (igual ao original) — `status: paused`, `pause_reason: route_fault` (pela via genérica de esgotamento, não pela via nova).

Payload verbatim (`route` block):
```json
{"node_id": "a", "provider": "openrouter", "model": "nonexistent-vendor/e8b-xyz",
 "error_kind": null,
 "cause": "a: leaf failed on the same route after 3 attempt(s); re-spawns exhausted",
 "last_error": "Error code: 400 - {'error': {'message': 'nonexistent-vendor/e8b-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_REDACTED'}",
 "envelope": "no_envelope"}
```

### T17b(b) — tier map presente, esperado substituição/advisory/complete — **FAIL** (idêntico byte a byte à causa de (a))

- **FAIL** em todos os pontos: `status` real `paused[route_fault]` (esperado `complete`); `advisory_faults: []` (esperado advisory "substituted by"); `leaf_respawns: 2` (esperado `1`); `outputs: {"a": null}`, `b` nunca rodou (esperado outputs para `a` e `b`).
- **FAIL** — `workflow audit <run_id>` no run real (`94b2cb5f...`): `"rerouted": 0`, nenhum evento `node.rerouted`/`channel: catalog`.
- **FAIL** — `~/.lohra/profiles/lohra-dogfood-w75/workflows/templates/` NÃO lista `e8-t17b-b-tier-map.json` (run pausou, nunca certificou) — só `e8-t17b-c-control.json` (do controle (c), que completou) apareceu, confirmando que a certificação em si funciona e que a ausência é evidência válida do FAIL, não um bug de certificação.
- **FAIL** — cópia read-only de `state.db` (`state-copy-rerun.db`), tabela `workflow_insight_candidates`: `SELECT ... WHERE summary LIKE '%model_not_found%' OR payload_json LIKE '%model_not_found%'` → **0 linhas**. Nenhum insight `rule:model_not_found` foi gerado.

### T17b(c) — controle, sem roteamento declarado — **PASS completo** (inalterado)

- `status: complete`, rodou no modelo de sessão (`openai-codex/gpt-5.6-sol`), `faults: []`, `advisory_faults: []`, `outputs: {"c": "CONTROL"}`.
- Certificado como template (`e8-t17b-c-control.json`), confirmando novamente que a certificação funciona e serve de controle positivo.

## CAUSA RAIZ do FAIL persistente — achada por script de diagnóstico read-only (não altera o repo)

O fix `429f4c6` adiciona `_MESSAGE_FINGERPRINTS` e `_matches_message_fingerprint` em `lohra/providers/errors.py`, casando no shape `("openai", "BadRequestError", 400)` com a substring `"is not a valid model ID"`. A lógica lê o corpo do erro via o helper pré-existente `_error_field(exc, key)`:

```python
def _error_field(exc, key):
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = body.get("error")          # <-- assume que body AINDA tem um wrapper "error"
    if not isinstance(error, dict):
        return None
    return error.get(key)
```

Essa suposição está ERRADA para o SDK `openai` real. Chamei diretamente `openai.OpenAI(base_url="https://openrouter.ai/api/v1").chat.completions.create(model="nonexistent-vendor/e8b-xyz", ...)` (mesma credencial `OPENROUTER_API_KEY` do profile, via `~/.lohra/.env`) e inspecionei a exceção crua:

```
class: openai BadRequestError
status_code attr: 400
top-level code attr: 400          # exc.code — property do próprio SDK, JÁ funciona
body: {'message': 'nonexistent-vendor/e8b-xyz is not a valid model ID', 'code': 400}
error_field code: None            # _error_field(exc, "code") — SEMPRE None
error_field message: None         # _error_field(exc, "message") — SEMPRE None
classify_provider_error -> None
```

O corpo HTTP cru É `{"error":{"message":"...","code":400},"user_id":"..."}` (confirmado lendo `exc.response.text` diretamente) — só que o `openai` SDK (`openai._client.OpenAI._make_status_error`, `site-packages/openai/_client.py`) já faz `data = body.get("error", body) if is_mapping(body) else body` ANTES de construir a exceção, e `openai._exceptions.APIError.__init__` faz `self.code = body.get("code")` sobre esse `data` já desembrulhado. Ou seja: no momento em que o código de `lohra` recebe a exceção, `exc.body` JÁ É o dict interno `{"message": ..., "code": 400}` — sem a chave `"error"` — e `exc.code` (o atributo de conveniência do próprio SDK) já vale `400` corretamente. `_error_field` faz um SEGUNDO desembrulho (`body.get("error")`) que não existe mais, e por isso retorna `None` sempre que é chamado sobre uma exceção `openai` real — o que derruba tanto `_matches_message_fingerprint` (a checagem nova) quanto qualquer outro uso futuro de `_error_field` contra o SDK `openai`.

**Efeito**: a condição 2 de `_matches_message_fingerprint` (`code = _error_field(exc, "code"); ... code != status: return False`) sempre reprova (`code` é `None`, não `400`), então a função retorna `False` antes mesmo de checar a mensagem — o fingerprint NUNCA é comparado, apesar de a substring `"is not a valid model ID"` estar de fato presente em `exc.body["message"]`.

**A comparação seria trivial se corrigida**: `_error_field(exc, "code")` deveria ler `body.get(key)` diretamente (sem o `.get("error")` intermediário) para o shape do `openai` SDK — ou `_matches_message_fingerprint` deveria usar `getattr(exc, "code", None)` (que já funciona, é a property do SDK) em vez de `_error_field(exc, "code")`, e `exc.body.get("message")` direto em vez de `_error_field(exc, "message")`. O teste RED do commit `17ed111` provavelmente usa um mock de exceção com `.body = {"error": {...}}` (o shape ERRADO/hipotético), que não reflete o `exc.body` real de uma exceção `openai` de verdade — daí o teste passar em CI e o mecanismo continuar morto ao vivo.

## Custo

| Run | status | leaf_respawns | custo (api_equivalent) |
|---|---|---|---|
| T17b(a) `5ccb2b8e` | paused[route_fault] | 2 | $0.518852 |
| T17b(b) `94b2cb5f` | paused[route_fault] | 2 | $0.124290 |
| T17b(c) `a535abf0` | complete | 0 | $0.317502 |
| **Total (turnos de chat)** | | | **$0.960644** |

(O script de diagnóstico fez 1 chamada adicional direta ao OpenRouter, fora do meter do CLI — custo desprezível, modelo inexistente nunca cobra tokens de saída.)

## SURPRESAS

1. **O fix não corrigiu o comportamento observável — mesma saída, byte a byte, do T17 original**, apesar de citar exatamente o corpo capturado no dogfood anterior no seu próprio comentário (`# Captured 2026-09-05, dogfood T17(a): "nonexistent-vendor/e8-xyz is not a valid model ID"`).
2. **Causa raiz identificada por fora do harness**: `_error_field` (helper reaproveitado de código pré-existente, não novo neste commit) assume um wrapper `{"error": {...}}` em `exc.body` que o SDK `openai` real já removeu antes de a exceção chegar ao código de classificação. Isso não é um problema de "shape do OpenRouter" — é um problema de como o `openai` SDK expõe `.body`/`.code` em QUALQUER exceção 400/401/403/404/429, então qualquer classificação futura que reuse `_error_field` contra uma exceção `openai` real herda o mesmo defeito.
3. O atributo `exc.code` (property nativa do SDK, já usada corretamente em outro ramo de `_is_model_not_found` para o caso `code == "model_not_found"` string) já contém o valor correto (`400`, int) — a peça que falta é só trocar `_error_field` por esse atributo (ou por `exc.body.get(key)` direto) na checagem do fingerprint.
4. Confirmado de novo que a certificação por `meta.name` só ocorre em runs `complete` (T17b(c) certificou, T17b(a)/T17b(b) não) — controle positivo consistente entre as duas rodadas.

## Recomendação (não implementada — fora do escopo deste dogfood)

Em `lohra/providers/errors.py::_matches_message_fingerprint`, trocar:
```python
code = _error_field(exc, "code")
...
message = _error_field(exc, "message")
```
por leitura direta do `exc.body` (já desembrulhado pelo SDK `openai`) e/ou do atributo `exc.code`:
```python
code = getattr(exc, "code", None)
...
body = getattr(exc, "body", None)
message = body.get("message") if isinstance(body, dict) else None
```
Sem esse ajuste, #85/E8 continua morto contra QUALQUER shape que dependa de `_error_field` sobre uma exceção `openai` real — inclusive o shape que este exato fix foi escrito para cobrir.
