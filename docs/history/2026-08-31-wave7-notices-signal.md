# Wave 7 — fechamento: #39 (rastro de acks) + #40 (morte por sinal)

Executadas sob ultracode: 4 leitores paralelos → 2 designers → 4 céticos
adversariais (painel `wave7-notices-signal-design`); as decisões abaixo são as
que sobreviveram à refutação, com as emendas dos céticos aplicadas.

## #39 — comparação tombstone × flag (AC#4, registrada antes de fixar)

**Decisão: tombstone (tabela `notice_trail`), rejeitando a flag `acked_at`.**

As três sondas da issue, respondidas no código real de `state/notices.py`:

1. **A flag quebraria a republicação pós-ack/pós-TTL?** Sim. Uma row
   ackada-mas-não-purgada seguiria ocupando `UNIQUE(owner_id, fingerprint)`;
   o `INSERT OR IGNORE` do publish devolveria `False` para um fato legítimo
   recorrente — exatamente o cenário do 429 que motivou a issue. O único
   conserto (publish purgar rows ackadas) destruiria o rastro no momento em
   que ele seria consultado.
2. **O claim ressuscitaria linhas ackadas?** Sim, nas duas variantes: com o
   lease limpo, o SELECT de pendentes (`lease_token IS NULL`) re-entrega a
   notice consumida; com o lease mantido, a recuperação de lease morto a
   devolve 300s depois. Todo caminho precisaria de guard `acked_at IS NULL` —
   superfície de erro espalhada.
3. **Custo do segundo cap?** A flag pareceria mais barata, mas cadáveres
   ocupariam o cap de 32/owner (deslocando fatos vivos); o tombstone paga um
   cap próprio (64/owner + TTL 30d + varredura global) e deixa o quadro vivo
   intocado.

Emendas dos céticos incorporadas: `notice_id` + `fingerprint` no tombstone;
`lease_token` da própria row nos reasons `expired`/`evicted` (morte em voo ≠
morte sem claim); prune deriva owners das rows removidas (a purga do claim é
global/cross-lineage) + varredura global de TTL; evicção deleta pelos MESMOS
ids selecionados; `consumed()` ordena com tie-break e filtra TTL na leitura;
superfície de operador `lohra notices <session_id>`. Lacuna honesta: processo
de versão antiga ackando no mesmo banco não grava tombstone.

## #40 — sinal→exceção; SIGKILL adiado com custo nomeado

**Decisão: converter SIGTERM/SIGHUP em `TerminatedBySignal` (BaseException) no
handler e deixar o epílogo NORMAL do `run_chat` publicar** — nunca escrever de
dentro do handler (roda entre bytecodes da frame interrompida; um publish
re-entrante no lock/conexão do store deadlockaria). Desarme-no-primeiro-sinal
(2º sinal mata nativo — escape hatch), SIG_IGN respeitado (nohup), janelas
multi-commit protegidas por `defer_signals`, morte final por `die_by_signal`
(SIG_DFL + os.kill → WIFSIGNALED/128+N fiel). SIGINT sob `--json` ganhou a
mesma semântica: 1 envelope parseável + dead-turn notice + exit fiel (antes:
stdout vazio + traceback).

**SIGKILL/OOM/queda de energia: adiado, documentado.** A alternativa
(marcador-de-sessão-aberta + detecção na próxima abertura, prior-art
`workflow_run_locks`/lease+heartbeat) custa: uma tabela/colunas de sessão
aberta, heartbeat por turno (thread + writes periódicos no caminho quente do
chat) e a detecção/publicação no boot seguinte. Mitigação já existente: o
lease das notices expira em 300s (nada se perde para sempre — re-entrega) e um
turno não-persistido é invisível por design. Implementar quando houver
evidência de SIGKILL frequente em operação real; o desenho fica aqui como
follow-up.

**Gaps residuais aceitos (nomeados):** (a) sinal durante o finally de cleanup
perde envelope/notice, mas morre fiel pelo sinal (guard no `main`); (b) turno
persistido morto por sinal antes do `record_turn` fica sem custo no ledger da
sessão.
