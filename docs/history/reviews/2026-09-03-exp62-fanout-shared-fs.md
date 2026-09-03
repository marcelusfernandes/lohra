# Issue #62 — fan-out intra-nó sobre recurso compartilhado (experimento controlado)

**Data:** 2026-09-03 · **Código:** `main@9547cdc` (0.0.23), árvore limpa, nada editado ou commitado
**Escopo:** E6 do Relatório 2 de `docs/history/reviews/2026-09-02-wave8-investigation.md`; decide H2/H3 de #45 e a parte A de #42
**Artefatos:** `exp62/experiment.py` (script), `exp62/aggregate.py`, `exp62/results/*.jsonl` (300 repetições),
`exp62/results_resume/*.jsonl` (80 repetições com resume em todas), `exp62/summary.json`, `exp62/run.log`

> **Nota de processo:** o Acceptance Criteria da #62 pede o script em `docs/history/evidence/`. Esta investigação
> foi executada sob proibição explícita de editar o repo, então o script vive em `exp62/`. Movê-lo (e o relatório
> para `docs/history/reviews/`) é passo do dono, não meu.

---

## Sumário executivo (3 linhas)

1. **Dois writers no mesmo arquivo dentro de um `parallel` se destroem em silêncio**: **24/25 sob jitter**
   (25/25 quando o interleaving é forçado) terminaram com **perda de atualização**, `status: complete`,
   **zero faults**, e a célula do cache guardando as DUAS afirmações de sucesso — que replayam para sempre no
   resume, com zero spawns.
2. **O manifesto de artefato (0.0.21) não é remédio para isto**: ele **não é autorável em `parallel`** (o campo é
   aceito pelo validador e ignorado pelo engine) e, onde é autorável (`pipeline`), a detecção é uma **loteria de
   timing** entre dois pontos anti-correlacionados — e some por completo quando o manifesto declara só `path`
   (o único campo que o schema **exige**): **3/20 invisíveis sob jitter, 20/20 no pior caso** (writes simultâneos).
3. **A doutrina "um arquivo por leaf" (config D) se confirma para CONTEÚDO** (50/50 arquivos íntegros, jitter e
   forçado), **mas não para ORDEM**: o reader do mesmo `parallel` leu arquivo inexistente em **11/25 sob jitter**
   e isso virou uma resposta cacheada com `status: complete`.

---

## 1. Método

### 1.1 O que roda de verdade e o que é stub

| Camada | Real / stub |
|---|---|
| `WorkflowService` + `WorkflowEngine` + `OrchestrationCore` + pool de leaves | **real** (0.0.23) |
| `WorkflowPolicy` / `sandbox_dispatch` / `make_sandboxed_leaf_factory` | **real** (política construída à mão) |
| Fábrica de leaf | **real** — `lohra.agent.delegate.make_child_factory` (o mesmo caminho de produção: `subagent_dispatch(registry.dispatch)`) |
| Tools `read_file` / `write_file` / `terminal` | **reais** (registry, syscalls de verdade) |
| Cache de nó, manifesto de artefato, resume | **reais** (`workflow_node_cache`, `artifact.py`) |
| Provider (LLM) | **STUB** — `ScriptedLeafClient`, zero tokens, zero rede |

`LOHRA_HOME` aponta para `exp62/lohra_home`; `~/.lohra` real **não** foi lido nem escrito. `LOHRA_AUDIT=off`
(o ledger não é o instrumento deste experimento). Cada repetição tem seu próprio `home/`, `state.db` e diretório
compartilhado — nada vaza entre repetições.

### 1.2 O stub

`ScriptedLeafClient.create(**kwargs)` é **sem estado próprio**: tudo que ele decide sai das mensagens que recebe.

- **Identidade do leaf**: regex de um marcador (`[WRITER-A]`, `[WRITER-B]`, `[READER]`) e do path
  (`<PATH:/abs/...>`) no prompt do usuário. O path entra no spec como texto do prompt — é o que um autor real faria.
- **Índice da chamada**: número de blocos `tool_result` já presentes. (Descoberta empírica antes de escrever o
  stub: o transport anthropic **não** usa `role: "tool"` — um resultado chega como bloco `tool_result` dentro de
  uma mensagem `user`. A primeira versão do stub contava errado e todo leaf batia em `max_iterations`.)
- **Read-modify-write REAL**: na 2ª chamada o stub **usa o conteúdo que o `read_file` devolveu** e escreve
  `conteúdo_lido + minha_linha`. A perda de atualização, portanto, é um fato do sistema — não um roteiro.
  (Nota mecânica que importa: `write_file` **não tem modo append**. Somar algo a um arquivo pela tool de fs é,
  por construção da superfície, um read-modify-write.)

### 1.3 Como o interleaving é forçado — e por que há dois modos

| Modo | Mecanismo | O que ele mede |
|---|---|---|
| `jitter` | `sleep(uniform(0, 8ms))` entre o read e o write de cada writer | **frequência natural** sob concorrência real do pool |
| `barrier` | `threading.Barrier(2)` entre o read e o write dos dois writers | **alcançabilidade determinística** do pior caso |

**Os números do modo `barrier` NUNCA são reportados como taxa.** Eles respondem "isto é alcançável?", não
"com que frequência acontece?". As taxas vêm só do `jitter`.

### 1.4 Guarda de validade (o jeito mais provável do experimento mentir)

Cada repetição registra timestamps (`perf_counter`) de `read.done` e `write.request` por writer e calcula
`overlapped = (read_A < write_B) and (read_B < write_A)`. Se o pool tivesse serializado os leaves, a repetição
não teria medido nada e apareceria como `overlapped: false`. **Resultado: 297/300 repetições com sobreposição
confirmada** (as 3 sem sobreposição — 1 em `a-jitter`, 2 em `b2-jitter` — ficam em "não exercitado" no §7,
não são contadas como "seguro").

### 1.5 Classificador de desfecho (definido ANTES de rodar)

`both_AB | both_BA | lost_update_A | lost_update_B | torn | empty | other(...)`, derivado do conteúdo final do
arquivo. `torn` (escrita rasgada no meio de uma linha) **nunca apareceu** — ver §7.

### 1.6 Configurações

| Cfg | Nó | Writers | Reader | Política |
|---|---|---|---|---|
| **A** | `parallel` (3 branches) | `read_file` → `write_file` (RMW) no MESMO `shared.txt` | branch do mesmo nó | fs_allow rw, sem shell |
| **B1** | `parallel` (3) | `terminal`: `printf '%s\n' LINE >> shared.txt` (append, O_APPEND) | idem | `allow_terminal: true` |
| **B2** | `parallel` (3) | `terminal`: `cat` → `printf ... > shared.txt` (RMW no shell) | idem | `allow_terminal: true` |
| **C** | `pipeline` (items A,B × stages writer→reader) | RMW + devolve manifesto `{path, sha256, bytes}` | stage 2 do mesmo item | fs_allow rw |
| **C3** | idem C | RMW + manifesto **só com `path`** (o único campo `required` do schema) | idem | fs_allow rw |
| **D** | `parallel` (3) | `write_file` em **arquivos distintos** (`a.txt`, `b.txt`) | lê os dois | fs_allow rw |
| **C2** | probe de autoria | `schema_ref` + `model` numa **branch de `parallel`** | — | — |

`pipeline` foi escolhido para C/C3 por um motivo estrutural, não estético: **`parallel` não sabe validar schema
de branch** (§3, achado C2). O pipeline é o único fan-out cujo `cache_store` recebe `schema=` e portanto o único
onde o manifesto é medido.

---

## 2. Resultados

### 2.1 Desfecho do arquivo compartilhado (N=25 por célula)

| Cfg | Modo | N | ambas as linhas | perda de atualização | status | faults do run |
|---|---|---|---|---|---|---|
| A | jitter | 25 | **1** | **24** (13 A / 11 B) | 25× `complete` | **0** |
| A | barrier | 25 | 0 | **25** | 25× `complete` | **0** |
| B1 (append shell) | jitter | 25 | **25** | 0 | 25× `complete` | 0 |
| B1 (append shell) | barrier | 25 | **25** | 0 | 25× `complete` | 0 |
| B2 (RMW shell) | jitter | 25 | 0 | **25** | 25× `complete` | **0** |
| B2 (RMW shell) | barrier | 25 | 0 | **25** | 25× `complete` | **0** |
| C (manifesto completo) | jitter | 25 | 0 | **25** | 25× `complete` | 3 (advisory) |
| C (manifesto completo) | barrier | 25 | 0 | **25** | 25× `complete` | 25 (advisory) |
| C3 (manifesto só `path`) | jitter | 25 | 0 | **25** | 25× `complete` | **0** |
| C3 (manifesto só `path`) | barrier | 25 | 0 | **25** | 25× `complete` | **0** |
| D (arquivos distintos) | jitter | 25 | **25 íntegros** | 0 | 25× `complete` | 0 |
| D (arquivos distintos) | barrier | 25 | **25 íntegros** | 0 | 25× `complete` | 0 |

Nenhum run em nenhuma configuração terminou diferente de `complete`. **A perda de atualização nunca degradou um run.**

### 2.2 O que o reader (irmão no mesmo fan-out) enxergou

| Cfg | Modo | estado final correto | estado **intermediário** (vazio ou 1 linha de 2) | erro (arquivo ausente) |
|---|---|---|---|---|
| A | jitter | 19/25 | **6/25** (vazio) | 0 |
| B1 | jitter | 7/25 | **18/25** (vazio) | 0 |
| B1 | barrier | 24/25 | **1/25** (1 de 2 linhas) | 0 |
| B2 | jitter/barrier | 0/50 | **50/50** (vazio — leu antes de qualquer write chegar) | 0 |
| D | jitter | 14/25 | — | **11/25** (um ou os dois arquivos ainda não existiam) |
| D | barrier | 25/25 | — | 0 |

**Um reader dentro do mesmo `parallel` não tem nenhuma relação de happens-before com os writers.** O que ele
leu — inclusive "não existe" — vira output do leaf, entra na célula do nó e replaya no resume como resposta boa.

### 2.3 Manifesto de artefato: os dois pontos de detecção (N=20, resume em TODAS as repetições)

| Cfg | Modo | fault advisory **durante** o run | `artifact_changed` no **resume** (re-spawn) | **união** (detectado em algum ponto) | **invisível ponta a ponta** |
|---|---|---|---|---|---|
| C (`{path,sha256,bytes}`) | jitter | **4/20** | 16/20 | 20/20 | **0/20** |
| C | barrier | 19/20 | 1/20 | 20/20 | **0/20** |
| C3 (só `path`) | jitter | **0/20** | 17/20 | 17/20 | **3/20** |
| C3 (só `path`) | barrier | **0/20** | **0/20** | **0/20** | **20/20** |

Três mecânicas explicam a tabela inteira:

- **O fault advisory compara a alegação do leaf com a medição do harness.** Ele só acusa quando o irmão
  sobrescreveu o arquivo **antes** de o `cache_store` do perdedor medir. Com jitter isso é raro (4/20); com os
  writes simultâneos é a regra (19/20). **É uma corrida, não uma verificação.**
- **A mensagem do fault diagnostica errado.** Ela diz *"the leaf claimed sha256 X but the harness measured Y"* —
  isto é, "o leaf mentiu sobre o próprio hash". A causa real é "o irmão sobrescreveu o teu arquivo". Nenhuma
  palavra sobre concorrência aparece em lugar nenhum.
- **Os dois pontos são anti-correlacionados.** Quando o fault dispara, as duas células guardaram a medição do
  arquivo **final** — o recheck do resume então confirma as duas como válidas (0 re-spawns) e a perda fica
  **congelada no cache**. Quando o fault não dispara, a célula do perdedor guardou um sha **obsoleto** — e aí o
  recheck acusa `changed` e re-spawna. Nunca os dois; frequentemente a sorte.
- **C3 (`path` sozinho) é o caso que importa**, porque `sha256`/`bytes` **não são obrigatórios no schema**
  (`MANIFEST_SCHEMA.required == ["path"]`) e um LLM real não computa sha256 confiável do que acabou de escrever.
  Sem a alegação não há divergência para acusar; e quando os dois writes são simultâneos as duas células medem o
  **mesmo** arquivo final, o recheck bate, e o dano é **invisível de ponta a ponta em 20/20**.

### 2.4 Cache: o que fica gravado e o que o resume afirma

- **`parallel` (A, B1, B2, D): UMA célula para o nó inteiro**, com a lista dos outputs das 3 branches — incluindo
  as duas afirmações `"claim": "appended LINE-FROM-WRITER-X"` com `bytes_written: 19` cada, e a leitura
  intermediária do reader. `artifact_verification` é **NULL** em 100% das células (`run_parallel` chama
  `cache_store` **sem** `schema=` — não há medição nenhuma).
- **Resume da config A: `resume_spawns == 0`**, `status: complete`, `faults: []`, conteúdo do arquivo inalterado
  (1 linha). Ou seja: **o run replaya, para sempre e de graça, a afirmação de que os dois writers escreveram**,
  enquanto o disco tem o trabalho de um só. É a **mesma assinatura no cache** que a run real `lohra-notion-v4`
  deixou (célula afirmando o que o disco não tem; lá foram 3 de 5 artefatos mutados depois da gravação) — mas por
  um **vetor que a v4 não tinha**: fan-out intra-nó sobre o mesmo path, não leaf vivo (#45) nem zumbi de cancel
  (#42). Ver §6.9.
- Efeito colateral honesto do C-jitter: quando o recheck re-spawna o writer perdedor, o re-spawn faz o RMW de novo
  contra o conteúdo atual e o arquivo **acidentalmente fica com as duas linhas** (16/20 resumes terminaram com 2
  linhas). Isto é acidente de mecânica, **não** um caminho de reparo — e no barrier ele não acontece.
- **Achado adicional, não previsto: o recheck de manifesto pode CORROMPER um run que estava certo.**
  `c3-jitter` rep 3 é o caso: os writers **não** se sobrepuseram (A escreveu `A\n`; B leu e escreveu `A\nB\n`),
  o arquivo final tinha as duas linhas e o run estava correto. Mas a célula de A guardou o sha de `A\n`, que B
  legitimamente mudou logo depois. No resume o recheck viu `changed`, invalidou a célula de A e re-spawnou —
  e o re-spawn refez o RMW contra o conteúdo ATUAL, deixando o arquivo com **`A\nB\nA\n`** (3 linhas).
  Nenhum fault, `status: complete`, 1 spawn. Ou seja: num pipeline em que um stage constrói sobre o arquivo do
  anterior, **o manifesto invalida células corretas e o re-spawn duplica trabalho no disco**. É o custo de a
  identidade do artefato ser "o arquivo inteiro" quando o artefato de verdade é "a minha contribuição a ele".
  (Verificado no dado bruto: `exp62/results_resume/c3-jitter.jsonl`, rep 3.)

### 2.5 C2 — o manifesto não é autorável no único fan-out com barreira

`validate_spec` **ACEITA** um `parallel` cujas branches são dicts com `schema_ref: artifact_manifest` e `model`:

```
{"accepted": true, "branches": [{"prompt": "...", "schema_ref": "artifact_manifest"}, ...]}
```

E no runtime (probe executado, `exp62/probe_c2/`): outputs voltam como **`str` crus** (nenhum parse, nenhuma
validação), **uma** célula para o nó, `artifact_verification` ausente, `faults: []`. O `model` também é ignorado.
`branch_prompt()` lê só a chave `prompt`; `run_parallel` chama `collect_with_schema(sub_id, **None**)`.

Isto **não é um bug novo** — `nodes.py` documenta a decisão ("its branches are prompts, not nodes") — mas é um
**footgun silencioso**: o autor escreve o contrato, o validador aceita, e nada acontece. É o único achado deste
experimento que é acionável sem decisão de produto.

---

## 3. Veredito por configuração

| Cfg | Veredito | Justificativa em uma linha |
|---|---|---|
| **A** — `parallel` + `write_file` no mesmo path | 🔴 **INVISÍVEL** | 24/25 (jitter) perdem trabalho; `complete`, 0 faults, célula única afirma os dois sucessos, resume replaya de graça |
| **B1** — shell com `>>` (append) | 🟢 **SEGURO para conteúdo** / 🟡 ordem não determinística | O_APPEND do kernel resolve; 50/50 com as duas linhas. Não é o shell que salva — é a **operação** |
| **B2** — shell com `cat` + `>` (RMW) | 🔴 **INVISÍVEL** | 50/50 perdem, idêntico a A. Confirma que o hazard é o **read-modify-write**, não a tool |
| **C** — pipeline + manifesto `{path,sha256,bytes}` | 🟡 **DETECTÁVEL POR SORTE** | 4/20 no run (jitter) + 16/20 no resume; união 20/20, mas por dois mecanismos anti-correlacionados, ambos **advisory** (nunca degradam), e com **diagnóstico errado** ("o leaf mentiu") |
| **C3** — pipeline + manifesto só `path` (o mínimo do schema) | 🔴 **INVISÍVEL** no pior caso | 20/20 invisíveis com writes simultâneos; 3/20 invisíveis com jitter |
| **D** — arquivos distintos | 🟢 **SEGURO para conteúdo** / 🟡 **inseguro para ordem** | 50/50 arquivos íntegros — a doutrina se confirma. Mas o reader irmão leu arquivo ausente em 11/25 e isso virou resposta cacheada com `complete` |
| **C2** — `schema_ref`/`model` em branch de `parallel` | 🟠 **ACEITO E IGNORADO** | validador aceita, engine descarta; contrato que o autor escreveu não existe em runtime |

---

## 4. Consequência para H2/H3 de #45 e a parte A de #42 — **proposta de decisão** (não decisão)

### 4.1a #45 H3 — **redução hierárquica**: antes **indecidível**, agora **parcialmente provada**

H3 (fan-out para saídas DISTINTAS + um nó de fan-in que agrega) era indecidível por falta de fan-in no run real.

**O dado:** a **metade fan-out está provada** — config D, 50/50 arquivos íntegros (jitter e forçado), zero
corrupção de conteúdo, com dois writers simultâneos no mesmo diretório. A **metade fan-in não foi rodada**: eu
não executei um nó posterior que lê os N arquivos. Mas ela cai no regime **já provado seguro**, porque o engine é
estritamente sequencial ENTRE nós (`engine.py:1021`, Relatório 2) — um nó de agregação só começa depois de os
writers terem terminado, e portanto não disputa nada.

**Proposta:** promover H3 de "indecidível" para **"metade fan-out provada; metade fan-in inferida do regime
sequencial, não executada"** — e tratá-la como **doutrina de autoria** (P4), não como primitive nova: ela já é
expressável hoje com os node-types existentes. O que falta não é mecanismo, é o autor saber que tem de usá-la —
e o único risco residual é de ORDEM, não de conteúdo (§2.2: o reader dentro do MESMO `parallel` leu arquivo
ausente em 11/25; num nó SEGUINTE isso não acontece).

### 4.1b `working_root` por nó/branch (adiado em #42/#45) — **refutado como remédio**

**O dado:** o hazard existe e é invisível, mas **não vem do `working_root`**. Em 300 repetições o recurso
disputado foi um root de `fs_allow` — o mesmo padrão da run real (`terminal` alcançando o projeto do usuário).
O `working_root` continua vazio e sem consumidor: nenhum prompt, nó ou código do engine entrega o path ao leaf.
Um `working_root` por branch **isolaria um diretório que ninguém usa** e deixaria intacto o caminho por onde o
dano de fato acontece.

**Proposta:** manter o **NÃO FAZER** (o Relatório 2 já dizia isso por outra razão; agora há dado) — não porque o
hazard não exista, mas porque a primitive proposta não o toca.

### 4.2 #45 H2 (handle de 1ª classe / manifesto) — **promovida de "sem evidência" para "necessária mas insuficiente"**

**O dado:** o manifesto (0.0.21) é a coisa certa e chega perto — mas nesta topologia ele (i) não é autorável em
`parallel`, (ii) tem detecção in-run que é corrida, (iii) some quando o autor declara só o campo obrigatório, e
(iv) diagnostica "o leaf mentiu sobre o hash" quando o fato é "o irmão sobrescreveu".

**Proposta:** **não** construir handle imutável / conteúdo-endereçado agora (é L, e a evidência ainda é de um
laboratório, não de uma run real com `${ref}`). Fazer, em vez disso, as três fatias S de §5 que fecham o buraco
com o que já existe. Reavaliar handle depois de a primeira run REAL com fan-out + `${ref}` sobre arquivo existir.

### 4.3 #42 parte A (dependência por recurso) — **proposta: NÃO implementar ordenação por recurso**

**O dado:** o Relatório 2 já havia observado que o engine é estritamente sequencial **entre** nós, então
"dependência por recurso" não compra nada lá. Este experimento fecha o outro lado: **dentro** de um nó existe
concorrência real e ela corrompe — mas ordenar por recurso dentro de um fan-out é a mesma coisa que **serializar
o fan-out**, que é destruir a única razão de ele existir. Config D prova que o padrão correto (um arquivo por
leaf, agregação depois) já é seguro para conteúdo e **não precisa de ordenação nenhuma**.

**Proposta:** fechar a parte A de #42 com "não implementar", e transferir a energia para (a) tornar o padrão
seguro **autorável e ensinado**, e (b) tornar o padrão inseguro **visível**. Um `reads:`/`writes:` declarativo
continua no NÃO FAZER: sem enforcement ele é decoração, e com enforcement ele é o handle de 1ª classe pela porta
dos fundos — decisão L que este dado ainda não justifica.

---

## 5. Menor primitive honesta (só onde há caso invisível)

Em ordem de "custo por unidade de invisibilidade fechada":

### P1 — lint: campo aceito e ignorado em fan-out (**S**) — *nenhuma decisão de produto*
`lint_spec` (issue #49, `lint.py`) já existe com uma regra e já rende `warnings` na aceitação do spec. Regra 2:
uma branch de `parallel` que é dict e traz qualquer chave fora de `prompt` (`schema`, `schema_ref`, `model`,
`tier`, `effort`, `provider`, `timeout`, `retries`, `max_iterations`) **é aceita e ignorada** — avisar, com o
remédio ("se este leaf precisa de contrato ou de rota própria, faça dele um nó `agent`, ou use `pipeline`").
Mecânico, zero heurística, zero falso positivo, fecha o achado C2. **Não** transformar em rejeição: quebraria
specs válidos hoje.

### P2 — manifesto que fala de concorrência (**S**) — *decisão do dono: só texto, ou também status?*
Duas mudanças pequenas em `artifact.py`/`engine.py`:
- **(a) texto honesto**: quando a divergência é entre a alegação do leaf e a medição, a mensagem deve nomear a
  hipótese concorrente ("outro leaf pode ter reescrito este path entre a tua gravação e a medição"), não só
  "o leaf mentiu". Custo ≈ uma string + teste. Fecha o diagnóstico errado de §2.3.
- **(b) colisão de path entre células do MESMO run — chaveada por PATH, nunca por hash**: hoje nada compara duas
  células que declaram o mesmo `path`. Um dicionário `path → [node_ids]` no run e um fault quando um **segundo**
  nó declara um path que outro já declarou **pega o caso C3-barrier que é invisível 20/20** — e pega **sem
  depender de timing**, porque não olha o disco: compara duas declarações entre si.
  **O critério tem de ser o path sozinho.** Comparar shas não funciona: em C3-barrier as duas células mediram o
  MESMO arquivo final e guardaram o MESMO sha (`cells_all_match_final = 20/20`) — sha igual é exatamente a
  assinatura do dano, não prova de segurança. Sha igual **não pode** suprimir o aviso.
  **Decisão do dono:** advisory (coerente com o que 0.0.23 decidiu para manifesto) ou fault que degrada.

### P3 — `write_file` com `append: true` (**S/M**) — *fecha o hazard na origem*
O RMW não é escolha do autor: é a **única** forma que a superfície de tools oferece para somar algo a um arquivo.
B1 prova que quando a operação é append de verdade (O_APPEND) o resultado é correto em 50/50 sem nenhuma
coordenação — e hoje isso só está disponível para quem o operador deu **shell**, que é a capacidade mais larga
que existe. Um `append` no `write_file` dá a operação segura **sem** dar o shell. Custo: a tool + o texto do
schema + o guard do sandbox (o mesmo `_fs_denial`, inalterado). Risco a nomear: O_APPEND é atômico para escritas pequenas em
filesystem local, e nem toda escrita grande / nem todo filesystem (NFS) preserva isso — documentar o limite,
não prometer atomicidade universal.

### P4 — guidance de autoria (**S**) — *complementa E5, não substitui*
Na skill `workflow-authoring`: "num fan-out, cada leaf escreve **o próprio** arquivo; a agregação é um nó
posterior" (config D), "um reader dentro do mesmo `parallel` não tem ordem garantida em relação aos writers —
leia num nó seguinte" (§2.2), "branches de `parallel` são prompts: contrato e rota só em `agent`/`pipeline`" (C2).
Sozinha é o remédio mais barato e o menos confiável — por isso vem depois de P1/P2, não no lugar delas.

**Fora**: handle imutável / conteúdo-endereçado (**L**), `working_root` por branch (**M**, e §4.1 mostra que não
toca o hazard), `reads:`/`writes:` declarativos com enforcement (**L**), lock de arquivo no sandbox (**M**, e
transforma fan-out em fila).

---

## 6. O que NÃO fazer

1. **Não** fazer `working_root` por nó/branch — §4.1: isola um diretório que ninguém usa; o dano acontece em
   `fs_allow` (e, com shell, no projeto do usuário).
2. **Não** implementar ordenação/dependência por recurso (#42-A) — dentro de um nó, é serializar o fan-out.
3. **Não** adicionar `reads:`/`writes:` declarativos sem enforcement (segue valendo do Relatório 2), e **não**
   adicioná-los COM enforcement por causa deste dado — é o handle de 1ª classe disfarçado, custo L.
4. **Não** tornar `sha256`/`bytes` obrigatórios no `MANIFEST_SCHEMA` para "consertar" C3. Um LLM não computa
   sha256 confiável do que escreveu; obrigar produz retry de schema queimado e uma alegação inventada — e o
   comentário em `artifact.py` já explica por que eles são hint. A saída certa é P2(b), que não depende do leaf.
5. **Não** ler o modo `barrier` como frequência. Ele é prova de alcançabilidade.
6. **Não** ler B1 como "com shell é seguro". B2 (mesmo shell, operação RMW) perde 50/50. O que salva é
   **a operação atômica**, não a capacidade.
7. **Não** transformar o lint P1 em rejeição de spec.
8. **Não** tratar o efeito colateral de §2.4 (resume que "conserta" o arquivo) como caminho de reparo — é
   acidente de RMW e não acontece no pior caso.
9. **Não** concluir daqui que a run real `lohra-notion-v4` sofreu perda de atualização — ela não tinha fan-out
   sobre o mesmo path; este experimento explica um vetor **diferente** do zumbi de cancel (#42) e do leaf vivo
   (#45), com a mesma assinatura no cache.

---

## 7. Onde não consegui provar

- **Escrita rasgada (`torn`)** — nunca observada em 300 repetições. Não é prova de atomicidade: `write_text` é um
  `open(w)` + `write()` de payload pequeno (19 bytes) no APFS, e o GIL não ajuda a espaçar dois syscalls. Com
  payloads grandes (MB) o desfecho pode ser diferente. **Não medi payload grande.**
- **Cross-process** — tudo aqui é um processo, threads, um `work-{fence}`. O caso de dois donos (stretch antigo +
  resume) escrevendo no mesmo path **não foi exercitado**; a fence protege o SQLite, não o filesystem.
- **3 repetições sem sobreposição confirmada** (1 em `a-jitter`, 2 em `b2-jitter`): contadas como
  **não exercitadas**, não como seguras.
- **`n` maior que 2** — dois writers. Com N branches a probabilidade de perda cresce, mas não medi a curva.
- **LLM real** — o stub sempre lê antes de escrever e sempre declara honestamente. Um modelo real pode escrever
  sem ler (pior: sobrescreve sem nem tentar preservar), pode alegar sha inventado (o fault advisory viraria ruído)
  ou pode não devolver manifesto nenhum. Nenhum desses caminhos foi medido.
- **`${ref}` sobre o output do writer** — como na run real, meus specs têm zero `${ref}` consumindo o manifesto
  a jusante. O **prejuízo** da célula mentirosa continua sendo **residual demonstrado, não dano demonstrado**.
- **Concorrência entre `verify`/`judge_panel`/`loop_until_dry`** — medi `parallel` e `pipeline`. Os outros
  fan-outs intra-nó têm a mesma forma (spawn N, colher), mas não foram rodados.
- **`workflow_audit`** — desligado (`LOHRA_AUDIT=off`). Não sei o que o ledger teria mostrado; provavelmente as
  escritas concorrentes com timestamps, que seria mais um ponto de detecção *post-mortem* a avaliar.
- **Higiene da árvore durante a execução** — toda a coleta (matriz 15:35–15:40, resume 15:42, probe C2 15:43)
  aconteceu com `HEAD = 9547cdc`. Outra sessão commitou `8584572` (evento de audit, #64) às 15:46, **depois** de
  toda a coleta. Não posso descartar que houvesse mudanças não-commitadas dessa sessão na árvore durante as
  minhas execuções; o risco é baixo (a mudança é de audit, que eu rodei desligado), mas **não é zero** — uma
  re-execução limpa a partir de uma tag confirmaria.
