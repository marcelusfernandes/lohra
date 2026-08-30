# SUP-04 — pivô de workflow preservando trabalho

**Data:** 2026-08-30  
**Branch:** `feat/lohra-epic-sup`  
**Base da investigação:** `746a5e1` (SUP-01..03 já presentes)  
**Classificação final:** **reformulada**

## Pergunta

Quando uma rota desaparece no meio de um workflow, a agente precisa de um helper novo de
reroteamento, deve reautorar um run novo, ou a superfície existente de resume já permite
adaptar o spec no mesmo `run_id` sem repagar trabalho concluído? A resposta também precisa
respeitar a fronteira de autonomia da SUP-01: provider, credencial, billing route, custo
incerto e aumento de token budget continuam humanos.

## Baseline revalidado

Antes de propor mudança permanente, o comportamento foi lido e executado no HEAD atual:

1. `WorkflowService.start()` aceita `spec_dict` junto de `resume_run_id`; um spec explícito é
   validado e vence o spec persistido, que é somente fallback (`workflow/service.py`). O teste
   legado `test_an_explicit_spec_still_wins_over_the_persisted_one` confirma o contrato.
2. `NodeCache` é content-addressed e escopado por `run_id`; completions de outro run nunca
   são reutilizadas (`workflow/cache.py`).
3. A identidade de toda célula inclui `meta.name` e `meta.version`
   (`workflow/engine.py:430,986`). Alterar qualquer um não é um pivô cirúrgico: rekeya o
   spec inteiro.
4. O spend e o teto original são cumulativos no resume; omitir `token_budget` herda o teto.
   Aumentá-lo já é uma operação explícita, humana.
5. Quota tem dono próprio: `AutoResumeScheduler` espera no mínimo 60 s e possui contador
   compartilhado de no máximo 5 tentativas. Ele repete o spec persistido; não escolhe rota.
6. `workflow_steer` só alcança uma ocorrência viva e causalmente identificada. Runs
   `complete`, `failed`, `cancelled` ou `paused` são recusados. Portanto steering não é
   reparo de falha terminal nem substitui pivô estrutural.
7. Roteamento por node existe em `agent`, `verify`, `judge_panel`, `loop_until_dry`, `gate` e
   `completeness_check`. `parallel` e stages de `pipeline` não têm routing próprio. Logo não
   existe — nem se deve prometer — pivô genérico e granular para toda forma de fan-out.

O baseline já continha quase toda a mecânica necessária. A lacuna era operacional: a
superfície e a skill ensinavam resume com prompt corrigido, mas não definiam a identidade a
preservar, a comparação com run novo/steering, a granularidade real do cache, a interação
com quota e a fronteira de billing/custo.

## Hipóteses e condições de falsificação

| Hipótese | Condição de falsificação |
|---|---|
| H1. Spec adaptado + mesmo `run_id` basta para pivô suportado. | Uma célula concluída e efetivamente idêntica reexecuta, ou a célula alterada não reexecuta. |
| H2. Run novo é mais caro que same-run resume para o mesmo objetivo. | O run novo reutiliza células do anterior ou custa o mesmo número de leaves que o pivô. |
| H3. Preservar apenas node id é suficiente. | `meta.name`/`meta.version` ou conteúdo efetivo entram no hash e invalidam células. |
| H4. Fan-out incompleto pode ser retomado por leaf interno. | Um painel incompleto preserva e mistura resultados parciais com uma nova execução. |
| H5. Nesting quebra o cache no pivô. | Uma célula interna estável do child reexecuta quando somente uma irmã muda. |
| H6. O budget pode ser tratado como orçamento de cada stretch. | Resume reinicia spend ou aceita silenciosamente mais teto. |
| H7. Steering substitui pivô após o fault. | Uma ocorrência terminal aceita steer ou troca sua rota congelada. |
| H8. Um helper dedicado é necessário. | A combinação spec explícito + resume não consegue expressar a mudança mínima ou observar seu resultado. |
| H9. Esperar quota e rerotear são operações equivalentes. | O autoresume escolhe nova rota e existe evidência de equivalência de custo/cache futuro. |

## Experimentos controlados

Os experimentos permanentes estão em `tests/test_workflow_pivot.py`. Um provider determinístico
cobra 5 tokens de entrada + 3 de saída por leaf bem-sucedido e injeta faults sem custo externo.

| Experimento | Resultado |
|---|---|
| Modelo inválido em uma de duas células; corrigir somente `target.model` no mesmo run. | 1 célula reutilizada, 1 reexecutada; 16 tokens cumulativos, 8 incrementais. **H1 confirmada.** |
| Mesmo cenário, mas criar run novo com spec corrigido. | 0 células reutilizadas; 2 leaves executadas e 16 tokens no run novo, contra 8 no pivô. **H2 confirmada.** |
| Trocar `meta.version` durante a correção. | A célula estável também reexecutou; 0 hits e 24 tokens cumulativos. **H3 refutada como formulada:** node id não basta. |
| `verify` com dois skeptics, um fault injetado, seguido de resume corrigido. | O nó rigoroso incompleto não foi cacheado; os 2 skeptics reexecutaram como unidade. A irmã `agent` foi reutilizada. **H4 refutada:** não há cache parcial seguro. |
| Workflow nested com child `stable` + `target`; atualizar o template preservando ref, `meta` e a célula estável. | `parent_stable` e child `stable` foram reutilizados; somente child `target` reexecutou; 24 tokens cumulativos. **H5 refutada.** |
| Teto original de 8 tokens já totalmente gasto; pivô exigiria mais uma célula. | O resume foi recusado antes de spawn, informou os 8 tokens já gastos e exigiu autorização humana para um teto maior; o run anterior permaneceu `degraded`. **H6 refutada.** |
| Resume adaptado sem reenviar `args`. | O valor original foi reidratado no prompt do alvo; a irmã estável permaneceu em cache. |
| Parâmetro opcional `effort` recusado pelo mesmo transport; removê-lo no mesmo run. | A irmã estável foi reutilizada; só o alvo corrigido executou. Isso é seguro apenas se effort não era requisito do usuário. |
| Fault 401 controlado. | Run terminou degradado, sem `resume_at` e sem reparo automático. Credencial/rota não foi alterada. Evidência do pior caso: o pivô autônomo é proibido. |
| Contratos existentes de quota/autoresume. | O scheduler agenda a mesma execução, honra cooldown e contador; não contém seleção de provider/model. **H9 refutada como equivalência.** |
| Contratos existentes de steering terminal. | Run settled é recusado antes de enqueue; o prompt/route congelado não é mutado. **H7 refutada.** |

### Dogfood real, sem steering humano

Foi rodado um workflow de duas células via subscription Codex, budget 4.000 tokens:

- run `9d2025001986409896760056f9d6ef2e`;
- `stable` em `gpt-5.6-sol`; `target` em slug inexistente;
- primeiro stretch: `degraded`, 1 complete/1 null, fault 400 no `target`, 428 tokens;
- a agente diagnosticou pelo fault + `list_models`, registrou antes do contorno a chave
  `(run, invalid-model, target)`, fingerprint, mudança, reuso esperado e estimativa ≤1.500;
- retomou o **mesmo run** com o mesmo `meta`, provider, credencial, billing route e budget,
  alterando somente `target.model` para o slug catalogado;
- segundo stretch: `complete`, apenas `target` apareceu em `node_costs`, 428 tokens
  incrementais, `stable` veio do cache;
- `faults_total` preservou o erro anterior, portanto o recovery não reescreveu a história.

Isso demonstra a ação completa `diagnosticar -> adaptar -> retomar` pela agente, sem
`workflow_steer` e sem novo runtime.

## Resultados negativos preservados

1. **Cell cache não prevê o estado futuro do prompt cache do provider.**
   `tokens_cache_read` registra cache reads históricos reportados pelo provider quando essa
   telemetria existe; zero no dogfood pode significar ausência de reuse ou telemetria
   ausente/incompleta. Esperar evita a troca de rota certamente fria, mas a retenção pode
   expirar durante o cooldown. Não foi possível comparar honestamente o cache futuro de
   “esperar quota” com “trocar provider”.
2. **Preço não vem de `list_models`.** O catálogo qualifica existência, não equivalência de
   preço. Em API-key route sem pricing metadata ou preautorização, a agente não pode concluir
   que um slug alternativo custa o mesmo; deve escalar.
3. **Fan-out rigoroso não tem cache por membro.** Um único skeptic morto repaga o painel
   inteiro. Implementar cache parcial agora arriscaria combinar uma amostra velha com outra
   nova; foi descartado.
4. **`parallel` e stages de `pipeline` não são roteáveis individualmente.** O pivô não cria
   uma capacidade que o schema não possui. Para mudar a rota desses trabalhos é necessário
   reautorar a estrutura (por exemplo, separar em nodes `agent`), o que pode mudar escopo e
   exige julgamento humano quando não for uma transformação estritamente equivalente.
5. **Status corrente não resume a história.** Após recovery, o stretch fica `complete`, mas
   `faults_total` permanece. Certificar o run lendo apenas `faults` seria falso.
6. Um subagente de implementação em `z-ai/glm-5.3-flash` terminou por `max_iterations` sem
   resposta final; nenhuma conclusão técnica foi atribuída a ele.

## Comparação das três vias

### 1. Same-run resume com spec adaptado — preferido

É a operação mínima para uma rota qualificada: preserva identidade, args, budget e cache do
run. Só devem mudar os campos causalmente afetados. `meta.name` e `meta.version` permanecem.
O registro pré/pós deve nomear campos alterados, fingerprint, células esperadas/efetivas e
custo incremental estimado/real.

### 2. Novo run/reautoria — baseline, não default

Novo `run_id` não reutiliza o cache anterior. É correto quando o objetivo, identidade ou
escopo mudou; usar para simples correção de slug repaga trabalho conhecido e apaga a relação
operacional com `faults_total` do run original.

### 3. Steering — só correção causal pequena em leaf vivo

É adequado para uma instrução pequena entre iterações. Não troca provider/model/client, não
preempta turno em voo, não alcança execução terminal e não corrige spec estrutural. Para um
fault de rota já settled, steering é a ferramenta errada.

## Fronteira de autonomia aplicada ao pivô

A agente pode pivotar sozinha somente quando **todas** forem verdadeiras:

- a causa está diagnosticada e o contorno é reversível, registrado e cabe nos freios SUP-01;
- o modelo novo foi verificado no catálogo;
- provider, credencial e billing route são os mesmos;
- há evidência de subscription fixed-price, ou pricing/preautorização prova custo não maior;
- o budget original comporta a estimativa; nenhum aumento é feito;
- a mudança não altera objetivo, escopo ou requisito do usuário.

Sempre humano: trocar provider/credencial/billing route; reparar 401/403; custo desconhecido
ou maior; aumentar budget; alterar objetivo/escopo; responder checkpoint; ação irreversível.
Se `quota_exhausted` tem `resume_at` futuro, a agente espera o autoresume e não cria corrida.

## Decisão de implementação

**Não foi criado helper de reroteamento.** H8 foi refutada: a superfície existente já
expressa o pivô e o cache fornece sua economia. Um helper duplicaria `start(spec,
resume_run_id)`, teria de reimplementar as mesmas fronteiras e poderia sugerir falsamente que
qualquer provider/custo é autorizado.

A mudança permanente é deliberadamente textual + contratos anti-drift:

- guidance de `run_workflow` declara same-run pivot, identidade, comparação, quota, billing e
  registro;
- skill builtin explica as três vias, granularidade, nesting, prompt cache e `faults_total`;
- `tests/test_workflow_pivot_guidance.py` exige que tool e skill mantenham os limites;
- `tests/test_workflow_pivot.py` protege a mecânica real contra regressões.

## Classificação final — REFORMULADA

A formulação forte — “quando um provider desaparece, reescrever o alvo e continuar” — é
insegura e ampla demais. Provider/credencial/billing route são propriedade humana, e algumas
formas do DAG não têm roteamento granular.

A formulação sustentada pela evidência é:

> Para um fault de rota em node roteável, a agente pode fazer um pivô mínimo no mesmo
> `run_id` usando spec explícito, sem helper novo, **apenas** dentro da rota/custo autorizados
> pela SUP-01. O cache reutiliza células efetivamente idênticas; fan-outs seguem a
> granularidade atual; quota futura deve ser aguardada; novo run é reservado a mudança real
> de identidade/objetivo e steering a correção pequena de leaf ainda vivo.

## Validação final

- regressão focada: **169 passed**;
- suíte completa, com a credencial OpenRouter do ambiente removida para preservar os testes
  de máquina sem provider: **2273 passed**, cobertura **95%**;
- `ruff check .` e `git diff --check`: limpos;
- skill builtin: **799 linhas** (limite 800);
- review adversarial final: nenhum achado alto ou médio.

A primeira suíte completa, executada sem isolar `OPENROUTER_API_KEY`, teve 15 failures: os
casos de onboarding/“sem provider” auto-detectaram a credencial real e deixaram um writer de
audit vivo após as asserções precoces; além disso, o teste de 800 linhas encontrou a skill
com 811 linhas. O resultado negativo foi preservado: a skill foi compactada sem remover
contrato e a suíte foi repetida com `env -u OPENROUTER_API_KEY`, não por mudança de código de
produção.

## Comandos de reprodução

```bash
cd backend
python -m pytest -q --no-cov \
  tests/test_workflow_pivot.py \
  tests/test_workflow_pivot_guidance.py \
  tests/test_workflow_m7_fixes.py \
  tests/test_workflow_quota.py \
  tests/test_workflow_service_steering.py \
  tests/test_workflow_rigor_routing.py
env -u OPENROUTER_API_KEY python -m pytest -q
ruff check .
```
