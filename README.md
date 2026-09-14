# Lohra

**Um agente de IA com memória persistente, skills e workflows de múltiplos agentes.** Use pelo terminal ou delegue trabalho a ele a partir do Claude Code, Codex e scripts. O runtime é Python e funciona sem interface gráfica.

- **Trabalho no projeto:** lê instruções, inspeciona arquivos, executa ferramentas e mantém sessões que você pode continuar.
- **Memória e skills:** guarda conhecimento e procedimentos reutilizáveis, com estado separado por profile.
- **Workflows:** combina tarefas paralelas, revisão, síntese e checkpoints humanos em um fluxo declarativo, com orçamento de tokens, progresso e retomada.
- **Escolha de modelos:** usa diferentes providers e permite escolher modelo e esforço por subagente ou etapa compatível do workflow.

[Começar](#começar) · [Claude Code e Codex](#usar-no-claude-code-ou-codex) · [Modelos](#escolher-providers-modelos-e-esforço) · [Subagentes](#quando-usar-subagentes-ou-workflows) · [Workflows](#executar-e-acompanhar-workflows) · [Instruções prontas](#instruções-para-seu-projeto) · [Desenvolvimento](#desenvolvimento)

## Começar

Requer **Python 3.11–3.13**. Instale em um ambiente virtual ou use seu gerenciador de ferramentas Python:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -U lohra
lohra --version
```

No Windows, use `py -3.13 -m venv .venv` e `.venv\Scripts\Activate.ps1` no PowerShell. Detalhes em [Standalone](docs/STANDALONE.md).

Na raiz do projeto em que quer trabalhar:

```bash
lohra init --profile lohra-meu-projeto
lohra doctor --profile lohra-meu-projeto
lohra chat --profile lohra-meu-projeto \
  "Leia as instruções do projeto e explique sua estrutura. Não altere arquivos."
```

`init` orienta a configuração em um terminal interativo. `doctor` diagnostica o ambiente sem executar uma conversa com um modelo e indica comandos para corrigir problemas. Cada `chat` executa um turno; para continuar, use o `session_id` retornado com `--session`.

Escolha um nome de profile por projeto e use **o mesmo profile** em autenticação, chat e observação dos workflows. Memória, skills, sessões e políticas ficam em `<base>/profiles/<nome>/`; sem profile, ficam na base. A base padrão é `~/.lohra` no macOS/Linux e `%LOCALAPPDATA%\lohra` no Windows; `LOHRA_HOME` permite escolher outra. Os exemplos de caminhos abaixo usam a base macOS/Linux.

A habilitação e a preferência de autenticação são por profile, mas o `.env` de chaves e defaults é compartilhado em `<base>/.env`. Variáveis do ambiente têm precedência sobre esse arquivo. Profile não cria um checkout nem restringe o acesso aos arquivos do projeto.

### Configurar uma API key

O `init` pode ajudar com a configuração. Para configurar pelo shell, por exemplo com OpenAI:

```bash
export OPENAI_API_KEY="sua-chave"
lohra auth prefer api_key --profile lohra-meu-projeto
lohra models --profile lohra-meu-projeto --provider openai
lohra chat --profile lohra-meu-projeto --provider openai \
  --model "<id-do-modelo-do-catálogo>" "Explique este projeto."
```

Substitua a chave e o ID pelos seus valores. No PowerShell, use `$env:OPENAI_API_KEY = "sua-chave"`. Também é possível guardar chaves em `~/.lohra/.env`. Chamadas autenticadas com API key são cobradas pelo provider.

Providers disponíveis incluem `anthropic`, `openai`, `openrouter`, `deepseek`, `groq`, `together`, `gemini`, `xai`, `glm`, `kimi` e `ollama`. O Ollama usa seu servidor local e não exige API key. `lohra models --profile lohra-meu-projeto` lista o catálogo e os providers configurados: não faz inferência, mas pode consultar os serviços dos providers.

### Usar a assinatura ChatGPT/Codex

A integração de assinatura da Lohra é **opcional e de terceiros**. Leia o aviso apresentado por `lohra auth` antes de habilitar; exportar uma skill para Codex ou Claude Code não habilita essa autenticação.

```bash
lohra auth login --profile lohra-meu-projeto
lohra auth prefer subscription --profile lohra-meu-projeto
lohra auth status --profile lohra-meu-projeto
```

O login próprio pede consentimento, usa um código no navegador e permite renovação de credenciais pela Lohra. Como alternativa ao `auth login`, se já houver um login disponível no arquivo de autenticação do Codex CLI, use `lohra auth enable --profile lohra-meu-projeto`; nesse caminho, a Lohra não renova as credenciais do Codex.

**Um profile novo não herda a habilitação de assinatura do home compartilhado.** Configure-o antes de delegar. A preferência `subscription` exige essa rota; `api_key` seleciona a rota de API; `auto` permite a seleção automática. Assim você evita depender de uma API key encontrada no ambiente quando pretendia usar assinatura.

Na main ainda não publicada no PyPI, processos longos verificam o login antes de cada requisição: o login próprio pode ser renovado; o login do Codex é apenas relido. `auth disable` impede novas requisições de assinatura desse profile. `auth logout` remove somente o login próprio, permitindo voltar ao login do Codex se ele estiver disponível. Streams já abertos mantêm as credenciais com que começaram. Detalhes de recuperação em [Standalone](docs/STANDALONE.md#autenticação-em-processos-longos).

## Usar no Claude Code ou Codex

A skill **`use-lohra`** vem no pacote. Ela ensina seu agente a formular tarefas, chamar `lohra chat --json`, continuar sessões e verificar os resultados. A execução acontece na Lohra pelo CLI; não é necessário iniciar `lohra serve` nem configurar um servidor MCP para essa integração.

### Exportar a skill

Na raiz do projeto, escolha o comando do seu agente:

```bash
# Claude Code: skill deste projeto
lohra skill export use-lohra --to .claude/skills

# Codex: skill deste projeto
lohra skill export use-lohra --to .agents/skills
```

O destino é a **pasta que contém as skills**. A Lohra cria `<destino>/use-lohra/SKILL.md`; não acrescente `use-lohra` ao `--to`. Para instalar para todos os projetos do seu usuário:

```bash
lohra skill export use-lohra --to ~/.claude/skills
lohra skill export use-lohra --to ~/.agents/skills
```

Sem `--to`, `lohra skill export use-lohra` imprime o conteúdo no terminal. Exportar novamente sobrescreve o `SKILL.md` existente: preserve suas personalizações antes de atualizar. Esses são os diretórios documentados pelo [Claude Code](https://code.claude.com/docs/en/skills) e pelo [Codex](https://learn.chatgpt.com/docs/build-skills).

`skill export` exporta **kits empacotados**, atualmente `use-lohra`. Não exporta automaticamente a memória, as sessões ou todas as skills que a Lohra aprendeu. A skill interna `workflow-authoring` já acompanha a instalação e é lida pela própria Lohra ao criar workflows.

### Pedir trabalho ao agente

Garanta que o terminal usado pelo Claude Code ou Codex encontre `lohra --version` e a configuração do profile. Se instalou em um ambiente virtual, inicie o agente a partir de um shell com esse ambiente ativado. Confira a skill no seletor; se ela não aparecer, reinicie a sessão do agente.

No **Claude Code**, envie:

```text
/use-lohra Analise este repositório usando o profile lohra-meu-projeto.
Identifique os três maiores riscos na recuperação de erros, cite arquivos
e testes relevantes e proponha como validar cada hipótese. Não edite arquivos.
```

No **Codex CLI ou extensão**, envie:

```text
$use-lohra Analise este repositório usando o profile lohra-meu-projeto.
Identifique os três maiores riscos na recuperação de erros, cite arquivos
e testes relevantes e proponha como validar cada hipótese. Não edite arquivos.
```

São mensagens para o agente, não comandos de shell. Você também pode pedir em linguagem natural: “Use Lohra para investigar esta falha e validar a causa com os testes do projeto”. O agente pode selecionar a skill pela descrição; a invocação explícita torna a intenção clara.

### Usar diretamente em scripts

Na segunda chamada, substitua `<session_id>` pelo valor do campo `session_id` em `resultado.json`.

```bash
lohra chat --profile lohra-meu-projeto --json \
  --token-budget-cap 30000 \
  "Inspecione este projeto e explique como os erros são tratados. Não edite arquivos." \
  > resultado.json

lohra chat --profile lohra-meu-projeto --json \
  --session "<session_id>" \
  --token-budget-cap 30000 \
  "Aprofunde a segunda hipótese e indique a evidência que a confirmaria."
```

O stdout de `--json` contém o envelope estruturado; progresso e diagnósticos vão para stderr. Para considerar o turno bem-sucedido, exija código de saída `0`, `error: null` e uma entrega que atenda ao pedido; confira `output` e `tool_calls`. Uma resposta sem uso de ferramentas não comprova inspeção do repositório. `usage_total` agrega as chamadas do turno; `usage` representa apenas a última. Quando presentes, examine também `cost` e `workflows`: uma resposta de chat bem-sucedida pode conter um workflow pausado ou cancelado.

`--json` e `--no-input` não pedem aprovação pelo stdin. Comandos classificados como perigosos são negados, salvo quando você passa `--yolo` para autorizar essa invocação. A escolha interativa `session` vale para o comando exato durante a invocação viva; outro `lohra chat`, mesmo com o mesmo `--session`, começa sem essas aprovações. O gateway sem callback próprio e os subagentes mantêm suas recusas. Isso não torna as demais ferramentas somente leitura. `--no-tools` desativa as ferramentas e serve para respostas conceituais, sem inspeção do projeto.

O isolamento de aprovações entre consumidores está na main e ainda não faz parte do wheel 0.0.27. Para integrar a Lohra em Python, vincule explicitamente um `ApprovalManager` ao dispatch; configurar o objeto legado `approval` sozinho não autoriza o terminal. Veja o [contrato de aprovação](docs/specs/02-tool-system.md).

## Escolher providers, modelos e esforço

Consulte o catálogo do profile e escolha IDs disponíveis na sua rota:

```bash
lohra models --profile lohra-meu-projeto
lohra models --profile lohra-meu-projeto --json
lohra auth status --profile lohra-meu-projeto
```

| Onde escolher | Como configurar | Efeito |
| --- | --- | --- |
| Agente principal da Lohra | `lohra chat --provider <provider> --model <id>` | Modelo desta invocação; não troca o modelo do Claude Code/Codex que a chamou. |
| Subagente comum | Campos `provider`, `model`, `effort` em `delegate_task` ou `spawn_session` | Escolha feita pela Lohra ao delegar a tarefa. |
| Nó compatível do workflow | Campos `provider`, `model`, `effort` ou `tier` na spec | Roteamento do trabalho daquele nó. |
| Mapa reutilizável de modelos | `lohra tiers suggest` / `lohra tiers list` | Mapeia `small`, `medium` e `big` no `workflow_tiers.json` do profile. |

No chat pela rota de API, a precedência é `--model` → `LOHRA_MODEL` → default do provider. Pela assinatura, é `--model` → modelo no `config.toml` do Codex CLI → fallback da Lohra; `LOHRA_MODEL` não substitui essa escolha. Use `--model` para fixar a escolha independentemente desses defaults.

Com preferência de autenticação `auto`, um `--provider` explícito vale para aquela chamada e pode selecionar uma API key mesmo com assinatura habilitada. Uma preferência salva como `subscription` ou `api_key` tem precedência sobre a flag. Confira a rota antes de executar.

Não existe uma flag `lohra chat --effort`. `effort` é um campo de delegação e dos nós compatíveis; sua aceitação depende do modelo/provider. Os transports Responses e Chat Completions encaminham esse campo, enquanto o transport Anthropic não o aplica.

Para configurar tiers:

```bash
lohra tiers suggest --profile lohra-meu-projeto
lohra tiers list --profile lohra-meu-projeto
```

`suggest` propõe um mapa a partir do catálogo e pede confirmação antes de gravar. Revise a escolha conforme seu acesso e orçamento. Um tier sem mapeamento usa o modelo padrão do run e registra a situação como degradação; configure-o antes de usar em templates. O mesmo mapa é a única fonte que a Lohra consulta para substituir um `model` inexistente (veja [pausas](#entender-pausas-e-retomar)).

Exemplo de instrução para a Lohra:

```text
Consulte list_models antes de delegar. Use o tier small configurado para
extração, medium para a análise e big para a revisão final. Use nós agent
separados para esses papéis. Na revisão, defina effort high se o modelo
suportar esse parâmetro. Informe os modelos e providers resolvidos.
Não troque a rota de cobrança nem aumente o orçamento autorizado.
```

Os nós `agent`, `verify`, `judge_panel`, `loop_until_dry`, `gate` e `completeness_check` aceitam roteamento. Nos nós de revisão, ele vale para todos os agentes internos daquele nó. `parallel` e os stages de `pipeline` não aceitam modelos diferentes por branch/stage: use nós `agent` separados quando precisar dessa escolha. Ao mudar de provider, use um modelo do provider de destino; as credenciais também precisam estar configuradas.

## Quando usar subagentes ou workflows

| Situação | Use | Exemplo |
| --- | --- | --- |
| Uma pergunta ou alteração que cabe em poucas chamadas de ferramenta | Chat direto | Explicar uma função ou corrigir um teste. |
| Uma tarefa independente, com escopo e entrega claros | Subagente com `delegate_task` | Investigar uma hipótese e devolver evidências ao agente principal. |
| Trabalho paralelo que precisa receber novas instruções | `spawn_session`, `steer_session`, `collect_session` | Iniciar investigações, enviar contexto adicional e recolher resultados. |
| Várias etapas, dependências, revisão ou necessidade de retomada | Workflow com `run_workflow` | Extrair achados, verificar os relevantes e produzir uma síntese. |

As ferramentas acima são chamadas **pela Lohra**; você pode pedir o resultado em linguagem natural. Por exemplo:

```bash
lohra chat --profile lohra-meu-projeto --max-parallel 2 \
  "Delegue duas análises independentes: uma sobre tratamento de erros e outra \
  sobre cobertura de testes. Dê escopo e critérios de conclusão a cada subagente, \
  peça referências ao código e consolide os resultados. Não altere arquivos."
```

Subagentes comuns recebem contexto próprio: não herdam a conversa, memória, skills ou instruções de projeto do pai. O agente principal deve passar o contexto necessário na tarefa. Eles podem usar ferramentas de trabalho e manter seu próprio histórico; a Lohra pode continuar uma delegação com `resume_id`. Não criam automaticamente worktrees isoladas — para edições paralelas, separe diretórios ou checkouts.

`--max-parallel` limita as sub-sessões da orquestração comum (padrão 4; também configurável por `LOHRA_MAX_PARALLEL`). Workflows têm seu próprio pool; essa flag não controla a concorrência dos leaves de workflow. Use workflows quando as dependências, validação ou retomada justificarem a estrutura adicional.

Se `steer_session` informar que o turno já encerrou, aguarde `collect_session` com `wait: true` antes de enviar uma nova instrução. Aceite do steer não comprova conclusão da tarefa: um cancelamento pode impedir seu consumo ou a continuação. A correção dessa janela de encerramento está na main, ainda fora do wheel 0.0.27; veja o [contrato de orquestração](docs/specs/06-orchestration.md).

## Executar e acompanhar workflows

Peça que a Lohra leia a skill interna `workflow-authoring`, consulte `workflow_templates` para encontrar um fluxo reutilizável e chame `run_workflow` com a spec e os dados da tarefa. A CLI `lohra workflow` serve para **observar** runs; não existe um comando `lohra workflow run`.

Um primeiro exemplo, sem depender de arquivos ou rede nos subagentes:

```bash
lohra chat --profile lohra-meu-projeto --json \
  --token-budget-cap 30000 \
  "Leia workflow-authoring e execute um único workflow pequeno: duas análises \
  independentes sobre os prós e contras de uma fila em memória para jobs de \
  desenvolvimento, seguidas de uma síntese. Use três nós agent com tool_less: true \
  e apenas o contexto desta tarefa. Defina token_budget de 30000, acompanhe até \
  terminar ou pausar e reporte run_id, status, limitações e tokens consumidos." \
  > workflow.json
```

O valor de 30.000 é um exemplo de teto, não uma estimativa ou garantia de conclusão. `--token-budget-cap` impõe o limite autorizado **por run** e restringe o `token_budget` solicitado pela Lohra, inclusive na retomada. Não limita todo o turno nem subagentes comuns: vários runs têm orçamentos separados, e o agente principal também consome tokens. O controle impede novos spawns; chamadas em andamento podem ultrapassar o teto.

Em outro terminal, observe o mesmo profile sem fazer novas chamadas a um LLM:

```bash
lohra workflow --profile lohra-meu-projeto list
lohra workflow --profile lohra-meu-projeto watch --last
lohra workflow --profile lohra-meu-projeto audit "<run_id>"
```

Coloque `--profile` **antes** de `list`, `watch` ou `audit`. Outra opção é definir `LOHRA_PROFILE` no ambiente. `watch` acompanha a execução; não a mantém viva. Se o processo de chat sair com trabalho ainda em andamento, esse trabalho pode ser cancelado (`cancelled_on_exit` no envelope). Estado durável permite retomada, não significa execução automática em segundo plano após o CLI sair.

### Escolher as etapas

| Necessidade | Nó |
| --- | --- |
| Uma tarefa com saída em texto ou JSON validado | `agent` |
| Tarefas independentes, seguidas de uma consolidação | `parallel` |
| Vários itens percorrendo as mesmas etapas | `pipeline` |
| Busca iterativa até parar de encontrar resultados | `loop_until_dry` |
| Tentar refutar um achado | `verify` |
| Comparar tentativas e sintetizar a melhor | `judge_panel` |
| Revisar e refazer até atender um critério | `gate` |
| Conferir lacunas de cobertura | `completeness_check` |
| Aguardar uma decisão humana | `checkpoint` |
| Reusar um template salvo | `workflow` |

Comece com poucas etapas. Peça schemas para resultados consumidos por outros nós e critérios claros de conclusão. A [skill de autoria](backend/lohra/skills/builtin/workflow-authoring/SKILL.md) contém specs JSON completas, campos aceitos e exemplos de composição.

### Dar acesso ao projeto

Este README acompanha a main. Recursos listados em [Não publicado](backend/CHANGELOG.md#não-publicado) exigem a instalação a partir do código até a próxima versão PyPI.

Leaves de workflow têm uma política de ferramentas própria e um diretório de trabalho gravável por run, separado do checkout. Acesso a outros diretórios exige permissão. Para permitir leitura do seu repositório, configure o arquivo **do profile usado no run**, `~/.lohra/profiles/lohra-meu-projeto/workflow_policy.json`. Exemplo, substituindo o caminho absoluto:

```json
{
  "fs_allow": [{"path": "/caminho/absoluto/do/projeto", "mode": "ro"}],
  "egress_allow": [],
  "allow_search": false,
  "allow_terminal": false,
  "mcp_allow": []
}
```

`ro` permite leitura; `rw` também permite escrita. `egress_allow` limita os hosts de `web_fetch`, inclusive cada destino de redirect. `web_search` exige `allow_search: true` do operador, pois usa um backend de busca externo e não é limitado à lista de hosts do fetch. Terminal e MCP também exigem habilitação própria; liberar arquivos não libera comandos. A spec não amplia essa política. Quando o turno autor ingere conteúdo web/MCP, a restrição de taint pode retirar essas capacidades dos leaves. Para um fluxo sem acesso adicional, o agente principal pode reunir a evidência e passá-la em `args`/prompts. Esses controles de workflow não são uma sandbox geral do chat ou dos subagentes comuns.

O `web_fetch` conecta somente a IPs públicos validados em cada nova conexão, preservando o hostname e a verificação TLS. Se a configuração de ambiente ou do sistema selecionar um proxy, o fetch recusa a chamada: configure uma rota direta autorizada, por exemplo com `NO_PROXY` para o host pretendido. `SSL_CERT_FILE` e `SSL_CERT_DIR` continuam disponíveis para certificados de confiança. Veja o [contrato de fetch e seus limites](docs/specs/10-web-fetch.md). Essa correção está na main e ainda não faz parte do pacote 0.0.27 publicado.

### Entender pausas e retomar

Leia o status e os faults: `complete`, `degraded`, `failed`, `cancelled` e `paused` são resultados diferentes. Confira também as saídas e os critérios da tarefa; um status isolado não comprova qualidade.

- **Checkpoint:** responda à pergunta humana no mesmo run, copiando `checkpoint.answer_address` do status. Esse array identifica a pergunta; `node_id` é o rótulo de exibição.
- **Orçamento esgotado:** decida se autoriza um `token_budget` maior, suficiente para o próximo trabalho. Se o cap do operador também impedir esse aumento, relance o chat com um `--token-budget-cap` maior; se ainda houver margem no cap atual, basta aumentar o orçamento do run.
- **Rota indisponível:** corrija a autenticação ou escolha uma rota autorizada; aumentar tokens não resolve esse erro. Um `model` que não existe no provider é um caso à parte: com um mapa de tiers configurado, a Lohra executa aquele nó uma única vez no modelo mapeado para o tier (mesmo provider, nunca assinatura) e registra um aviso e `meta.model_substitutions`; sem mapa, o run pausa após um leaf para você corrigir o slug.
- **Quota temporária:** o runtime pode pausar e tentar novamente com backoff limitado.

Se o checkpoint definiu `go` como aceite e você decidiu aprovar, o comando abaixo envia sua resposta. O agente deve repassá-la literalmente, sem decidir por você:

```bash
lohra chat --profile lohra-meu-projeto --json \
  --session "<session_id>" --token-budget-cap 30000 \
  'Minha resposta ao checkpoint de endereço <answer_address> é go. Retome o run <run_id> usando
  resume_run_id e checkpoint_answers como lista de objetos address/answer, copiando
  o endereço e essa resposta literalmente. Preserve a spec e o orçamento; acompanhe até terminar ou pausar.'
```

No prompt acima, substitua `<answer_address>` pelo array completo do status, por exemplo `["revisao", "aprovar"]` para um checkpoint aninhado. A chamada resultante usa `checkpoint_answers: [{"address": ["revisao", "aprovar"], "answer": "go"}]`; um checkpoint raiz tem apenas um elemento no endereço. Mapas textuais legados continuam aceitos quando inequívocos.

Retome com o **mesmo `run_id`** para reusar células concluídas e ainda válidas. Trabalho incompleto ou invalidado volta a executar e pode consumir tokens. Ao adaptar a spec, peça que a Lohra examine `cache_preview` antes de prosseguir. Mais detalhes no [kit de delegação](docs/skills/use-lohra/SKILL.md) e na [spec do harness](docs/specs/07-workflow-harness.md).

## Instruções para seu projeto

Você pode adaptar o bloco abaixo ao `AGENTS.md` do Codex ou ao `CLAUDE.md` do Claude Code, preservando as regras existentes. São instruções de colaboração; os limites técnicos devem ser configurados também na Lohra. Veja a documentação de [AGENTS.md](https://learn.chatgpt.com/docs/agent-configuration/agents-md) e da [memória de projeto do Claude Code](https://code.claude.com/docs/en/memory).

```markdown
## Uso da Lohra

- Para trabalho substancial e independente, use a skill use-lohra na raiz
  do projeto, com o profile lohra-meu-projeto e saída --json.
- Resolva tarefas pequenas diretamente. Peça subagentes à Lohra para
  investigações independentes; use workflow quando houver várias etapas,
  dependências, revisão ou valor em salvar progresso para retomada.
- Passe a cada tarefa objetivo, contexto necessário, arquivos permitidos,
  restrições e critério de conclusão. Para edições simultâneas, use escopos
  separados; subagentes não ganham worktrees automaticamente.
- Antes de escolher modelos, consulte o catálogo e os tiers do profile.
  Use apenas modelos disponíveis e mantenha a rota de cobrança autorizada.
  Se forem necessários modelos diferentes por papel, use nós agent separados.
- Ao pedir um workflow, instrua a Lohra a ler workflow-authoring, consultar
  workflow_templates e definir poucas etapas, schemas e critérios verificáveis.
- Em execução headless, passe --token-budget-cap com o teto autorizado
  por run. Não aumente o teto nem mude para uma rota mais cara sem autorização.
- Acompanhe runs com workflow list/watch; em uma pausa, reporte o motivo
  e retome pelo mesmo run_id. Use checkpoint para decisões humanas necessárias.
- Para concluir, confira error, tool_calls, workflows, arquivos alterados
  e validações relevantes. Informe session_id, run_id quando houver,
  consumo reportado e qualquer resultado parcial ou incerto.
```

### Skills usadas pela própria Lohra

A Lohra descobre instruções `AGENTS.md`/`CLAUDE.md` do projeto e skills em `.claude/skills/` e `.lohra/skills/`, além do diretório de skills do seu profile e das skills internas. Ela pode ler, criar e atualizar procedimentos pelas ferramentas `skill_view` e `skill_manage`.

Esses diretórios são distintos da descoberta de skills do Codex em `.agents/skills`. Para compartilhar uma skill criada pela Lohra com outro agente, copie sua pasta para o diretório de skills desse agente e confira as ferramentas, scripts e caminhos de que ela depende. O kit `use-lohra` integra os runtimes; não sincroniza automaticamente suas skills e memórias.

## Outros pontos de entrada

| Comando | Uso |
| --- | --- |
| `lohra doctor --json --profile lohra-meu-projeto` | Diagnóstico estruturado, sem conversa com LLM. |
| `lohra notices <session_id> --profile lohra-meu-projeto` | Avisos pendentes e consumidos na sessão. |
| `lohra serve` | API compatível com OpenAI: `/v1/chat/completions`, `/v1/responses`, `/v1/models`. |
| `lohra dashboard` | Gateway WS/REST para conectar uma interface. |

O servidor `serve` opera como relay por padrão; ferramentas no servidor exigem uma allowlist em `--tools`. Ele não disponibiliza o harness de workflows por essa API. Se a assinatura estiver habilitada no profile, o servidor recusa iniciar; `auth prefer api_key` não basta, é necessário `auth disable`. Para delegação com memória, skills e workflows, use o CLI. Use `lohra <comando> --help` para as opções disponíveis.

## Desenvolvimento

Para trabalhar no código a partir deste checkout, com um ambiente Python compatível ativado:

```bash
cd backend
python -m pip install -e ".[dev]"
lohra --version
pytest
ruff check .
```

O runtime vive em `backend/lohra/`, os testes em `backend/tests/` e a documentação em `docs/`. O antigo app desktop está fora deste repositório.

- [Arquitetura](docs/ARCHITECTURE.md): subsistemas e invariantes.
- [Changelog](backend/CHANGELOG.md): mudanças por versão.
- [Estado do projeto](docs/STATUS.md) e [roadmap](docs/ROADMAP.md).
- [Orquestração](docs/specs/06-orchestration.md), [workflows](docs/specs/07-workflow-harness.md) e [auditoria de runs](docs/specs/08-workflow-node-audit.md).

Projeto original, distribuído sob a licença [MIT](LICENSE). Software em fase alpha. A arquitetura do Hermes Agent foi uma referência histórica nas primeiras fases; o runtime da Lohra é independente.
