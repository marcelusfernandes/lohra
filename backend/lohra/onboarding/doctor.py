"""`lohra doctor` — the diagnosis you can run at any moment (ONB-6).

The failures this exists for are all failures of *silence*: a missing
``workflow_policy.json`` turns every workflow leaf deny-by-default without ever
naming the file; a malformed ``mcp.json`` logs a warning that drowns in the chat
stream; an absent ``.env`` is a no-op nobody sees; an absent ``auth.json`` falls
back to the paid path quietly. In the ``flutter doctor`` shape, each of those
becomes one line: **a check, its state, and the exact command that fixes it.**

Contracts:

* **Read-only and free.** No prompt, no client, no token, no write. It is the
  non-interactive sibling of ``lohra init`` and is safe in CI, in a pipe, in a
  container.
* **Never raises.** Every probe degrades to a line; a hostile home produces a
  report, not a traceback.
* **The exit code answers exactly one question — can Lohra answer you right now?**
  ``fail`` is reserved for "no path to an answer exists"; everything that is
  broken-but-survivable is a ``warn`` that still carries its remedy and still
  exits 0. That keeps ``lohra doctor`` usable as a script gate.
* **Every non-ok line carries a copyable command**, never a description of one.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from lohra.onboarding import choice, detect

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MAX_BYTES = 256_000
_PYTHON_RANGE = ">=3.11,<3.14"


@dataclass(frozen=True)
class Check:
    """One diagnosed fact: what was looked at, how it is, and how to fix it."""

    name: str
    state: str
    detail: str
    remedy: str = ""  # a command, always; empty only when there is nothing to fix

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "detail": self.detail,
            "remedy": self.remedy,
        }


# --- the checks ---------------------------------------------------------------


def run_checks(
    snapshot: detect.EnvironmentSnapshot,
    *,
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] | None = None,
) -> tuple[Check, ...]:
    """Diagnose ``snapshot`` (plus the config files it points at). Never raises."""
    import os

    environ = os.environ if env is None else env
    clock = time.time if now is None else now
    home = Path(snapshot.home)
    resolution = _resolution(snapshot, environ)

    return (
        _python_check(snapshot),
        _provider_check(snapshot, environ, resolution),
        _subscription_check(snapshot),
        _login_check(snapshot, clock),
        _profile_check(snapshot),
        _env_file_check(snapshot),
        _json_config_check(
            "mcp.json", home / "mcp.json", absent="not configured (no MCP servers)",
            summarize=_count_servers,
        ),
        _json_config_check(
            "cron/jobs.json", home / "cron" / "jobs.json", absent="not configured (no jobs)",
            summarize=_count_jobs,
        ),
        _workflow_policy_check(home),
        _json_config_check(
            "workflow_tiers.json", home / "workflow_tiers.json",
            absent="not configured (a node's own model decides)",
            summarize=_count_tiers, absent_remedy="lohra tiers suggest",
        ),
        _ollama_check(snapshot, resolution),
        _harness_check(snapshot),
    )


def _resolution(snapshot, environ) -> choice.Resolution | None:
    """How provider resolution WILL go, reusing the snapshot's probe result.

    Passing the already-taken Ollama status as the probe is what keeps doctor
    from ever contradicting the chat path — same resolver, same inputs, zero
    extra network. None means "not applicable" (the subscription route, an
    unusable preference, or a typo the resolver rejects).

    Gated on the ROUTE, not on the opt-in: under `lohra auth prefer api_key` the
    subscription is on file and chat still resolves a provider the normal way.
    """
    if snapshot.auth_route != "api_key":
        return None
    try:
        return choice.resolve_choice(env=environ, probe=lambda: snapshot.ollama)
    except ValueError:
        return None


def _python_check(snapshot) -> Check:
    if snapshot.python_supported:
        return Check("python", OK, f"{snapshot.python_version} (supported: {_PYTHON_RANGE})")
    # A warn, not a fail: this interpreter is demonstrably running Lohra right
    # now. It is out of the supported range, which is a real risk, not an outage.
    return Check(
        "python",
        WARN,
        f"{snapshot.python_version} — outside the supported range {_PYTHON_RANGE}",
        f"python3.12 -m pip install --force-reinstall lohra   # requires-python {_PYTHON_RANGE}",
    )


def _provider_check(snapshot, environ, resolution) -> Check:
    """The one check that can fail: is there any way to get an answer at all?"""
    if snapshot.auth_route == "unusable":
        # preference="subscription" with nothing to honour it: chat exits 2 here,
        # whatever keys exist. Naming `auth login` alone would send the user to
        # fix a route they are not on.
        return Check(
            "provider", FAIL,
            f"preference={snapshot.auth_preference} but subscription mode is not usable",
            "lohra auth login   # or take the key path: lohra auth prefer auto",
        )
    if snapshot.auth_route == "subscription":
        if snapshot.lohra_oauth_present or snapshot.codex_auth_present:
            return Check("provider", OK, "OpenAI/Codex subscription (opt-in, ToS-gray)")
        return Check(
            "provider", FAIL,
            "subscription mode is enabled but there is no login to use",
            "lohra auth login   # or reuse the Codex CLI login: codex login",
        )
    if resolution is None or resolution.provider is None:
        return Check(
            "provider", FAIL,
            "none configured — no API key, no subscription, no local daemon",
            'lohra init   # or: export ANTHROPIC_API_KEY=... | lohra auth enable | ollama serve',
        )
    # Delegate "is this provider actually workable" to the same function `init`
    # and the first-run wizard use, pinned to the name resolution just picked —
    # one truth, so a doctor line can never disagree with what chat does.
    from lohra.onboarding import wizard

    ready, message = wizard.evaluate(snapshot, dict(environ, LOHRA_PROVIDER=resolution.provider))
    if ready:
        detail = f"{resolution.provider} (from {resolution.origin}"
        detail += f": {resolution.detail})" if resolution.detail else ")"
        if resolution.model:
            detail += f", model {resolution.model}"
        return Check("provider", OK, detail)
    lines = message.splitlines()
    remedy = lines[1].strip() if len(lines) > 1 else "lohra init"
    return Check("provider", FAIL, lines[0], remedy)


def _subscription_check(snapshot) -> Check:
    """Always OK: this line reports state, and every state here is a choice.

    Including ``preference=api_key`` over an active subscription — the user asked
    for it, so a warn would be exactly the cry-wolf this file argues against. It
    is still named, because "active" alone would read as "and in use".
    """
    if snapshot.subscription_active:
        if snapshot.auth_route != "subscription":
            return Check(
                "subscription", OK,
                f"active, but preference={snapshot.auth_preference} — API keys are used "
                "(back: lohra auth prefer auto)",
            )
        return Check("subscription", OK, f"active (OpenAI/Codex) — {snapshot.home}/auth.json")
    return Check("subscription", OK, "off — API keys are used (enable: lohra auth enable)")


def _login_check(snapshot, clock) -> Check:
    """Lohra's own OAuth token: present and still valid? Never renders the token."""
    if not snapshot.lohra_oauth_present:
        return Check("login", OK, "no own login (subscription mode only: lohra auth login)")
    expires = snapshot.lohra_oauth_expires_at or 0.0
    if expires and expires <= clock():
        return Check(
            "login", WARN,
            f"own OAuth token expired ({_stamp(expires)})",
            "lohra auth login   # mints a fresh, auto-refreshing token",
        )
    return Check("login", OK, f"own OAuth token valid until {_stamp(expires)}")


def _profile_check(snapshot) -> Check:
    """The ONB-9 cost footgun, as a line: this workspace will bill a paid key."""
    if not snapshot.active_profile:
        return Check("profile", OK, f"none — shared home {snapshot.base}")
    warning = choice.cost_warning(
        base=Path(snapshot.base), home=Path(snapshot.home), profile=snapshot.active_profile
    )
    if warning is None:
        return Check("profile", OK, f"{snapshot.active_profile} — {snapshot.home}")
    return Check(
        "profile", WARN,
        f"{snapshot.active_profile} has no subscription of its own — it bills a paid API key",
        f"lohra auth enable --profile {snapshot.active_profile}",
    )


def _env_file_check(snapshot) -> Check:
    """``.env`` is deliberately global (base root), never per-profile."""
    from lohra.config.env_file import parse_env_text

    path = Path(snapshot.env_file)
    if not snapshot.env_file_present:
        return Check(".env", OK, f"{path} — not found; keys may come from the shell")
    text = _read(path)
    if text is None:
        return Check(
            ".env", WARN, f"{path} exists but could not be read",
            f"chmod 600 {path}   # and make sure it is a regular file, not a symlink",
        )
    keys = sorted(parse_env_text(text))  # NAMES only — never a value
    return Check(".env", OK, f"{path} — {len(keys)} key(s): {', '.join(keys) or 'none'}")


def _json_config_check(
    name: str, path: Path, *, absent: str, summarize, absent_remedy: str = ""
) -> Check:
    """An optional JSON config: absent is fine, malformed is a warning with a fix."""
    text = _read(path)
    if text is None:
        return Check(name, OK, f"{path} — {absent}", absent_remedy)
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return Check(
            name, WARN, f"{path} — invalid JSON ({exc.__class__.__name__})",
            f"python3 -m json.tool {path}   # shows the syntax error; fix it, then re-run",
        )
    return Check(name, OK, f"{path} — {summarize(data)}")


def _workflow_policy_check(home: Path) -> Check:
    """Absent is NOT harmless here: it is a total deny for every workflow leaf."""
    path = home / "workflow_policy.json"
    text = _read(path)
    if text is None:
        return Check(
            "workflow_policy.json", WARN,
            f"{path} not found — workflow leaves run deny-by-default (no fs, no egress)",
            f"""printf '{{"fs_allow": ["%s"], "egress_allow": []}}\\n' "$PWD" > {path}""",
        )
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        return Check(
            "workflow_policy.json", WARN, f"{path} — invalid JSON ({exc.__class__.__name__})",
            f"python3 -m json.tool {path}   # shows the syntax error; fix it, then re-run",
        )
    fs_allow = _entries(data, "fs_allow")
    egress = _entries(data, "egress_allow")
    return Check(
        "workflow_policy.json", OK,
        f"{path} — {len(fs_allow)} fs path(s), {len(egress)} egress host(s)",
    )


def _ollama_check(snapshot, resolution) -> Check:
    """Liveness of the keyless local path — a warning only when it IS the choice."""
    status = snapshot.ollama
    if status.alive:
        pulled = ", ".join(status.models) or "none pulled"
        return Check("ollama", OK, f"running at {status.url} — {len(status.models)} model(s): {pulled}")
    selected = resolution is not None and resolution.provider == choice.KEYLESS_PROVIDER
    return Check(
        "ollama", WARN if selected else OK,
        f"not running ({status.url})",
        "ollama serve   # keyless local models; no API key needed",
    )


def _harness_check(snapshot) -> Check:
    """Other agent harnesses that could orchestrate Lohra (`lohra chat --json`)."""
    found = [h for h in snapshot.harnesses if h.installed or h.home_present]
    if not found:
        return Check("harnesses", OK, "none found (claude / codex not on PATH)")
    names = ", ".join(f"{h.name}{'' if h.installed else ' (config only)'}" for h in found)
    dest = f"{found[0].home}/skills"
    return Check(
        "harnesses", OK, f"{names}",
        f"lohra skill export use-lohra --to {dest}   # let it drive Lohra",
    )


# --- summarizers and small readers --------------------------------------------


def _count_servers(data) -> str:
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        servers = data.get("servers") if isinstance(data, dict) else None
    return f"{len(servers)} server(s)" if isinstance(servers, dict) else "valid JSON"


def _count_jobs(data) -> str:
    jobs = data.get("jobs") if isinstance(data, dict) else data
    return f"{len(jobs)} job(s)" if isinstance(jobs, list) else "valid JSON"


def _count_tiers(data) -> str:
    tiers = data.get("tiers") if isinstance(data, dict) else None
    if not isinstance(tiers, dict):
        tiers = data if isinstance(data, dict) else None
    return f"{len(tiers)} tier(s)" if isinstance(tiers, dict) else "valid JSON"


def _entries(data, key: str) -> list:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


def _read(path: Path) -> str | None:
    """Bounded, symlink-safe read; None for absent/unreadable/not-a-regular-file."""
    from lohra.safeio import read_text_bounded

    try:
        return read_text_bounded(path, _MAX_BYTES)
    except Exception:  # noqa: BLE001 — a diagnostic never propagates
        return None


def _stamp(epoch: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OSError, ValueError, OverflowError):
        return "unknown"


# --- output -------------------------------------------------------------------


def exit_code(checks) -> int:
    """0 while some path to an answer exists; 2 when none does. Warns never fail."""
    return 2 if any(check.state == FAIL for check in checks) else 0


def render(checks) -> str:
    """The human report: one line per check, remedies indented under their line."""
    width = max(len(check.name) for check in checks)
    lines = ["Lohra doctor", ""]
    for check in checks:
        lines.append(f"[{check.state:<4}] {check.name:<{width}}  {check.detail}")
        if check.remedy:
            lines.append(f"{' ' * (width + 9)}→ {check.remedy}")
    counts = {state: sum(1 for c in checks if c.state == state) for state in (OK, WARN, FAIL)}
    lines += ["", f"{counts[OK]} ok, {counts[WARN]} warn, {counts[FAIL]} fail — " + (
        "nothing can answer yet; fix the fail line(s) above."
        if counts[FAIL]
        else "Lohra can answer."
    )]
    return "\n".join(lines) + "\n"


def doctor_payload(snapshot, checks) -> dict:
    """The `--json` object: the verdict, the lines, and the raw ONB-2 snapshot."""
    code = exit_code(checks)
    return {
        "ok": code == 0,
        "exit_code": code,
        "checks": [check.to_dict() for check in checks],
        "environment": snapshot.to_dict(),
    }


def run_doctor(
    *,
    json_output: bool = False,
    out=None,
    snapshot: detect.EnvironmentSnapshot | None = None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    """Print the report (or one JSON object) and return the exit code."""
    out = sys.stdout if out is None else out
    snapshot = detect.detect_environment() if snapshot is None else snapshot
    checks = run_checks(snapshot, env=env, now=now)
    if json_output:
        out.write(json.dumps(doctor_payload(snapshot, checks), ensure_ascii=True, sort_keys=True) + "\n")
    else:
        out.write(render(checks))
    return exit_code(checks)
