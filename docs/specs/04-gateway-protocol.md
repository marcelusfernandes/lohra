# Lohra Gateway / Client-Server Protocol Spec

> **Nota:** doc de bootstrap (Fases 0–3); o vocabulário WS abaixo inclui métodos que o spec 06 registra como "listados, não implementados" (`session.steer`, `prompt.background`, `session.branch`). Para os métodos realmente atendidos, a fonte é `backend/lohra/gateway/app.py`.

> Do Hermes Agent (MIT). **DUAS superfícies distintas** — não confundir.

| Superfície | Comando | Porta | Framework | Cliente |
|---|---|---|---|---|
| **Dashboard/Gateway** | `lohra dashboard` | **9119** | FastAPI + uvicorn | App Tauri/React |
| **OpenAI-compatible** | adapter `api_server` | **8642** | aiohttp | Clientes OpenAI externos |

O app desktop fala com **9119** (FastAPI dashboard). O `/v1/*` em 8642 é integração separada.

## 1. Startup

### Dashboard (`lohra dashboard`)
```
lohra dashboard [--host 127.0.0.1] [--port 9119] [--no-open] [--insecure]
```
`FastAPI(title="Lohra", lifespan=...)` via uvicorn. Expõe SPA estática, `/api/*` REST, WS endpoints (`/api/ws`, `/api/pty`, `/api/pub`, `/api/events`), auth routes.

## 2. WebSocket JSON-RPC (`/api/ws`) — o seam mais importante

### Transporte
URL `ws://<host>:9119/api/ws`. **JSON-RPC 2.0 newline-delimited**, bidirecional. `TCP_NODELAY`. No accept, push imediato de `gateway.ready`.

### Três tipos de frame
```ts
interface JsonRpcFrame {
  error?: { message?: string }; id?: number | string | null;
  method?: string;          // "event" p/ pushes server→client
  params?: GatewayEvent; result?: unknown;
}
interface GatewayEvent<P = unknown> { type: GatewayEventName; session_id?: string; payload?: P }
```
1. **Client→Server request:** `{jsonrpc:"2.0", id:7, method:"prompt.submit", params:{...}}`
2. **Server→Client response:** `{jsonrpc:"2.0", id:7, result:{...}}` ou `{...error:{code,message}}`
3. **Server→Client event:** `{jsonrpc:"2.0", method:"event", params:{type, session_id, payload}}`

### Métodos Client→Server (vocabulário de request)

**Sessão:** `session.create {cols?, messages?, title?, cwd?, profile?, close_on_disconnect?}`, `session.list`, `session.most_recent`, `session.resume`, `session.delete`, `session.title`, `session.usage`, `session.status`, `session.history`, `session.cwd.set`, `session.undo`, `session.compress`, `session.branch`, `session.interrupt`, `session.steer {text}`.

**Driver do turno:** `prompt.submit {session_id, text, truncate_before_user_ordinal?}` → retorna `{status:"streaming"}` imediato, roda em thread, streama eventos. `4009 "session busy"` se turno ativo. `prompt.background`.

**Respostas interativas:** `clarify.respond {request_id, answer}`, `sudo.respond {request_id, password}`, `secret.respond {request_id, value}`, `approval.respond {choice, all?}`.

**Anexos/input:** `clipboard.paste`, `image.attach`, `pdf.attach`, `file.attach`, `terminal.resize {cols,rows}`.

**Config/model/tools:** `config.set/get/show`, `model.options/save_key/disconnect`, `tools.list/show/configure`, `toolsets.list`, `setup.status`, `reload.mcp/env`, `plugins.list/manage`, `cron.manage`, `skills.manage/reload`.

### Eventos Server→Client (vocabulário de streaming)
```ts
type GatewayEventName =
  | 'gateway.ready' | 'session.info'
  | 'message.start' | 'message.delta' | 'message.complete'
  | 'thinking.delta' | 'reasoning.delta' | 'reasoning.available'
  | 'status.update'
  | 'tool.start' | 'tool.progress' | 'tool.complete' | 'tool.generating'
  | 'clarify.request' | 'approval.request' | 'sudo.request' | 'secret.request'
  | 'background.complete' | 'error' | 'skin.changed'
  | (string & {})   // forward-compatible
```

| Evento | payload |
|---|---|
| `gateway.ready` | `{skin:{name,colors,branding,banner_logo,...}}` |
| `session.info` | `{model, reasoning_effort, fast, yolo, tools, skills, cwd, running, version, usage, profile_name}` |
| `message.start` | *(vazio)* |
| `message.delta` | `{text, rendered?}` |
| `message.complete` | `{text, usage, status:"complete"\|"interrupted"\|"error", reasoning?, warning?}` |
| `reasoning.delta` | `{text, verbose?}` |
| `status.update` | `{kind, text}` |
| `tool.start` | `{tool_id, name, context, args_text?}` |
| `tool.complete` | `{tool_id, name, args, result, duration_s?, summary?, inline_diff?}` |
| `tool.generating` | `{name}` |
| `clarify.request` | `{request_id, question, choices}` |
| `approval.request` | `{request_id, command, description, pattern_key, pattern_keys}` |
| `error` | `{message}` |

### Mecânica de streaming
`prompt.submit` retorna `{status:"streaming"}` síncrono, agente roda em thread, output multiplexado no WS único como frames `event` (mesmo `session_id`). Prompts interativos bloqueantes: `_block(event, sid, payload, timeout=300)` emite request com `request_id`, bloqueia agente em `threading.Event`, espera `*.respond`. Correlação: `session_id` / `tool_id` / `request_id`.

## 3. REST (FastAPI 9119) — principais
- **Sessions:** `GET /api/sessions`, `GET /api/sessions/{id}/messages`, `PATCH/DELETE /api/sessions/{id}`, `POST /api/sessions/bulk-delete|prune`.
- **Status/config:** `GET /api/status`, `GET/PUT /api/config`, `GET /api/config/schema`.
- **Env/creds:** `GET/PUT/DELETE /api/env`, `POST /api/providers/validate`, `GET/POST /api/credentials/pool`.
- **Model:** `GET /api/model/info|options|auxiliary`, `POST /api/model/set`.
- **OAuth:** `GET /api/providers/oauth`, `POST .../{id}/start|submit`, `GET .../{id}/poll/{session_id}`.
- **Skills/tools:** `GET /api/skills`, `PUT /api/skills/toggle`, `GET /api/tools/toolsets`.
- **Cron:** `GET/POST /api/cron/jobs`, `POST .../{id}/pause|resume|trigger`.
- **Profiles:** `GET/POST /api/profiles`, `GET/PUT /api/profiles/{name}/soul`.
- **Auth:** `GET /api/auth/me`, **`POST /api/auth/ws-ticket`** (ticket single-use 30s).

## 4. OpenAI-Compatible (8642)
- `POST /v1/chat/completions` — OpenAI padrão. Headers custom: `X-Lohra-Session-Id` (continuar sessão), `Idempotency-Key`. Streaming SSE: chunks padrão + evento custom `event: lohra.tool.progress` para progresso de tool. Keepalive 30s.
- `POST /v1/responses` — Responses API. SSE: `response.created/.output_text.delta/.completed`. State em `ResponseStore` (LRU 100).
- `POST /v1/runs` — async. `202` com `run_id`; `GET /v1/runs/{id}/events` SSE; `POST .../approval|stop`.
- Auth: `Authorization: Bearer <API_SERVER_KEY>`.

## 5. Auth do WebSocket (dashboard)
Browsers não setam header em WS upgrade → 3 formas via query param:
1. **Token loopback** (default local): `?token=<SESSION_TOKEN>`, constant-time compare. Rejeitado em modo gated.
2. **Ticket OAuth:** `POST /api/auth/ws-ticket` (cookie) → `{ticket, ttl:30}`, conecta com `?ticket=`. Single-use, 30s.
3. **Internal:** `?internal=<cred>` — só para WS children spawned pelo server.

Close codes: `4401` auth, `4403` forbidden, `4400` bad channel.

## Notas para Lohra
- Tratar dashboard-WS e OpenAI-SSE como **dois protocolos separados** compartilhando o mesmo agent core.
- `GatewayEventName` é o vocabulário do dashboard-WS; o api_server emite eventos OpenAI-spec (`response.*`, `lohra.tool.progress`).
- O contrato `prompt.submit` → stream de eventos → `message.complete` é o coração; implementar primeiro.
