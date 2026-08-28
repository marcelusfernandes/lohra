"""Environment detection for onboarding (ONB-2) — a pure, immutable snapshot.

"Detect > ask > fail": every question to the user is a detection that was never
written. This module is the one place that looks at the machine, so the wizard,
``lohra init`` and ``lohra doctor`` all read the same truth.

Contract, load-bearing for every consumer:

* **Never raises.** Every probe fails to "unknown/absent"; a hostile or
  half-written file degrades, it does not propagate.
* **Never prompts, never writes.** Read-only, no token spent.
* **Bounded.** The only network call is a ~0.5s liveness GET to Ollama; the rest
  is ``stat``/``shutil.which``. Total budget ~1s.
* **Injectable.** ``env``/``base``/``user_home``/``which``/``ollama_probe``/
  ``stdin``/``stderr`` are all parameters, so a test never touches the real
  machine and production keeps the ambient defaults.
* **Secret-free.** Key *names* are reported, never key values.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from lohra.memory.paths import lohra_base, validate_profile_name

OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
OLLAMA_TIMEOUT = 0.5  # a local daemon answers in milliseconds or it isn't there
MIN_PYTHON = (3, 11)
MAX_PYTHON_EXCLUSIVE = (3, 14)  # backend/pyproject.toml: requires-python
HARNESSES = ("claude", "codex")


# --- pieces of the snapshot ---------------------------------------------------


@dataclass(frozen=True)
class ProviderKeyStatus:
    """Which of a provider's API-key env vars are actually set (names only)."""

    provider: str
    display_name: str
    env_vars: tuple[str, ...]
    present_vars: tuple[str, ...]
    requires_api_key: bool

    @property
    def configured(self) -> bool:
        return bool(self.present_vars)

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "display_name": self.display_name,
            "env_vars": list(self.env_vars),
            "present_vars": list(self.present_vars),
            "requires_api_key": self.requires_api_key,
            "configured": self.configured,
        }


@dataclass(frozen=True)
class OllamaStatus:
    """Liveness of the local Ollama daemon (keyless fallback, ONB-7)."""

    alive: bool
    url: str
    models: tuple[str, ...] = ()
    detail: str = ""  # short reason when dead; never a stack trace

    def to_dict(self) -> dict:
        return {
            "alive": self.alive,
            "url": self.url,
            "models": list(self.models),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class HarnessStatus:
    """Another agent harness that could orchestrate Lohra (ONB-14)."""

    name: str
    path: str | None
    home: str
    home_present: bool

    @property
    def installed(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "home": self.home,
            "home_present": self.home_present,
            "installed": self.installed,
        }


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """Everything onboarding knows about this machine. Immutable, serializable."""

    python_version: str
    python_supported: bool
    platform: str
    os_name: str
    stdin_tty: bool
    stderr_tty: bool
    active_profile: str | None
    base: str
    home: str
    env_file: str
    env_file_present: bool
    providers: tuple[ProviderKeyStatus, ...]
    detected_provider: str | None
    provider_origin: str  # "env-var" | "api-key" | "none"
    provider_error: str | None
    subscription_active: bool
    base_subscription_active: bool
    lohra_auth_present: bool
    lohra_oauth_present: bool
    lohra_oauth_expires_at: float | None
    codex_home: str
    codex_auth_present: bool
    ollama: OllamaStatus
    harnesses: tuple[HarnessStatus, ...] = field(default_factory=tuple)
    # Which route the user ASKED for (auth.json, per profile). Defaults reproduce
    # today's behaviour exactly, so a hand-built snapshot keeps meaning the same.
    auth_preference: str = "auto"
    base_auth_preference: str = "auto"

    @property
    def interactive(self) -> bool:
        """Whether a prompt is allowed: both the reader and the writer are a TTY."""
        return self.stdin_tty and self.stderr_tty

    @property
    def has_api_key(self) -> bool:
        return any(p.configured for p in self.providers)

    @property
    def auth_route(self) -> str:
        """"subscription" | "api_key" | "unusable" — the path chat WILL take.

        Not the same question as ``subscription_active`` ("is the opt-in on
        file?"): a user who ran ``lohra auth prefer api_key`` keeps the opt-in
        and still rides the key. Answered by the one truth table in
        ``subscription.credentials``, never by a second copy of it here.
        """
        from lohra.subscription.credentials import route_for

        route = route_for(self.auth_preference, self.subscription_active)
        return "unusable" if route.error else route.mode

    @property
    def base_auth_route(self) -> str:
        """The shared home's route — what the profile is being compared against."""
        from lohra.subscription.credentials import route_for

        route = route_for(self.base_auth_preference, self.base_subscription_active)
        return "unusable" if route.error else route.mode

    @property
    def usable(self) -> bool:
        """Whether *some* path to an answer exists (key, subscription, or Ollama).

        An unusable preference (``subscription`` with no usable subscription)
        makes the answer False even with a key in hand: chat refuses first,
        by design, rather than falling back onto a billed key silently.
        """
        if self.auth_route == "unusable":
            return False
        return self.has_api_key or self.auth_route == "subscription" or self.ollama.alive

    @property
    def subscription_divergence(self) -> bool:
        """A profile billing an API key while the base home rides a subscription (ONB-9).

        Route, not opt-in: a base that opted in but prefers the key path bills
        the same key this profile does, and there is no divergence to announce.
        """
        return (
            bool(self.active_profile)
            and self.base_auth_route == "subscription"
            and self.auth_route != "subscription"
        )

    def to_dict(self) -> dict:
        return {
            "python_version": self.python_version,
            "python_supported": self.python_supported,
            "platform": self.platform,
            "os_name": self.os_name,
            "stdin_tty": self.stdin_tty,
            "stderr_tty": self.stderr_tty,
            "interactive": self.interactive,
            "active_profile": self.active_profile,
            "base": self.base,
            "home": self.home,
            "env_file": self.env_file,
            "env_file_present": self.env_file_present,
            "providers": [p.to_dict() for p in self.providers],
            "detected_provider": self.detected_provider,
            "provider_origin": self.provider_origin,
            "provider_error": self.provider_error,
            "has_api_key": self.has_api_key,
            "auth_preference": self.auth_preference,
            "auth_route": self.auth_route,
            "base_auth_preference": self.base_auth_preference,
            "subscription_active": self.subscription_active,
            "base_subscription_active": self.base_subscription_active,
            "subscription_divergence": self.subscription_divergence,
            "lohra_auth_present": self.lohra_auth_present,
            "lohra_oauth_present": self.lohra_oauth_present,
            "lohra_oauth_expires_at": self.lohra_oauth_expires_at,
            "codex_home": self.codex_home,
            "codex_auth_present": self.codex_auth_present,
            "ollama": self.ollama.to_dict(),
            "harnesses": [h.to_dict() for h in self.harnesses],
            "usable": self.usable,
        }


# --- individual probes (each one falls back to "unknown", never raises) -------


def probe_ollama(
    *,
    client=None,
    timeout: float = OLLAMA_TIMEOUT,
    url: str = OLLAMA_TAGS_URL,
) -> OllamaStatus:
    """Is a local Ollama daemon answering? ``GET /api/tags``, no auth, ~0.5s.

    Deliberately the NATIVE API, not the provider's OpenAI-compatible ``base_url``
    (``/v1``): they are different endpoints and only this one lists pulled models.
    Any failure — refused, timeout, non-200, non-JSON — means "dead".
    """
    try:
        import httpx

        owns = client is None
        active = client or httpx.Client(timeout=timeout, follow_redirects=False)
        try:
            response = active.get(url)
            if response.status_code != 200:
                return OllamaStatus(alive=False, url=url, detail=f"HTTP {response.status_code}")
            payload = response.json()
        finally:
            if owns:
                active.close()
    except Exception as exc:  # noqa: BLE001 — liveness probes never propagate
        return OllamaStatus(alive=False, url=url, detail=type(exc).__name__)
    return OllamaStatus(alive=True, url=url, models=_model_names(payload))


def default_probe() -> OllamaStatus:
    """The ambient probe every consumer defaults to — the ONE injection seam.

    ``detect_environment``, provider resolution (``choice``) and ``doctor`` all
    reach the daemon through this name, so a test suite neutralizes the network
    by patching exactly one attribute. ``probe_ollama`` stays untouched for its
    own tests, which exercise the real implementation against a mock transport.
    """
    return probe_ollama()


def _model_names(payload: object) -> tuple[str, ...]:
    """Model names out of an ``/api/tags`` body; () for any unexpected shape."""
    if not isinstance(payload, dict):
        return ()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    return tuple(
        entry["name"] for entry in models if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    )


def provider_key_statuses(env: Mapping[str, str]) -> tuple[ProviderKeyStatus, ...]:
    """Per-provider API-key presence, in registration order (= detection order).

    "Present" is plain truthiness, deliberately the SAME rule the resolver's key
    scan uses (``resolve.resolve_provider_name``): detection exists to predict
    what the chat path will do, so a value the resolver would accept must not be
    reported here as missing.
    """
    from lohra.providers import list_providers

    return tuple(
        ProviderKeyStatus(
            provider=profile.name,
            display_name=profile.display_name or profile.name,
            env_vars=tuple(profile.env_vars),
            present_vars=tuple(var for var in profile.env_vars if env.get(var)),
            requires_api_key=profile.requires_api_key,
        )
        for profile in list_providers()
    )


def detect_harnesses(
    env: Mapping[str, str],
    *,
    user_home: Path,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[HarnessStatus, ...]:
    """Agent harnesses on PATH plus their config homes (``$CODEX_HOME`` honored)."""
    homes = {"claude": user_home / ".claude", "codex": codex_home(env, user_home=user_home)}
    found = []
    for name in HARNESSES:
        try:
            path = which(name)
        except Exception:  # noqa: BLE001 — a broken PATH must not kill detection
            path = None
        home = homes[name]
        found.append(
            HarnessStatus(
                name=name, path=path, home=str(home), home_present=_is_dir(home)
            )
        )
    return tuple(found)


def codex_home(env: Mapping[str, str], *, user_home: Path) -> Path:
    """``$CODEX_HOME`` or ``~/.codex`` — the same override codex_creds respects."""
    override = (env.get("CODEX_HOME") or "").strip()
    return Path(override) if override else user_home / ".codex"


def _is_file(path: Path) -> bool:
    try:
        return path.is_file()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _isatty(stream) -> bool:
    """Whether ``stream`` is a terminal; a wrapper that raises counts as "no"."""
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001
        return False


def _profile_from_env(env: Mapping[str, str]) -> str | None:
    """The active profile name, or None — an invalid value degrades to None.

    Validation is the same anti-traversal allowlist the path layer enforces; a
    hostile ``LOHRA_PROFILE`` must not make detection raise.
    """
    name = (env.get("LOHRA_PROFILE") or "").strip()
    if not name:
        return None
    try:
        return validate_profile_name(name)
    except ValueError:
        return None


def _resolve_choice(env: Mapping[str, str]) -> tuple[str | None, str, str | None]:
    """(provider, origin, error) as ``resolve_provider_name`` would decide it."""
    from lohra.providers import resolve_provider_name
    from lohra.providers.resolve import AUTO_PROVIDER, ENV_PROVIDER_VAR

    try:
        name = resolve_provider_name(env=env)
    except ValueError as exc:
        return None, "none", str(exc)
    if name == AUTO_PROVIDER:
        return None, "none", None
    origin = "env-var" if (env.get(ENV_PROVIDER_VAR) or "").strip() else "api-key"
    return name, origin, None


def _auth_store_snapshot(home: Path) -> tuple[bool, str]:
    """(active, preference) from ONE read of the store — two separate reads
    could interleave with an `auth enable`/`prefer` and report a combination
    that never existed on disk. Unreadable/absent → (False, "auto")."""
    from lohra.subscription.store import read_config

    try:
        config = read_config(home)
    except Exception:  # noqa: BLE001 — fail closed, never raise
        return False, "auto"
    if config is None:
        return False, "auto"
    return bool(config.active), config.preference


def _subscription_active(home: Path) -> bool:
    """Opt-in + ToS acknowledged for this store. Unreadable/absent → False."""
    return _auth_store_snapshot(home)[0]


def _auth_preference(home: Path) -> str:
    """The stored auth preference. Unreadable/absent/garbage -> "auto"."""
    return _auth_store_snapshot(home)[1]


def _oauth_expiry(home: Path) -> tuple[bool, float | None]:
    """(present, expires_at) for Lohra's own login. Never returns the token."""
    from lohra.subscription import token_store

    try:
        tokens = token_store.read_tokens(home)
    except Exception:  # noqa: BLE001
        return False, None
    if tokens is None:
        return False, None
    return True, tokens.expires_at


# --- the snapshot -------------------------------------------------------------


def detect_environment(
    *,
    env: Mapping[str, str] | None = None,
    base: Path | None = None,
    user_home: Path | None = None,
    ollama_probe: Callable[[], OllamaStatus] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    stdin=None,
    stderr=None,
    version_info: tuple[int, ...] | None = None,
) -> EnvironmentSnapshot:
    """Snapshot this machine. Pure with respect to its arguments; never raises.

    ``env``/``base``/``user_home`` default to the ambient process state, so
    production calls this with no arguments; tests pin all three. ``home`` is
    derived from ``base`` + the active profile exactly as ``lohra_home()`` does,
    keeping an injected ``env`` coherent with the paths it implies.
    """
    environ = os.environ if env is None else env
    root = base if base is not None else lohra_base()
    who = user_home if user_home is not None else Path.home()
    version = tuple(version_info or sys.version_info[:3])

    profile = _profile_from_env(environ)
    home = root / "profiles" / profile if profile else root
    detected, origin, error = _resolve_choice(environ)
    oauth_present, oauth_expiry = _oauth_expiry(home)
    ollama = (ollama_probe or default_probe)()

    # One store read per home: paired (active, preference) can never be a
    # combination that briefly never existed on disk (TOCTOU with `auth enable`).
    home_auth = _auth_store_snapshot(home)
    base_auth = home_auth if root == home else _auth_store_snapshot(root)
    return EnvironmentSnapshot(
        python_version=".".join(str(part) for part in version[:3]),
        python_supported=MIN_PYTHON <= tuple(version[:2]) < MAX_PYTHON_EXCLUSIVE,
        platform=sys.platform,
        os_name=os.name,
        stdin_tty=_isatty(sys.stdin if stdin is None else stdin),
        stderr_tty=_isatty(sys.stderr if stderr is None else stderr),
        active_profile=profile,
        base=str(root),
        home=str(home),
        # ``.env`` is deliberately global: it lives on the base root and is never
        # read per-profile (see lohra.config.env_file and cli.main).
        env_file=str(root / ".env"),
        env_file_present=_is_file(root / ".env"),
        providers=provider_key_statuses(environ),
        detected_provider=detected,
        provider_origin=origin,
        provider_error=error,
        subscription_active=home_auth[0],
        base_subscription_active=base_auth[0],
        auth_preference=home_auth[1],
        base_auth_preference=base_auth[1],
        lohra_auth_present=_is_file(home / "auth.json"),
        lohra_oauth_present=oauth_present,
        lohra_oauth_expires_at=oauth_expiry,
        codex_home=str(codex_home(environ, user_home=who)),
        codex_auth_present=_is_file(codex_home(environ, user_home=who) / "auth.json"),
        ollama=ollama,
        harnesses=detect_harnesses(environ, user_home=who, which=which),
    )


def snapshot_json(snapshot: EnvironmentSnapshot) -> str:
    """The snapshot as a single JSON line — for `doctor --json` and logs."""
    return json.dumps(snapshot.to_dict(), ensure_ascii=True, sort_keys=True)
