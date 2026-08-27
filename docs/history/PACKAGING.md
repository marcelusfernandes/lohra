# Packaging (Fase 6)

Como empacotar o Lohra num app desktop que roda **sem Python do sistema**.

## Arquitetura

O app Tauri spawna o backend (`lohra dashboard`). Em dev isso é o `lohra` do
PATH (editable install). Num app empacotado o usuário não tem `lohra` no PATH, então
o backend é **congelado** (PyInstaller) e enviado como **resource** do Tauri:

```
Lohra.app/Contents/Resources/lohra-backend/lohra   ← binário congelado (onedir)
```

`backend.rs::backend_executable()` resolve:
- **release:** `resource_dir()/lohra-backend/lohra` — **falha alto** se faltar (um
  app mal-empacotado não cai silenciosamente num `lohra` que só existe no PATH do dev).
- **dev (`debug_assertions`):** `lohra` do PATH.

### `hardenedRuntime: false` (build local não-assinado)

Precaução, **não** o fix de "backend não subia" (esse era a chave ausente — ver
abaixo). O hardened runtime faz *library validation*: um app hardened pode não
conseguir spawnar um executável de outra identidade. Como o sidecar é ad-hoc
(sem Team ID — não há cert), deixamos `false` p/ o build local não-assinado por
segurança (não chegou a ser confirmado que `true` bloqueia o spawn aqui). **Ao
notarizar é obrigatório voltar p/ `true`** + assinar o sidecar com o mesmo
Developer-ID + entitlements.

### Chave de API no app empacotado

Um app aberto pelo Finder **não herda o ambiente do shell**, então a
`ANTHROPIC_API_KEY` do `~/.zshrc` fica ausente e o backend recusa subir
("no provider configured") → botão de enviar desabilitado. O backend carrega
`~/.lohra/.env` (`KEY=valor`) no startup, e o desktop tem uma UI de settings que
escreve nesse arquivo. **A chave fica em plaintext em `~/.lohra/.env` (chmod
600), não no Keychain.** Salvar pela UI faz um rewrite atômico do arquivo
(create_new + 0600 + rename): os **valores** das outras chaves são preservados,
mas **comentários e a ordem das chaves não** (reescrito alfabético). Edite à mão
só se não se importar com isso — a UI é a dona do arquivo.

## Build (macOS, não-assinado)

```bash
desktop/scripts/build-macos.sh
# 1. PyInstaller onedir → backend/packaging/dist/lohra/
# 2. copia p/ desktop/src-tauri/binaries/lohra-backend/  (resource do Tauri)
# 3. npm run tauri build  → .app + .dmg (ad-hoc signed)
```

Bundle em `desktop/src-tauri/target/release/bundle/`. Gatekeeper avisa no 1º open
(é ad-hoc, não Developer-ID) → botão direito → Abrir.

## O freeze (não-óbvio — ler antes de mexer)

`backend/packaging/lohra.spec`. Quatro coisas que custaram a achar:

1. **`pathex` = raiz do backend.** O editable (`-e`) install esconde o source do
   `lohra` do analisador; sem o pathex o binário builda mas crasha com
   `ModuleNotFoundError: lohra`.
2. **Coletar os deps do `uvicorn[standard]`** (`uvloop`, `httptools`, `websockets`,
   `watchfiles`, `h11`) — o uvicorn os carrega por string em runtime; sem eles o
   server sobe e nunca faz bind, **silenciosamente**.
3. **onedir, não onefile.** O re-exec/unpack-em-/tmp do onefile mascara a saída e
   está implicado em travas do server congelado.
4. **`uvicorn.run(..., lifespan="off")`** (em `cli.py`). O handshake de lifespan ASGI
   dá deadlock dentro do binário congelado; como os apps não registram
   startup/shutdown, desligar é lossless e destrava o bind.

**"Buildou" não é o teste.** Os imports lazy (providers, MCP, web) só quebram em
runtime. Validar rodando: `dist/lohra/lohra --version`, um `chat --provider openai`
(prova o import lazy do SDK), e `dashboard`/`serve` que de fato dão bind (`/health`).

## Validado vs adiado

- ✅ **Validado neste ambiente:** freeze (version/chat/SDKs lazy); `dashboard` e
  `serve` congelados dão bind e respondem (`/health` 200, `/api/status` 200);
  `cargo check` verde; **`Lohra.app` empacotado** (359 MB) com o sidecar em
  `Contents/Resources/lohra-backend/lohra` — rodado com `env -i` (PATH sem
  `lohra`, sem vars de Python) **deu bind em 2s, HTTP 200** → backend é de fato
  self-contained.
- ⚠️ **`.dmg` não gerado aqui:** o `.app` builda primeiro e funciona; o
  `bundle_dmg.sh` do Tauri usa Finder/AppleScript (`osascript`) p/ estilizar a
  janela do dmg e **falha em sessão não-interativa** (headless). Numa sessão
  macOS real (login gráfico) o dmg sai normal — é ambiente, não o código.
- ⏳ **Escrito mas NÃO verificado aqui** (precisa de recursos externos):
  - **Assinatura Developer-ID + notarização** (macOS) — precisa do cert Apple.
  - **appimage / msi** — precisam de Linux / Windows.
  - **Matriz CI** multi-OS — precisa de runners/push.
  - **Casca self-updater do Tauri** — precisa de chave de assinatura + feed de releases.
