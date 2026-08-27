# Lohra Desktop Shell — Tauri + React Spec

> Do Hermes Agent (MIT). a Lohra implementa a própria casca Tauri + React. **Divergências Tauri-vs-Electron marcadas com ⚠️.**

## 0. Insight central
O Hermes já contém um app Tauri+React funcional (o instalador `apps/bootstrap-installer`). Seus padrões Rust (commands, event channel, spawn de processo, shell plugin) são o blueprint para portar o main process Electron para a casca Tauri do Lohra.

## 1. Responsabilidades da Casca (lifecycle do backend)

O job central é **spawnar e supervisionar o backend Python local** (`lohra dashboard`, FastAPI/uvicorn) e conectar o renderer via HTTP + WebSocket.

### Resolução do backend (fallback ordenado)
1. Env override (dev checkout) → 2. Dev source → 3. Install bootstrap-complete em `$HOME/lohra-agent` → 4. CLI no PATH (smoke-test `--version`) → 5. módulo pip via Python → 6. sentinela `bootstrap-needed` (dispara first-launch install).

### Seleção de porta
Scan linear `9120–9199`, primeiro disponível (probe `TcpListener::bind`). Token de sessão de 32 bytes (`crypto.randomBytes`/`rand`) por launch, passado via env `LOHRA_DASHBOARD_SESSION_TOKEN`; HTTP carrega como `X-Lohra-Session-Token`.

### Spawn
`lohra dashboard --no-open --host 127.0.0.1 --port <port>` (+ `--profile`). stdout/stderr → `desktop.log` rotativo. `connectionPromise` memoiza o startup in-flight.

### Liveness
Poll `GET /api/status` a cada 500ms até deadline 45s. Ready → `{baseUrl, wsUrl: ws://127.0.0.1:<port>/api/ws?token=…, token, mode:'local'}`.

### Restart/crash
`exit` → broadcast `backend-exit` → UI mostra retry. Crash budget: max 3 reloads/60s. Bootstrap failure latch. Power resume → reconecta WS.

### ⚠️ Replicação Tauri (Rust)
| Concern | Electron | **Tauri** |
|---|---|---|
| Spawn | `child_process.spawn` | `tokio::process::Command` (padrão `run_streamed` do instalador) ou `tauri-plugin-shell` sidecar |
| Port probe | `net.createServer` | `std::net::TcpListener::bind("127.0.0.1:0")` |
| Liveness | `http.request` | `reqwest` polling `/api/status` |
| Stream stdout→UI | stdout `.on('data')` | `BufReader::lines()` → `app.emit("backend-log", …)` |
| State | módulo global | `tauri::State<Arc<Mutex<…>>>` |
| Self-update | git pull + bundle swap | `update.rs` do instalador é referência completa |

## 2. Janela / UX

Janela `1220×800`, min `400×620`. Titlebar custom frameless (`decorations:false` + CSS `data-tauri-drag-region`; macOS vibrancy via `TitleBarStyle::Overlay`). `backgroundThrottling:false` equivalente.

### Rotas (React Router 7)
`/` chat novo; `/:sessionId` chat; `/settings`, `/command-center`, `/skills`, `/messaging`, `/artifacts`, `/cron`, `/profiles`, `/agents`. OVERLAY_VIEWS renderizam como modais full-screen.

### Composição
`SidebarProvider` → `TitlebarControls` → `<main>` com view roteada. Sidebar de chat (esquerda), sidebar direita (terminal + preview), command palette (`cmdk`), status bar.

## 3. Chat UI (`@assistant-ui/react`)

### Runtime
Estado em **nanostores** (`$messages`, `$busy`, `$connection`, ...) alimentado pelo WS JSON-RPC gateway. Bridge via **custom `useIncrementalExternalStoreRuntime`** (sync incremental por mensagem, não clear-and-reimport — trick de perf crítico; portar verbatim). `<AssistantRuntimeProvider>` envolve `<Thread>`.

### Rendering
`MessagePrimitive.Parts`: Text→MarkdownText, Reasoning→thinking disclosure, ToolGroup→tool slot. **Streamdown** (mode streaming, parseIncompleteMarkdown). Code via Shiki (deferido durante streaming). Math via KaTeX memoizado. `useSmoothReveal` (rAF char-reveal) + `DeferStreamingText` (`useDeferredValue`). StallIndicator após 2s.

⚠️ **Tauri:** protocolo `lohra-media://` → `register_uri_scheme_protocol` ou `convertFileSrc`. Leitura data-URL → `#[tauri::command]` retornando base64 ou `tauri-plugin-fs`.

### Tool-call & approval
Tool calls → `ToolFallback`. `approval.request` → `$approvalRequest` nanostore → inline `ApprovalBar` sob a tool row pendente (binding posicional). Choices `once/session/always/deny` via `gateway.request('approval.respond')`. `⌘/Ctrl+Enter`=run, `Esc`=reject.

## 4. Terminal Embutido

### Renderer (portável)
**xterm** (`@xterm/xterm` 6) + addons fit/unicode11/web-links/**webgl**. API: `start({cols,rows,cwd})→{id,shell}`, `write/resize/dispose`, `onData/onExit`.

### ⚠️ DIVERGÊNCIA CRÍTICA: node-pty NÃO funciona no Tauri
Tauri não tem runtime Node. Substituir por **`portable-pty`** (crate do wezterm) na casca Rust. Expor `pty_start/pty_write/pty_resize/pty_kill` como `#[tauri::command]`; stream via `app.emit("pty://<id>/data", …)` + `listen` no React. Manter a API renderer-facing idêntica para portar `use-terminal-session.ts` sem mudança.

## 5. Padrões Tauri (do instalador)

### tauri.conf.json
`productName`, `identifier` (reverse-DNS), `build` (`beforeDevCommand`, `devUrl`, `frontendDist:"../dist"`), `app.windows[]` declarativo, `app.security.csp` (**Lohra adiciona** `ws://127.0.0.1:* http://127.0.0.1:*` ao `connect-src`), `bundle`.

### Capabilities (default-deny)
```json
{ "identifier":"default", "windows":["main"],
  "permissions":["core:default","core:window:allow-close","core:event:default",
    "shell:default","dialog:default","process:default"] }
```
Webview fica off-network; única chamada HTTP externa é Rust-side `reqwest`.

### Commands Rust
`#[tauri::command]` async/sync; registra via `tauri::generate_handler![...]`. Args via serde (`#[derive(Deserialize)]`), state via `tauri::State<Arc<AppState>>`. Padrão fire-and-forget: retorna `Result<(),String>` imediato + `tokio::spawn` do trabalho longo streamando progresso por evento.

### Invoke do React
```ts
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
await invoke('start_backend', { args: {…} })
const unlisten = await listen<Event>('backend-log', e => …)
```
Centralizar invoke/listen em nanostores (padrão do instalador).

### Self-update
Instalador NÃO usa `tauri-plugin-updater` — flow Rust custom (`update.rs`): venv-lock wait, `git`/update, rebuild, atomic bundle swap com rollback, relaunch. ⚠️ Para o backend Python (repo separado), Git-pull fit melhor que o modelo de signed-bundle. Usar `tauri-plugin-updater` só para a casca Tauri em si.

## 6. Design System

### Princípios
1. Flat, não boxed (sem card-in-card). 2. Borderless + shadow p/ elevação. 3. Um primitivo por concern (um Button, um SearchField). 4. Tokens, não literais. 5. Estilo vive no primitivo (call sites passam `variant`/`size`).

### Tokens (CSS vars)
Stroke `--ui-stroke-primary…quaternary`; Text `--ui-text-primary…quaternary`; Fill `--ui-bg-quaternary`; Brand `--theme-primary`, `--ui-accent`; Elevação `shadow-*`; type scale de conversa.

### Inventário
**Button** (variants default/destructive/secondary/outline/ghost/link/text; sizes default/xs/sm/lg/inline + icon family). **Form** (controlVariants, SearchField, SegmentedControl, Switch). **Layout** (OverlaySplitLayout, ListRow). **Feedback** (Loader animado, ErrorState, EmptyState). Iconografia: Codicon (`@vscode/codicons`). Motion ~100ms, respeita `prefers-reduced-motion`. i18n via `useI18n()`.

⚠️ Lohra: rebrandar tokens (`--ui-*`, brand mark) mas manter estrutura e inventário de primitivos.

## 7. Build / Packaging (Tauri)
`tauri build` → `beforeBuildCommand` (Vite) → bundle. Targets `["app","dmg","appimage"]` (+ nsis/msi Windows). Cargo release: `panic="abort"`, `lto=true`, `opt-level="s"`, `strip=true` (5–10MB). Signing: `bundle.macOS.hardenedRuntime:true`, notarização via `APPLE_*` env vars. Build-time pin do commit do backend via `option_env!`.

## Resumo de portabilidade
**Portar quase as-is (renderer):** routes, componentes assistant-ui (Thread/MarkdownText/tool-approval/thinking-disclosure), `incremental-external-store-runtime`, `use-terminal-session` (lado xterm), camada nanostore + gateway client, design system (rebrandado).

**Reescrever em Rust (casca):** spawn/port/liveness/restart/self-update do backend, IPC `window.hermesDesktop` → `#[tauri::command]`, protocolo de media, file/clipboard/dialog via plugins Tauri.

**Substituir (sem equivalente Tauri):** ⚠️ node-pty → `portable-pty`; registro de custom protocol; modelo de self-update do backend.
