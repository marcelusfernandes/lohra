"""Turn notice overlay (SUP-05) — delivery helpers sobre DurableNoticeStore.

Bridge puro: dado um store de notices (o ``db.notices`` da SessionDB) e os
owners elegíveis do turno (a cadeia root→tip da SessionDB), fornece:

- ``lineage_owners`` — owners elegíveis lidos da SessionDB (a SessionDB é quem
  sabe andar parent_session_id; o store de notices permanece PURO, sem
  conhecimento de lineage);
- ``claim_lineage_notices`` — claim das notices pendentes desses owners, token
  em mãos (posse exclusiva via lease single-winner do store);
- ``format_notice_overlay`` — texto bounded do overlay request-facing;
- ``build_turn_notice`` — texto do notice operacional publicado quando um turno
  morre (erro/interrupção), owner = session_id, bounded.

Sem I/O oculto além do store injetado, sem ownerless (o store recusa).
"""

from __future__ import annotations

from lohra.state.notices import DurableNoticeStore

# TTL do notice operacional de turno morto (SUP-05: "TTL dead turn 24h").
DEAD_TURN_TTL_SECONDS = 24 * 3600.0

# Cap de caracteres do overlay inteiro (paridade com MAX_CLAIM_CHARS; o claim
# já é bounded pelo store, mas o formato adiciona moldura por linha).
_OVERLAY_HEADER = "AVISOS OPERACIONAIS (não são fala do usuário):"
_OVERLAY_MAX_CHARS = 4096


def lineage_owners(db, session_id: str) -> list[str]:
    """Owners elegíveis = a cadeia root→tip à qual esta sessão pertence.

    Lê a SessionDB (``lineage_root_to_tip``) — o store de notices NÃO ganha
    acoplamento a ela. Filtra owners vazios (ownerless é recusado no store,
    mas a defesa começa aqui).
    """
    try:
        lineage = db.lineage_root_to_tip(session_id)
    except Exception:  # noqa: BLE001 — lineage indisponível não mata o turno
        return []
    return [owner for owner in (lineage or []) if isinstance(owner, str) and owner.strip()]


def claim_lineage_notices(
    store: DurableNoticeStore, owners: list[str]
) -> tuple[str | None, list[dict]]:
    """Claim notices pendentes dos owners do lineage.

    Retorna ``(token, rows)``; sem pendências, ``(None, [])``. O token é a
    prova de posse: ack após persistência canônica, release em qualquer falha.
    Nunca lança por problema do store — um claim que falha é tratado como
    "sem notices" (o turno segue; at-least-once re-entrega depois).
    """
    if not owners:
        return (None, [])
    try:
        return store.claim(owners)
    except Exception:  # noqa: BLE001 — claim quebrado = turno sem overlay
        return (None, [])


def format_notice_overlay(rows: list[dict]) -> str | None:
    """Formata as notices claimadas como UM overlay bounded para o turno.

    ``None`` quando não há rows (o chamador passa ``request_overlay=None`` e o
    request sai byte-idêntico). Bounded por caracteres totais: linhas que não
    couberem ficam para o próximo claim (a notice NÃO foi ackada ainda).
    """
    if not rows:
        return None
    lines: list[str] = []
    budget = _OVERLAY_MAX_CHARS - len(_OVERLAY_HEADER) - 2
    for row in rows:
        text = " ".join(str(row.get("text", "")).split())
        if not text:
            continue
        line = f"- {text}"
        if len(line) > budget:
            break
        lines.append(line)
        budget -= len(line) + 1
    if not lines:
        return None
    return _OVERLAY_HEADER + "\n" + "\n".join(lines)


def build_turn_notice(
    *,
    status: str,
    error: str | None = None,
    error_kind: str | None = None,
) -> str:
    """Texto bounded do notice operacional de um turno morto.

    Fato OPERACIONAL para continuidade entre turnos/processos — nunca é
    classificado como insight/aprendizado (SUP-05). Owner é sempre a sessão.
    """
    parts = [f"turn {status}"]
    if error_kind:
        parts.append(f"error_kind={error_kind}")
    if error:
        parts.append(f"error={error}")
    text = "; ".join(parts)
    return text[:500]
