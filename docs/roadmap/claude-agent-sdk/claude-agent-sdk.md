⚠️  using your OpenAI/Codex subscription (opt-in, ToS-gray).
workflow avaliar-integracao-claude-agent-sdk (50657402) · budget 30000 tok
  1. perspectivas (parallel)
  2. parecer (agent) <- depends: perspectivas
perspectivas (parallel) ✓ items 5/5 · 26.6k tok
[50657402] ⚠ parecer: leaf timeout after 240s (cancelled)
perspectivas (parallel) ✓ items 5/5 · 26.6k tok
parecer (agent) ✗ null
─ 2/2 nodes · 26.6k tok · 1 fault
Rodei **5 pareceres independentes** (arquitetura, segurança, produto/DX, compatibilidade/operação e crítica adversarial). O nó final de síntese estourou o timeout; portanto o workflow ficou **degraded** — mas os cinco pareceres foram concluídos. Síntese honesta abaixo.

## Veredito: **recomendar condicionalmente**

A fronteira proposta é a correta: **Claude Agent SDK como executor/leaf especializado**, nunca como `ModelClient`, provider de chat, nem substituto do loop Lohra.

Isso preserva:

- Lohra como dona de loop, memória, skills, tools, workflows, budget e orquestração;
- o Invariante #1 (o prompt vivo da sessão Lohra permanece congelado);
- multi-provider sem criar um “provider clandestino”;
- falha do Claude Code isolada do core.

Mas “subprocesso” **não é sandbox**. Sem controles reais, vira uma tool de privilégio alto com Bash, filesystem, rede e MCP — ou seja, um pequeno gremlin com acesso ao host.

## O desenho recomendado

```text
Lohra (pai/orquestrador)
  └─ claude_agent (tool interceptada)
      └─ adapter Agent SDK
          └─ job Claude Code isolado e descartável
              ├─ tools permitidas por capability grant
              ├─ MCP opcional e allowlisted
              └─ resultado estruturado + manifesto operacional
```

**Default:** one-shot por invocação.  
**Sessão curta:** só depois de provar valor, com TTL, ownership por `run_id/task_id`, e fechamento garantido.

## Impeditivos / hipóteses a validar antes de comprometer roadmap

1. **OAuth/Max:** confirmar em ambiente limpo que `CLAUDE_CODE_OAUTH_TOKEN` funciona no caminho escolhido (`query()`/`ClaudeSDKClient`), sem login interativo, e que de fato consome a quota esperada.  
   Não prometer “desbloqueia Max” antes disso.

2. **ToS / elegibilidade:** a simetria com Codex é uma decisão interna de risco, não uma confirmação de que o uso é permitido.  
   Tratar OAuth/Pro/Max como:
   - opt-in;
   - experimental;
   - local/single-user inicialmente;
   - feature flag + kill switch;
   - sem pooling de conta, multitenancy ou fallback implícito de credencial.

3. **Cancelamento real:** cancelar a coroutine não basta. É preciso matar a **árvore de processos**, drenar stdout/stderr, fechar o client e confirmar cleanup.

4. **Empacotamento/plataformas:** validar o extra em macOS, Windows, Linux e CI headless; não assumir que CLI bundled/SDK resolve tudo.

5. **MCP:** o “subset Lohra” não pode virar o registry inteiro de tools. Precisa ser um gateway efêmero, capability-scoped e revalidado por chamada.

## Caveats prioritários

### P0 — Segurança e isolamento
- Ambiente do filho por **allowlist**, nunca herdado integralmente.
- `HOME` efêmero; não herdar `SSH_AUTH_SOCK`, Docker socket, credenciais cloud, chaves de outros providers, `.env`, keychains etc.
- Workspace temporário/worktree por job; não rodar no cwd do runtime Lohra.
- Rede negada por padrão.
- `Bash` desligado inicialmente ou muito restrito.
- Limites de CPU, RAM, PIDs, disco, output e wall-clock.
- Resultado do leaf é **não confiável**: nunca executar automaticamente comandos, URLs, patches ou tool calls sugeridos por ele.

### P0 — Efeitos e retries
Se o leaf pode editar/executar, timeout deixa ambiguidade: ele pode ter alterado algo antes de morrer. Portanto:
- retries de tarefas mutáveis precisam ser opt-in;
- preferir gerar patch/diff em workspace descartável;
- aplicar mudanças em etapa separada, validada/gateada pelo pai;
- nada de deploy, migração, produção ou ação irreversível dentro do leaf.

### P1 — Cota e concorrência
Quota Max não deve virar fan-out alegre:
- concorrência inicial: **1 por identidade OAuth**;
- `max_turns`, timeout, tamanho de contexto/output e número de tools;
- circuit breaker para quota/auth/rate limit;
- sem fallback silencioso OAuth → API key, pois isso pode gerar cobrança inesperada.

### P1 — Observabilidade
Exigir um manifesto normalizado:
- status: `completed | partial | failed | cancelled | timed_out | quota_limited | policy_denied`;
- arquivos lidos/alterados, diff opcional;
- comandos e exit codes;
- MCP/tools chamadas;
- auth mode (sem segredo);
- duração, turns/uso se disponíveis;
- motivo terminal, warnings e logs redigidos.

## Contrato mínimo sugerido

```python
claude_agent(
    task: str,                       # destilada pelo pai
    workspace: str,                  # já autorizada
    policy_profile: str,             # analysis | patch | test
    allowed_tools: list[str],
    allowed_paths: list[str],
    timeout_seconds: int,
    max_turns: int,
    mcp_servers: list[str],          # IDs allowlisted, opcional
    session_mode: "one_shot" | "resume",
) -> {
    "status": ...,
    "summary": ...,
    "changes": {"files_modified": [...], "diff": ...},
    "verification": [...],
    "artifacts": [...],
    "usage": {...},
    "error": {"code": ..., "retryable": ...}
}
```

Não aceite configuração arbitrária de MCP, shell, env ou paths via prompt/workflow/UI.

## MVP em fases

1. **Spike obrigatório**
   - auth API key vs OAuth e precedência;
   - `query()` vs `ClaudeSDKClient`;
   - cancelamento/timeout/orphan process;
   - resultado estruturável;
   - matriz de plataformas.

2. **V1**
   - extra opcional: `lohra[claude-agent]`;
   - tool `claude_agent` one-shot;
   - API key primeiro;
   - `Read`/busca em workspace isolado;
   - sem MCP;
   - sem Bash por padrão;
   - timeout, cancelamento e erro tipado;
   - CLI `doctor` + `auth status`;
   - sem `lohra serve` e sem subagentes rasos.

3. **V1.5**
   - edição em worktree descartável + diff;
   - Bash sandboxed/allowlisted;
   - dashboard como observador/disparador da mesma tool.

4. **V2, só se os spikes justificarem**
   - OAuth/Max feature-flag;
   - MCP read-only allowlisted via gateway efêmero;
   - short session com TTL e ownership estrito.

## Critérios de go/no-go

**Go** se:
- o leaf entrega ganhos mensuráveis em tarefas de código delimitadas versus `delegate_task`;
- cancelamento não deixa processos vivos;
- não há acesso fora do workspace/capabilities concedidas;
- falhas são classificadas, não “Claude deu ruim”;
- não exige mudanças no `ModelClient`, loop ou engine de workflow.

**No-go / reconsiderar** se:
- a maior parte das tarefas precisa de sessão longa opaca;
- o valor só existe porque “tem Max”;
- a tool passa a fazer roteamento de modelo, contexto, fallback e streaming genéricos;
- a integração exige exceções no core;
- MCP/Bash vira bypass de policies Lohra.

**Resumo curto:** a tese é boa, desde que `claude_agent` seja uma leaf **estreita, descartável, isolada de verdade, capabiperspectivas (parallel) ✓ items 5/5 · 26.6k tok
parecer (agent) ✗ null
─ degraded · 2/2 nodes · 26.6k tok · 1 fault

session: 3fc4d4547c8a42149fd769a792254ef2  (resume with --session 3fc4d4547c8a42149fd769a792254ef2)