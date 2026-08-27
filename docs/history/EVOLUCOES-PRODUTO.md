# Lohra — Evoluções de Produto

> Levantamento gerado pelo próprio agente Lohra (sessão `d1e0d0fff2d040f3b9998a8ea088d216`, modelo `gpt-5.6-terra`, 2026-08-26).  
> Contexto: roadmap fases 0-10 completas, fase 7 parcial (orquestração), CC-Parity completo, arquitetura 3 camadas (Tauri+React, FastAPI, Python core).

A Lohra já possui uma base técnica incomum para um agente desktop: núcleo Python extensível, desktop nativo, workflows duráveis, orquestração, múltiplos providers, perfis, skills, memória e controles de segurança. As oportunidades prioritárias agora são transformar essa capacidade em uma experiência mais compreensível, confiável e adotável por pessoas que não acompanharam sua evolução técnica.

---

## Experiência do Usuário

### 1. Onboarding guiado por objetivo
**Prioridade: alta**

Criar um fluxo inicial que pergunte o que a pessoa quer fazer — programar, pesquisar, automatizar tarefas, trabalhar com arquivos ou operar projetos — e configure provider, permissões, perfil e sugestões de skills conforme a resposta. Isso reduz a barreira atual de entender modelos, API keys, perfis e ferramentas antes de obter o primeiro resultado útil.

O onboarding deve terminar em uma tarefa concreta executada com o usuário, não apenas numa tela de configurações.

### 2. Centro de controle de permissões e atividade
**Prioridade: alta**

Adicionar uma área visual que mostre, por sessão e por agente/subagente, quais ferramentas foram usadas, arquivos lidos ou modificados, comandos executados, sites acessados e permissões concedidas. O usuário deve poder aprovar, negar, revogar ou restringir escopos persistentes de forma clara.

A infraestrutura de approval gate e sandbox já cria uma base; o produto precisa tornar esses controles visíveis e auditáveis para inspirar confiança.

### 3. Workspace de tarefas e artefatos
**Prioridade: alta**

Transformar a conversa em um workspace que agrupe arquivos gerados, imagens, relatórios, resultados de pesquisas, execuções de terminal e workflows associados a uma tarefa. Em vez de o resultado ficar disperso em mensagens e paths locais, o usuário teria uma visão organizada do que foi produzido e poderia abrir, exportar ou continuar cada artefato.

Isso aproxima a Lohra de uma ferramenta de trabalho, e não apenas de um chat com tools.

### 4. Histórico pesquisável e continuidade de trabalho
**Prioridade: média**

Oferecer uma interface de busca unificada sobre conversas, sessões, artefatos e decisões tomadas, com filtros por projeto, perfil, data, provider e tipo de atividade. A memória persistente existe, mas o usuário precisa conseguir localizar e retomar trabalhos anteriores sem depender de lembrar em qual chat eles ocorreram.

Também vale oferecer "retomar esta tarefa" com um resumo explícito do contexto que será carregado.

### 5. Modos de interação: rápido, assistido e autônomo
**Prioridade: média**

Apresentar modos claros no desktop: **Rápido** para respostas diretas, **Assistido** para pedir confirmação antes de ações relevantes, e **Autônomo** para executar um plano dentro de limites definidos. Isso traduz capacidades técnicas como ferramentas, approvals, orquestração e workflows em escolhas simples de produto.

Cada modo deve explicar custo, autonomia e risco esperado antes da execução.

---

## Capacidades do Agente

### 6. Planejamento visível, editável e executável
**Prioridade: alta**

Antes de tarefas longas, a Lohra deveria propor um plano com etapas, dependências, critérios de conclusão, ferramentas previstas e estimativa de custo/tempo. O usuário poderia editar, reordenar, desabilitar passos ou aprovar apenas parte do plano antes da execução.

Isso reduz a sensação de "caixa-preta" e aproveita o harness de workflows sem exigir que usuários escrevam uma spec declarativa.

### 7. Automação por linguagem natural com prévia segura
**Prioridade: alta**

Permitir pedidos como "toda segunda, revise meus tickets e gere um resumo" ou "quando este arquivo mudar, rode os testes e me avise", convertendo-os em automações revisáveis. Antes de ativar, a interface deve mostrar gatilho, frequência, ferramentas, dados acessados, destino do resultado e uma simulação da primeira execução.

Os jobs cron atuais são uma fundação, mas o produto precisa ampliar gatilhos, visibilidade e segurança operacional.

### 8. Agente de projeto com contexto persistente explícito
**Prioridade: alta**

Evoluir a consciência de projeto para uma experiência em que o usuário conecte um repositório e veja claramente instruções detectadas, skills disponíveis, convenções, comandos de validação e estado atual do trabalho. A Lohra poderia manter um "cartão do projeto" editável, contendo objetivos, stack, comandos frequentes e restrições adicionais escolhidas pelo usuário.

Isso torna a ferramenta especialmente valiosa para desenvolvimento contínuo, em vez de depender apenas da descoberta implícita de `AGENTS.md` e `CLAUDE.md`.

### 9. Revisão de mudanças e pull requests orientada a risco
**Prioridade: média**

Criar um fluxo de revisão que analise alterações locais, commits ou pull requests e apresente: resumo, impacto por área, riscos, testes ausentes, possíveis regressões e sugestões de patch. O usuário deveria poder escolher o nível de profundidade — revisão rápida, segurança, performance, arquitetura ou cobertura de testes.

A Lohra já tem ferramentas e orquestração suficientes para produzir revisões multi-perspectiva; falta empacotar isso como caso de uso principal.

### 10. Conectores de conhecimento pessoal e de trabalho
**Prioridade: média**

Adicionar integrações opt-in com fontes como GitHub, GitLab, Google Drive, Notion, Slack, Linear/Jira e calendários, inicialmente com leitura e escopo mínimo. O valor está em permitir perguntas e tarefas úteis sobre o contexto real do usuário: "o que bloqueia esta entrega?", "resuma as decisões desta semana" ou "prepare meu status update".

As integrações devem usar credenciais por conector, permissões granulares e uma explicação clara de quais dados entram no contexto do agente.

---

## Plataforma e Infraestrutura

### 11. Sincronização opcional e backup criptografado
**Prioridade: média**

Oferecer backup e sincronização opcional de sessões, skills, perfis, automações e configurações entre computadores, com criptografia ponta a ponta e chaves controladas pelo usuário. O modo local-first deve continuar sendo o padrão, mas usuários que alternam entre máquinas precisam de continuidade sem copiar manualmente `~/.lohra`.

Uma primeira versão pode focar em exportação/importação criptografada antes de lançar sincronização contínua.

### 12. Observabilidade de custo, desempenho e confiabilidade
**Prioridade: alta**

Criar um painel que explique uso por provider, modelo, projeto, workflow e automação: tokens, custo estimado, latência, falhas, pausas por budget e taxa de sucesso. Além de ajudar o usuário a controlar gastos, isso permite escolher modelos adequados por tipo de tarefa com base em dados reais.

Alertas configuráveis — por exemplo, orçamento mensal, workflow repetidamente falho ou credencial expirada — tornam a Lohra mais confiável no uso diário.

### 13. Atualizações de produto seguras e canais de release
**Prioridade: média**

Completar a distribuição desktop com atualização automática assinada, notas de versão úteis, rollback e canais estável/beta/nightly. O backend já possui um mecanismo de self-update, mas o produto desktop precisa de uma experiência única, verificável e multiplataforma para usuários não técnicos.

Isso é importante tanto para adoção quanto para distribuir correções de segurança rapidamente.

---

## Ecossistema e Adoção

### 14. Galeria de skills, workflows e automações reutilizáveis
**Prioridade: alta**

Criar uma galeria curada onde usuários possam descobrir, instalar e versionar skills e templates para casos concretos: revisão de PR, pesquisa competitiva, triagem de issues, preparação de reuniões, análise de planilhas e documentação. Cada item deve expor autor, permissões necessárias, providers compatíveis, custo típico, código/spec inspecionável e avaliações.

A segurança precisa ser central: instalação com revisão de conteúdo, proveniência e isolamento de permissões, evitando que o ecossistema se torne um vetor de prompts ou automações maliciosas.

### 15. Compartilhamento e colaboração de resultados
**Prioridade: baixa**

Permitir compartilhar uma conversa, relatório, plano ou resultado de workflow por link/exportação, com opções de remover dados sensíveis, congelar o conteúdo e definir validade. Para equipes, seria útil comentar ou continuar um artefato compartilhado sem necessariamente compartilhar toda a memória privada do autor.

Começar por exportações portáteis — Markdown, PDF, JSON e bundle de artefatos — reduz complexidade antes de colaboração em tempo real.

---

## Resumo de Prioridade — Apostas mais fortes para a próxima etapa

1. **Onboarding guiado** — reduzir fricção de entrada.
2. **Planejamento visível e workspace de artefatos** — tornar a autonomia controlável e o resultado tangível.
3. **Centro de permissões** — converter a arquitetura de segurança em confiança percebida.
4. **Agente de projeto** — estabelecer um caso de uso recorrente e diferenciador.
5. **Galeria de skills/workflows** — escalar utilidade e adoção além do time que constrói o produto.
6. **Observabilidade de custo** — para que workflows e múltiplos modelos sejam utilizáveis sem surpresa financeira.