"""Which models are ACTUALLY reachable right now, per provider (model-routing B).

Three different worlds answer that question and none of them can be guessed from
config alone: a provider with an API key (live ``/models`` fetch), the keyless
local Ollama daemon (its NATIVE ``/api/tags``, via the onboarding probe), and an
OpenAI/Codex subscription (no listing endpoint exists — the Codex config names
the one model). This module reads all three and, once the HTTP client exists,
never raises: a provider that is down, unauthorized or unreachable degrades to
one ``error`` entry with a short, token-free detail while every other provider
still reports. Building (or closing) the client itself is deliberately OUTSIDE
that isolation — it is the seam the test suite blocks, and a swallowed failure
there would turn a forgotten injection into a quietly green run.

Deliberately NOT built on the SDK clients or ``ClientPool``: listing is a pure
read, and ``ClientPool.get`` constructs real clients (a side effect, and a cost).
The one ambient network seam is ``default_http_client`` — the test suite
neutralizes that single name, exactly like ``detect.default_probe``.

Pendency: ``ProviderProfile.models_url`` (providers/base.py:30) is declared and
set on 3 of the 8 profiles, but it is not read here — it is published in the
wheel, so it stays inert rather than half-authoritative. Endpoints are derived
from ``base_url`` instead, which is set on all 8. Wiring the field up (and
filling the 5 blanks) is a separate change.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from lohra.agent.client import resolve_api_key
from lohra.onboarding import detect
from lohra.providers.base import ProviderProfile, get_provider_profile, list_providers

# "fallback" is reserved for a consumer that decides to fall back on a profile's
# static ``fallback_models``; the catalog itself never emits it — a provider we
# could not read reports empty + a reason, and the caller chooses what to do.
SOURCES = ("live", "fallback", "config", "skipped", "error")

DEFAULT_TIMEOUT = 3.0
_MAX_FETCH_WORKERS = 8
_ANTHROPIC_VERSION = "2023-06-01"

# GET /v1/models is PAGINATED (anthropic SDK: limit defaults to 20, ranges 1..1000,
# and the envelope carries ``has_more``/``last_id``). Reading the default page and
# calling its length the account total is exactly the silent cap the harness
# forbids, so ask for the documented maximum — and still disclose ``has_more``.
ANTHROPIC_PAGE_LIMIT = 1000

# The listing endpoints are a handful of static, first-party URLs, but the repo
# reads nothing unbounded (cf. ``safeio.read_text_bounded``): a body past this
# never reaches the JSON parser. OpenRouter, the largest, is well under a MB.
MAX_RESPONSE_BYTES = 4_000_000

# The subscription provider is not in the registry (it has no API-key profile),
# so it is special-cased by name — the same way ClientPool does.
SUBSCRIPTION_PROVIDER = "openai-codex"
_SUBSCRIPTION_DETAIL = "subscription; no live listing — model from the Codex config"
_SUBSCRIPTION_OFF = (
    "subscription mode is off — run `lohra auth enable` (opt-in) and `lohra auth login`"
)

OllamaProbe = Callable[[], detect.OllamaStatus]


@dataclass(frozen=True)
class ProviderModels:
    """One provider's answer. ``total`` is the REAL count even when ``models``
    has been trimmed for display, so a bound is never silent."""

    provider: str
    source: str
    models: tuple[str, ...] = ()
    total: int = -1  # -1 = derive from ``models``
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"unknown source {self.source!r} (expected one of {SOURCES})")
        if self.total < 0:
            object.__setattr__(self, "total", len(self.models))

    @property
    def truncated(self) -> bool:
        return self.total > len(self.models)

    def head(self, limit: int) -> "ProviderModels":
        """A copy with at most ``limit`` ids, keeping the real ``total``."""
        if limit >= len(self.models):
            return self
        return ProviderModels(
            self.provider, self.source, self.models[:limit], self.total, self.detail
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "source": self.source,
            "total": self.total,
            "models": list(self.models),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class Catalog:
    """Every provider's answer, in registry order (subscription last)."""

    entries: tuple[ProviderModels, ...] = ()

    def get(self, provider: str) -> ProviderModels | None:
        return next((e for e in self.entries if e.provider == provider), None)

    def to_dict(self) -> dict[str, Any]:
        return {"providers": [entry.to_dict() for entry in self.entries]}


# --- the ONE ambient network seam --------------------------------------------


def default_http_client(timeout: float = DEFAULT_TIMEOUT):
    """The catalog's only route to a real socket. Tests rebind this name."""
    import httpx

    return httpx.Client(timeout=timeout, follow_redirects=False)


# --- endpoints + parsing ------------------------------------------------------


def models_endpoint(profile: ProviderProfile) -> str:
    """The listing URL for a profile, derived from ``base_url``.

    ``rstrip("/")`` matters: gemini's base_url ends in a slash, and a naive
    f-string join would produce ``…/openai//models``.
    """
    base = profile.base_url.rstrip("/")
    if profile.api_mode == "anthropic_messages":
        return f"{base}/v1/models?limit={ANTHROPIC_PAGE_LIMIT}"
    return f"{base}/models"


def auth_headers(profile: ProviderProfile, api_key: str) -> dict[str, str]:
    """Anthropic wants its two headers; every OpenAI-compatible one wants Bearer.

    A keyless provider sends neither — an empty ``Bearer`` header is worse than
    no header at all on a local endpoint that never asked for one.
    """
    if not api_key:
        return {}
    if profile.api_mode == "anthropic_messages":
        return {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION}
    return {"Authorization": f"Bearer {api_key}"}


def _model_ids(payload: Any) -> tuple[str, ...] | None:
    """Ids out of ``{"data": [...]}`` or a bare list (Together), deduped, in the
    provider's own order — sorting is presentation, and this is a faithful read.

    ``None`` means "I could not read this shape" — an empty tuple means the
    provider answered honestly with nothing, which is not a failure.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    ids: list[str] = []
    for row in rows:
        name: Any = row if isinstance(row, str) else None
        if isinstance(row, dict):
            name = row.get("id") or row.get("name")
        if isinstance(name, str) and name and name not in ids:
            ids.append(name)
    return tuple(ids)


def fetch_models(profile: ProviderProfile, *, api_key: str, client: Any) -> ProviderModels:
    """One provider's live list. Never raises; never echoes a body or the key."""
    try:
        response = client.get(models_endpoint(profile), headers=auth_headers(profile, api_key))
        if response.status_code != 200:
            # Status only: a 401/403 body routinely quotes the key back at you.
            return ProviderModels(profile.name, "error", detail=f"HTTP {response.status_code}")
        size = len(response.content)
        if size > MAX_RESPONSE_BYTES:
            return ProviderModels(profile.name, "error", detail=f"response too large ({size} B)")
        payload = response.json()
        ids = _model_ids(payload)
    except Exception as exc:  # noqa: BLE001 — one provider never sinks the catalog
        return ProviderModels(profile.name, "error", detail=type(exc).__name__)
    if ids is None:
        return ProviderModels(profile.name, "error", detail="unexpected response shape")
    if not ids:
        # Reachable with nothing enabled — the same fact as a live Ollama with no
        # models pulled, and reported the same way. An account legitimately empty
        # is not a broken provider.
        return ProviderModels(profile.name, "live", (), 0, "reachable, no models listed")
    return ProviderModels(profile.name, "live", ids, detail=_page_detail(payload, ids))


def _page_detail(payload: Any, ids: tuple[str, ...]) -> str | None:
    """Say so when the provider admits it handed back one page of many."""
    if isinstance(payload, dict) and payload.get("has_more") is True:
        return f"first page only ({len(ids)} ids) — the provider has more"
    return None


# --- per-provider branches ----------------------------------------------------


def _skipped(profile: ProviderProfile) -> ProviderModels:
    """No credential: say WHICH variable is missing, and touch no network."""
    names = " or ".join(profile.env_vars) or "an API key"
    return ProviderModels(profile.name, "skipped", detail=f"no API key — set {names}")


def _ollama_entry(probe: OllamaProbe) -> ProviderModels:
    """Ollama through its NATIVE probe (``/api/tags``), never ``{base_url}/models``
    — they are different endpoints and only the native one lists pulled models."""
    try:
        status = probe()
    except Exception as exc:  # noqa: BLE001 — an injected probe is untrusted too
        return ProviderModels("ollama", "error", detail=type(exc).__name__)
    if not status.alive:
        return ProviderModels("ollama", "error", detail=status.detail or "not running")
    if not status.models:
        return ProviderModels("ollama", "live", (), 0, "daemon alive, no models pulled")
    return ProviderModels("ollama", "live", tuple(status.models))


def _subscription_entry(home: Path | None) -> ProviderModels | None:
    """The Codex subscription model, if the opt-in store is usable.

    Independent of ``auth_preference``: the catalog reports what is AVAILABLE;
    which route an invocation takes is a different question. ``None`` means
    cleanly opted out — a store that BLEW UP reports an error instead, because a
    corrupt ``auth.json`` must not look identical to "never enabled".
    """
    try:
        from lohra.memory.paths import lohra_home
        from lohra.subscription import credentials
        from lohra.subscription.provider import codex_default_model

        root = home if home is not None else lohra_home()
        if not credentials.subscription_active(root):
            return None
        return ProviderModels(
            SUBSCRIPTION_PROVIDER, "config", (codex_default_model(),), detail=_SUBSCRIPTION_DETAIL
        )
    except Exception as exc:  # noqa: BLE001 — the catalog degrades, never raises
        return ProviderModels(SUBSCRIPTION_PROVIDER, "error", detail=type(exc).__name__)


def _select_profiles(providers: Sequence[str] | None) -> tuple[ProviderProfile, ...]:
    """Registered profiles, optionally narrowed by name or alias."""
    profiles = list_providers()
    if providers is None:
        return tuple(profiles)
    wanted = set()
    for name in providers:
        resolved = get_provider_profile(str(name))
        wanted.add(resolved.name if resolved else str(name).lower())
    return tuple(p for p in profiles if p.name in wanted)


def _wants_subscription(providers: Sequence[str] | None) -> bool:
    return providers is None or SUBSCRIPTION_PROVIDER in {str(n).lower() for n in providers}


# --- the catalog --------------------------------------------------------------


def build_catalog(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    *,
    client: Any | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    providers: Sequence[str] | None = None,
    ollama_probe: OllamaProbe | None = None,
) -> Catalog:
    """Every provider's reachable models. Fetches in parallel; never raises once
    the HTTP client exists (building/closing it is the blocked-in-tests seam).

    A provider with no key is ``skipped`` WITHOUT a network call — knowing the
    variable is missing needs no socket. ``client`` follows the ``probe_ollama``
    convention: injected means the caller owns it, and it is only created at all
    when something is actually fetchable.
    """
    profiles = _select_profiles(providers)
    entries: dict[str, ProviderModels] = {}
    fetchable: list[tuple[ProviderProfile, str]] = []

    for profile in profiles:
        # Ollama FIRST and by NAME: it is the one provider whose listing lives at a
        # different endpoint (native /api/tags), so neither branch below fits it.
        if profile.name == "ollama":
            entries[profile.name] = _ollama_entry(ollama_probe or detect.default_probe)
            continue
        api_key = resolve_api_key(profile, env)
        # Keylessness is a PROPERTY, not a name: a future local endpoint must not
        # be reported as missing a variable it never wanted.
        if api_key is None and profile.requires_api_key:
            entries[profile.name] = _skipped(profile)
            continue
        fetchable.append((profile, api_key or ""))

    if fetchable:
        owns = client is None
        active = client if client is not None else default_http_client(timeout)
        try:
            workers = min(_MAX_FETCH_WORKERS, len(fetchable))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for entry in pool.map(
                    lambda item: fetch_models(item[0], api_key=item[1], client=active), fetchable
                ):
                    entries[entry.provider] = entry
        finally:
            if owns:
                active.close()

    ordered = tuple(entries[p.name] for p in profiles if p.name in entries)
    if _wants_subscription(providers):
        subscription = _subscription_entry(home)
        if subscription is None and providers is not None:
            # Asked for BY NAME while opted out: report it as skipped with the
            # remedy. Silence here reads as "no such provider", which is a lie —
            # it is known, just not enabled. Unfiltered, an inactive subscription
            # stays absent (nothing to route to is not a finding).
            subscription = ProviderModels(
                SUBSCRIPTION_PROVIDER, "skipped", detail=_SUBSCRIPTION_OFF
            )
        if subscription is not None:
            ordered += (subscription,)
    return Catalog(ordered)
