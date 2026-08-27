"""Onboarding — the path between `pip install lohra` and the first real answer.

Installation is mute (PEP 427 wheels have no post-install hook), so the whole
onboarding surface lives in the CLI: detect the environment (``detect``), and
say the right thing when something is missing (``messages``).

``detect`` is a pure snapshot, ``choice`` decides which provider answers and why
(including the keyless Ollama fallback), ``doctor`` turns both into an actionable
report, and ``messages`` is static text — none of them prompts, writes, or spends
a token. ``wizard`` is the one interactive layer (`lohra init` and the first-run offer),
``auth_login`` is its ToS-consent sibling for `lohra auth login`, and ``env_write``
is the only thing here that touches disk; all three are gated so a headless caller
never reaches a prompt.

See docs/roadmap/onboarding/README.md.
"""

from lohra.onboarding.auth_login import Consent, ensure_opt_in
from lohra.onboarding.choice import Resolution, cost_warning, resolve_choice, transparency_line
from lohra.onboarding.detect import EnvironmentSnapshot, detect_environment, probe_ollama
from lohra.onboarding.doctor import Check, run_checks, run_doctor
from lohra.onboarding.env_write import upsert_env_file
from lohra.onboarding.messages import NO_PROVIDER_CONFIGURED
from lohra.onboarding.wizard import offer_wizard, run_init, should_offer_wizard

__all__ = [
    "Consent",
    "ensure_opt_in",
    "EnvironmentSnapshot",
    "detect_environment",
    "probe_ollama",
    "Resolution",
    "resolve_choice",
    "transparency_line",
    "cost_warning",
    "Check",
    "run_checks",
    "run_doctor",
    "NO_PROVIDER_CONFIGURED",
    "upsert_env_file",
    "offer_wizard",
    "run_init",
    "should_offer_wizard",
]
