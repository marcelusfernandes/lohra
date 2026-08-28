"""``list_models`` — the agent's read-only view of what it can actually route to.

Intercepted like the orchestration triad: the schema lives in the registry so the
model sees it, execution is bound per session (to this home's tier map) in
``build_session_dispatch``, and it is excluded from subagents and ``lohra serve``
— naming a model is an AUTHORING-time decision the orchestrator makes, not
something a leaf revisits.

Bounded on purpose: OpenRouter alone answers with hundreds of ids. Each provider
reports at most ``limit`` of them AND its real total, so the cap is always
visible — a silent truncation is the failure mode the harness forbids.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from lohra.catalog.catalog import Catalog, ProviderModels, build_catalog
from lohra.tools.registry import registry, tool_error, tool_result
from lohra.workflow.tiers import MODEL_TIERS, Tier, TierMap, load_tiers

DEFAULT_LIMIT = 25
MAX_LIMIT = 100

GUIDANCE = (
    "List the models reachable right now, per provider: live from each provider "
    "whose API key is configured, from the local Ollama daemon, and the "
    "subscription model when subscription mode is on. Providers without a key "
    "come back as 'skipped' naming the variable to set. Also returns the "
    "operator's tier map (small|medium|big) — prefer naming a TIER over a "
    "hard-coded slug. Read-only: it starts no session and spends no tokens."
)

_SCHEMA = {
    "description": GUIDANCE,
    "parameters": {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Only this provider (e.g. 'openai', 'ollama'). Omit for all.",
            },
            "query": {
                "type": "string",
                "description": "Case-insensitive substring filter on model ids.",
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Max ids reported per provider (default 25, max 100). The real "
                    "total is always reported, so nothing is cut silently."
                ),
            },
        },
    },
}

CatalogBuilder = Callable[..., Catalog]
TierLoader = Callable[[Path], TierMap]


def _coerce_limit(raw: Any) -> tuple[int, str | None]:
    """Clamp rather than reject (same spirit as image_gen's ``n``) — and accept
    the shapes a model actually emits (``"50"``, ``50.0``).

    An argument that cannot be read at all is REPORTED, not quietly replaced:
    the cap is disclosed everywhere else, so the fallback must be too.
    """
    value: int | None = None
    if raw is None:
        return DEFAULT_LIMIT, None
    if isinstance(raw, bool):
        value = None
    elif isinstance(raw, int):
        value = raw
    elif isinstance(raw, float) and raw.is_integer():
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = None
    if value is None:
        return DEFAULT_LIMIT, f"limit {raw!r} is not a whole number — used {DEFAULT_LIMIT}"
    return max(1, min(MAX_LIMIT, value)), None


def _text(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _render(entry: ProviderModels, query: str, limit: int) -> dict[str, Any]:
    """One provider, bounded — and honest about BOTH bounds.

    A filter is as much a cap as a limit: without the pre-filter count, a query
    with zero hits is indistinguishable from a provider that has nothing.
    """
    scanned = entry.total
    if query:
        matched = tuple(model for model in entry.models if query in model.lower())
        entry = ProviderModels(entry.provider, entry.source, matched, len(matched), entry.detail)
    payload = entry.head(limit).to_dict()
    notes: list[str] = []
    if query and scanned:
        notes.append(f"{payload['total']} of {scanned} matched {query!r}")
    if payload["total"] > len(payload["models"]):
        notes.append(
            f"showing {len(payload['models'])} of {payload['total']} — "
            "refine with 'query' or raise 'limit'"
        )
    if notes:
        payload["note"] = f"{entry.provider}: " + "; ".join(notes)
    return payload


def _render_tier(tier: Tier | None) -> dict[str, str] | None:
    if tier is None:
        return None
    fields = {"model": tier.model, "provider": tier.provider, "effort": tier.effort}
    return {name: value for name, value in fields.items() if value}


class ListModelsTool:
    """Binds the catalog + this home's tier map to one session.

    ``builder``/``tier_loader`` are injected so the tool is testable with zero
    I/O; the defaults are the real ones.
    """

    def __init__(
        self,
        home: Path | str,
        *,
        builder: CatalogBuilder | None = None,
        tier_loader: TierLoader | None = None,
    ) -> None:
        self._home = Path(home)
        self._builder = builder if builder is not None else build_catalog
        self._tier_loader = tier_loader if tier_loader is not None else load_tiers

    def handle(self, args: dict[str, Any]) -> str:
        provider = _text(args.get("provider"))
        query = _text(args.get("query")).lower()
        limit, limit_note = _coerce_limit(args.get("limit"))
        try:
            catalog = self._builder(
                home=self._home, providers=(provider,) if provider else None
            )
        except AssertionError:
            # The suite's network guard raises this when a test forgot to inject a
            # transport. Downgrading it to a tool_error would hide a real fetch
            # behind a green run — the one exception that must stay loud.
            raise
        except Exception as exc:  # noqa: BLE001 — a read must not crash the turn
            return tool_error(f"could not read the model catalog ({type(exc).__name__})")
        if provider and not catalog.entries:
            return tool_error(
                f"unknown provider {provider!r} — call list_models with no "
                "'provider' to see the ones this install knows about"
            )
        # Reloaded on EVERY call: the operator may edit workflow_tiers.json
        # mid-session, and a long-lived service that cached it would answer stale.
        tiers = self._tier_loader(self._home / "workflow_tiers.json")
        extra = {"note": limit_note} if limit_note else {}
        return tool_result(
            providers=[_render(entry, query, limit) for entry in catalog.entries],
            tiers={name: _render_tier(tiers.get(name)) for name in MODEL_TIERS},
            **extra,
        )


def _intercepted(_args: dict[str, Any], **_kwargs: Any) -> str:
    return tool_error("list_models must be intercepted with a session home")


def register_list_models_tool_schema() -> None:
    """Register the schema (execution is intercepted per session)."""
    registry.register(
        "list_models", "catalog", _SCHEMA, _intercepted, override=True, emoji="🗂️"
    )
