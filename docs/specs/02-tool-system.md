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
) -> None
```

Regras de shadowing: ambos `mcp-` → permitido; `override=True` → permitido (logado); senão rejeitado. Contador `_generation` incrementa em cada mutação. Mutações protegidas por `RLock`.

## 2. Schema para o LLM
Formato interno canônico = **OpenAI function-calling**. `get_definitions()` envolve em `{"type":"function","function":{...}}`. Conversão Anthropic (`input_schema`) é feita na **fronteira do adapter**, não no registry. Schemas sanitizados para compatibilidade (llama.cpp grammar).

## 3. Dispatch
- `registry.dispatch(name, args, **kwargs)` — low-level, captura todas exceções → `{"error": ...}`.
- `handle_function_call(...)` — dispatcher principal: `coerce_tool_args` (string→tipo), bridge de Tool Search, middleware + hooks.
- **Single** → sequencial. **Multiple** → `ThreadPoolExecutor(max_workers=8)` com slots por índice (resultados na ordem original).
- Erros sanitizados (`_sanitize_tool_error`): remove tags XML, code fences, cap 2000 chars, prefixo `[TOOL_ERROR]`.

## 4. Toolsets
- 57 toolsets estáticos. Estrutura: `{"description", "tools":[...], "includes":[...]}`.
- `resolve_toolset(name)` resolve `includes` recursivamente com detecção de ciclo.
- Per-sessão: `enabled_toolsets` (união) menos `disabled_toolsets` (subtração final).
- **Tool Search (progressive disclosure):** quando a superfície deferível (MCP + plugin) excede ~10% da janela, são substituídas por 3 bridge tools `tool_search`/`tool_describe`/`tool_call`. Core nunca é deferido.

## 5. Approval Gate
- `DANGEROUS_PATTERNS`: ~47 regex (`rm -rf`, `chmod 777`, `mkfs`, `dd`, `DROP`, `curl|sh`, fork bomb...).
- `detect_dangerous_command(command) -> (is_dangerous, pattern_key, description)`.
- Estado thread-safe: `_session_approved`, `_permanent_approved`, `_session_yolo`.
- **CLI:** callback thread-local `(command, description, *, allow_permanent) -> "once"|"session"|"always"|"deny"`.
- **Gateway:** fila per-sessão de `_ApprovalEntry{event, data, result}`; agente bloqueia em `threading.Event`; UI chama `resolve_gateway_approval(session_key, choice, resolve_all)`.

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
- Single→sequencial, multiple→ThreadPool(8) com slots por índice.
- Interceptar `todo/memory/session_search/clarify/delegate_task`.
- Approval = lista regex → callback CLI OU fila bloqueante de gateway resolvendo `once|session|always|deny`.
