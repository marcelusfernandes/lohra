"""`lohra init` and the first-run wizard (ONB-3, ONB-4, ONB-5).

The one interactive moment onboarding is allowed to have. A wheel install runs no
code (PEP 427), so the first command is the only place left to guide anyone —
and it has to guide without ever breaking the contracts the runtime already sells:

* **Prompts take streams, never ``builtins.input``.** ``Prompter`` reads a reader
  and writes a writer, so a test scripts an answer without touching a terminal
  and the chat wizard can keep every byte on stderr (stdout carries the agent's
  answer, or the ``--json`` envelope).
* **Enter always resolves.** Every question has a default, and EOF counts as
  Enter — a closed stdin can never hang the process.
* **Enter never writes unless it fixes something.** Each default is the current
  *effective* value, and only a real change reaches ``.env``. That single rule
  gives idempotence (a second all-Enter run is byte-identical), "never overwrite
  without confirmation" (typing IS the confirmation), and a virgin machine with
  Ollama running still ending usable on Enter alone.
* **Headless never prompts.** ``should_offer_wizard`` is the gate, and it closes
  on ``--json`` before it even looks at the terminal.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from lohra.onboarding import detect, env_write
from lohra.onboarding.messages import NO_PROVIDER_CONFIGURED

MARKER_NAME = ".initialized"
KIT_NAME = "use-lohra"
PROMPT_SUFFIX = "]: "  # every question ends with it, so prompts are countable
NO_WIZARD_VAR = "LOHRA_NO_WIZARD"
PROVIDER_VAR = "LOHRA_PROVIDER"
MODEL_VAR = "LOHRA_MODEL"


# --- prompting ----------------------------------------------------------------


@dataclass(frozen=True)
class Prompter:
    """Ask on ``writer``, read from ``reader``; EOF and a blank line mean "default"."""

    reader: object
    writer: object

    def ask(self, label: str, *, default: str = "") -> str:
        self._emit(f"{label} [{default or 'skip'}{PROMPT_SUFFIX}")
        return self._readline() or default

    def confirm(self, label: str, *, default: bool) -> bool:
        self._emit(f"{label} [{'Y/n' if default else 'y/N'}{PROMPT_SUFFIX}")
        answer = self._readline().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        return default

    def note(self, text: str) -> None:
        self._emit(f"{text}\n")

    def _emit(self, text: str) -> None:
        try:
            self.writer.write(text)
            self.writer.flush()
        except Exception:  # noqa: BLE001 — a broken pipe must not kill onboarding
            pass

    def _readline(self) -> str:
        try:
            return (self.reader.readline() or "").strip()
        except Exception:  # noqa: BLE001 — unreadable stdin behaves like EOF
            return ""


# --- the "already onboarded" marker (per workspace, like every other signal) ---


def marker_path(home: Path) -> Path:
    """``<home>/.initialized`` — profile-aware, because ``home`` already is.

    A fresh ``--profile work`` must be offered the wizard even though the base
    home was onboarded: isolation is the entire point of a profile, and it is the
    profile's store that decides whether that workspace can answer at all.
    """
    return Path(home) / MARKER_NAME


def marker_present(home: Path) -> bool:
    try:
        return marker_path(home).exists()
    except OSError:
        return False


def write_marker(home: Path) -> Path:
    """Record that this workspace was offered onboarding. Never fatal."""
    path = marker_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("onboarding offered — re-run `lohra init` any time.\n", encoding="utf-8")
    except OSError:
        pass
    return path


# --- the gate (ONB-5): the only thing standing between a pipe and a prompt ------


def should_offer_wizard(
    *,
    provider_arg: str | None = None,
    json_output: bool = False,
    no_input: bool = False,
    env=None,
    stdin=None,
    stderr=None,
    home: Path | None = None,
) -> bool:
    """Whether `lohra chat` may interrupt with the first-run wizard.

    Ordered cheapest-and-most-absolute first: a machine contract (``--json``) beats
    an operator flag, which beats an environment opt-out, which beats the terminal
    test, which beats "we already asked", which beats "there is nothing to fix".
    """
    if json_output or no_input:
        return False
    environ = os.environ if env is None else env
    if (environ.get(NO_WIZARD_VAR) or "").strip():
        return False
    if not (detect._isatty(sys.stdin if stdin is None else stdin)
            and detect._isatty(sys.stderr if stderr is None else stderr)):
        return False
    if marker_present(_home(home)):
        return False
    return _would_fall_through(provider_arg, environ)


def _would_fall_through(provider_arg: str | None, environ) -> bool:
    """True only when nothing at all can answer — not even a keyless daemon.

    Asks the same resolver the chat path will ask (``choice.resolve_choice``), so
    the wizard and the runtime can never disagree about whether this machine is
    configured. That includes ONB-7: a local Ollama daemon that is already up IS
    configuration, and the right response to it is to use it, not to survey the
    user about it. This is the LAST check in the gate, so the probe only ever runs
    on a virgin interactive terminal.

    An unknown ``--provider`` deliberately returns False: that user made a choice
    and deserves ``_resolve_profile``'s "unknown provider" error, not a wizard.
    """
    from lohra.onboarding import choice

    try:
        return choice.resolve_choice(provider_arg, env=environ).provider is None
    except ValueError:
        return False


# --- the report ---------------------------------------------------------------


def print_report(snapshot: detect.EnvironmentSnapshot, out) -> None:
    """One line per fact, in the ``flutter doctor`` shape ONB-6 will extend."""
    lines = [
        "Lohra — environment",
        _row("python", f"{snapshot.python_version}"
             f"{'' if snapshot.python_supported else '  (unsupported: needs >=3.11,<3.14)'}"),
        _row("home", f"{snapshot.home}  (profile: {snapshot.active_profile or 'none'})"),
        _row(".env", f"{snapshot.env_file}  ({'found' if snapshot.env_file_present else 'not found'})"),
        _row("provider", _provider_line(snapshot)),
        _row("subscription", _subscription_line(snapshot)),
        _row("ollama", _ollama_line(snapshot.ollama)),
        _row("harnesses", _harness_line(snapshot)),
    ]
    out.write("\n".join(lines) + "\n\n")


def _subscription_line(snapshot) -> str:
    """State AND whether it is the route in use — "active" alone reads as "in use"."""
    if not snapshot.subscription_active:
        return "off"
    if snapshot.auth_route != "subscription":
        return f"active, but preference={snapshot.auth_preference} — API keys are used"
    return "active (OpenAI/Codex)"


def _row(label: str, value: str) -> str:
    return f"  {label:<13}{value}"


def _provider_line(snapshot: detect.EnvironmentSnapshot) -> str:
    if snapshot.provider_error:
        return f"error: {snapshot.provider_error}"
    if not snapshot.detected_provider:
        keys = [var for p in snapshot.providers for var in p.present_vars]
        return f"none detected (keys seen: {', '.join(keys)})" if keys else "none detected"
    return f"{snapshot.detected_provider}  (from {snapshot.provider_origin})"


def _ollama_line(ollama: detect.OllamaStatus) -> str:
    if not ollama.alive:
        return f"not running ({ollama.url})"
    return f"running — {len(ollama.models)} model(s): {', '.join(ollama.models) or 'none pulled'}"


def _harness_line(snapshot: detect.EnvironmentSnapshot) -> str:
    found = [h.name for h in snapshot.harnesses if h.installed or h.home_present]
    return ", ".join(found) if found else "none found"


# --- the three questions ------------------------------------------------------


def run_configure(
    snapshot: detect.EnvironmentSnapshot,
    prompter: Prompter,
    *,
    base: Path,
    home: Path,
    environ,
    out,
) -> tuple[str, ...]:
    """Ask at most three questions, persist only what changed; return written keys."""
    updates: dict[str, str] = {}
    chosen = _ask_provider(snapshot, prompter, updates)
    _ask_credential_or_model(snapshot, prompter, chosen, environ, updates)

    written = env_write.upsert_env_file(Path(base) / ".env", updates) if updates else ()
    environ.update(updates)  # take effect in THIS process, not just the next one

    _offer_kit(snapshot, prompter)
    _print_outcome(snapshot, environ, out)
    return written


def _ask_provider(snapshot, prompter: Prompter, updates: dict[str, str]) -> str:
    """Question 1. The default is what the machine already implies, if anything."""
    names = [p.provider for p in snapshot.providers]
    suggested = snapshot.detected_provider or ("ollama" if snapshot.ollama.alive else "")
    answer = prompter.ask(f"provider ({'/'.join(names)})", default=suggested)
    if answer and answer not in names:
        prompter.note(f"  unknown provider {answer!r} — keeping {suggested or 'none'}.")
        answer = suggested
    if answer and answer != snapshot.detected_provider:
        updates[PROVIDER_VAR] = answer
    return answer


def _ask_credential_or_model(snapshot, prompter: Prompter, chosen: str, environ, updates) -> None:
    """Question 2, whichever of the two the machine cannot answer by itself."""
    from lohra.providers import get_provider_profile

    profile = get_provider_profile(chosen) if chosen else None
    if profile is None:
        return
    if profile.requires_api_key and not any(environ.get(var) for var in profile.env_vars):
        var = profile.env_vars[0]
        key = prompter.ask(f"{var} (paste it, or Enter to skip)", default="")
        if key:
            updates[var] = key
        return
    current = (environ.get(MODEL_VAR) or "").strip() or _default_model(profile)
    # A provider with no fallback (ollama) has no floating default, so what the
    # daemon actually has pulled is the only real suggestion we can make.
    suggested = current or (snapshot.ollama.models[0] if chosen == "ollama" and snapshot.ollama.models else "")
    picked = prompter.ask("default model", default=suggested)
    if picked and picked != current:
        updates[MODEL_VAR] = picked


def _default_model(profile) -> str:
    return profile.fallback_models[0] if profile.fallback_models else ""


def _offer_kit(snapshot, prompter: Prompter) -> None:
    """Question 3, and only when there is actually a harness to export to."""
    from lohra.skills.exportkit import write_exportable

    present = [h for h in snapshot.harnesses if h.installed or h.home_present]
    if not present:
        return
    labels = ", ".join(h.name for h in present)
    if not prompter.confirm(f"export the {KIT_NAME} kit to {labels}?", default=False):
        return
    for harness in present:
        dest = Path(harness.home) / "skills"
        try:
            written = write_exportable(KIT_NAME, dest)
        except (KeyError, OSError) as exc:
            prompter.note(f"  could not export to {dest}: {exc}")
            continue
        prompter.note(f"  wrote {written}")


# --- outcome ------------------------------------------------------------------


def evaluate(snapshot, environ) -> tuple[bool, str]:
    """(ready, the one line to print) for the state after the questions.

    Deliberately about the provider that was CHOSEN, not about the machine in
    general. A key for some other provider does not make your choice work, and a
    configured provider whose prerequisite is merely missing is not "no provider
    configured" — saying that right after configuring one is a lie the user has to
    debug. Recomputed from the same snapshot, so no probe runs twice.
    """
    from lohra.providers import get_provider_profile

    # An unusable preference outranks every key on the machine: chat refuses
    # before it ever resolves a provider, so "ready — provider anthropic" here
    # would be a line the user has to debug against a rc-2 chat.
    if snapshot.auth_route == "unusable":
        return False, (
            f"not ready — preference={snapshot.auth_preference} but subscription mode "
            "is not usable.\n  lohra auth login   # or take the key path: "
            "lohra auth prefer auto"
        )

    provider = (environ.get(PROVIDER_VAR) or "").strip() or snapshot.detected_provider or ""
    if not provider:
        # The ROUTE, not the opt-in: `lohra auth prefer api_key` keeps the opt-in
        # on file while chat rides a key, and calling that "ready" would be the
        # same lie this function's docstring refuses to tell.
        if snapshot.auth_route == "subscription":
            return True, "ready — OpenAI/Codex subscription (opt-in)."
        # ONB-7: a local daemon that is already up, with a model pulled, IS a
        # provider — it is what `lohra chat` will actually use. Reporting "no
        # provider configured" for a machine that answers would make `init` and
        # `doctor` contradict each other about one state.
        if snapshot.ollama.alive and snapshot.ollama.models:
            return True, (
                f"ready — provider ollama (local, keyless), "
                f"model {snapshot.ollama.models[0]}."
            )
        return False, NO_PROVIDER_CONFIGURED

    profile = get_provider_profile(provider)
    if profile is None:
        return False, f"provider {provider!r} is selected but unknown — re-run `lohra init`."
    if profile.requires_api_key and not any(environ.get(var) for var in profile.env_vars):
        return False, (
            f"provider {provider} is selected, but {profile.env_vars[0]} is not set.\n"
            f"  export {profile.env_vars[0]}=...   (or add it to {snapshot.env_file}, "
            f"or re-run `lohra init`)"
        )
    if provider == "ollama" and not snapshot.ollama.alive:
        return False, (
            f"provider ollama is selected, but no daemon answered at {snapshot.ollama.url}.\n"
            '  start it with:  ollama serve       then:  lohra chat "oi"'
        )
    model = (environ.get(MODEL_VAR) or "").strip() or _default_model(profile)
    return True, f"ready — provider {provider}{f', model {model}' if model else ''}."


def _print_outcome(snapshot, environ, out) -> None:
    out.write(evaluate(snapshot, environ)[1] + "\n")


# --- the two entry points -----------------------------------------------------


def run_init(
    *,
    no_input: bool = False,
    out=None,
    err=None,
    reader=None,
    snapshot: detect.EnvironmentSnapshot | None = None,
    base: Path | None = None,
    home: Path | None = None,
    environ=None,
) -> int:
    """`lohra init` — report, then (only on a terminal) at most three questions."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    reader = sys.stdin if reader is None else reader
    environ = os.environ if environ is None else environ
    base = _base(base)
    home = _home(home)
    snapshot = detect.detect_environment() if snapshot is None else snapshot

    print_report(snapshot, out)
    if no_input or not snapshot.interactive:
        # Read-only mode is a report, not a half-run: no prompt, no write, and
        # deliberately NO marker — nobody was asked anything to remember.
        _print_outcome(snapshot, environ, out)
        return 0

    run_configure(snapshot, Prompter(reader, err), base=base, home=home, environ=environ, out=out)
    write_marker(home)
    return 0


def offer_wizard(
    *,
    err=None,
    reader=None,
    snapshot: detect.EnvironmentSnapshot | None = None,
    base: Path | None = None,
    home: Path | None = None,
    environ=None,
) -> bool:
    """The ONB-4 first-run wizard, inline in `lohra chat`. Everything on stderr.

    Returns whether anything was configured. Either way the marker is written:
    "no" is an answer, and re-asking every turn would be worse than the error.
    """
    err = sys.stderr if err is None else err
    reader = sys.stdin if reader is None else reader
    environ = os.environ if environ is None else environ
    base = _base(base)
    home = _home(home)
    snapshot = detect.detect_environment() if snapshot is None else snapshot

    err.write("\nLohra is not configured yet.\n\n")
    print_report(snapshot, err)
    prompter = Prompter(reader, err)
    if not prompter.confirm("configure now?", default=True):
        write_marker(home)
        err.write("skipped — run `lohra init` whenever you want to do this.\n")
        return False

    run_configure(snapshot, prompter, base=base, home=home, environ=environ, out=err)
    write_marker(home)
    return evaluate(snapshot, environ)[0]


def _base(base: Path | None) -> Path:
    from lohra.memory.paths import lohra_base

    return lohra_base() if base is None else Path(base)


def _home(home: Path | None) -> Path:
    from lohra.memory.paths import lohra_home

    return lohra_home() if home is None else Path(home)
