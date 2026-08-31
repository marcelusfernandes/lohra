# Lohra como runtime standalone

O backend É o runtime — o desktop app é um consumidor opcional. Instalação sem repo, sem TUI,
sem desktop (validado em venv limpo, Python 3.13, 2026-08-26):

```bash
# requer Python 3.11–3.13 (o python3 default do macOS pode ser mais velho — use python3.13)
pip install lohra                     # PyPI (recomendado) — ou, de um checkout: pip install ./backend
lohra --version
```

O que o wheel carrega: o pacote inteiro + a skill builtin `workflow-authoring`
(package-data; `builtin_root()` resolve de site-packages — validado).

## As quatro portas (nenhuma exige UI)
| Porta | Comando | Uso |
|---|---|---|
| CLI humano | `lohra chat` | terminal |
| Envelope de orquestração | `lohra chat --json` | Codex/Claude Code/scripts (skill `use-lohra`) |
| API OpenAI-compatível | `lohra serve` | qualquer cliente OpenAI |
| Gateway WS/REST | `lohra dashboard` | opcional — só se uma UI plugar |

Estado em `~/.lohra` (ou `--profile`). Configs do operador: `.env` (keys),
`workflow_policy.json` (fs/egress dos leaves), `workflow_tiers.json` (tiers de modelo).

## Diferenças vs checkout de dev
- `lohra update` é git-pull — fora de um checkout ele recusa e aponta o remédio pip.
- Subscription (ToS-gray) é opt-in POR STORE (`lohra auth enable`) — profile novo não herda.
- Freeze PyInstaller (sidecar do desktop) é outro caminho — ver `docs/history/PACKAGING.md`.

## Pendente (decisões do dono)

- Caminho de update automático para instalações pip (hoje: `lohra update` fora de git
  recusa e aponta o remédio `pip install -U lohra`).

(Nome no PyPI, versionamento e CHANGELOG — resolvidos: publicada como `lohra`, 0.0.13,
`backend/CHANGELOG.md` mantido por release.)

## Windows (validado uma vez em 2026-08-26 — resultado no fim do doc; caminho reproduzível)

O pacote é Python puro → o wheel `lohra-<versão>-py3-none-any.whl` é multiplataforma.
Zero imports Unix-only no backend (verificado); os `chmod 600` dos arquivos de auth
viram no-op no Windows (funciona, mas sem a permissão restrita — ciente).

1. Instale Python 3.11–3.13 (python.org; marque "Add to PATH"). Confira: `py -3.13 --version`.
2. Gere o wheel (no macOS/Linux: `python -m build backend -o dist` → `dist/lohra-<versão>-py3-none-any.whl`), copie para a máquina e:
   ```powershell
   py -3.13 -m venv lohra-env
   .\lohra-env\Scripts\Activate.ps1
   pip install .\lohra-<versão>-py3-none-any.whl
   lohra --version
   ```
3. Auth por subscription (sem key) — dois caminhos:
   - **A (recomendado, não precisa do Codex CLI):**
     ```powershell
     lohra auth enable --yes
     lohra auth login       # imprime URL + código; entre no navegador de qualquer aparelho
     ```
     Login próprio da Lohra com auto-refresh (`%USERPROFILE%\.lohra\oauth.json`).
     O device flow é print puro — funciona em qualquer terminal.
   - **B (reuse):** Codex CLI NATIVO no Windows já logado → só `lohra auth enable --yes`
     (a Lohra lê `%USERPROFILE%\.codex\auth.json`; respeita `$CODEX_HOME`). Sem auto-refresh.
   - **Pegadinha WSL:** Codex dentro do WSL tem OUTRO home — a Lohra nativa não enxerga o
     auth.json de lá. Nesse caso use o caminho A (ou rode a Lohra dentro do WSL — mas aí a
     prova de fogo vira Linux, não Windows).
   (Alternativa com key: `%USERPROFILE%\.lohra\.env` com `ANTHROPIC_API_KEY=...` — o SDK anthropic já vem embutido.)
4. Teste de fogo sugerido, em ordem: `lohra chat --no-tools "oi"` (provider ok?) →
   `lohra chat "liste os arquivos deste diretório"` (tools/terminal no Windows) →
   `lohra chat --json "use um workflow pequeno para ..."` (harness completo; para leaves
   lerem disco, crie `%USERPROFILE%\.lohra\workflow_policy.json` com fs_allow).
5. Pontos a observar (é para isso que a prova existe): terminal tool sob cmd/powershell,
   paths nos leaves do workflow, SQLite/lease em NTFS, console UTF-8 (se acentos
   quebrarem: `set PYTHONUTF8=1`).

## Kit de delegação (v0.0.2+)
A skill `use-lohra` (para Codex CLI / Claude Code delegarem trabalho à Lohra) viaja no
pacote: `lohra skill export use-lohra --to <projeto>/.codex/skills` (ou `.claude/skills`).
Sem `--to`, imprime no stdout. Anti-drift: teste pina a cópia empacotada == docs/skills/.

## Prova de fogo no Windows — resultado (2026-08-26)
`pip install lohra` (0.0.3, do PyPI) + `lohra chat` **funcionaram** na máquina Windows
pessoal do usuário — primeira execução da Lohra fora do macOS, direto da distribuição
pública. Round seguinte: Codex CLI de lá delegando via o kit `use-lohra`.
