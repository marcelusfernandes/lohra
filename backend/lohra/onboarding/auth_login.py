"""The ToS consent gate that `lohra auth login` runs on its way in (ONB-8).

Typing ``lohra auth login`` IS the declaration of intent — "I want to use my
ChatGPT/Codex subscription". Making that user first discover ``lohra auth enable``
put paperwork in front of an intention already stated, so the acknowledgement
moved *into* the login. What did NOT move is the opt-in itself: the warning is
still printed every time, the answer is still explicitly given, and the ack is
still recorded by the same writer ``lohra auth enable`` uses.

Three rules hold this together:

* **The warning is unconditional.** ``--yes`` skips the *question*, never the
  text — automation that already decided still gets the risk on the record.
* **An acknowledged store is untouched.** The gate returns before printing
  anything, so a second login is byte-for-byte the old device flow (including on
  a pipe — that is how people log in over SSH).
* **No terminal, no guessing.** A piped stdin cannot say yes, so the gate refuses
  in milliseconds with the flag that would have worked, instead of asking into a
  void. Silence is never taken for consent.

The ack is written *before* the device flow so a flow that fails (network, a
closed browser, Ctrl-C) never discards an acceptance the user already gave.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from lohra.onboarding import detect
from lohra.onboarding.wizard import Prompter

# Exit codes, matching `lohra auth`'s existing vocabulary: 1 = the user said no,
# 2 = we could not ask (an environment problem the caller can fix with a flag).
DECLINED = 1
CANNOT_ASK = 2

QUESTION = "Enable subscription mode and continue to login?"

DECLINED_MESSAGE = (
    "aborted — subscription mode NOT enabled and no login was attempted."
)

NO_TERMINAL_MESSAGE = (
    "subscription mode is opt-in and this is not a terminal, so there is no way "
    "to ask.\n"
    "Accept the risk above explicitly instead:\n"
    "  lohra auth login --yes    # same warning, no question — then the device flow\n"
    "  lohra auth enable --yes   # accept now, then `codex login` to reuse the "
    "Codex CLI login"
)

ACKNOWLEDGED_MESSAGE = "ToS risk acknowledged — subscription mode enabled. Continuing to login."


@dataclass(frozen=True)
class Consent:
    """Whether the caller may proceed, and the exit code to use if it may not."""

    proceed: bool
    exit_code: int = 0


def ensure_opt_in(
    home: Path,
    *,
    assume_yes: bool = False,
    stdin=None,
    stderr=None,
    is_active: Callable[[Path], bool] | None = None,
    enable: Callable[[Path], None] | None = None,
) -> Consent:
    """Make sure this store has an explicit ToS acknowledgement before logging in.

    ``is_active``/``enable`` are injectable so the decision is testable without a
    store on disk; both default to the real subscription manager.
    """
    active = is_active or _default_is_active
    if active(Path(home)):
        return Consent(True)  # already opted in: this login is the plain old flow

    out = _stream(stderr, "stderr")
    prompter = Prompter(reader=_stream(stdin, "stdin"), writer=out)
    prompter.note(_tos_warning())

    if assume_yes:
        (enable or _default_enable)(Path(home))
        return Consent(True)

    if not (detect._isatty(_stream(stdin, "stdin")) and detect._isatty(out)):
        prompter.note(NO_TERMINAL_MESSAGE)
        return Consent(False, CANNOT_ASK)

    if not prompter.confirm(QUESTION, default=False):
        prompter.note(DECLINED_MESSAGE)
        return Consent(False, DECLINED)

    (enable or _default_enable)(Path(home))
    prompter.note(ACKNOWLEDGED_MESSAGE)
    return Consent(True)


def _tos_warning() -> str:
    from lohra.subscription import manage

    return manage.TOS_WARNING


def _default_is_active(home: Path) -> bool:
    from lohra.subscription.credentials import subscription_active

    return subscription_active(home)


def _default_enable(home: Path) -> None:
    from lohra.subscription import manage

    manage.enable(home)


def _stream(stream, name: str):
    """The given stream, or the live one — resolved late so a test can swap it."""
    if stream is not None:
        return stream
    import sys

    return getattr(sys, name)
