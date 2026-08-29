"""Custo acumulado por SESSÃO — o UPDATE por turno que liga as colunas da
``sessions`` provisionadas na Fase 2/3 e nunca escritas.

Contrato:
- Acumula NO MOMENTO DO GASTO: o dinheiro somado usou o preço vigente daquele
  turno (snapshot ou ``pricing.json``); nunca é recalculado na leitura —
  recalcular com preço de hoje mentiria sobre gasto de ontem.
- Sessão = os PRÓPRIOS turnos. Subagentes e workflow runs têm ledger próprio
  (``workflow_run_spend``); somar tudo na mãe seria dupla contagem. A árvore de
  custo completa exigirá wirar este registro nas sub-sessões do core (hoje elas
  só têm o ledger próprio) antes de um JOIN por ``parent_session_id`` render
  algo — pendência nomeada, não promessa.
- Fail-closed em dinheiro: (provider, model) sem preço acumula só tokens.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def record_turn(
    db: Any,
    session_id: str,
    usage_total: Any,
    *,
    provider: str,
    model: str,
    home: Path,
    api_calls: int = 1,
) -> dict | None:
    """Acumula o ``usage_total`` de um turno na sessão e devolve o ACUMULADO
    (pronto para o envelope). ``None`` usage → no-op. Nunca levanta: custo por
    sessão é relatório, jamais pode derrubar um turno que já deu certo."""
    if usage_total is None:
        return None
    try:
        from lohra.pricing import estimate_cost
        from lohra.pricing.overrides import load_price_overrides, price_overrides_path

        overrides = load_price_overrides(price_overrides_path(home))
        cost = estimate_cost(usage_total, provider=provider, model=model, overrides=overrides)
        db.session_add_usage(
            session_id,
            usage_total,
            real_usd=cost.usd if cost else None,
            gross_usd=cost.gross_usd if cost else None,
            api_calls=api_calls,
        )
        row = db.session_usage(session_id)
    except Exception:  # noqa: BLE001 — relatório nunca derruba o turno
        import logging

        logging.getLogger(__name__).warning(
            "session cost not recorded for %s", session_id, exc_info=True
        )
        return None
    if row is None:
        return None
    summary: dict = {
        key: row[key] or 0
        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens",
            "cache_write_tokens", "reasoning_tokens", "api_call_count",
            "priced_call_count",
        )
    }
    if row.get("actual_cost_usd") is not None:
        gross = row.get("estimated_cost_usd")
        if gross is None:  # 0.0 é bruto legítimo (write premium puro) — só None cai no real
            gross = row["actual_cost_usd"]
        summary["cost"] = {
            "usd": round(row["actual_cost_usd"], 6),
            "gross_usd": round(gross, 6),
        }
        if summary["priced_call_count"] < summary["api_call_count"]:
            # subtotal honesto: nem toda chamada teve preço — dizer, nunca
            # apresentar um número baixo demais como completo.
            summary["cost"]["partial"] = True
    return summary
