"""DurableNoticeStore — fatos operacionais por sessão, cross-process (SUP-05).

Uma tabela SQLite no arquivo compartilhado da SessionDB guarda "notices":
pequenos fatos operacionais (ex.: "provider quota exhausted") que uma sessão
publica e outra (ou a mesma, em outro processo) reclama depois. É a camada
durável do steering — sobrevive a restarts e é visível de qualquer processo.

Invariantes (cada uma imposta no limite de escrita, não por convenção):

- **owner = session_id** — toda notice pertence a UM owner; publish/claim
  recusam ownerless (``None``/``""``), fechando injeção profile-global;
- **dedup por fingerprint** — texto normalizado (whitespace colapsado, case
  folded) virá hash; republicar o mesmo fato é um no-op (``False``);
- **cap por owner** (default 32, hard cap) — inserir além do cap evicta a
  pendência mais antiga daquele owner na MESMA transação do insert; lease
  ativo NUNCA é evictado (fato em voo não se perde) — se as pendências não
  bastam para respeitar o cap, o publish é revertido e retorna ``False``;
- **TTL** (default 7 dias) — notice expirada é deletada no claim seguinte
  (ou antes, no publish do MESMO fato: row expirada não é dedup válido, e o
  TTL vence sobre lease vivo — ver ``publish``);
- **claim é lease single-winner** via ``BEGIN IMMEDIATE``: o token devolvido
  é a prova de posse; um segundo claim enquanto o lease vive não vê as rows;
  lease expirado (crash do claimer) é recuperável — entrega at-least-once;
- **texto bounded** — clipado em ``MAX_TEXT_CHARS`` no limite do schema;
- **claim bounded** — no máximo ``MAX_CLAIM`` notices por claim, e no máximo
  ``MAX_CLAIM_CHARS`` de texto acumulado (o consumidor injeta isso no turno,
  então o bound protege a janela de contexto).

Owns its own connection (padrão do audit/insight store): writers são threads
de leaf e processos inteiros; este store não pode convoy o lock geral da
SessionDB. Lêituras cross-process são seguras sob WAL.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS durable_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    lease_token TEXT,
    lease_expires_at REAL,
    UNIQUE (owner_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_dn_owner_created
    ON durable_notices(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dn_lease ON durable_notices(lease_token);
CREATE INDEX IF NOT EXISTS idx_dn_expires ON durable_notices(expires_at);
"""

# Hard bounds. O cap é POR OWNER (cada sessão carrega seus próprios fatos;
# nenhum owner consegue deslocar o outro). TTL 7 dias: fato operacional velho
# não é steering, é ruído. Lease 300s: o consumidor ack/release em segundos.
DEFAULT_CAP = 32
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_LEASE_SECONDS = 300.0
MAX_TEXT_CHARS = 500
MAX_CLAIM = 8
MAX_CLAIM_CHARS = 4096

_WHITESPACE = re.compile(r"\s+")


def _fingerprint(text: str) -> str:
    normalized = _WHITESPACE.sub(" ", text).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _require_owner(owner_id: object) -> str:
    if not isinstance(owner_id, str) or not owner_id.strip():
        raise ValueError("owner_id is required (ownerless notices are refused)")
    return owner_id


class DurableNoticeStore:
    """SQLite-backed notice board com dedup, cap, TTL e lease single-winner."""

    def __init__(
        self,
        path: str,
        *,
        cap: int = DEFAULT_CAP,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._path = path
        self._cap = max(1, int(cap))
        self._ttl_seconds = float(ttl_seconds)
        self._lease_seconds = float(lease_seconds)
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        # Espera (em vez de "database is locked") quando outro processo segura
        # a write lock — o claim concorrido é exatamente esse caso.
        self._connection.execute("PRAGMA busy_timeout=5000")
        try:
            self._connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.executescript(SCHEMA)
        self._connection.commit()

    # --- write path -------------------------------------------------------

    def publish(
        self,
        owner_id: str,
        text: str,
        *,
        now: float | None = None,
        ttl_seconds: float | None = None,
    ) -> bool:
        """Publica um fato para ``owner_id``. False quando é duplicata recusada.

        Dedup, insert e evicção de cap compartilham UM ``BEGIN IMMEDIATE``:
        dois processos publicando o mesmo texto concorrentemente produzem UMA
        row. Também retorna ``False`` (rollback do insert) quando o hard cap
        do owner não pode ser respeitado sem apagar lease ativo — o fato novo
        não sobrevive, mas nenhum lease em voo é perdido.

        Semântica de republicação após TTL: uma row EXPIRADA
        (``expires_at <= ts``) não é dedup válido — o fato dela já morreu.
        Como o claim seguinte apagaria a row expirada, bloquear a republicação
        via ``INSERT OR IGNORE`` perderia o fato fresco para sempre. Dentro da
        MESMA transação, a row expirada é deletada (purga antecipada, mesma
        regra do claim: ``expires_at <= ts``) antes do insert. Isso vale mesmo
        quando a row expirada ainda carrega lease: o claim atual já teria
        descartado a row pelo TTL (``expires_at <= ts`` roda ANTES da
        recuperação de lease em ``claim``), então o TTL vence sobre o lease —
        um claimer em voo nunca veria essa row de novo. Duplicata VIVA continua
        dedup dura (``False``), inclusive com lease ativo.
        """
        owner = _require_owner(owner_id)
        if not isinstance(text, str) or not text.strip():
            raise ValueError("notice text must be a non-empty string")
        bounded = text.strip()[:MAX_TEXT_CHARS]
        fp = _fingerprint(bounded)
        ts = time.time() if now is None else float(now)
        ttl = self._ttl_seconds if ttl_seconds is None else float(ttl_seconds)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # Purga antecipada do par (owner, fingerprint): row expirada
                # não é dedup válido e seria apagada no claim seguinte de
                # qualquer forma. Deletá-la AQUI, na mesma transação do insert,
                # destrava a republicação após TTL sem abrir janela em que dois
                # processos vêm a "ausência" e inserem duas rows (o UNIQUE +
                # BEGIN IMMEDIATE mantêm a dedup cross-process). Inclui row
                # expirada com lease vivo: o claim atual purga TTL antes de
                # recuperar leases, então o TTL vence — o claimer em voo nunca
                # mais veria essa row.
                self._connection.execute(
                    """DELETE FROM durable_notices
                        WHERE owner_id = ? AND fingerprint = ? AND expires_at <= ?""",
                    (owner, fp, ts),
                )
                cursor = self._connection.execute(
                    """INSERT OR IGNORE INTO durable_notices
                       (owner_id, fingerprint, text, created_at, updated_at,
                        expires_at, lease_token, lease_expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)""",
                    (owner, fp, bounded, ts, ts, ts + ttl),
                )
                inserted = cursor.rowcount == 1
                if inserted and not self._enforce_cap(owner, ts, cursor.lastrowid):
                    # Hard cap não respeitável sem tocar lease ativo: desfaz o
                    # insert (e a própria evicção parcial) na mesma transação.
                    self._connection.rollback()
                    return False
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
        return inserted

    def _enforce_cap(self, owner_id: str, ts: float, inserted_id: int) -> bool:
        """Hard cap POR OWNER, mais antigo primeiro — lease ativo é intocável.

        Evictável é só row SEM lease ativo: pendente (``lease_token IS NULL``)
        ou lease já expirado (``lease_expires_at <= ts``, que o próximo claim
        trataria como pendente). Notice em voo (lease vivo) nunca é apagada —
        quebraria at-least-once após crash do claimer. A row recém-inserida
        também é intocável (senão o publish "sobreviveria" apagando a si
        mesmo). Se as evictáveis não bastam para voltar ao cap, retorna
        ``False`` e o chamador reverte o insert: o hard cap não é excedido e
        nada em voo se perde.
        """
        overflow = (
            self._connection.execute(
                "SELECT COUNT(*) FROM durable_notices WHERE owner_id = ?", (owner_id,)
            ).fetchone()[0]
            - self._cap
        )
        if overflow <= 0:
            return True
        cursor = self._connection.execute(
            """DELETE FROM durable_notices WHERE id IN (
                   SELECT id FROM durable_notices
                    WHERE owner_id = ?
                      AND id != ?
                      AND (lease_token IS NULL OR lease_expires_at <= ?)
                    ORDER BY created_at ASC, id ASC
                    LIMIT ?
               )""",
            (owner_id, inserted_id, ts, overflow),
        )
        return cursor.rowcount >= overflow

    # --- claim / ack / release --------------------------------------------

    def claim(
        self,
        owner_ids: str | list[str],
        *,
        limit: int | None = None,
        now: float | None = None,
        lease_seconds: float | None = None,
    ) -> tuple[str | None, list[dict]]:
        """Reclama notices pendentes de um ou mais owners (lineage).

        Retorna ``(token, rows)``; sem pendências, ``(None, [])``. As rows
        ficam leased sob ``token`` até ack (remove), release (devolve) ou
        expiração do lease (outro claim recupera — at-least-once). Ownerless
        (``None``/``""`` na lineage, ou lista vazia) é recusado: sem owner
        não há escopo, e o fallback "todos" seria injeção global.
        """
        if isinstance(owner_ids, str):
            owners = [_require_owner(owner_ids)]
        else:
            owners = [_require_owner(o) for o in owner_ids]
        if not owners:
            raise ValueError("owner_ids is required (ownerless claims are refused)")
        max_claims = min(limit if limit is not None else MAX_CLAIM, MAX_CLAIM)
        if max_claims < 1:
            raise ValueError("limit must be >= 1")
        ts = time.time() if now is None else float(now)
        lease = self._lease_seconds if lease_seconds is None else float(lease_seconds)
        token = uuid.uuid4().hex
        placeholders = ",".join("?" for _ in owners)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                # TTL e leases mortos primeiro: dentro da MESMA transação do
                # select/update, para que nenhuma row expirada seja entregue.
                self._connection.execute("DELETE FROM durable_notices WHERE expires_at <= ?", (ts,))
                self._connection.execute(
                    """UPDATE durable_notices SET lease_token = NULL,
                              lease_expires_at = NULL
                        WHERE lease_token IS NOT NULL AND lease_expires_at <= ?""",
                    (ts,),
                )
                rows = self._connection.execute(
                    f"""SELECT id, owner_id, text, created_at FROM durable_notices
                         WHERE owner_id IN ({placeholders}) AND lease_token IS NULL
                         ORDER BY created_at ASC, id ASC LIMIT ?""",
                    (*owners, max_claims),
                ).fetchall()
                # Bound de caracteres: para de incluir no primeiro row que
                # estourar o orçamento (o resto fica pendente pro próximo claim).
                budget = MAX_CLAIM_CHARS
                selected: list[sqlite3.Row] = []
                for row in rows:
                    if len(row["text"]) > budget:
                        break
                    budget -= len(row["text"])
                    selected.append(row)
                claimed: list[dict] = []
                for row in selected:
                    self._connection.execute(
                        """UPDATE durable_notices SET lease_token = ?,
                                  lease_expires_at = ?
                            WHERE id = ?""",
                        (token, ts + lease, row["id"]),
                    )
                    claimed.append(
                        {
                            "id": row["id"],
                            "owner_id": row["owner_id"],
                            "text": row["text"],
                            "created_at": row["created_at"],
                        }
                    )
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
        return (token, claimed) if claimed else (None, [])

    def ack(
        self,
        token: str,
        *,
        notice_ids: list[int] | None = None,
        now: float | None = None,
    ) -> int:
        """Remove rows entregues sob ``token`` (todas, ou só ``notice_ids``).

        Quando um subconjunto é ackado, o lease das demais é LIBERADO na mesma
        transação — o consumidor parcial não retém posse que não vai usar.
        Token errado remove/release nada.
        """
        if not token:
            return 0
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                if notice_ids is None:
                    cursor = self._connection.execute(
                        "DELETE FROM durable_notices WHERE lease_token = ?", (token,)
                    )
                    removed = cursor.rowcount
                else:
                    placeholders = ",".join("?" for _ in notice_ids) or "NULL"
                    cursor = self._connection.execute(
                        f"""DELETE FROM durable_notices
                             WHERE lease_token = ? AND id IN ({placeholders})""",
                        (token, *notice_ids),
                    )
                    removed = cursor.rowcount
                    # o restante do lease volta a pendente imediatamente
                    self._connection.execute(
                        """UPDATE durable_notices SET lease_token = NULL,
                                  lease_expires_at = NULL
                            WHERE lease_token = ?""",
                        (token,),
                    )
            except sqlite3.Error:
                self._connection.rollback()
                raise
            self._connection.commit()
        return removed

    def release(self, token: str, *, now: float | None = None) -> int:
        """Devolve TODAS as rows do lease para pendente. 0 se token inválido."""
        if not token:
            return 0
        with self._lock:
            cursor = self._connection.execute(
                """UPDATE durable_notices SET lease_token = NULL,
                          lease_expires_at = NULL
                    WHERE lease_token = ?""",
                (token,),
            )
            released = cursor.rowcount
            self._connection.commit()
        return released

    # --- read path ---------------------------------------------------------

    def pending_count(self, owner_id: str, *, now: float | None = None) -> int:
        owner = _require_owner(owner_id)
        # Sem ``now``, contagem "ativa": leases em curso não contam como
        # pendentes, mas a expiração só é avaliada com clock explícito (a
        # purga acontece no claim, que é o único caminho de descarte).
        query = "SELECT COUNT(*) FROM durable_notices WHERE owner_id = ? AND lease_token IS NULL"
        params: list = [owner]
        if now is not None:
            query += " AND expires_at > ?"
            params.append(float(now))
        with self._lock:
            row = self._connection.execute(query, params).fetchone()
        return int(row[0]) if row is not None else 0

    def close(self) -> None:
        with self._lock:
            self._connection.close()
