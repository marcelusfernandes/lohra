# Backlog de Onboarding — Lohra

Escopo: o caminho entre `pip install lohra` e o primeiro sucesso real do usuário.
Todas as dores abaixo foram vividas em dogfood, não imaginadas.

---

## 1. Instalação vs. primeiro uso — onde o onboarding pode existir

**Não existe momento interativo na instalação, e o desenho tem que assumir isso.** A especificação do wheel (PEP 427) define instalar como copiar arquivo — "pode ser instalado simplesmente descompactando em site-packages com a ferramenta padrão `unzip`" (https://peps.python.org/pep-0427/); não há passo de execução de código no formato. A ausência é confirmada por omissão: a issue `pypa/packaging-problems#64`, literalmente "non-hacky, wheel-compatible way to implement a post-install hook", segue aberta sem solução oficial (https://github.com/pypa/packaging-problems/issues/64), e o único jeito conhecido de rodar código pós-install é abusar de arquivos `.pth` para executar no startup do interpretador (`wheel-axle-runtime`, https://pypi.org/project/wheel-axle-runtime/) — hack, não feature. O ecossistema anda na direção oposta: pip/npm/pnpm/uv estão restringindo execução de código no install, não abrindo (https://nesbitt.io/2026/06/05/install-script-allowlists.html). Logo o onboarding da Lohra é um pipeline de três tempos: **[a instalação é muda]** → **[o primeiro comando detecta o ambiente e guia]** (é o único ponto interativo possível, dentro do código Python da CLI, acionado por `lohra chat` / `lohra init`) → **[`lohra doctor` re-executável a qualquer momento]** (idempotente, diagnóstico acionável, no modelo `flutter doctor` / `brew doctor`). Hoje o tempo 2 é um erro de exit 2 (`backend/lohra/cli.py:139-145`) e o tempo 3 não existe.

---

## 2. Princípios

- **Detectar > perguntar > errar.** Toda pergunta ao usuário é uma detecção que não foi escrita. Só pergunte o que a máquina não pode responder sozinha.
- **Wizard só em TTY.** O gate já existe e é robusto (`_isatty`, `backend/lohra/cli.py:450-455` — trata wrappers que levantam em `isatty`). Sem terminal, nenhum prompt, nunca.
- **Headless nunca bloqueia.** Pipe, CI, `lohra serve`, `--json`, subagente: o comportamento é falhar rápido com mensagem didática ou seguir com default — jamais esperar input.
- **Default sensato em toda pergunta.** Enter sempre resolve algo publicável (modelo `aws configure`: 4 campos, todos puláveis). Nenhuma pergunta sem saída.
- **ToS/subscription SEMPRE opt-in explícito, nunca automático.** Decisão já tomada no projeto e implementada (`lohra auth enable` imprime `TOS_WARNING` e exige confirmação, com `--yes` só para automação — `backend/lohra/cli.py:735-755`). Onboarding pode *oferecer*, nunca *ativar*.
- **Erro continua ensinando o remédio.** O padrão já está estabelecido no repo e tem um exemplar: o warning de tier cita o arquivo exato e o efeito (`backend/lohra/workflow/strategies.py:115-120`). Todo erro novo copia essa forma; todo erro velho que não cita é bug de onboarding.
- **Primeiro sucesso em menos de 2 minutos.** Da instalação à primeira resposta do agente. É o critério que decide prioridade quando dois itens empatam.

---

## 3. Backlog

### ONB-1 — Mensagem de "no provider configured" cita TODOS os caminhos

- **Dor:** numa máquina virgem, `lohra chat "oi"` falha em `cli.py:139-145` com *"no provider configured — set an API key (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) or pass --provider."*. A mensagem omite os dois caminhos que **não exigem key nenhuma**: `lohra auth enable` (subscription OpenAI/Codex) e `--provider ollama` (único `requires_api_key=False`, `backend/lohra/providers/builtin.py:113-123`). Ollama local nunca é auto-detectado porque a detecção varre `env_vars=("OLLAMA_API_KEY",)` (`backend/lohra/providers/resolve.py:53-58`) — uma instância rodando em `localhost:11434` é invisível.
- **Escopo:** reescrever o bloco `name == "auto"` para listar os 3 caminhos (key / subscription / ollama local), cada um com o comando exato. Sem detecção nova, sem I/O — só texto. É a mudança mais barata do backlog inteiro e não depende de nada.
- **Aceite:** a mensagem de exit 2 contém literalmente `lohra auth enable`, `--provider ollama` e ao menos duas env vars de key; teste de CLI fixa o texto.
- **Prioridade:** P1 · **Dependências:** nenhuma.

### ONB-2 — Núcleo de detecção de ambiente (`lohra/onboarding/detect.py`)

- **Dor:** hoje não existe nenhuma detecção (`find -iname "*onboard*" -o -iname "*wizard*"` → vazio; greenfield). Cada consumidor futuro (wizard, `init`, `doctor`) reimplementaria a mesma varredura.
- **Escopo:** um módulo puro, sem prompt e sem escrita, que retorna um snapshot imutável: keys presentes por provider (`ProviderProfile.env_vars`), `~/.lohra/.env` existente, `~/.codex/auth.json` (ou `$CODEX_HOME`, override já respeitado em `backend/lohra/subscription/codex_creds.py:32-33`), `~/.lohra/auth.json`/`oauth.json` do profile ativo, daemon Ollama vivo (`GET http://localhost:11434/api/tags`, sem auth), harnesses no PATH, versão de Python, profile ativo. Timeouts curtos e falha-para-desconhecido em cada probe: detecção nunca pode travar nem levantar.
- **Aceite:** snapshot serializável; nenhum probe demora mais que ~1s no total; testes com env fake cobrindo máquina virgem, máquina com 2 keys, máquina só com Codex logado, máquina só com Ollama.
- **Prioridade:** P1 · **Dependências:** nenhuma. **É dependência de ONB-3, ONB-4, ONB-6, ONB-7, ONB-14.**

### ONB-3 — `lohra init` explícito

- **Dor:** não há comando para "me deixa pronto" (`backend/lohra/cli.py:32-115` — subcomandos existentes: chat, dashboard, serve, cron, workflow, profile, auth, skill, update). O usuário só descobre configuração por erro.
- **Escopo:** subcomando que roda o snapshot do ONB-2, imprime o que achou e faz no máximo 3 perguntas, todas puláveis com Enter (provider default, modelo default, exportar kit `use-lohra`?). Grava em `~/.lohra/.env` (chmod 600, mesmo caminho que a UI de Settings já usa via `config.rs`). Idempotente: rodar duas vezes não duplica nem sobrescreve valor existente sem confirmação. Com `--no-input` ou fora de TTY, vira relatório read-only (equivalente a `doctor`) e sai 0.
- **Aceite:** numa máquina virgem com TTY, `lohra init` seguido de Enter em tudo termina em estado utilizável ou imprime exatamente o que falta; rodar de novo é no-op; sem TTY não pergunta nada.
- **Prioridade:** P1 · **Dependências:** ONB-2.

### ONB-4 — Primeiro-run wizard disparado por `lohra chat` sem config

- **Dor:** `README.md:8-9` manda rodar `pip install lohra` → `lohra chat "olá"`, e isso falha sem key/subscription. A descoberta acontece na prática, não na leitura.
- **Escopo:** quando `_resolve_profile` chegaria no sentinel `"auto"` **e** stdin/stderr são TTY (`_isatty`, `cli.py:450`), em vez de sair 2, oferecer o fluxo do ONB-3 inline e, ao final, executar o prompt original sem o usuário ter que redigitar. Uma única pergunta de entrada ("configurar agora? [Y/n]") — "n" cai no erro do ONB-1. Nunca dispara se já há provider resolvido; nunca dispara duas vezes (marca `~/.lohra/.initialized`).
- **Aceite:** em TTY virgem, `lohra chat "oi"` termina com a resposta do agente sem nenhum comando intermediário; em TTY já configurado, zero mudança de comportamento (byte-idêntico).
- **Prioridade:** P1 · **Dependências:** ONB-1, ONB-2, ONB-3.

### ONB-5 — Contrato headless: `--no-input`, `LOHRA_NO_WIZARD`, e erro que aponta `lohra init`

- **Dor:** o runtime tem consumidores estruturalmente headless — `lohra chat --json` (envelope de orquestração: stdout é SEMPRE 1 JSON parseável), `lohra serve`, cron, subagentes. Um prompt vazando aí corrompe o contrato de saída e trava CI.
- **Escopo:** flag global `--no-input` + env `LOHRA_NO_WIZARD=1`, ambas forçando o caminho não-interativo independentemente de TTY. Sob `--json`, o wizard é proibido por construção e a falta de config vira `error_envelope`, não texto solto. O erro headless nomeia `lohra init` e `lohra doctor`.
- **Aceite:** `lohra chat --json "oi"` sem config emite exatamente um JSON válido em stdout e nada mais; `echo oi | lohra chat` não pergunta nada; `LOHRA_NO_WIZARD=1` em TTY virgem cai no erro do ONB-1.
- **Prioridade:** P1 · **Dependências:** ONB-4.

### ONB-6 — `lohra doctor` (diagnóstico acionável e re-executável)

- **Dor:** os arquivos de config que faltam falham em silêncio ou com erro que não nomeia o arquivo: `workflow_policy.json` ausente → deny-by-default total sem citar o caminho (`backend/lohra/workflow/sandbox.py:94-107,137-138,164`); `mcp.json` malformado → `logger.warning` que sai pelo lastResort handler do Python (não há `basicConfig`/`dictConfig` em lugar nenhum do backend) e se perde no meio do stream do chat; `.env` ausente é no-op silencioso; `auth.json` ausente cai silenciosamente no caminho pago.
- **Escopo:** subcomando idempotente, rodável a qualquer momento, no formato `flutter doctor`: uma linha por check com estado textual `ok` / `warn` / `fail` e, quando não-ok, **o comando exato** que corrige. Checks: versão de Python (`>=3.11,<3.14`, `backend/pyproject.toml:6`), provider resolvido (e **qual** e **por quê** — ver ONB-9), key válida (ONB-11), subscription do profile ativo, presença/validade de `.env`, `auth.json`, `oauth.json`, `mcp.json`, `cron/jobs.json`, `workflow_policy.json`, `workflow_tiers.json`, daemon Ollama, harnesses instalados. Exit 0 mesmo com `warn`; exit != 0 só com `fail`.
- **Aceite:** todo `fail` e todo `warn` imprime um comando copiável; nenhum check derruba o comando por exceção; roda sem gastar token; `--json` para consumo por script.
- **Prioridade:** P1 · **Dependências:** ONB-2.

### ONB-7 — Fallback keyless: Ollama local detectado vira sucesso, não pergunta

- **Dor:** o provider `ollama` existe e é keyless (`builtin.py:113-123`), mas é inalcançável por auto-detecção porque a varredura é por env var. O usuário só chega nele lendo a lista de providers.
- **Escopo:** replicar a filosofia zero-config do Ollama (`ollama run` conversa sem config nenhuma; `ollama launch` plugou outras ferramentas "sem variáveis de ambiente ou arquivos de config" — https://ollama.com/blog/launch). Quando não há key nem subscription **e** `GET localhost:11434/api/tags` responde, usar Ollama automaticamente, imprimindo uma linha em stderr dizendo qual provider/modelo foi escolhido e como fixar. Connection refused → caminho normal de erro. Nunca substitui uma escolha explícita.
- **Aceite:** com Ollama rodando, `lohra chat "oi"` numa máquina sem nenhuma key responde; a escolha é anunciada em stderr (nunca em stdout, para não sujar `--json`); sem Ollama, comportamento inalterado.
- **Prioridade:** P1 · **Dependências:** ONB-2.

### ONB-8 — `lohra auth login` absorve o enable: um comando, uma intenção

- **Dor:** hoje o fluxo é `enable` (aceite de ToS) e DEPOIS `login` — dois comandos para uma intenção só. Quem digita `login` já declarou que quer a subscription; esbarrar em "antes rode enable" é burocracia na frente da intenção (`cli.py:749-753` imprime os dois comandos alternativos e some). Direção dada pelo dono revisando este backlog: *"se o usuário digitou auth login ele já está ciente de que quer usar subscription — por que não fazer isso no próprio login? Seria um comando só."*
- **Escopo:** `lohra auth login` num store sem opt-in imprime o `TOS_WARNING` e pede a confirmação **inline** (o aceite acontece dentro do login — princípio do opt-in explícito preservado, só muda o momento), grava o acknowledgment e segue direto para o device flow. Fallback sem browser (SSH/container): imprimir URL + código para copiar, modelo `gh auth login`/`claude`. `lohra auth enable` continua existindo para o caminho reuse-do-Codex (não tem login) e para automação (`--yes`).
- **Aceite:** máquina virgem + TTY: `lohra auth login`, um "y" no aviso de ToS, e o usuário termina logado — um comando. `enable` avulso segue funcionando (reuse/automação); `login --yes` pula só a confirmação, nunca o aviso impresso.
- **Prioridade:** P2 · **Dependências:** ONB-2.

### ONB-9 — Transparência de escolha: qual provider foi usado, e o footgun de custo do profile

- **Dor real (custo):** subscription é opt-in **por store** — um profile novo não herda o `auth.json` do home base, então `--profile work` volta a faturar API key paga em silêncio. Dor vivida pelo dono, hoje mitigada só na skill de delegação (`docs/skills/use-lohra/SKILL.md:26-31`), nem no `lohra auth` nem no CLI (`docs/STANDALONE.md:28`).
- **Dor (ambiguidade):** com `ANTHROPIC_API_KEY` e `OPENAI_API_KEY` setadas, Anthropic vence por ser o primeiro em `BUILTIN_PROFILES` (`builtin.py:125`) e nada informa a escolha nem o motivo (`resolve.py:53-58`).
- **Escopo:** (a) uma linha em stderr no primeiro turno de cada sessão dizendo provider + modelo + origem da escolha (`--provider` / config / env / auto-detecção / subscription); (b) ao rodar sob um profile cujo store não tem subscription **enquanto o home base tem**, avisar explicitamente que aquele profile vai gastar API key paga, com o comando `lohra --profile <nome> auth enable`; (c) `lohra profile create` sugere o mesmo no final.
- **Aceite:** o aviso de custo aparece exatamente uma vez por sessão e só quando há divergência real entre stores; nada disso escapa para stdout sob `--json`.
- **Prioridade:** P1 · **Dependências:** ONB-2.

### ONB-10 — Detecção de modelos por provider (o campo declarado que ninguém consome)

- **Dor:** `models_url` é campo de `ProviderProfile` (`backend/lohra/providers/base.py:30`) populado em **3 de 8** providers (anthropic, openai, openrouter — `builtin.py:21,37,56`) e `grep -rn "models_url"` no backend retorna **zero consumidores**: é metadado morto. Todo mundo cai em `fallback_models` hardcoded, que envelhece em silêncio — a mesma classe de problema do WF-5 (template gravava `model: <slug>` literal, não portável entre providers).
- **Escopo:** popular `models_url` nos 8 e escrever o consumidor (`lohra models list [--provider]`, consumido por `init`/`doctor`). Matriz verificada: anthropic `GET /v1/models` (`x-api-key` + `anthropic-version`); openai `GET /v1/models` (Bearer); openrouter `GET /api/v1/models` (Bearer exigido pela referência oficial — **não** tratar 401 aqui como prova de key ruim sem teste ao vivo); deepseek `GET https://api.deepseek.com/models` (sem `/v1`); groq `GET /openai/v1/models`; together `GET /v1/models` (**quirk: resposta historicamente é array JSON puro, não o envelope `{"object":"list","data":[…]}`** — parser OpenAI ingênuo quebra); gemini nativo `GET https://generativelanguage.googleapis.com/v1beta/models?key=…` (confiança alta; o espelho OpenAI-compat `/v1beta/openai/models` **não foi confirmado em doc** — tratar como não-verificado); ollama `GET /api/tags`.
- **Aceite:** os 8 providers listam modelos com key válida; together e gemini têm teste de shape próprio; falha de rede degrada para `fallback_models` com um `warn`, nunca com exceção; nenhum caminho de chat passa a depender da rede para começar.
- **Prioridade:** P3 · **Dependências:** ONB-2. Bloqueia parcialmente ONB-11.

### ONB-11 — Validar key sem gastar token

- **Dor:** hoje a única prova de que uma key funciona é uma conversa real — o usuário descobre key errada/expirada gastando um turno e lendo erro de provider.
- **Escopo:** reusar o `GET .../models` do ONB-10 como validação barata (401/403 = key ruim, 200 = ok) e o `GET /api/tags` do Ollama como liveness do daemon. Consumido por `doctor` e pelo passo final de `init`. Ressalva registrada: para openrouter, não inferir "key ruim" de um 401 antes de validar ao vivo.
- **Aceite:** `lohra doctor` distingue "key ausente", "key presente mas rejeitada" e "key ok" para cada provider configurado, sem consumir tokens de chat.
- **Prioridade:** P2 · **Dependências:** ONB-10.

### ONB-12 — `workflow_policy.json`: o erro do sandbox nomeia o arquivo e oferece criá-lo

- **Dor real:** sem o arquivo, `load_policy` devolve `WorkflowPolicy()` vazia e o sandbox é **deny-by-default total** — nenhum `read_file`/`write_file` fora do working_root, nenhum `web_fetch`. O leaf falha com *"path is outside the workflow working scope (sandbox denied)"* / *"host is not in the workflow egress allowlist (sandbox denied)"* (`backend/lohra/workflow/sandbox.py:94-107,137-138,164`), e **nenhuma dessas mensagens cita o nome do arquivo, o caminho `~/.lohra/workflow_policy.json` nem a forma esperada** (`fs_allow` / `egress_allow`). Foi assim que se descobriu o WF-21, e é o que a prova de fogo no Windows manda fazer na mão (`docs/STANDALONE.md:66`).
- **Escopo:** copiar o exemplar de tier (`strategies.py:115-120`): a denial string passa a citar `~/.lohra/workflow_policy.json` e o campo relevante (`fs_allow` vs `egress_allow`), incluindo o caso read-only que já tem frase própria. Em TTY, o primeiro `run_workflow` que bate numa negação **oferece** gerar o arquivo com um template comentado (working_root do projeto como `fs_allow`, `egress_allow` vazio). A oferta nunca é automática — política é decisão do operador, não do spec.
- **Aceite:** as duas denial strings contêm o caminho do arquivo e o nome do campo; o template gerado é JSON válido que `load_policy` lê sem warning; headless nunca oferece nada e o deny-by-default permanece intacto.
- **Prioridade:** P2 · **Dependências:** ONB-6.

### ONB-13 — `workflow_tiers.json`: template gerado a partir do que já se sabe

- **Dor:** o warning de tier já é o padrão-ouro do repo — cita o caminho exato e o efeito, e foi chamado de "PASSOU PERFEITO" no dogfood via Codex (`docs/history/reviews/2026-08-26-dogfood-codex.md:27-30`). O que falta não é a mensagem, é o arquivo: o usuário lê o warning correto e ainda tem que inventar a estrutura do zero.
- **Escopo:** `lohra init`/`doctor` oferecem gerar `~/.lohra/workflow_tiers.json` com tiers convencionais (`fast` / `balanced` / `deep`) já mapeados para modelos do provider **resolvido no momento** (não slugs hardcoded — foi exatamente o WF-5), com comentários explicando `model`/`effort`/`provider` e a precedência (explícito no nó vence o tier).
- **Aceite:** rodar um workflow com `tier: fast` logo após o scaffold não gera fault de tier; trocar de provider e regerar produz mapeamento coerente; o arquivo é legível sem consultar código.
- **Prioridade:** P2 · **Dependências:** ONB-3, ONB-10.

### ONB-14 — Detecção de harnesses + oferta do kit `use-lohra` no diretório certo

- **Dor:** a Lohra é feita para ser orquestrada por outro agente (envelope `--json`, skill `docs/skills/use-lohra/`), mas instalar o kit é manual e o usuário precisa saber que o comando existe e para onde apontar (`lohra skill export use-lohra --to <dest>`, `backend/lohra/cli.py:109-111`, `backend/lohra/skills/exportkit.py:34`, uso em `docs/STANDALONE.md:73`).
- **Escopo:** detectar `claude` e `codex` no PATH (`shutil.which`) e os homes (`~/.claude`, `$CODEX_HOME` ou `~/.codex`) e, em TTY, oferecer exportar o kit para o diretório correto de cada um (`<projeto>/.claude/skills`, `<projeto>/.codex/skills`). Detectar kit já instalado (não reescrever sem confirmação). Nenhuma escrita fora do que o usuário aprovar.
- **Aceite:** numa máquina com os dois harnesses, uma resposta afirmativa instala o kit nos dois caminhos e o harness alvo consegue lê-lo; com o kit já presente, o passo vira no-op anunciado.
- **Prioridade:** P2 · **Dependências:** ONB-2, ONB-3.

### ONB-15 — Gap: `discover_skill_roots` não conhece `.codex/skills`

- **Dor:** o scanner de skills de projeto só olha `(".claude/skills", ".lohra/skills")` (`backend/lohra/project/discover.py:92`). Um projeto que só tem `.codex/skills/` — como esta própria branch, onde o kit foi instalado ali — é invisível para a Lohra, ainda que a convenção seja real e a Lohra já respeite `$CODEX_HOME` em outro subsistema (`backend/lohra/subscription/codex_creds.py:32-33`).
- **Escopo:** decidir e registrar a política (ler `.codex/skills` também, ou documentar explicitamente que não lê e por quê). Se ler: adicionar ao tuple mantendo a precedência atual (projeto sobre home) e a regra de create/delete home-only — delete nunca toca repo do usuário.
- **Aceite:** ou existe teste cobrindo `.codex/skills` como raiz de projeto, ou existe uma linha em `docs/` dizendo que não é lida e qual é o remédio (exportar para `.claude/skills`). Nenhum caminho de escrita novo em diretório de outro harness.
- **Prioridade:** P3 · **Dependências:** ONB-14.

### ONB-16 — Guia de profiles (isolamento, o que herda, o que não herda)

- **Dor:** `LOHRA_PROFILE` re-rooteia **todo** o estado sob `~/.lohra/profiles/<nome>/` — memória, skills, sessões, cron, mcp.json, imagens — mas duas exceções mordem: (a) `.env` é lido **só** de `lohra_base()`, nunca de `lohra_home()`, então um `.env` dentro de um profile é ignorado (intencional e comentado no código, `backend/lohra/config/env_file.py:43-48`, `backend/lohra/cli.py:1062-1068`, mas invisível para quem não lê o fonte); (b) subscription é por-store e não herda (ONB-9). Some-se a isso que `--profile` precisava vir antes do subcomando até ser corrigido — correção documentada só num comentário de código (`backend/lohra/cli.py:22-24`, commit `6589beb`), sem changelog visível.
- **Escopo:** uma página curta: o que um profile isola, o que ele deliberadamente **não** isola (`.env` global por design), o que não herda (subscription, e o custo disso), e as duas formas equivalentes de selecionar (`LOHRA_PROFILE` e `--profile` em qualquer posição). `lohra profile list` passa a marcar quais stores têm subscription ativa.
- **Aceite:** a página responde "por que meu `.env` do profile não fez efeito?" e "por que esse profile está cobrando?" sem abrir código; `profile list` mostra o estado de subscription por profile.
- **Prioridade:** P3 · **Dependências:** ONB-9.

### ONB-17 — Notas Windows e preflight de Python

- **Dor:** `requires-python = ">=3.11,<3.14"` (`backend/pyproject.toml:6`) e o `python3` default do macOS costuma ser mais velho; o aviso existe só em `docs/STANDALONE.md:7,41`, não no README nem numa mensagem da Lohra — o usuário descobre pelo erro cru do pip ("Package requires a different Python"). No Windows, chmod 600 vira no-op (permissão restrita não aplicada, ciente e documentado em `docs/STANDALONE.md:38-39`) e leaves de workflow não enxergam disco sem `workflow_policy.json` escrito à mão (`docs/STANDALONE.md:66`). `pip install lohra` + `lohra chat` já funcionaram lá na prova de fogo de 2026-08-26 (`docs/STANDALONE.md:76-77`) — o problema é o resto do caminho.
- **Escopo:** checks específicos no `doctor` (versão de Python com o range exato e o comando de correção; aviso de que os arquivos de credencial ficam sem permissão restrita no Windows; ponteiro para o scaffold do ONB-12). README ganha a linha de versão suportada junto do `pip install`.
- **Aceite:** `lohra doctor` no Windows produz a mesma quantidade de linhas acionáveis que no macOS, sem `fail` espúrio por chmod; README cita o range de Python na mesma tela do comando de instalação.
- **Prioridade:** P3 · **Dependências:** ONB-6.

### ONB-18 — Cronometrar o primeiro sucesso (o critério vira teste)

- **Dor:** "menos de 2 minutos" é princípio sem medição — e o caminho feliz atual (`README.md:8-9`) não foi cronometrado em máquina virgem depois de nenhuma das mudanças acima.
- **Escopo:** um roteiro reprodutível de máquina limpa (container ou VM) para os quatro caminhos de entrada: key de API, subscription, Ollama local, e "nada configurado". Registrar tempo e número de comandos digitados. Vira o gate de aceite da campanha de onboarding.
- **Aceite:** os quatro caminhos chegam a uma resposta do agente; nenhum exige mais de 3 comandos; o resultado fica registrado em `docs/history/`.
- **Prioridade:** P2 · **Dependências:** ONB-4, ONB-6, ONB-7, ONB-8.

---

## 4. Fora de escopo por ora (nomeado, não esquecido)

- **Auto-aceite de ToS.** Subscription é ToS-gray e a decisão do projeto está travada: opt-in explícito, com aviso e confirmação (`lohra auth enable`). Nenhum item deste backlog pode ativar subscription sem o usuário dizer sim; `--yes` existe só para automação de quem já decidiu.
- **Telemetria.** Nenhuma coleta, nenhum ping, nenhum "ajude-nos a melhorar". O sinal de onboarding vem do dogfood e do roteiro cronometrado do ONB-18, não da máquina do usuário.
- **Instalador gráfico.** O runtime é `pip install lohra`; o app desktop está fora do repo e o packaging macOS já tem caminho próprio. Onboarding aqui é CLI-only.
