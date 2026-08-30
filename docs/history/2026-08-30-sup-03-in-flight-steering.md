# SUP-03 (#29) — Direcionamento in-flight de uma ocorrência exata

> Data: 2026-08-30 · Branch: `feat/lohra-epic-sup` · Baseline pré-SUP-03:
> `cdf9db7` · Predecessoras:
> `docs/history/2026-08-29-sup-01-active-supervision.md` e
> `docs/history/2026-08-30-sup-02-in-flight-observation.md`.
>
> **Resultado:** contrato estreito de steering suportado; steering genérico foi
> rejeitado. A conclusão global é **REFORMULADA**: é seguro enfileirar uma
> correção causal pequena para uma ocorrência local, exata e ainda ativa, mas
> não interromper uma chamada, reviver uma leaf idle, alterar o DAG nem prometer
> convergência.

## 1. Pergunta investigada

A SUP-02 tornou observável a identidade de uma execução em voo
(`run_id`, `segment_id`, `sub_id`, `attempt`, `turn`). Esta issue perguntou se
essa observabilidade sustenta uma ação workflow-facing de direcionamento ou se
cancelar e reexecutar continua sendo o único contrato seguro.

A propriedade em jogo, herdada da SUP-01, é mais forte que “conseguir mandar
texto”: todo contorno automático deve ser reversível, bounded, registrado e
finito. O prompt de sistema deve continuar congelado. Uma identidade observada
não pode atingir silenciosamente outra tentativa ou outro turno.

## 2. Baseline revalidado

O estado pré-SUP-03 foi revalidado diretamente em `cdf9db7`:

- não existia `workflow_steer` no guidance, na skill, no registry ou no dispatch;
- `workflow_audit` já expunha a identidade efêmera da execução;
- `OrchestrationCore.steer()` aceitava uma sub-sessão busy, **mas também criava
  um novo turno quando ela estava idle**;
- o loop já drenava o inbox somente entre iterações, como mensagem user
  `<system-reminder>`, sem reconstruir o prompt;
- correções internas de schema já usavam steering, porém o orçamento ainda não
  era compartilhado com uma correção externa;
- cancelar e reexecutar era o escape estrutural existente.

Portanto, o gap não era criar transporte de texto. Era impedir que uma
superfície workflow-facing herdasse a semântica ampla de `steer()` e confundisse
“aceito na fila” com “lido pela leaf”.

## 3. Hipóteses e condições de falsificação

### H1 — contrato estreito é suficiente

Uma superfície limitada a **uma ocorrência local, exata e ativa**, com entrega
apenas entre iterações, pode corrigir um desvio causal pequeno sem abrir
steering geral.

**Falsifica se:** identidade stale puder atingir tentativa posterior; a ação
criar turno idle; houver preempção; o prompt congelado mudar; o contorno não
terminar com recibo ou teto finito.

### H2 — `run_id + sub_id` identifica o alvo

A identidade efêmera do audit talvez bastasse sem coordenadas adicionais.

**Falsifica se:** o mesmo `sub_id` puder avançar de contexto causal entre a
observação e o enqueue, ou se houver janela TOCTOU entre snapshot e fila.

### H3 — aceitação equivale a entrega

O retorno síncrono da tool talvez pudesse dizer que a instrução chegou.

**Falsifica se:** completion, cancel, erro ou shutdown puderem ocorrer depois do
enqueue e antes do próximo drain.

### H4 — steering é economicamente superior a cancel-and-respawn

Preservar o contexto e o trabalho já feito talvez custe menos que cancelar e
repetir.

**Falsifica se:** um cenário controlado tiver cancel-and-respawn mais barato, ou
se não houver como demonstrar convergência comparável.

### H5 — limites só em memória bastam

Um teto por engine talvez fosse suficiente.

**Falsifica se:** resume/restart reconstruir o engine e restaurar o orçamento do
run.

## 4. Alternativas comparadas

### A — sem steering; sempre cancel + reexecução

É o caminho mais simples e continua correto para erro estrutural de spec/prompt.
Ele desperdiça trabalho já executado quando o problema é apenas uma pequena
correção causal em uma leaf ainda viva.

### B — expor `OrchestrationCore.steer()` diretamente

**Refutada.** Essa API é apropriada para sessões gerais porque uma sub-sessão
idle recebe um turno novo. Para workflow, isso reviveria trabalho que já parou,
separaria a ação da ocorrência observada e permitiria loops invisíveis.

### C — contrato narrow, active-only e exact-occurrence

**Escolhida.** `workflow_steer` exige:

- `run_id`;
- `sub_id` efêmero;
- `segment_id` observado;
- `attempt` observado;
- `turn` observado;
- `text` não vazio, com no máximo 4.000 caracteres.

Todos os campos causais precisam continuar iguais sob o mesmo lock que valida
estado ativo e enfileira. Drift rejeita antes de gastar budget. A API genérica
permanece interna.

## 5. Contrato permanente

### 5.1 Alvo e timing

A ação só aceita uma leaf:

- de um run `running` no registry **deste processo**;
- com core e engine vivos e não fenced;
- cuja ocorrência completa ainda coincide com a observada;
- que ainda aceita steering.

Cache replay não tem `sub_id`; linha apenas durável e run de outro processo não
são steerable. Terminal, idle, cancelled e erro são recusados. Uma leaf
enfileirada no pool, mas ainda ativa, pode aceitar; cancelá-la antes da leitura
descarta o texto.

A aceitação significa **queued**, não read. Não há interrupção de provider nem
de tool síncrona. A leaf lê o texto somente num boundary entre iterações como
`<system-reminder>` no tail da conversa. Se não alcançar esse boundary, a
instrução é descartada.

### 5.2 Prompt congelado

O texto de steering nunca entra no system prompt. Teste de identidade cobre o
mesmo objeto e o mesmo texto de prompt através de:

1. primeiro turno;
2. conversa retomada;
3. steering entre iterações;
4. compactação forçada.

A invariante stable → context → volatile continua congelada por sessão.

### 5.3 Lifecycle e auditoria

O audit usa vocabulário fechado e metadata-only:

- `steering.accepted`: enfileirado;
- `steering.read`: lido — gasta o slot;
- `steering.discarded`: não lido — restaura o slot;
- `steering.rejected`: core recusou — rollback;
- `steering.exhausted`: algum teto bloqueou antes do enqueue.

A instrução nunca é persistida no audit. Identidade e contadores podem ser
persistidos. As causas fechadas de exaustão (`leaf_limit`, `run_limit`,
`correction_limit`) sobrevivem ao sanitizador.

### 5.4 Freios

Os valores são intencionalmente menores ou iguais aos freios SUP-01:

- **1 external steer por leaf**: steering externo é excepcional, não canal de
  conversa;
- **3 external steers por run**: coincide com o teto global de contornos da
  SUP-01;
- **2 correções cumulativas por leaf**: soma steering externo e correção interna
  de schema; impede que dois mecanismos consumam tetos paralelos;
- **4.000 caracteres por instrução**: limita payload e evita reautorizar um
  prompt inteiro por steering.

O teto de run é transacional e durável em SQLite. O reserve usa
`BEGIN IMMEDIATE`; disputa entre threads, conexões ou processos tem um único
vencedor para o último slot. `discarded` e `rejected` restauram exatamente uma
vez; `read` permanece gasto.

**Semântica de crash, fail-closed:** se o processo morrer depois da reserva
durável e antes do settlement, o slot pode permanecer gasto. O sistema prefere
subutilizar o teto a recarregá-lo silenciosamente. Os contadores per-leaf e de
correções continuam engine-local; o teto global de três é a defesa durável.

Os freios comportamentais SUP-01 continuam obrigatórios por cima desses tetos:
uma tentativa por `(run, causa, alvo)`, K=2 sem progresso e allowance de custo.
`workflow_steer` não autoriza uma tentativa extra.

## 6. Experimentos e resultados

Todos os experimentos permanentes usam o caminho real de loop/core/service e
provider falso determinístico. Isso controla races e tokens sem rede nem
sleep probabilístico.

### 6.1 Busy, primeiro turno e tool longa

- **Busy provider:** steer aceito durante a chamada não aparece nessa chamada;
  aparece somente na próxima iteração.
- **Primeiro turno:** enquanto a primeira provider call está bloqueada, uma
  identidade stale é recusada e a identidade exata é queued. Depois do gate, o
  primeiro resultado termina sem preempção e a correção é lida no follow-up.
- **Tool síncrona longa:** com a tool parada por `threading.Event`, não há
  retorno da tool, settlement ou nova provider call. Após liberar o gate, o
  tool result precede o reminder e a segunda provider call ocorre normalmente.
- **Leaf enfileirada:** pode receber o texto antes de começar; cancel antes da
  leitura produz `discarded` exatamente uma vez.

Resultado: entrega entre iterações confirmada; preempção refutada.

### 6.2 Idle, terminal, erro e cancel

- `steer_active` recusa uma sub-sessão terminal e não cria follow-up;
- depois de cancel, recusa;
- accepted seguido de cancel não cria nova chamada e settle é `discarded`;
- erro no turno descarta o steer e steers posteriores são recusados;
- o `steer()` genérico continua capaz de criar turno idle, mas não é exposto como
  workflow tool.

Resultado negativo preservado: steering não recupera uma leaf que já parou.
Isso é propriedade, não deficiência.

### 6.3 Races de identidade, completion e cancel

A validação ocorre em duas etapas deliberadas:

1. service compara `segment_id/attempt/turn` com o snapshot;
2. `OrchestrationCore.steer_active(expected_causal=...)` compara novamente sob
   o lock que verifica atividade e enfileira.

Stale segment, attempt e turn falham antes do orçamento. A segunda comparação
fecha a janela snapshot → enqueue. Completion/cancel que ganham a corrida
recusam ou descartam; acceptance nunca promete delivery. Callbacks síncronos de
settlement são estacionados até `accepted` ser auditado, preservando a ordem
`accepted` antes de `read/discarded`.

### 6.4 Budget, restart e contenção

- três reservas lidas são aceitas; a quarta retorna `exhausted/run_limit` e
  `run_used=3`;
- close/reopen do SQLite preserva o contador;
- oito threads disputando limite 1 produzem exatamente um vencedor;
- duas instâncias de `SessionDB` sobre o mesmo arquivo também produzem um único
  vencedor;
- duplicate settlement não decrementa duas vezes;
- exaustão é resposta visível e evento auditado, não loop nem retry.

Resultado: a não-convergência termina de forma finita e inspecionável.

### 6.5 Prompt congelado

`tests/test_loop_inbox.py::test_sup03_one_frozen_prompt_across_four_turns`
verifica first turn, resume, steering e compaction. Objeto e conteúdo do prompt
permanecem iguais; o reminder vive somente nas mensagens.

Resultado: H1 não foi falsificada pela invariante de prompt.

### 6.6 Comparação econômica determinística

`tests/test_workflow_steering_comparison.py` fixa uso explícito por resposta e o
mesmo sentinel de qualidade (`CORRECT`):

| Estratégia | Provider calls | Tools executadas | Tokens totais do fixture | Resultado |
| --- | ---: | ---: | ---: | --- |
| Fresh correto desde o início | 1 | 0 | 45 | `CORRECT` |
| Steering entre iterações | 2 | 1 | 240 | `CORRECT` |
| Cancel + respawn corrigido | 3 no total | 2 | 350 | `CORRECT` |

No cenário construído, steering evita repetir uma tool e economiza 110 tokens
contra cancel-and-respawn. Mas o fresh correto é muito mais barato que ambos.
Logo, **H4 foi apenas confirmada para este fixture**, não universalmente.

**Negativo/limitação:** não houve benchmark live-model controlado de qualidade
ou convergência. O fixture demonstra accounting, ordering e igualdade de um
sentinel; não mede qualidade semântica. Não há base para prometer que steering
melhora uma resposta real ou sempre custa menos. Para erro estrutural, cancelar
e reautorizar continua preferível.

### 6.7 Segurança de superfície

- `workflow_steer` é interceptada e parent-only;
- delegate/subagent exclui a tool;
- texto inválido ou coordenadas inválidas falham antes do service;
- nenhum conteúdo de instrução entra no audit;
- guidance, schema da tool e skill repetem alvo, timing, limites e lifecycle.

## 7. Resultados negativos consolidados

1. `run_id + sub_id` não é contrato suficiente: H2 foi **refutada**. É preciso
   exigir `segment_id + attempt + turn` e revalidar atomicamente.
2. Accepted não significa delivered: H3 foi **refutada**. Completion, cancel,
   erro e shutdown podem produzir `discarded`.
3. Steering genérico/idle foi rejeitado; a API ampla não é superfície de
   workflow.
4. Steering não preempta provider nem tool longa. Não existe “interromper agora”.
5. Cache replay, estado durable-only e run de outro processo não são alvos.
6. Um crash entre reserve e settlement pode deixar slot gasto; não há reclaim
   automático porque seria fail-open.
7. Não houve benchmark live-model controlado; os números 45/240/350 são fixture
   determinístico, não pricing nem garantia de qualidade.
8. Fresh correto foi mais barato que steering; direção em voo não substitui boa
   autoria inicial.
9. Contadores per-leaf não sobrevivem a restart; somente o teto global do run é
   durável. Isso pode permitir distribuição diferente dos até três slots após
   resume, nunca exceder o teto global.

## 8. Testes de contrato anti-drift

Superfícies protegidas:

- `tests/test_workflow_supervision_steering.py`: guidance, skill, limites,
  vocabulário fechado, causas sanitizadas e teto de 800 linhas da skill;
- `tests/test_workflow_steer_tool.py`: schema exato, validação, dispatch e
  exclusão de subagentes;
- `tests/test_workflow_service_steering.py`: gates, stale identity, lifecycle,
  races, audit e orçamento durável no service;
- `tests/test_workflow_steering_durable.py`: SQLite, reopen e contenção;
- `tests/test_orchestration_core.py` e
  `tests/test_orchestration_steer_identity.py`: active-only, first-turn,
  busy/queued/tool/cancel/error e compare-under-lock;
- `tests/test_loop_inbox.py`: ordering e prompt congelado;
- `tests/test_workflow_steering_comparison.py`: convergência e accounting do
  fixture comparativo.

Reprodução focada:

```bash
cd backend
env -u OPENROUTER_API_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY \
  python -m pytest -q --no-cov \
  tests/test_orchestration_core.py \
  tests/test_orchestration_steer_identity.py \
  tests/test_workflow_service_steering.py \
  tests/test_workflow_steer_tool.py \
  tests/test_workflow_steering_durable.py \
  tests/test_workflow_supervision_steering.py \
  tests/test_loop_inbox.py \
  tests/test_workflow_steering_comparison.py
```

Validação final em 2026-08-30:

- suíte focada acima: **91 passed**;
- suíte completa hermética (credenciais de providers removidas do ambiente):
  **2258 passed em 42,07 s**, cobertura total **95%**;
- `ruff check .`: limpo;
- `git diff --check`: limpo.

## 9. Classificação das hipóteses

| Hipótese | Resultado |
| --- | --- |
| H1 — contrato estreito suficiente | **Confirmada no escopo mecânico testado**: local, exact-occurrence, queued, entre iterações, bounded e auditado |
| H2 — `run_id + sub_id` basta | **Refutada**: coordenadas causais completas e compare-under-lock são necessários |
| H3 — accepted = delivered | **Refutada**: lifecycle separa `accepted`, `read` e `discarded` |
| H4 — steering sempre mais econômico | **Reformulada**: 240 < 350 no fixture com trabalho repetido, mas fresh=45; sem benchmark live universal |
| H5 — budget só em memória basta | **Refutada**: restart recarregaria o run; teto global passou a ser durável |

## 10. Conclusão global — **REFORMULADA**

A pergunta ampla “a agente pode direcionar um workflow em voo?” foi
**reformulada** para uma capacidade muito menor:

> A agente pode enfileirar **uma correção causal pequena** para **uma ocorrência
> exata, local e ainda ativa**, usando a identidade que acabou de observar. A
> aceitação só promete fila. O texto pode ser lido no próximo boundary ou
> descartado. A ação compartilha os freios SUP-01, tem tetos menores e deixa
> recibos metadata-only.

Não há contrato para steering idle, cross-process, cache replay, preempção,
alteração de DAG, mudança estrutural de prompt/spec ou convergência garantida.
Esses casos continuam em cancel + reexecução corrigida ou escalada humana.

O enforcement mínimo no harness provou-se necessário — texto sozinho não fecha
TOCTOU, budget cross-restart nem lifecycle. Ao mesmo tempo, a doutrina continua
necessária — o harness não sabe se a correção é pequena, causal, reversível ou
se houve progresso. A alternativa vencedora é, portanto, **enforcement estreito
+ doutrina**, não um dos dois isoladamente.
