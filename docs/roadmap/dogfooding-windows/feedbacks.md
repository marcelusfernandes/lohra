# Observações da execução de workflows do Lohra

Data da observação: 26–27 de agosto de 2026
Contexto: implementação da Fase 2 da issue ⁠ #636 ⁠ no repositório Marvinz, após a Fase 1 da toolbar de formatação Markdown.

## Objetivo da avaliação

Avaliar se o Lohra realmente cria e executa workflows DAG adaptativos, em vez de apenas receber instruções sequenciais de um orquestrador externo. A configuração solicitada foi:

•⁠  ⁠Sol para orquestração, análise, revisão e decisão final;
•⁠  ⁠Luna para implementação, testes e pesquisa somente quando necessária;
•⁠  ⁠timeouts altos por folha;
•⁠  ⁠nenhum teto artificial de tokens;
•⁠  ⁠validação e revisão antes de considerar cada fase concluída.

Este documento separa fatos observados de inferências e recomendações.

## Evidências de execução

### Run inicial — ⁠ b4bc37cf2afd4032ae82808c1addc330 ⁠

Nome: ⁠ issue-636-phase-2-toolbar ⁠

Estado final: ⁠ degraded ⁠

Orçamento de tokens: ausente (⁠ token_budget: null ⁠)
Uso registrado: 324.456 tokens de entrada e 13.165 tokens de saída.

O Sol criou um DAG declarativo com dez nós principais:

⁠ text
semantics_recon (Sol) ─┐
                       ├─> implement (Luna)
scope_test_recon (Sol) ┘       ├─> focused_validation (Luna)
                               ├─> adversarial_review (Sol)
                               └─> remediate (Luna)
                                      └─> release_validation (Luna)
                                             └─> release_claim (Sol)
                                                    └─> verify_release_claim (3 céticos)
                                                           └─> final_judgment (Sol)
 ⁠

Os dois nós de reconhecimento eram independentes e foram executados como fan-out. O run também usou o nó nativo ⁠ verify ⁠, com três céticos e lentes diferentes, para tentar refutar a alegação de release.

Os dois reconhecimentos concluíram. O nó ⁠ implement ⁠, contudo, terminou com:

⁠ text
leaf error: max_iterations (50) reached without a final response
 ⁠

Como a saída de ⁠ implement ⁠ ficou nula, os nós dependentes receberam falhas derivadas de ⁠ upstream null ⁠. O mecanismo tentou novamente parte do caminho e repetiu os mesmos diagnósticos no histórico de falhas; o run não chegou à validação, revisão final ou decisão de release.

### Run de recuperação — ⁠ 0052a80afda74c53902ba6278a464d24 ⁠

Nome: ⁠ issue-636-phase-2-recovery ⁠
Estado observado: em execução, bloqueado na folha de implementação durante a observação.

Após analisar o erro anterior, o Sol não retomou cegamente o primeiro run. Ele criou um DAG novo, com outro padrão de controle:

⁠ text
inspect_partial (Sol)
  └─> implement_recovery (Luna)
         └─> focused_validation (Luna)
                └─> adversarial_review_gate (gate Sol, até 2 tentativas)
                       └─> remediate_review (Luna)
                              └─> release_validation (Luna)
                                     └─> final_sol_judgment (Sol)
 ⁠

O Sol escolheu uma cadeia serial, não fan-out, porque a mudança está concentrada em um componente, CSS e seus testes. Essa escolha evita concorrência de escrita sobre os mesmos arquivos. O run substituiu o ⁠ verify ⁠ do primeiro fluxo por um ⁠ gate ⁠ nativo: revisão Sol com critério de aceitação e caminho de correção Luna.

O nó ⁠ inspect_partial ⁠ concluiu e liberou a Luna. A folha ⁠ implement_recovery ⁠ tinha timeout de 1.200 s, instrução de terminar em até 25 chamadas de ferramenta e um único comando de teste focado. Apesar disso, ela continuou ativa muito além do timeout nominal, sem publicar resultado, erro estruturado ou transição para ⁠ focused_validation ⁠ durante a janela observada.

## O que funcionou bem

### 1. O Lohra criou DAGs nativos, não apenas delegações narrativas

Há evidência persistida de especificações declarativas, ⁠ run_id`s, tipos de nós, dependências e estados por nó. Os runs usaram recursos nativos do motor, incluindo referências entre saídas ( ⁠${node.field}⁠ ), execução de folhas, `verify ⁠ e ⁠ gate ⁠.

Isso é diferente do fluxo da Fase 1, que foi uma sequência externa de chamadas Luna/Sol coordenada manualmente.

### 2. O padrão do DAG se adaptou à situação

O primeiro run usou reconhecimento independente em fan-out e verificação adversarial por painel de céticos. Após falhar, o Sol escolheu um workflow de recuperação serial com inspeção prévia, uma única escritora, revisão em ⁠ gate ⁠ e remediação. A troca de padrão é justificável pelo estado parcial do worktree e pelo risco de concorrência em arquivos compartilhados.

### 3. A separação de papéis e modelos foi respeitada na especificação

Os nós de arquitetura, revisão adversarial e julgamento final usaram ⁠ gpt-5.6-sol ⁠; implementação, testes e validação usaram ⁠ gpt-5.6-luna ⁠. Essa divisão permite concentrar o modelo mais caro/robusto nos pontos onde julgamento independente tem maior valor.

### 4. O reconhecimento técnico foi aprofundado e acionável

O nó ⁠ semantics_recon ⁠ verificou fontes reais do projeto e das dependências. Entre os achados relevantes:

•⁠  ⁠Inline Code não deve usar apenas ⁠ toggleMark ⁠: o comando Milkdown equivalente rejeita cursor sem seleção, remove outras marcas ao aplicar ⁠ inlineCode ⁠ e remove apenas code ao desfazer.
•⁠  ⁠H1–H3 são o nó ⁠ heading ⁠ com atributo ⁠ level ⁠, não três tipos de nó diferentes.
•⁠  ⁠O estado ativo de blocos precisa avaliar toda a seleção e ignorar o atributo de ID gerado automaticamente em headings.
•⁠  ⁠⁠ setBlockType ⁠ deve controlar disponibilidade por dry-run, independentemente de o bloco estar visualmente ativo.
•⁠  ⁠A preservação da seleção depende de prevenir ⁠ mousedown ⁠ em cada botão, além de devolver foco ao ⁠ EditorView ⁠ depois do dispatch.

Esses achados reduzem risco real de regressões e mostram que os agentes não se limitaram a inferir comportamento por nomes de API.

### 5. O workflow de recuperação tratou o erro anterior como dado de planejamento

O segundo spec incluiu contenção explícita: trabalho cirúrgico, um único comando de teste, sem navegação desnecessária, limite de ferramentas por instrução e arquivos permitidos. Também corrigiu o escopo autoritativo para os oito controles totais da Fase 2.

## Problemas observados

### 1. Limite interno de iterações pode invalidar todo o ramo dependente

O run inicial falhou porque a Luna atingiu ⁠ max_iterations (50) ⁠ sem responder ao contrato de saída. O motor propagou ⁠ upstream null ⁠ para todos os descendentes, produzindo várias falhas secundárias pouco informativas.

Impacto:

•⁠  ⁠não houve validação independente do código possivelmente alterado;
•⁠  ⁠não houve julgamento final;
•⁠  ⁠o histórico de falhas ficou repetitivo;
•⁠  ⁠uma única folha longa derrubou a maior parte do DAG.

### 2. O timeout declarado por nó não foi aplicado de forma observável

No recovery run, ⁠ implement_recovery ⁠ tinha ⁠ timeout: 1200 ⁠, mas permaneceu ativo muito além desse prazo. Não houve cancelamento, estado ⁠ paused ⁠, erro explícito, fallback ou liberação dos dependentes durante a observação.

Impacto:

•⁠  ⁠o run deixa de ter previsibilidade temporal;
•⁠  ⁠o processo pode consumir recursos indefinidamente;
•⁠  ⁠o campo ⁠ timeout ⁠ no spec não oferece, nesta execução, uma garantia operacional confiável;
•⁠  ⁠o operador não consegue distinguir facilmente trabalho legítimo de folha travada.

### 3. O escopo de um nó de reconhecimento divergiu do objetivo autorizado

O objetivo autoritativo da Fase 2 era adicionar Inline Code, Paragraph e H1–H3, preservando os três controles já existentes. Porém, o ⁠ scope_test_recon ⁠ do primeiro run recomendou também listas e quote, porque tomou a issue completa como referência.

O prompt de implementação preservava o escopo menor, mas a divergência entre entradas aumenta a chance de escopo indevido, de uso excessivo de ferramentas e de implementação ambígua.

### 4. A recuperação não tem um caminho de falha bem tipado

O motor sabe que uma folha falhou, mas o workflow subsequente não recebeu um objeto estruturado com causa, alterações parciais, logs resumidos e estratégia de recuperação. O primeiro run tornou os dependentes inválidos por referência nula, em vez de encaminhar o erro para um nó de triagem/recuperação.

### 5. A observabilidade externa foi frágil

O comando de chat iniciou processos em segundo plano e retornou apenas a indicação de uso da assinatura antes de o resultado estruturado estar disponível. Para acompanhar o run, foi necessário consultar o estado persistido localmente. Essa consulta encontrou contenção no banco SQLite enquanto o motor escrevia o estado.

Impacto:

•⁠  ⁠não há feedback contínuo e confiável para o usuário;
•⁠  ⁠um operador externo não recebe naturalmente eventos de início, progresso, finalização ou erro por nó;
•⁠  ⁠o JSON final do chat não é uma interface adequada para workflows longos ou que se desprendem do processo chamador.

### 6. A instrução textual de “até 25 ferramentas” não é uma garantia do executor

No recovery spec, a contenção de chamadas foi colocada no prompt da Luna, não em um campo validado e imposto pelo motor. Se a folha ignorar ou não conseguir cumprir a instrução, não há um limite operacional equivalente ao erro ⁠ max_iterations ⁠ observado no primeiro run.

### 7. Uma folha de implementação ainda é grande demais como unidade de recuperação

Mesmo com escopo limitado, ⁠ implement_recovery ⁠ precisava entender estado parcial, editar componente/CSS/testes, remover artefatos e rodar teste. Essa unidade combina diagnóstico, escrita e validação suficiente para ficar vulnerável a loops de ferramentas e respostas tardias.

## Oportunidades de melhoria

### Especificação e autoria de workflows

•⁠  ⁠Criar um nó inicial de *contrato de escopo* com saída estruturada: controles permitidos, controles proibidos, arquivos permitidos e critérios de aceite. Todos os demais nós devem receber essa saída, não uma interpretação livre da issue.
•⁠  ⁠Adicionar tipos de saída de erro estruturados, por exemplo ⁠ LEAF_FAILURE ⁠, com ⁠ reason ⁠, ⁠ partial_changes ⁠, ⁠ commands_run ⁠, ⁠ last_tool ⁠, ⁠ retryable ⁠ e ⁠ recommended_recovery ⁠.
•⁠  ⁠Modelar dependências de falha: um descendente não deveria receber ⁠ ${implement.output} ⁠ quando ⁠ implement ⁠ falha; deveria seguir uma aresta explícita de recuperação ou encerrar o run com um rollup conciso.
•⁠  ⁠Salvar templates especializados, como ⁠ code-change-with-review-gate ⁠ e ⁠ recover-stalled-implementation ⁠, na biblioteca de workflows validados.

### Motor de execução

•⁠  ⁠Implementar ⁠ max_tool_calls ⁠ ou ⁠ max_iterations ⁠ como campo de nó validado pelo schema, não como recomendação em prompt.
•⁠  ⁠Aplicar timeout em dois níveis: watchdog do motor e deadline/cancelamento da chamada do provedor. O cancelamento precisa produzir estado terminal durável (⁠ timed_out ⁠ ou ⁠ paused ⁠) e liberar a execução.
•⁠  ⁠Após timeout ou limite de ferramentas, executar uma folha curta de triagem Sol, em vez de propagar referências nulas para toda a árvore.
•⁠  ⁠Fazer retry com estratégia declarada: reduzir escopo, trocar de modelo, limitar ferramentas ou partir a tarefa; nunca apenas repetir silenciosamente o mesmo caminho.
•⁠  ⁠Evitar repetição de mensagens ⁠ upstream null ⁠ no histórico. Um rollup deve apontar uma causa raiz e os nós afetados.

### Decomposição do trabalho de código

Uma recuperação mais robusta poderia usar um pipeline serial menor:

⁠ text
contrato de escopo (Sol)
  → implementação de comandos e estado (Luna)
    → testes do componente e acessibilidade (Luna)
      → revisão adversarial (Sol)
        → correção focada, se necessária (Luna)
          → validação de release (Luna)
            → decisão (Sol)
 ⁠

Os dois nós Luna de escrita devem permanecer seriais quando alteram os mesmos arquivos. A separação reduz o volume de contexto e o número de chamadas de ferramentas exigido por cada folha.

### Observabilidade e operação

•⁠  ⁠Disponibilizar ⁠ workflow_status ⁠ e ⁠ workflow_list ⁠ fora da sessão original, com acesso seguro por ⁠ run_id ⁠ e profile.
•⁠  ⁠Emitir eventos persistidos por nó: ⁠ queued ⁠, ⁠ running ⁠, ⁠ tool_call ⁠, ⁠ completed ⁠, ⁠ failed ⁠, ⁠ timed_out ⁠, ⁠ retried ⁠ e ⁠ skipped ⁠.
•⁠  ⁠Incluir no status o início real, deadline, duração acumulada, última atividade e motivo de espera.
•⁠  ⁠Fazer o CLI de chat devolver imediatamente ⁠ session_id ⁠ e ⁠ run_id ⁠, além de permitir acompanhar os eventos sem depender de consultas diretas ao banco.
•⁠  ⁠Tratar leituras de estado como concorrentes de primeira classe: WAL, snapshot consistente, timeout curto real e retorno explícito de ⁠ busy ⁠, em vez de bloqueio indeterminado.
•⁠  ⁠Exibir no resumo final quais nós executaram de fato, quais foram apenas planejados e quais foram pulados por dependência ou falha.

## Prioridades recomendadas

| Prioridade | Problema | Mudança recomendada | Critério de sucesso |
| --- | --- | --- | --- |
| P0 | Timeout não efetivo | Watchdog com cancelamento real de folha e estado terminal durável | Um nó de 10 s não permanece ⁠ running ⁠ após o deadline; os dependentes recebem estado coerente |
| P0 | Falha monolítica de implementação | Campo imposto ⁠ max_tool_calls ⁠ e saída de falha estruturada | Uma folha que atinge o limite produz diagnóstico, não referências nulas em cascata |
| P1 | Escopo divergente | Contrato de escopo estruturado e obrigatório antes da implementação | Nenhum nó recomenda ou implementa controles fora da lista autorizada |
| P1 | Recuperação pouco explícita | Arestas/políticas de falha e template de recuperação | Falha de implementação aciona triagem e replanejamento sem duplicar erros |
| P1 | Baixa observabilidade | Eventos por nó e acompanhamento por ⁠ run_id ⁠ fora da sessão | Um operador acompanha progresso sem consultar SQLite diretamente |
| P2 | Folhas grandes | Pipeline serial com unidades menores de escrita/teste | Cada folha finaliza dentro de limites previsíveis e deixa saída verificável |
| P2 | Avaliação de padrões | Catálogo de templates e ledger de resultados por padrão | O sistema escolhe padrões com base em evidência histórica, não só no prompt atual |

## Insights para avaliações futuras

1.⁠ ⁠*Verificar o spec não basta.* A presença de um DAG declarativo, ⁠ verify ⁠ ou ⁠ gate ⁠ prova capacidade de autoria, mas não prova que o executor aplica deadlines, retries e cancelamentos corretamente.
2.⁠ ⁠*Distinguir “planejado” de “executado”.* O primeiro run planejou validação, remediação e julgamento, mas nenhum deles executou após a falha da implementação.
3.⁠ ⁠*Exigir uma trilha de recuperação.* Workflows de alteração de código precisam demonstrar, em testes, o que acontece quando uma folha se esgota, falha ou fica sem resposta.
4.⁠ ⁠*Medir a coerência de escopo entre nós.* Um DAG pode ter bons papéis e ainda produzir inputs contraditórios. O contrato de escopo deve ser um artefato compartilhado e verificável.
5.⁠ ⁠*Usar paralelismo somente onde não há conflito de escrita.* Fan-out é valioso para análise independente; mudanças no mesmo conjunto de arquivos devem continuar seriais.
6.⁠ ⁠*Preferir crítica adversarial após evidência concreta.* O ⁠ verify ⁠ com céticos é útil para alegações de release. Um ⁠ gate ⁠ com correção é mais adequado quando existe um artefato de código que precisa ser revisado e eventualmente alterado.
7.⁠ ⁠*Tratar observabilidade como parte do produto.* Um workflow longo sem eventos e sem timeout efetivo é difícil de operar mesmo que sua estrutura de DAG seja sofisticada.

## Conclusão

O Lohra demonstrou capacidade real de criar e iniciar DAGs com padrões distintos e adaptativos: fan-out de pesquisa, verificação adversarial, workflow de recuperação e gate de revisão/correção. A qualidade do planejamento e da investigação técnica foi alta.

O principal risco não está na autoria do workflow, mas em sua execução confiável: folhas podem exceder limites de iteração, timeouts não foram aplicados de maneira observável, e falhas não seguem um caminho de recuperação estruturado. Antes de confiar no Lohra para mudanças autônomas de código de ponta a ponta, devem ser priorizados cancelamento/delineamento de timeout, limites de ferramenta impostos pelo motor, propagação de falhas tipada e observabilidade por ⁠ run_id ⁠.