"""Which provider, why, and what it costs (ONB-7, ONB-9).

Provider resolution used to answer one question — *which name* — and answer it
silently. Two real dogfood failures came out of that silence:

* A machine with a local Ollama daemon running had **no** way to be detected:
  the scan is by env var and nobody sets ``OLLAMA_API_KEY``. A keyless provider
  that is already up and answering is configuration; asking about it is a
  detection that was never written (ONB-7).
* With two keys exported, ``anthropic`` wins by registration order and nothing
  says so; and a ``--profile`` whose store has no subscription silently goes back
  to billing a paid API key while the shared home rides the subscription (ONB-9).

So this module resolves the name *and* keeps the reason attached to it, in one
immutable ``Resolution``. Rules that hold everywhere:

* **An explicit choice is never probed and never explained.** ``--provider``,
  ``LOHRA_PROVIDER`` and config short-circuit before any I/O: a configured
  machine behaves exactly as it did before this module existed.
* **The probe is injectable and only runs on the ``auto`` sentinel** — the branch
  whose current behavior is an immediate exit 2.
* **Everything here is written to stderr by the caller**, never stdout: stdout
  carries the agent's answer or the ``--json`` envelope.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lohra.onboarding import detect
from lohra.onboarding.messages import NO_PROVIDER_CONFIGURED

# How a provider name was reached. The first three are the user speaking; the
# last two are the machine guessing, and only those two get announced.
FLAG = "flag"
CONFIG = "config"
ENV_VAR = "env-var"
API_KEY = "api-key"
KEYLESS = "keyless"
NONE = "none"

AUTOMATIC_ORIGINS = (API_KEY, KEYLESS)
KEYLESS_PROVIDER = "ollama"


@dataclass(frozen=True)
class Resolution:
    """A provider name plus the reason it was chosen. Immutable, serializable.

    ``model`` is only ever set by the keyless path: ``ollama`` declares no
    ``fallback_models``, so the tag the daemon actually has pulled is the only
    default that exists. It is a *last* resort — ``--model`` and ``LOHRA_MODEL``
    still win.
    """

    provider: str | None
    origin: str
    model: str | None = None
    detail: str = ""  # the env var / URL that decided it; never a secret value
    error: str | None = None  # what to print when there is no provider at all

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "origin": self.origin,
            "model": self.model,
            "detail": self.detail,
        }


def resolve_choice(
    arg: str | None = None,
    *,
    config_value: str | None = None,
    env: Mapping[str, str] | None = None,
    probe: Callable[[], detect.OllamaStatus] | None = None,
) -> Resolution:
    """Resolve the provider exactly as the runtime always did, plus a keyless leg.

    Raises ``ValueError`` for an unknown explicit name — a user who typed a
    provider deserves the typo error, not a fallback that ignores them.
    """
    import os

    from lohra.providers import resolve_provider_name
    from lohra.providers.resolve import AUTO_PROVIDER

    environ = os.environ if env is None else env
    name = resolve_provider_name(arg=arg, config_value=config_value, env=environ)

    if name != AUTO_PROVIDER:
        return Resolution(provider=name, origin=_origin(arg, config_value, environ, name),
                          detail=_detail(arg, config_value, environ, name))

    # Nothing configured: a live local daemon with a model pulled IS a provider.
    status = (probe or detect.default_probe)()
    if status.alive and status.models:
        return Resolution(
            provider=KEYLESS_PROVIDER,
            origin=KEYLESS,
            model=status.models[0],
            detail=status.url,
        )
    # An alive-but-empty daemon is deliberately NOT a provider: ollama has no
    # fallback model, so choosing it here would trade the actionable "no provider
    # configured" for the opaque "has no default model — pass --model".
    return Resolution(provider=None, origin=NONE, error=NO_PROVIDER_CONFIGURED)


def _origin(arg, config_value, environ, name: str) -> str:
    from lohra.providers.resolve import ENV_PROVIDER_VAR

    if (arg or "").strip():
        return FLAG
    if (config_value or "").strip():
        return CONFIG
    if (environ.get(ENV_PROVIDER_VAR) or "").strip():
        return ENV_VAR
    return API_KEY


def _detail(arg, config_value, environ, name: str) -> str:
    """The thing that decided it — an env var name or the flag; never a value."""
    from lohra.providers import get_provider_profile
    from lohra.providers.resolve import ENV_PROVIDER_VAR

    origin = _origin(arg, config_value, environ, name)
    if origin == FLAG:
        return f"--provider {arg}"
    if origin == CONFIG:
        return "config"
    if origin == ENV_VAR:
        return ENV_PROVIDER_VAR
    profile = get_provider_profile(name)
    present = [var for var in (profile.env_vars if profile else ()) if environ.get(var)]
    return present[0] if present else ""


# --- ONB-9 (a): announce an automatic choice ---------------------------------


def transparency_line(resolution: Resolution, model: str | None) -> str | None:
    """One stderr line naming provider, model and *why* — or None to stay quiet.

    Silence is the contract for an explicit choice: a user who passed
    ``--provider`` or exported ``LOHRA_PROVIDER`` already knows, and a line on
    every single run would be noise nobody reads by the third time.
    """
    if resolution.provider is None or resolution.origin not in AUTOMATIC_ORIGINS:
        return None
    provider = resolution.provider
    what = f"provider {provider}" + (f", model {model}" if model else "")
    pin = f"pin it with --provider {provider} or LOHRA_PROVIDER={provider}"
    if resolution.origin == KEYLESS:
        return (
            f"→ {what} — no API key and no subscription found; "
            f"using the local Ollama daemon at {resolution.detail} ({pin})"
        )
    reason = f"auto-detected from {resolution.detail}" if resolution.detail else "auto-detected"
    return f"→ {what} — {reason} ({pin})"


# --- ONB-9 (b): the profile cost footgun -------------------------------------


def store_has_subscription(home: Path) -> bool:
    """Opt-in recorded AND ToS acknowledged in this store. Never raises."""
    from lohra.subscription.credentials import subscription_active

    try:
        return subscription_active(Path(home))
    except Exception:  # noqa: BLE001 — a diagnostic must not break the caller
        return False


def cost_warning(*, base: Path, home: Path, profile: str | None) -> str | None:
    """Warn when THIS profile will bill a paid key while the shared home would not.

    Subscription opt-in is per store and a profile deliberately inherits nothing,
    so ``--profile work`` silently returns to paid API usage. That is real money,
    discovered after the fact; the only honest place to say it is before the turn.
    Returns None whenever there is no real divergence — a paid key everywhere is
    not a surprise, and a warning that cries wolf stops being read.
    """
    if not profile:
        return None
    if not store_has_subscription(base) or store_has_subscription(home):
        return None
    # `--profile` is a subcommand option, not a global one: `lohra --profile x auth`
    # does not parse. Emitting the form that actually runs is the whole point of a
    # remedy — a command the user has to debug is not a remedy.
    return (
        f"⚠️  profile {profile!r} has no subscription of its own — this session bills your "
        f"paid API key. Enable it for this profile with: lohra auth enable --profile {profile}"
    )
