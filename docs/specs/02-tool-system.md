# Lohra Tool System Spec

> **Nota:** doc de bootstrap (Fases 0–3). O código divergiu desde então — ver `docs/ARCHITECTURE.md` §Referência.

> Reimplementação do sistema de tools do Hermes Agent (MIT).

## 1. Registry Pattern

### Auto-registro no import
Cada módulo de tool chama `registry.register(...)` no **top-level**. Singleton `registry = ToolRegistry()`.

### Auto-discovery via AST scan
`discover_builtin_tools()` faz glob de `tools/*.py`, **AST-parseia cada arquivo** e importa só os que têm `registry.register(...)` no corpo do módulo (não dentro de função). Exclui `__init__.py`, `registry.py`, `mcp_tool.py`. Falhas são logadas e puladas, nunca fatais.

### Contrato de registro
```
registry.register(
    name: str,
    toolset: str,
    schema: dict,                       # {"name","description","parameters"} estilo OpenAI
    handler: Callable[[dict, **kwargs], str],   # SEMPRE retorna JSON string
    check_fn: Callable[[], bool] = None,        # gate de disponibilidade, cache TTL ~30s
    requires_env: list[str] = None,
    is_async: bool = False,
    description: str = "",
    emoji: str = "",
    max_result_size_chars: int|None = None,
    dynamic_schema_overrides: Callable[[], dict] = None,
    override: bool = False,
    author_time_only: bool = False,
) -> None
```

Regras de shadowing: mesmo toolset → permitido; donos MCP diferentes com o mesmo nome público → colisão explícita (#115); `override=True` continua sendo uma substituição explícita confiável. Contador `_generation` incrementa em cada mutação. Mutações protegidas por `RLock`.

`ToolEntry.author_time_only` é metadata interna de registro (#84/#130), fora da
autoridade do schema ou dos argumentos JSON. Filhos, leaves e servidor agentic
filtram entradas marcadas e recusam sua execução; o autor continua autorizado.
As exclusões legadas de profundidade/stateful permanecem em união com a metadata:
desmarcar um nome legado não o libera, e marcar uma extensão nova não exige
acrescentá-la à lista histórica. Tools runtime não marcadas preservam os demais
gates do consumidor; isto não cria um catálogo completo de capacidades semânticas.

## 2. Schema para o LLM
Formato interno canônico = **OpenAI function-calling**. `get_definitions()` envolve em `{"type":"function","function":{...}}`. Conversão Anthropic (`input_schema`) é feita na **fronteira do adapter**, não no registry. Schemas sanitizados para compatibilidade (llama.cpp grammar).

## 3. Dispatch
- `registry.dispatch(name, args, **kwargs)` — low-level, captura todas exceções → `{"error": ...}`.
- `handle_function_call(...)` — dispatcher principal: `coerce_tool_args` (string→tipo), bridge de Tool Search, middleware + hooks.
- **Single** → sequencial. **Multiple** → até 8 workers por recursos independentes, com slots por índice (resultados na ordem original). `read_file`/`write_file` do mesmo recurso de arquivo formam uma fila executada na ordem emitida, incluindo overwrite, append e leituras intermediárias (#97).
- Erros sanitizados (`_sanitize_tool_error`): remove tags XML, code fences, cap 2000 chars, prefixo `[TOOL_ERROR]`.

**Autoridade por entrada (#115/#130).** O consumidor instala seus guards dentro
do dispatch que executa no worker. O `ToolRegistry` captura uma entrada sob lock,
aplica a interseção dos guards ativos e invoca aquele mesmo handler fora do lock.
Um rebind entre o preflight e a chamada não troca a autoridade da entrada executada;
uma entrada runtime já capturada pode terminar após sua substituição por outra
author-only, sem executar o novo handler. Guards permanecem ativos em chamadas
aninhadas feitas pelo handler e o token anterior é restaurado inclusive em
`BaseException`; um autor concorrente não herda a restrição do filho.

Filtros retornam novas tuplas. Registro/revogação posterior não reescreve definitions
nem prompt já congelados; a execução consulta a autoridade corrente. O servidor
mantém adicionalmente seu allowlist explícito de nomes, vazio por padrão. Helpers
com catálogo customizado recebem `tool_registry` explícito e consistente entre os
wrappers; não inferem o catálogo pela closure. Preflight recusa uma entrada marcada
antes de um interceptor, mas callbacks Python arbitrários do embedder continuam
confiáveis: seus efeitos próprios não são sandboxados por esse lookup. A garantia
de mesma entrada/handler cobre a cadeia que chega ao `ToolRegistry`.

**Ordenação de arquivos (#97).** O planner usa `Path.resolve(strict=False)` no
cwd do processo e identifica arquivos existentes por dispositivo/inode, incluindo
aliases relativos, absolutos, symlinks, hardlinks e aliases de caixa que o volume
resolve para o mesmo arquivo. Para um sufixo ainda não criado, usa a identidade
do ancestral existente e o sufixo com `casefold`/normalização Unicode NFC:
possíveis colisões de caixa ou composição Unicode compartilham conservadoramente
uma fila, sem presumir a configuração do volume.
Arquivos ou ancestrais existentes com identidades distintas continuam independentes,
assim como sufixos sem essa colisão. Resolve symlinks antes de `..`; não expande
`~`, que os handlers de arquivo tratam literalmente.
A sondagem só consulta metadados: não abre/lê conteúdo, não altera
os argumentos nem autoriza acesso. Cada chamada continua passando pelo dispatch
original e seus gates. Se uma identidade não puder ser determinada (argumento
inválido ou erro de resolução), todas as chamadas de arquivo daquela mensagem
compartilham uma fila conservadora; outras tools continuam independentes. O erro
de identidade não é exposto e o handler continua responsável pela resposta.

A ordem vem das filas construídas pela posição emitida, não da disputa por um
`Lock`. Filas de arquivos distintos e tools não classificadas como arquivo podem
executar simultaneamente; um campo `path` arbitrário em outra tool não cria uma
dependência. Um erro normal continua sendo um resultado e não pula as chamadas
seguintes. `BaseException`/SIGINT mantém shutdown sem join dos workers vivos e
cancela também a cauda ainda não iniciada de uma fila ativa. Isso não interrompe
a tool já em voo nem muda o abort cooperativo pendente de #68.
Os futures são consumidos por conclusão, preenchendo os índices originais: uma
exceção de fila posterior não fica escondida atrás de uma fila anterior bloqueada.
O próprio worker publica a parada ao capturar `BaseException`, antes de o
consumidor acordar; a exceção original é propagada.

O contrato vale **só dentro de uma mensagem**. Não há locks globais ou estado de
recursos persistente. A identidade é uma fotografia anterior ao dispatch: não
cobre substituição externa de arquivos/symlinks nem novos aliases criados depois
da sondagem, nem writers de outras mensagens/sessões/processos. Não é
isolamento de filesystem, detecção de staleness ou merge de conteúdo: overwrite
ainda substitui o arquivo inteiro; append acrescenta ao conteúdo anterior.

## 4. Toolsets
- 57 toolsets estáticos. Estrutura: `{"description", "tools":[...], "includes":[...]}`.
- `resolve_toolset(name)` resolve `includes` recursivamente com detecção de ciclo.
- Per-sessão: `enabled_toolsets` (união) menos `disabled_toolsets` (subtração final).
- **Tool Search (progressive disclosure):** quando a superfície deferível (MCP + plugin) excede ~10% da janela, são substituídas por 3 bridge tools `tool_search`/`tool_describe`/`tool_call`. Core nunca é deferido.

## 5. Approval Gate

O detector de comandos perigosos continua uma denylist heurística, não um
sandbox de SO. `ApprovalManager` mantém callback, yolo e cache do **comando
exato** dentro de um consumidor vivo (#129). `once` autoriza apenas aquela
chamada; `session`/`always` mantêm o comando no cache daquele manager, sem
permissão por categoria e sem persistência em disco. Falha/retorno inválido do
callback nega. O callback roda fora do lock do manager.

`build_session_dispatch(..., approval_manager=manager)` permite ao host vincular
explicitamente essa instância. Se omitida, cada dispatcher possui um manager
novo que nega comandos perigosos, sem herdar contexto ativo ou singleton.
`bind_approval_dispatch(base, manager=manager)` fornece o mesmo contrato para
embedders: captura a instância e instala o ContextVar **dentro da chamada que
executa no worker**, restaurando o token em `finally`, inclusive em nesting e
BaseException. Não copia o contexto inteiro do chamador para o executor nem
modifica args, handlers, registry ou a assinatura `dispatch(name, args)`.

- **CLI:** cada invocação tool-enabled cria e configura seu próprio manager.
  TTY mantém o prompt `once/session/deny`; JSON/no-input nunca promptam; `--yolo`
  autoriza somente a invocação que recebeu a opção. Outra invocação, inclusive
  com o mesmo ID persistido, não recupera cache/callback/yolo da anterior.
- **Dashboard/gateway:** cada novo Agent/dispatch nasce independente, sem
  callback interativo, e nega comandos perigosos. Não existe fila de approval
  implementada nesta superfície. Revival cria dispatcher novo; compaction que
  reutiliza o mesmo Agent/dispatch e busy-lock continua a mesma invocação viva,
  mesmo com um novo ID de linhagem no SQLite.
- **Subagentes/serve/workflow:** os guards canônicos continuam mais restritivos:
  subagent auto-deny de comandos perigosos, allowlist do serve, sandbox e taint
  das leaves. Um contexto yolo no pai não remove esses gates, inclusive quando
  um dispatch de filho é chamado sincronicamente dentro do pai.

**API de embedding e compatibilidade.** `ApprovalManager`, `approval`,
`bind_approval_dispatch` e `require_approval` são importáveis de `lohra.tools`.
O objeto legado `approval` continua disponível, mas configurar seus métodos não
concede autoridade implicitamente a terminal/CLI/gateway. Código que antes
configurava o singleton deve passar o manager ao seu próprio dispatcher:

```python
from lohra.tools import ApprovalManager, bind_approval_dispatch, registry

manager = ApprovalManager()
manager.set_callback(operator_callback)
dispatch = bind_approval_dispatch(registry.dispatch, manager=manager)
```

Também é possível passar explicitamente a instância legada; compartilhar uma
instância entre dispatchers é uma decisão do host confiável. Tool JSON ou kwargs
como `approval_manager` não selecionam autoridade. O terminal consulta somente
`require_approval`; fora de um binding, comandos perigosos são negados e comandos
seguros mantêm o comportamento anterior. Não há mapa global por sessão, grants
duráveis, callback de gateway novo ou alteração do prompt congelado.

Cancelar um future ainda na fila não instala binding. Cancelamento de uma tool
ou callback já em execução não interrompe sua thread: o binding fica somente
nessa chamada até a saída pelo `finally`. O cancelamento do terminal (#119) é
um contrato separado, que pode compor outro wrapper sem mudar esta assinatura.

## 6. Tools Interceptados no Agente
`_AGENT_LOOP_TOOLS = {"todo", "memory", "session_search", "delegate_task"}` (+ `clarify`). Schema no registry mas execução interceptada (precisam de estado do agente).

### delegate_task (subagents)
- **Contexto isolado:** `AIAgent` fresco, sem histórico do pai, `skip_context_files`, `skip_memory`, budget fresco.
- **Caps:** pai `max_iterations=90`, cada subagente `50`. Profundidade `MAX_DEPTH=1` (sem netos).
- **Concorrência:** `max_concurrent_children=3`. Aprovação: `_subagent_auto_deny` por padrão (seguro).
- **Retorno:** pai lê `result["final_response"]` como summary + status.

## 7. Backends de Terminal
`BaseEnvironment(ABC)` + factory por `env_type ∈ {local, docker, ssh, modal, daytona, singularity}`. `ProcessHandle` (Protocol). Instância por `task_id` (isolamento de sessão).

## 8. MCP Client
- `_convert_mcp_schema` → `mcp_{server}_{tool}`.
- Registra sob toolset `mcp-{server}` com handler/check_fn. Guard de colisão com built-ins.
- Refresh dinâmico em `notifications/tools/list_changed` (nuke-and-repave).

## 9. Inventário de Tools (superfície de capacidade)
**file:** read_file, write_file, patch, search_files. **terminal:** terminal, process. **web:** web_search, web_extract. **x_search**, **vision:** vision_analyze. **video/image_gen/video_gen/tts**. **browser:** navigate/snapshot/click/type/scroll/back/press/get_images/vision/console. **computer_use**. **code_execution:** execute_code. **skills:** skills_list, skill_view, skill_manage. **todo, memory, session_search, clarify, delegate_task** (interceptados). **moa:** mixture_of_agents. **cronjob**. **messaging:** send_message. **kanban** (9 tools). **homeassistant** (4). **discord/feishu/yuanbao** (plataformas).

## Notas para Lohra
- Registry = singleton thread-safe com generation counter; handlers retornam JSON string.
- Schema interno OpenAI; converter Anthropic só no adapter.
- Single→sequencial; multiple→ThreadPool(8), FIFO por path para file calls da mesma mensagem, slots por índice.
- Interceptar `todo/memory/session_search/clarify/delegate_task`.
- Approval = lista regex → manager do dispatch vivo; callback CLI/embedding com `once|session|always|deny`; gateway sem callback nega (#129).
