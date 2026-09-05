# Dogfood Wave 9 — T17 (#85, E8: catalog substitution) — 2026-09-05

Branch sob teste: `integration/wave9` (worktree `/Users/marcelusfernandes/Desktop/playground-ai/lohra-wt/w9-int/backend`, commit `96c4c1a`). Profile `lohra-dogfood-w75`. CLI invocado via `python3 -c "sys.argv=[...]; from lohra.cli import main; sys.exit(main())"` (o shim `lohra`/`python3 -m lohra` resolvem para o checkout de `main` 0.0.26 ou falham — `lohra.cli:main` é o entry point real e foi confirmado apontando para o worktree antes de cada chamada). Total 3 turnos de chat, ~$1.61 (api_equivalent), raw em `T17a-raw.json`/`T17b-raw.json`/`T17c-raw.json` (+ stderr, + prompts).

## Setup

- `~/.lohra/profiles/lohra-dogfood-w75/workflow_routes.json` (inalterado, não usado neste teste): `{"routes": {"openrouter/nonexistent-vendor/no-such-model-xyz": {"fallback": ["openrouter/deepseek/deepseek-chat"]}}, "max_fallbacks_per_run": 2}`. Por isso os specs usaram o slug `nonexistent-vendor/e8-xyz`, que não colide com essa entrada — garantindo que o teste exercitasse a CATALOG SUBSTITUTION (§7.7.2/#85), não o ROUTE ENVELOPE (§7.7.1/#63).
- Tier map: **não existia** no profile antes do teste (`~/.lohra/profiles/lohra-dogfood-w75/workflow_tiers.json` ausente — confirmado por `ls` antes de qualquer mudança). Schema confirmado em `lohra/workflow/tiers.py` (`home / "workflow_tiers.json"`, carregado por `WorkflowService.__init__`): shorthand string ou dict `{"model":..., "provider":..., "effort":...}` por tier, chaves fechadas em `small`/`medium`/`big`.
- `pricing.json` do profile só tem a seção `openrouter` com dois modelos: `nonexistent-vendor/no-such-model-xyz` (10/10 USD) e `deepseek/deepseek-chat` (1/1 USD) — único candidato PRECIFICADO disponível.
- Tier map CRIADO para T17(b) (removido ao final, restaurando a ausência original):
  ```json
  {
    "small": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
    "medium": {"provider": "openrouter", "model": "deepseek/deepseek-chat"},
    "big": {"provider": "openrouter", "model": "deepseek/deepseek-chat"}
  }
  ```
  Confirmado removido ao final (`ls` retorna "No such file or directory").

## T17(a) — sem tier map → esperado PAUSE após UM leaf — **FAIL**

Spec: `a` (`provider: openrouter`, `model: nonexistent-vendor/e8-xyz`, `retries: 2`, "Say hello."), `b` (`depends_on: [a]`, `provider: openrouter`, `model: deepseek/deepseek-chat`, "Say world.").

- **FAIL** — `error_kind == "model_not_found"`: real era `"error_kind": null`.
- **FAIL** — exatamente UM leaf / `leaf_respawns == 0`: real foi `"leaf_respawns": 2` (3 tentativas: "attempt 1/3", "attempt 2/3", "attempt 3/3", nos `faults`).
- **PASS parcial** — `status: paused`, `pause_reason: route_fault` — isso ocorreu, mas pela via GENÉRICA de esgotamento de retries (a mesma que já existia antes do #85), não pela via nova de `model_not_found`.
- **FAIL** — a `cause` esperada mencionava "the provider has no model by that name"; a real foi `"a: leaf failed on the same route after 3 attempt(s); re-spawns exhausted"` — o texto genérico do #43/opção C, não o texto específico do #85.
- **PASS** — `b` nunca foi agendado (`"outputs": {"a": null}`, progress mostra `b: pending`).

Payload verbatim (`route` block):
```json
{"node_id": "a", "provider": "openrouter", "model": "nonexistent-vendor/e8-xyz",
 "error_kind": null,
 "cause": "a: leaf failed on the same route after 3 attempt(s); re-spawns exhausted",
 "last_error": "Error code: 400 - {'error': {'message': 'nonexistent-vendor/e8-xyz is not a valid model ID', 'code': 400}, 'user_id': 'user_2vUa21XXyB8B3uLDkFAsGmMOwVh'}",
 "envelope": "no_envelope"}
```

**Causa raiz (SURPRISE central deste dogfood):** a spec 07 §7.7.2 já nomeia isso como um risco conhecido: *"the 400 an OpenRouter-style gateway would answer with is a HYPOTHETICAL shape until a real body is captured"*. A chamada real ao vivo prova que o corpo real do OpenRouter é `{'error': {'message': '... is not a valid model ID', 'code': 400}}` — `code` é o INTEIRO 400 (o status HTTP ecoado), não a string `"model_not_found"` que `_MODEL_NOT_FOUND_CODES = frozenset({"model_not_found"})` em `lohra/providers/errors.py` espera. A classe da exceção provavelmente bate em `_NOT_FOUND_TYPES` (`("openai", "BadRequestError")`), mas o `code` sendo `int` reprova o `isinstance(code, str)` na linha 182 de `errors.py`, e o status é 400 (não 404), reprovando também o ramo `not_found_error` da Anthropic. `classify_provider_error` retorna `None`, o leaf cai no caminho ORDINÁRIO de fault (não classificado), e a série de `retries` roda até esgotar — o pause final ainda é `route_fault` (via `should_pause_on_route_fault` genérico), mas `substitute_model` nunca é sequer consultado porque a categoria nunca é `model_not_found`.

**Conclusão:** o mecanismo #85/E8 (`_is_model_not_found`) está morto contra o shape real do OpenRouter — nunca detecta, nunca substitui, nunca produz o advisory. Cobertura hoje é só a shape ANTHROPIC (`not_found_error`, 404) e a shape hipotética `code: "model_not_found"` (string) que nenhum provedor testado ao vivo emite.

## T17(b) — tier map presente → esperado substituição única, advisory, complete — **FAIL** (mesma causa raiz)

Spec idêntica + `a` com `tier: "small"` além do model bogus.

- **FAIL** — `status: complete`: real foi `paused [route_fault]`, idêntico a T17(a) byte a byte na causa (`error_kind: null`, mesma `cause` genérica, `leaf_respawns: 2`).
- **FAIL** — `faults` com advisory "model '...' does not exist... substituted by 'deepseek/deepseek-chat'": `"advisory_faults": []` (vazio).
- **FAIL** — `leaf_respawns == 1`: real foi `2`.
- **FAIL** — outputs para `a` e `b`: real `"outputs": {"a": null}`, `b` nunca rodou.
- **FAIL** — `workflow audit <run_id>` com evento `node.rerouted`/`channel: catalog`: rodado contra o run real (`7c14d4ae...`) — `rerouted: 0` no bloco de rotina do audit, nenhum evento `node.rerouted` presente.
- **FAIL** — `meta.model_substitutions` num template certificado: o run ficou `paused`, nunca `complete`, então nunca foi certificado — confirmado por `ls ~/.lohra/profiles/lohra-dogfood-w75/workflows/templates/`, que NÃO lista `e8-t17b-tier-map.json` (mas lista `e8-t17c-control.json`, do T17c que completou — confirma que a certificação funciona para runs `complete` e simplesmente nunca foi alcançada aqui).
- **FAIL** — insight `rule:model_not_found`/`mechanism: validation`/`responsibility: agency` no `state.db`: copiado `state.db` para scratch, consultado `workflow_insight_candidates` (schema: `fingerprint, kind, status, mechanism, responsibility, confidence, summary, payload_json, ...`) filtrando por `model_not_found` em `summary`/`payload_json` — **0 linhas**. O run nunca gerou o `candidate` insight porque a categoria `model_not_found` nunca foi atingida.

Como esperado dado o T17(a): a presença do tier map não muda nada, porque `substitute_model` nunca é chamado — a falha está a montante, na classificação do erro, não na lógica de substituição em si (que não foi exercitada e permanece coberta só por testes/mocks, não por uma chamada real).

## T17(c) — nó sem roteamento, modelo de sessão → controle — **PASS completo**

Spec: `c` (só `prompt: "Say the single word CONTROL."`).

- **PASS** — `status: complete`.
- **PASS** — rodou no modelo da sessão (`"provider": "openai-codex", "model": "gpt-5.6-sol"` em `node_costs`), não em `openrouter`.
- **PASS** — `"faults": []`, `"advisory_faults": []` — nenhuma substituição, nenhum advisory.
- **PASS** — output correto: `"outputs": {"c": "CONTROL"}`.
- Certificado como template (`e8-t17c-control.json` presente em `~/.lohra/profiles/lohra-dogfood-w75/workflows/templates/`), confirmando que a certificação-por-`meta.name`-em-run-`complete` funciona corretamente e serve de controle positivo para a ausência do arquivo de T17(a)/T17(b).

## Custo / wall-clock

| Run | status | leaf_respawns | tentativas | custo (api_equivalent) |
|---|---|---|---|---|
| T17(a) `b8221e3e` | paused[route_fault] | 2 | 3 | $0.660428 |
| T17(b) `7c14d4ae` | paused[route_fault] | 2 | 3 | $0.456886 |
| T17(c) `370e9838` | complete | 0 | 1 | $0.495864 |
| **Total** | | | | **$1.613178** |

Wall-clock não medido precisamente (turnos únicos via `--json`, sem timestamps de início/fim capturados); cada turno levou dezenas de segundos (3 tentativas de leaf sequenciais em T17a/b, mais o overhead do agente de chat autorando o spec).

## SURPRESAS

1. **Achado principal — #85/E8 está morto contra o OpenRouter real.** O corpo de erro real (`{'error': {'message': '<slug> is not a valid model ID', 'code': 400}}`, `code` como INT 400) não bate em nenhum dos dois ramos de `_is_model_not_found` (nem `code: "model_not_found"` string, nem 404+`not_found_error` da Anthropic). A spec 07 §7.7.2 já sinalizava essa shape como "HYPOTHETICAL até um corpo real ser capturado" — este dogfood captura o corpo real e prova que a hipótese estava errada. Efeito prático: TODO nó `agent` que aponta para um slug OpenRouter inexistente ainda esgota os `retries` inteiros (3 tentativas neste teste) e cai no `route_fault` GENÉRICO, exatamente como se #85 nunca tivesse sido implementado — sem substituição, sem advisory, sem insight, sem economia de leaves.
2. O `route_fault` genérico continua reportando `"error_kind": null`, o que por si só já é um sinal legível de que a classificação estrutural falhou (nenhum dos 4 kinds bateu) — mas nada no output avisa explicitamente "isto parecia um model_not_found e não bateu"; só dá pra saber lendo `last_error`.
3. Certificação por `meta.name` funcionou exatamente como documentado no dogfood anterior (T14/Wave10.1): só runs `complete` viram template, confirmando que a ausência de `e8-t17a`/`e8-t17b` na pasta de templates é evidência válida (e não um bug de certificação).
4. Nenhuma issue de sandbox/policy nem custo inesperado — os $1.61 totais são majoritariamente overhead do agente de chat (autoria dos specs) mais as 3 tentativas reais de leaf em T17(a)/T17(b) batendo no OpenRouter de verdade.

## Recomendação (não implementada, fora do escopo deste dogfood)

`lohra/providers/errors.py::_is_model_not_found` precisa reconhecer o shape real capturado aqui: `("openai", "BadRequestError")` com body `{"error": {"code": <int status>, "message": "<algo> is not a valid model ID"}}` — seja lendo `code` também quando é int (comparando contra o status ao invés de contra uma string fixa), seja adicionando um terceiro ramo que casa na mensagem estruturada `is not a valid model ID` vinda de um `BadRequestError` da OpenRouter (evitar regex sobre prosa livre, mas isso é um formato ESTRUTURAL vindo do gateway, não uma citação de terceiros). Sem esse ajuste, #85/E8 nunca dispara em produção contra OpenRouter — apenas nos testes com mocks que já assumem o shape hipotético.
