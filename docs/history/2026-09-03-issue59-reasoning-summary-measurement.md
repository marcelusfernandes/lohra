# Issue #59 — `reasoning.summary` no transport de Responses: medição ao vivo

Data: 2026-09-03 · branch `feat/responses-reasoning-summary` · profile `lohra-dogfood-w75`
(subscription Codex, custo real ≈ 0) · modelo `gpt-5.6-sol`, `effort: high`.

A medição É o gate desta fatia: a issue afirma que sob o backend Codex a fase de
raciocínio não entrega **nenhum** evento, e que por isso o abort da 0.0.21 —
lido no topo do `for` de cada evento — não alcança o trecho mais longo do turno.
Este documento registra o que o backend realmente faz.

**Veredito em uma linha:** o backend aceita `summary` (e aceita o replay do
reasoning item com ele preenchido); a premissa do silêncio total está errada — o
que faltava era granularidade, não o primeiro pulso; e a fase de raciocínio passa
de ~13 para 29–49 eventos, com a espera esperada por um cancel caindo de ~4,5 s
para ~2,3 s. Default do switch: **ON**.

## Método

**A régua é o próprio `abort_check`.** Ele é consultado no topo do corpo do `for`
de cada evento (`agent/client.py`), então o intervalo entre duas consultas **é**,
por definição, a latência do abort naquele instante. A sonda passa um
`abort_check` que só carimba o relógio e devolve `False`: mede sem alterar o
caminho medido, e sem instrumentação nova em produção.

Três experimentos:

1. **Histograma de tipos** (`probe_types`): itera o stream cru e conta cada tipo
   de evento com o instante do primeiro de cada um. Responde *o que* o backend
   entrega durante o raciocínio.
2. **Cadência** (`probe_summary`): mede maior silêncio / p95 / mediana entre
   eventos, e faz um **segundo turno** que replaya o reasoning item capturado
   (agora com `summary` preenchido) para verificar se o backend o aceita de volta.
3. **Receita do briefing** (CLI 2×2): `lohra chat --json` pedindo um
   `run_workflow` de um nó `agent` sob `openai-codex` e respondendo na hora com o
   `run_id`; o `finally` do turno chama `workflow_service.shutdown()`, que cancela
   e faz join, **antes** de o envelope ser impresso (`cli.py:794` vs `:813`) — o
   wall-clock do processo já inclui a quiescência.

**Erro corrigido na rodada 1** (registrado porque muda a leitura dos números):
`python3 <caminho>/script.py` põe o diretório do **script** em `sys.path[0]`, não
o cwd, então as duas primeiras sondas importaram o `lohra` do checkout instalado
em vez do da worktree. O kwarg registrado em cada amostra prova qual código rodou;
as amostras afetadas viraram baseline extra. Da rodada 2 em diante cada sonda
imprime `lohra.__file__`, e o braço "depois" só é aceito com `summary` no kwarg.

## Resultado 1 — o backend ACEITA `summary`

Kwargs exatos enviados (`ResponsesTransport.build_kwargs`, `effort="high"`):

```
antes:  "reasoning": {"effort": "high"}
depois: "reasoning": {"effort": "high", "summary": "auto"}
include (inalterado): ["reasoning.encrypted_content"]
```

Nenhum 400. O backend emitiu `response.reasoning_summary_part.added`,
`response.reasoning_summary_text.delta`, `.done` e `response.reasoning_summary_part.done`.
Não foi preciso testar `detailed`/`concise`.

## Resultado 2 — o que existe durante o raciocínio (histograma de tipos)

Mesmo prompt, mesma conta, corridas consecutivas.

| tipo de evento | `summary` off | `summary` auto |
|---|---|---|
| `response.created` / `.in_progress` | 1 / 1 | 1 / 1 |
| `response.output_item.added` / `.done` | 6 / 6 | 7 / 7 |
| `response.reasoning_summary_part.added` | — | 14 |
| `response.reasoning_summary_text.delta` | — | **14** |
| `response.reasoning_summary_text.done` | — | 14 |
| `response.reasoning_summary_part.done` | — | 14 |
| `response.output_text.delta` | 860 | 827 |
| **primeiro evento de raciocínio** | — | **4,99 s** |
| **primeiro texto de saída** | 46,5 s | 50,1 s |
| **maior silêncio entre eventos** | **9,35 s** | **6,40 s** |
| maior silêncio: entre quais eventos | `output_item.added → output_item.done` | `summary_text.delta → summary_text.done` |

**A premissa da issue está parcialmente REFUTADA, e isso é o achado principal.**
O backend Codex não fica em silêncio absoluto durante o raciocínio: ele fecha e
abre um `output_item` por item de reasoning, o que dá ~12 eventos ao longo dos
~44 s de raciocínio deste turno. O que faltava não era *um* pulso — era
**granularidade**. Com `summary: auto` a mesma fase ganha 56 eventos a mais e o
primeiro deles chega aos 5 s em vez de ficar preso ao ritmo dos itens.

Nota honesta sobre o teto: o summary **não** vem tokenizado. São 14 partes, cada
uma entregue em UM delta seguido do `.done` — o espaçamento entre partes é
escolhido pelo modelo, não pelo cliente, então mais eventos ≠ silêncio limitado.
O ganho é real e limitado; quem quiser abort sub-segundo durante o raciocínio
precisa de um relógio entre eventos (hoje nada olha o relógio — ver
`agent/stream_abort.py`), não de mais eventos. Este par de corridas é ilustrativo:
o número que separa os braços de forma sistemática está no Resultado 3.

## Resultado 3 — cadência, antes × depois

Baseline (código do checkout principal / `summary` ausente):

| amostra | total (s) | eventos | 1º texto (s) | **maior silêncio (s)** | p95 (s) | mediana (s) |
|---|---|---|---|---|---|---|
| ANTES-1 | 64,03 | 886 | 48,29 | 9,39 | 0,054 | 0,017 |
| ANTES-1b | 61,51 | 881 | 45,84 | 11,42 | 0,057 | 0,007 |
| ANTES-2 | 37,48 | 959 | 20,22 | 11,40 | 0,040 | 0,018 |
| ANTES-R2-1 (worktree, `off`) | 72,49 | 821 | 57,84 | 10,35 | 0,046 | 0,017 |
| ANTES-R2-2 (worktree, `off`) | 101,05 | 886 | 85,39 | 9,40 | 0,050 | 0,018 |
| DEPOIS-1 (`auto`) | 58,48 | 988 | 42,19 | 15,97 | 0,056 | 0,011 |
| DEPOIS-2 (`auto`) | 63,76 | 996 | 47,29 | 5,90 | 0,041 | 0,016 |

O maior silêncio do **stream inteiro** não separa os braços: 9,4–11,4 s sem
summary contra 5,9–16,0 s com. É a métrica errada — ela é dominada por um único
silêncio, que pode cair DEPOIS do raciocínio (o pior silêncio de `DEPOIS-1` está
fora da fase de raciocínio), e nenhuma quantidade de amostras conserta uma régua
que mede o trecho errado.

### A métrica certa: só a fase de raciocínio

O que a issue pede é a latência do abort **enquanto o modelo raciocina**, ou
seja, os intervalos ANTES do primeiro token de saída. Além do máximo, vale a
**espera esperada** — `Σg²/2Σg` —, que é o que um cancel chegando num instante
aleatório realmente espera (um silêncio longo é proporcionalmente mais provável
de ser aquele em que se cai; a média simples subestima).

Três pares consecutivos, alternando os braços:

| par | modo | eventos antes do texto | maior silêncio (s) | média (s) | **espera esperada (s)** | chamadas de `on_reasoning` |
|---|---|---|---|---|---|---|
| 1 | off | 13 | 11,60 | 2,969 | **4,55** | 0 |
| 1 | auto | 49 | 6,29 | 0,803 | **2,36** | 9 |
| 2 | off | 13 | 10,31 | 3,162 | **4,61** | 0 |
| 2 | auto | 29 | 6,58 | 0,716 | **2,25** | 5 |
| 3 | off | 13 | 10,14 | 2,714 | **4,26** | 0 |
| 3 | auto | 39 | 6,85 | 0,769 | **2,42** | 7 |

O braço `auto` do par 3 terminou com `RemoteProtocolError: peer closed connection
without sending complete message body` **depois** de 897 eventos e ~16 s de texto
de saída já entregue. Não é um 400 nem uma recusa do campo `summary`: é a conexão
caindo no fim de um stream longo, e as métricas da fase de raciocínio (medidas
muito antes) valem. Fica registrado por ser o único incidente de rede de toda a
medição.

Aqui os braços separam limpo e na mesma direção em todas as amostras: **~2–4×
mais eventos durante o raciocínio, maior silêncio ~11 s → ~6,5 s, e a espera
esperada cai pela metade (~4,6 s → ~2,3 s)**. O primeiro sinal de raciocínio
chega aos 4,6–5,6 s; sem summary não existe sinal nenhum até o primeiro item
fechar.

**O dedup do `.done` está certo contra os eventos REAIS do SDK**, e isso nenhum
teste com stub de dicionário poderia provar: no par 1 o `on_reasoning` foi
chamado 9 vezes e o item capturado tem `[3,2,2,2]` = 9 partes de summary; no par
2, 5 chamadas para `[3,2]` = 5 partes. Cada parte chega como UM delta seguido de
um `.done`, e o `.done` foi descartado em todas — se `_summary_key` não batesse
nos objetos do SDK, os números seriam 18 e 10, e a live view mostraria cada
pensamento duas vezes.

## Resultado 4 — receita do briefing (CLI 2×2)

`lohra chat --json` pedindo um `run_workflow` de um nó `agent`
(`openai-codex` / `gpt-5.6-sol` / `effort: high`) e respondendo na hora com o
`run_id`. Os quatro braços fizeram exatamente um `run_workflow` (mais
`skill_view`/`list_models`/`workflow_templates`, benignos), nenhum
`workflow_status`, `cancelled_on_exit: true`, e o ledger registrou
`leaf.started` → `leaf.failed` em todos.

| braço | código | wall-clock do processo (s) | vida da leaf pelo ledger (s) | run_id |
|---|---|---|---|---|
| CLI-ANTES-1 | main | 25,90 | 11,51 | `fc884a9ba3c644e0b631ab253dbc09ef` |
| CLI-ANTES-2 | main | 28,84 | 12,29 | `b72724e2619b4fc8980bc7a5d401258d` |
| CLI-DEPOIS-1 | worktree | 24,47 | 9,09 | `eed9d823b4c04b73b772b76ecc04b7b3` |
| CLI-DEPOIS-2 | worktree | 18,57 | 4,99 | `af9fb668ecc44c03b76b187285b2cc8b` |

**Este experimento é sanidade de ponta a ponta, não o discriminador** — e foi
assim que se leu antes de ver os números. O wall-clock mistura o turno do pai
(que sozinho varia dezenas de segundos) com a quiescência, e a leaf é cancelada
poucos segundos depois de nascer, ou seja, quase sempre ANTES ou no começo do
raciocínio, que é justamente a fase que a mudança melhora. O que ele prova é que
o caminho real funciona sob `summary`: o run é aceito, a leaf roda, o cancel do
`shutdown()` a mata e o turno fecha limpo nos dois lados. A vida da leaf é menor
nos dois braços "depois", mas com n=2 e o pai variando, isso é indício, não
medida — o número medido de verdade está no Resultado 3.

O ledger é **metadata-only** por contrato (spec 08), então a cláusula
`stream aborted on cancel; provider usage unknown` não aparece nele; os eventos
registrados são `leaf.started`/`leaf.failed`/`node.failed`/`workflow.fault`.

## Replay sob `summary` preenchido

Antes desta fatia os reasoning items capturados sempre tinham `summary: []`;
agora carregam texto. Cada sonda faz um segundo turno que replaya o que capturou:

| braço | itens replayados | partes de summary por item | resultado |
|---|---|---|---|
| off (baseline) | 5, 6, 9 | `[0,0,0,0,0]`, … | `completed` |
| auto | 5 | `[3,2,3,2,1]` e `[3,2,3,1,2]` | `completed` |

**O backend aceita de volta o reasoning item com `summary` preenchido** — nenhum
400, nenhuma necessidade de esvaziar o array no replay. `_convert_messages`
continua copiando verbatim o que o backend deu (encrypted state + summary), sem
inventar prosa; os pinos de replay de `reasoning.encrypted_content` seguem
válidos e o teste que os cobre foi renomeado para dizer o que de fato verifica.

## O que esta fatia NÃO entrega (verificado no código, nomeado de propósito)

O critério de aceite da issue tem duas metades, e só uma fecha aqui.

- **Abort — fecha.** Toda leaf de workflow streama: `OrchestrationCore._run`
  chama `GatewaySession.submit`, que chama `run_conversation` com
  `stream_delta_callback` (`gateway/session.py:169`), e o assembler ITERA todos
  os eventos, consultando `abort_check` em cada um — independentemente de haver
  callback de raciocínio. Logo os eventos novos de summary viram pontos de abort
  reais no caminho de produção.
- **Live view de raciocínio — NÃO fecha.** `ResponsesClient.stream` agora
  consome `on_reasoning` (medido: dispara aos ~5 s), mas **nenhum chamador de
  `run_conversation` passa `reasoning_callback`** — nem o CLI (`cli.py:645` só
  passa `stream_delta_callback`), nem o gateway, nem o servidor. O parâmetro
  existe em `loop.py:248` e chega ao cliente, e é aí que a corrente termina.
  Ligar a ponta consumidora é outra fatia; esta entrega o lado do provider.
- **`detailed` / `concise`** são aceitos pelo switch e repassados sem tradução,
  mas **não** foram exercitados ao vivo: `auto` funcionou de primeira e não houve
  motivo para gastar turnos com os outros.
- **Escala não medida.** O raciocínio aqui dura ~35–85 s; o zumbi que originou o
  épico durou 156 s. Se o silêncio do braço `off` cresce com a duração do
  raciocínio (o que tornaria o ganho maior), isto não mede.

## Decisão do default do switch

**`LOHRA_RESPONSES_REASONING_SUMMARY` fica ON por default (`auto`).**

O gate que o briefing definiu era negativo: *se o backend recusar `summary`, o
default vira OFF*. Ele não recusou — aceitou de primeira, emitiu os eventos, e
aceitou de volta o replay do reasoning item com o `summary` preenchido. Somando:

- o ganho medido é real (mais eventos durante o raciocínio, primeiro pulso aos
  ~5 s, espera esperada menor), ainda que limitado;
- o custo é ≈ 0 no único consumidor deste transport (Codex por subscription);
- o caminho default sem `effort` continua byte-idêntico, então nada muda para
  quem não pede raciocínio;
- e quem for mordido por algo não previsto tem uma saída de uma variável de
  ambiente (`=off`) que restaura exatamente o request anterior.

A decisão **não** se apoia no pior silêncio: ele varia de run para run
(6,3–16,0 s com `summary`, 9,4–11,6 s sem) porque quem escolhe o espaçamento das
partes do summary é o modelo, não o cliente. Fixar isso exigiria olhar o relógio
entre eventos — nada hoje faz isso (`agent/stream_abort.py`), e seria outra fatia.
