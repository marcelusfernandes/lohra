"""notice_trail — tombstones bounded de notices removidas (issue #39).

O ack do ``DurableNoticeStore`` DELETA a linha — correto para o funcionamento,
cego para a auditoria: "entregue e consumida" e "nunca publicada" ficavam com a
mesma evidência (nenhuma). Este módulo grava, DENTRO das mesmas transações
``BEGIN IMMEDIATE`` que removem rows, um tombstone leve por remoção — a mesma
disciplina dos ``$compacted``/gaps da Wave 4: perda/consumo sempre visível.

Decisão tombstone-sobre-flag (comparação adversarialmente verificada, issue
#39 AC#4): a alternativa (flag ``acked_at`` na própria row + purga adiada)
quebraria a republicação legítima via ``UNIQUE(owner_id, fingerprint)``, seria
ressuscitada pela recuperação de lease morto do claim, e ocuparia o cap de
32/owner com cadáveres. O tombstone não toca dedup/cap/lease — é um LOG (sem
UNIQUE; N ciclos do mesmo fato são legítimos).

Invariantes:

- **todo sumiço explica-se**: ``reason`` ∈ {acked, expired, evicted}; release
  e recuperação de lease NUNCA gravam (a notice segue viva);
- **a tentativa identifica-se**: reason=acked carrega o token do ack; nos
  demais, o ``lease_token`` da PRÓPRIA row condenada — distingue "morreu sem
  claim" de "morreu em voo" a custo zero;
- **a trilha nunca vira a nova cauda infinita**: cap por owner (mantém as mais
  recentes) + varredura GLOBAL de TTL a cada escrita — espelhando a purga
  global do claim, que gera tombstones para owners fora da lineage;
- **atômico com o DELETE**: tombstone e remoção compartilham a transação; uma
  trilha inescrevível (corrupção) bloqueia o ack — trade-off aceito: a notice
  fica leased e re-entrega (at-least-once), nunca some sem rastro;
- **lacunas honestas**: um processo de versão ANTIGA (sem este módulo) ackando
  no mesmo banco não grava tombstone — a trilha tem buracos, nunca mentiras.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

TRAIL_SCHEMA = """
CREATE TABLE IF NOT EXISTS notice_trail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notice_id INTEGER NOT NULL,
    owner_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    removed_at REAL NOT NULL,
    reason TEXT NOT NULL,
    lease_token TEXT
);
CREATE INDEX IF NOT EXISTS idx_nt_owner_removed
    ON notice_trail(owner_id, removed_at);
CREATE INDEX IF NOT EXISTS idx_nt_removed ON notice_trail(removed_at);
"""

# Bounds próprios (issue: "cap + TTL próprios, para a trilha não virar a nova
# cauda infinita"). Cap maior que o das notices (2x32): a trilha acumula os N
# ciclos que o quadro vivo não guarda. TTL 30d > TTL das notices (7d): o
# post-mortem chega depois do fato.
TRAIL_CAP = 64
TRAIL_TTL_SECONDS = 30 * 24 * 3600

_COLUMNS = "notice_id, owner_id, fingerprint, text, created_at, removed_at, reason, lease_token"


def record_removals(
    connection: sqlite3.Connection,
    rows: Iterable[sqlite3.Row],
    *,
    reason: str,
    now: float,
    ack_token: str | None = None,
) -> None:
    """Grava um tombstone por row condenada — chamar DENTRO da transação que
    vai deletá-las. ``rows`` precisa carregar id, owner_id, fingerprint, text,
    created_at e lease_token. Para ``acked``, o token gravado é o do ack (a
    tentativa vencedora); para os demais, o da própria row (morte em voo)."""
    for row in rows:
        connection.execute(
            f"INSERT INTO notice_trail ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["owner_id"],
                row["fingerprint"],
                row["text"],
                row["created_at"],
                now,
                reason,
                ack_token if reason == "acked" else row["lease_token"],
            ),
        )


def prune(connection: sqlite3.Connection, owner_ids: Iterable[str], *, now: float) -> None:
    """Bound da trilha — chamar na mesma transação, APÓS ``record_removals``.

    ``owner_ids`` são os owners das rows RECÉM-REMOVIDAS (nunca os do claim: a
    purga TTL do claim é global e gera tombstones cross-lineage). A varredura
    de TTL é GLOBAL pela mesma razão — um owner morto nunca mais tem evento
    próprio, e seus tombstones precisam vencer por tempo."""
    connection.execute(
        "DELETE FROM notice_trail WHERE removed_at <= ?", (now - TRAIL_TTL_SECONDS,)
    )
    for owner in set(owner_ids):
        connection.execute(
            """DELETE FROM notice_trail WHERE id IN (
                   SELECT id FROM notice_trail WHERE owner_id = ?
                    ORDER BY removed_at DESC, id DESC
                    LIMIT -1 OFFSET ?
               )""",
            (owner, TRAIL_CAP),
        )


def consumed(
    connection: sqlite3.Connection,
    owner_ids: list[str],
    *,
    limit: int,
    now: float,
) -> list[dict[str, Any]]:
    """Tombstones dos owners, mais recente primeiro (tie-break por id — um ack
    total remove várias rows com o MESMO removed_at). Filtra TTL na leitura:
    owner dormente (que nunca mais dispara o prune de escrita) não devolve
    trilha vencida."""
    placeholders = ",".join("?" for _ in owner_ids)
    rows = connection.execute(
        f"""SELECT {_COLUMNS} FROM notice_trail
             WHERE owner_id IN ({placeholders}) AND removed_at > ?
             ORDER BY removed_at DESC, id DESC LIMIT ?""",
        (*owner_ids, now - TRAIL_TTL_SECONDS, max(1, limit)),
    ).fetchall()
    return [dict(row) for row in rows]
