# Dogfood via Codex CLI — 2026-09-02 (Wave 7.5 ao vivo)

> Operador: Codex CLI headless (`codex exec`, sandbox `workspace-write` + `network_access=true` +
> `writable_roots=["~/.lohra"]`), skill `use-lohra`, dirigindo a Lohra da branch `integration/wave-7.5`
> (0.0.18) no profile `lohra-dogfood-w75` (subscription habilitada; custo real ≈ 0, `api_equivalent`
> entre US$0,15 e US$0,47 por turno). Duas sessões Codex em paralelo (A: T1–T3, B: T4–T6), cada uma
> escrevendo o próprio relatório. Coordenação e julgamento final: Claude Code.
> Relatórios brutos do Codex: `docs/history/evidence/wave7.5-dogfood/`.

## Placar

| Teste | Issue | Resultado |
|---|---|---|
| T1 sandbox de leaves nega `terminal` | #4 | **Segurança PASS; expectativa de diagnóstico divergiu** (ver abaixo) |
| T2 lint de DAG desconexo | #49 | PASS |
| T3 `depends_on` string rejeitado | #2 | PASS |
| T4 `required: true` + quiescência pós-cancel | #15 / #8 / #42-B | PASS |
| T5 pausa por budget observável em headless | #47 | PASS |
| T6 `LOHRA_PROVIDER_READ_TIMEOUT` honrado | #48 | PASS |

## T1 — o que aconteceu de verdade
- Run `f6516b21` (default): leaf devolveu **"No terminal tool is available."**, `faults: []`, status `complete`,
  nenhum `42`. Run `f0db0a22` (`LOHRA_LEAF_ALLOW_TERMINAL=1`): `{"ok": true, "stdout": "42\n"}`.
- A expectativa escrita no prompt ("texto `sandbox denied` + remédio `allow_terminal`") era do comportamento
  **pré-gate**: o review adversarial do #4 exigiu filtrar `terminal`/`mcp_*` também das *definitions* do leaf
  (defesa em profundidade, igual a `delegate.py`), então o modelo nem tenta chamar. A negação didática do
  dispatch continua existindo para o caso de uma chamada escapar; o remédio ao operador vive na spec §8.3, na
  skill `workflow-authoring` e no docstring do módulo — não no leaf.
- Decisão: **manter**. Um leaf que não vê a tool não gasta iteração descobrindo que ela é negada. Registrado
  como comportamento esperado; a expectativa do teste é que estava errada.

## Achados colaterais (gratuitos)
- **#47 num caso real**: no 1º attempt do T1 o *orquestrador* caiu em "Our servers are currently overloaded"
  (Codex backend) com o leaf já em 1/1; o envelope saiu com
  `workflows: [{"run_id": "dc4d84cd…", "status": "running", "cancelled_on_exit": true}]` e a lista durável
  depois mostrou `cancelled`. Exatamente o contrato: fato observado + o que está prestes a acontecer, nunca
  um "cancelled" adivinhado.
- **T4 caiu no ramo honesto da quiescência**: `a: leaf timeout after 1s (cancelled; 1 leaf STILL RUNNING after
  5.0s quiescence wait — shared working_root may be mutated)`. O leaf estava dentro da chamada ao provider
  (ensaio de 600 palavras), onde o cancel cooperativo não chega. O fault agora diz isso; antes dizia só
  "(cancelled)".
- **Audit da Wave 4 com `skipped` legível**: `{"event_type":"node.failed","node_path":["b"],"data":{"state":"skipped"}}`
  — o gate A1 do review (vocabulário do audit) funcionou; sem ele viria `excluded_by_policy`. As *causas*
  dos faults aparecem redigidas (`"cause": {"state": "redacted", "characters": 122}`) — por design
  (ledger metadata-only), não regressão.
- **T5**: budget 100 → gate de fan-out pausou ANTES de gastar (`spent: 0`), fault
  "fan-out of 3 needs about 6000 tokens; only 100 … left"; `workflow list` mostra `[token_budget_exhausted]`;
  `watch` saiu sozinho em **1 s** com a doutrina do resume humano. Antes desta rodada girava para sempre.
- **T6**: `abc` → warning nomeando a env e PONG normal; `0.05` → "Request timed out." em 2,6 s.
- A Lohra chamou `skill_view('workflow-authoring')` antes de autorar em T4 e mesmo assim respeitou o spec
  "não melhore" — nenhuma deviação de instrução em nenhum dos 6 turnos.

## Infra do dogfood (para a próxima vez)
- **Sandbox do Codex**: o default bloqueia rede e escrita fora do cwd → a Lohra falha com
  `attempt to write a readonly database` (state.db do profile) e `Connection error.` em toda chamada. A
  primeira rodada (A e B) foi 100% infra-fail por isso; o Codex até improvisou um `LOHRA_HOME` em `/tmp`,
  inútil sem rede. Funciona com `-s workspace-write -c sandbox_workspace_write.network_access=true
  -c 'sandbox_workspace_write.writable_roots=["/Users/<u>/.lohra"]'` (e também com `danger-full-access`;
  a política enterprise anotada em 26/08 não se reproduziu).
- **Stop hook do Codex** (`.codex/hooks/pytest-check.sh`, gitignored) tinha o regex `failed|error`, que casa
  com "1 **x**failed" (o xfail estrito da triagem) → o Codex ficou 20+ min em loop rodando a suíte, sem poder
  editar o hook por instrução. Regex corrigido para `\b[0-9]+ (failed|errors?)\b` nos dois hooks (Claude e
  Codex). Se algum dia o hook voltar a girar: é isso.
- `codex exec` por Bash tem teto de 10 min no harness; rodar via `nohup` + marcador de término + Monitor.
- `lohra workflow list` não aceita `--profile`; usar `LOHRA_PROFILE=…`.
- `lohra --version` imprime a versão do metadata do editable install (0.0.14 aqui), não a do pyproject —
  `pip install -e backend` corrige.
