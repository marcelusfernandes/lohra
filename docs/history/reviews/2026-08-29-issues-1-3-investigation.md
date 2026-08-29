# Investigação das issues #1 e #3

Data: 2026-08-29

Baseline: `96440915d001640baf0956131af0672633eaf1fe`

Branch: `fix/gateway-auth-agent-loop`

Esta investigação usou apenas dados sintéticos. Nenhum provider real, token ou
sessão do usuário foi acessado.

## Issue #3 — autenticação REST do gateway

### Hipótese e falsificação

Hipótese: quando `create_app(..., token=...)` recebe um token, somente o
WebSocket aplica a prova de identidade; os endpoints REST ignoram a configuração.

A hipótese seria refutada se qualquer rota REST recusasse uma credencial ausente
ou incorreta, se existisse middleware/dependency de autenticação fora do corpo
das rotas, ou se a política variasse conforme o bind.

### Experimento e resultado

Um `TestClient` com banco e manager sintéticos consultou todas as rotas REST nos
modos `token=None` e `token="secret"`, usando:

- nenhuma credencial;
- header de sessão correto e incorreto;
- Bearer correto e incorreto;
- token na query string.

No baseline, as quatro rotas retornaram `200` em todos os casos:

- `GET /api/status`;
- `GET /api/sessions`;
- `GET /api/sessions/{id}/messages`;
- `GET /api/config`.

O WebSocket recusou token ausente/incorreto com `4401` e aceitou o token correto.
O host é apenas repassado ao uvicorn, portanto loopback versus bind externo não
altera a política da aplicação.

Classificação: **confirmado**.

### Alternativas comparadas

1. Decorar individualmente as rotas: diff pequeno, mas permite drift em rotas
   futuras.
2. Usar dependency de `APIRouter`: torna o header visível no OpenAPI, mas uma
   rota adicionada fora do router pode reabrir a falha.
3. Usar middleware HTTP fail-closed para `/api` e `/api/*`: cobre rotas atuais e
   futuras e preserva o contrato documentado do desktop.
4. Exigir apenas Bearer: aproxima o gateway do servidor OpenAI-compatible, mas
   quebra o consumidor documentado e mistura API key com token efêmero de sessão.
5. Manter status/config públicos: preserva liveness sem credencial, mas status
   expõe contagem de sessões e o shell já possui o token durante o polling.

Decisão: middleware HTTP para todo `/api` e `/api/*`, exceto `OPTIONS`, exigindo
`X-Lohra-Session-Token` quando o token está configurado. `token=None` preserva o
modo inseguro/local. Query string e Bearer não autenticam REST; o WebSocket mantém
`?token=` porque browsers não conseguem definir um header no upgrade.

`/docs`, `/redoc` e `/openapi.json` permanecem públicos por não exporem estado de
sessão.

Incerteza residual: o desktop não está no repositório atual. A compatibilidade é
baseada no contrato registrado em `docs/history/05-desktop-shell.md`, que define
o mesmo header HTTP adotado pela correção.

## Issue #1 — tool calls parciais no agent loop

### Hipótese e falsificação

Hipótese: o assembler de Chat Completions descarta uma slot parcial, preserva
`finish_reason="tool_calls"`, e a normalização entrega uma tupla vazia ao loop. O
loop então instancia `ThreadPoolExecutor(max_workers=0)`.

A hipótese seria refutada se o assembler rejeitasse/reclassificasse a sequência,
se a normalização impedisse o estado contraditório ou se o loop devolvesse um erro
estruturado sem levantar exceção.

### Matriz experimental no baseline

| Stream | Resultado observado |
|---|---|
| Tool call completa e término `tool_calls` | Executa normalmente |
| Término `tool_calls` sem nenhuma slot | `ValueError: max_workers must be greater than 0` |
| Slot sem id ou nome e término `tool_calls` | Slot descartada e mesmo `ValueError` |
| Uma slot completa e outra parcial | Executa apenas o subconjunto completo |
| Tool call completa sem evento terminal | Falso sucesso com assistant/tool call órfã |
| Argumentos JSON truncados com id/nome | Executa a tool com `{}` |

A primeira perda de informação ocorre no assembler: ele removia slots
incompletas, mas mantinha o motivo terminal. O crash era apenas a consequência
final no executor.

Classificação: **confirmado**, com o problema reformulado como duas proteções
necessárias.

### Alternativas comparadas

1. Coagir `tool_calls` vazio para `stop`: evita o crash, mas fabrica sucesso e
   pode persistir histórico inválido.
2. Tornar `_execute_tool_calls(())` um no-op: evita o `ValueError`, mas pode
   consumir iterações até o limite sem explicar a falha.
3. Proteger somente o loop: cobre todos os transports, mas permite que o
   assembler silenciosamente execute apenas parte das slots recebidas.
4. Validar somente o assembler: protege Chat Completions streaming, mas não
   respostas non-stream ou transports defeituosos.
5. Validar assembler e manter backstop no loop: preserva o primeiro ponto com
   informação suficiente e garante o contrato canônico global.

Decisão: rejeitar no assembler qualquer sequência que:

- termine como `tool_calls` sem ao menos uma chamada completa;
- contenha mais slots observadas do que chamadas completas;
- contenha tool-call delta e termine sem um motivo de tool call.

O erro acontece dentro de `client.stream`, já protegido pelo protocolo normal de
erro do turno. O loop também trata `finish_reason="tool_calls"` com tupla vazia,
chamada sem id/nome ou lista mista como erro estruturado, antes de persistir uma
mensagem assistant órfã ou criar o executor.

Argumentos JSON truncados continuam fora deste patch: `parse_tool_arguments`
documenta atualmente a conversão de input malformado para `{}`. Esse achado é
real, mas mudar o contrato ampliaria a issue além do crash e deve ser avaliado
separadamente.

## Validação

- Baseline focado: `74 passed`, seguido das reproduções sintéticas dos dois
  defeitos.
- Red TDD: `9 failed, 73 passed` nos novos casos.
- Após a correção e a revisão: `88 passed` nos testes de gateway, client e loop.
- Ruff focado: limpo.
- Suíte completa: `2076 passed`, cobertura global de 95%.
- Ruff global: limpo.

## Revisão independente

Um subagente que não participou da implementação revisou o diff. A primeira
rodada encontrou que o backstop aceitava tool calls non-stream presentes, mas
sem id/nome. A validação foi ampliada para chamadas vazias, incompletas e listas
mistas, com cobertura Anthropic e Chat Completions non-stream. A sugestão de
baixo risco sobre o middleware também foi incorporada com testes para uma rota
`/api/*` futura e para `OPTIONS`.

Segunda rodada: nenhum finding novo; veredito final **approve**.
