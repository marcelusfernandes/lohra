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

## Verificação do wheel na CI

Os jobs existentes Ubuntu/Python 3.11 e 3.13 mantêm Ruff e a suíte do checkout
editável e também executam `python -m ci.wheel_gate` a partir de `backend`.
O gate arquiva o commit realmente selecionado pelo checkout, constrói um wheel
com build isolation e instala esse arquivo com suas dependências numa nova venv,
fora do repositório e sem herdar os pacotes do ambiente editável.

Todos os arquivos rastreados sob `backend/lohra` entram na expectativa de conteúdo,
inclusive novos módulos e assets. O gate compara caminhos e hashes com o wheel e
com a instalação; RECORD válido sozinho não prova que um arquivo obrigatório foi
incluído. Arquivos gerados pelo instalador, como bytecode, entrypoint e metadata
do pip, não são confundidos com arquivos do source.

O smoke comprova origem dos imports em site-packages, versão e entrypoint,
leitura das skills builtin, export de `use-lohra`, help e listagem de workflows.
O controle de chat exige exit 2 e o JSON específico de provider não configurado,
com zero chamadas. Ele bloqueia rede no processo da CLI, incluindo a descoberta
automática de Ollama. Cwd, LOHRA_HOME e export são temporários; chaves, profile e
PYTHONPATH herdados são removidos, preservando HOME/CODEX_HOME. O instalador usa
configuração pip vazia, `NETRC` vazio e keyring/prompts desabilitados, para não
usar credenciais desses diretórios durante build ou instalação de dependências.

Para repetir sobre um commit local, com Python 3.11 ou 3.13 e um destino novo:

```sh
cd backend
python -m ci.wheel_gate --output /tmp/lohra-wheel-check
```

É necessário commitar alterações do pacote/build/helper/CI antes. `result.json`
e os logs individuais registram fases, falhas, duração, versão, SHA do source e
do wheel. A mesma evidência é impressa na CI. O SHA de checkout pode ser o merge
virtual de uma PR; head e base da PR são registrados separadamente. Uma versão
igual à publicada no PyPI não substitui essa identificação do artefato.

O gate não publica um pacote nem usa inferência real. Tempos de instalação com
cache local não são tempos de instalação fria no Ubuntu. A investigação #17
continua responsável pela cobertura nativa mais ampla; este smoke não valida
Windows, assinatura ou PyInstaller.

## As quatro portas (nenhuma exige UI)
| Porta | Comando | Uso |
|---|---|---|
| CLI humano | `lohra chat` | terminal |
| Envelope de orquestração | `lohra chat --json` | Codex/Claude Code/scripts (skill `use-lohra`) |
| API OpenAI-compatível | `lohra serve` | qualquer cliente OpenAI |
| Gateway WS/REST | `lohra dashboard` | opcional — só se uma UI plugar |

Estado em `~/.lohra` (ou `~/.lohra/profiles/<nome>/` com `--profile`). O `.env`
de keys e defaults fica no diretório base compartilhado, `~/.lohra/.env`
(ou `$LOHRA_HOME/.env`). No Windows, a base padrão é `%LOCALAPPDATA%\lohra`
em vez de `~/.lohra`. Configs do operador por profile:
`workflow_policy.json` (fs/egress dos leaves), `workflow_tiers.json` (tiers de modelo),
`workflow_routes.json` (envelope de rotas: para quais rotas alternativas um workflow
pode cair sozinho quando a rota morre), `pricing.json` (preços por modelo — é ele que
torna uma rota comparável). Nenhum deles é lido de uma spec: o que uma spec autorada
(ou injetada) pode pedir nunca amplia o que o operador autorizou.

### `workflow_routes.json` — envelope de rotas (spec 07 §7.7.1)
Sem esse arquivo, rota morta **pausa** o run e um humano responde. Com ele, o harness
pode mover **um** nó `agent` para a próxima rota que VOCÊ listou — nunca outra:

```json
{
  "routes": {
    "anthropic/claude-opus-4-8": {
      "fallback": ["anthropic/claude-haiku-4-5", "openai/gpt-4o-mini"]
    }
  },
  "max_fallbacks_per_run": 2
}
```

Chave e candidatas são `<provider>/<model>` (split na PRIMEIRA barra — model id já tem
barra). Duas coisas que surpreendem na prática: na topologia mais comum — todos os
nós na rota default do run — o envelope compra exatamente **um** nó e o run pausa no
seguinte, porque a franquia é por `(run, rota morta)` e os irmãos default-routed
precisam do mesmo conserto (o remédio deles é uma spec adaptada, não outro palpite);
e um preço mentiroso no `pricing.json` autoriza uma rota cara — a comparação é só tão
honesta quanto a tabela, e aí é o operador mentindo para si mesmo. Regras que o
harness não relaxa: a candidata precisa custar **igual ou menos**
por token nos dois medidores (input e output) segundo a tabela de preços, e preço
desconhecido de qualquer lado (openrouter sem entrada no `pricing.json`, subscription,
modelo sem preço) **não re-roteia** — pausa como antes; o gate de credencial continua
valendo (`openai-codex` só com `lohra auth enable`); e a franquia é durável — 1 fallback
por rota morta e `max_fallbacks_per_run` (default 2) no run inteiro, que um resume não
reabastece. Arquivo ausente/quebrado = sem envelope; uma entrada que declara
QUALQUER chave além de `fallback` — `max_usd_per_cell`, `on`, `budget_usd`, um
`fallbacks` digitado errado — é **descartada inteira**, nunca honrada pela metade:
uma lista de recusa só dos nomes conhecidos ignoraria em silêncio todo limite novo
que você escrevesse, honrando o fallback do lado.

## Catálogo e janela de contexto

`lohra models --provider anthropic` consulta o catálogo e guarda as janelas de
contexto publicadas em `model_windows.json` no estado do profile ativo. O chat
consulta esse cache local ao decidir quando compactar; essa resolução não faz
uma nova chamada de rede nem busca cada modelo individualmente.

O listing da Anthropic publica `max_input_tokens` para a janela de entrada e
`max_tokens` para o limite de saída. Ambos podem ser nulos. O catálogo usa o
primeiro como metadata de contexto; o segundo não define essa janela.
[Contrato da Models API](https://platform.claude.com/docs/en/api/models).
Também são reconhecidos `context_length` e `max_context_length`, inclusive em
`top_provider`. Quando há mais de um valor válido, prevalece o menor; só inteiros
positivos são aceitos, sem converter booleanos, strings ou números fracionários.

Um override explícito de contexto prevalece sobre o cache. Sem uma entrada útil
no cache, a resolução mantém o valor estático do modelo ou o padrão do provider.
A [Models API da OpenAI](https://developers.openai.com/api/reference/resources/models)
não define janela de contexto no objeto básico do modelo; essa rota conserva o
fallback local. A Lohra não extrai esse número de páginas de documentação durante
o chat. O cache distingue provider e modelo: uma entrada `openai` não altera a
janela da rota de assinatura `openai-codex`.

## Autenticação em processos longos

Dashboard, cron e workflows com subscription consultam um snapshot de token/conta
antes de cada request. O login próprio renova e persiste a família sob coordenação
por profile; o reuso Codex apenas relê o arquivo. Uma mudança de conta ou a remoção
do account header vale no próximo request, sem reconstruir o client.

Falha de refresh/store ou credencial recusada pede `lohra auth login` (ou renovação
pelo Codex no caminho de reuso). A Lohra não troca silenciosamente para uma API paga
nem repete esse request por autenticação. `auth disable` bloqueia novos requests;
`auth logout` remove só o login próprio, mantendo o contrato de reuso Codex. Streams
já abertos continuam com o snapshot original. Se houver crash entre rotação remota
e persistência local, pode ser necessário fazer login novamente. Contrato,
compatibilidade do SDK e limites de locks estão na
[spec de subscription](specs/09-subscription-auth.md).

## Diferenças vs checkout de dev
- `lohra update` é git-pull — fora de um checkout ele recusa e aponta o remédio pip.
- Subscription (ToS-gray) é opt-in POR STORE (`lohra auth enable`) — profile novo não herda.
- Freeze PyInstaller (sidecar do desktop) é outro caminho — ver `docs/history/PACKAGING.md`.

## Pendente (decisões do dono)

- Caminho de update automático para instalações pip (hoje: `lohra update` fora de git
  recusa e aponta o remédio `pip install -U lohra`).

(Nome no PyPI, versionamento e CHANGELOG — resolvidos: publicada como `lohra`,
[changelog](../backend/CHANGELOG.md) mantido por release.)

## Windows (validado uma vez em 2026-08-26 — resultado no fim do doc; caminho reproduzível)

O pacote é Python puro → o wheel `lohra-<versão>-py3-none-any.whl` é multiplataforma.
Imports de locks são condicionais por plataforma: `flock` no POSIX e byte-range no
Windows. A nova coordenação de auth ainda não foi validada nativamente no Windows;
0600 não substitui uma ACL restrita nesse sistema.

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
     Login próprio da Lohra com auto-refresh (`%LOCALAPPDATA%\lohra\oauth.json`, sem profile).
     O device flow é print puro — funciona em qualquer terminal.
   - **B (reuse):** Codex CLI NATIVO no Windows já logado → só `lohra auth enable --yes`
     (a Lohra lê `%USERPROFILE%\.codex\auth.json`; respeita `$CODEX_HOME`). Sem auto-refresh.
   - **Pegadinha WSL:** Codex dentro do WSL tem OUTRO home — a Lohra nativa não enxerga o
     auth.json de lá. Nesse caso use o caminho A (ou rode a Lohra dentro do WSL — mas aí a
     prova de fogo vira Linux, não Windows).
   (Alternativa com key: `%LOCALAPPDATA%\lohra\.env` com `ANTHROPIC_API_KEY=...` — o SDK anthropic já vem embutido.)
4. Teste de fogo sugerido, em ordem: `lohra chat --no-tools "oi"` (provider ok?) →
   `lohra chat "liste os arquivos deste diretório"` (tools/terminal no Windows) →
   `lohra chat --json "use um workflow pequeno para ..."` (harness completo; para leaves
   lerem o projeto, crie `%LOCALAPPDATA%\lohra\workflow_policy.json` com fs_allow,
   ou o arquivo correspondente em `profiles\<nome>\` se estiver usando um profile).
5. Pontos a observar (é para isso que a prova existe): terminal tool sob cmd/powershell,
   paths nos leaves do workflow, SQLite/lease em NTFS, console UTF-8 (se acentos
   quebrarem: `set PYTHONUTF8=1`).

## Kit de delegação (v0.0.2+)
A skill `use-lohra` (para Codex CLI / Claude Code delegarem trabalho à Lohra) viaja no
pacote: `lohra skill export use-lohra --to <projeto>/.agents/skills` para Codex
(ou `.claude/skills` para Claude Code; [diretórios e exemplos](../README.md#usar-no-claude-code-ou-codex)).
Sem `--to`, imprime no stdout. Anti-drift: teste pina a cópia empacotada == docs/skills/.

## Prova de fogo no Windows — resultado (2026-08-26)
`pip install lohra` (0.0.3, do PyPI) + `lohra chat` **funcionaram** na máquina Windows
pessoal do usuário — primeira execução da Lohra fora do macOS, direto da distribuição
pública. Round seguinte: Codex CLI de lá delegando via o kit `use-lohra`.
