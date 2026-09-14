"""One native lock per profile for auth config, login, refresh and logout.

Every transaction opens its own descriptor: native locks arbitrate both threads
and processes without a growing Python lock registry. The stable lock inode is
never unlinked. Callers acquire ONCE and use the explicitly named locked writers;
this lock is deliberately not reentrant. A process exit releases it automatically.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Iterator

from lohra.subscription.errors import SubscriptionError

_LOCK_TIMEOUT = 35.0  # OAuth HTTP timeout is 30 s; waiting is bounded, never a lease.
_STORE_ERROR = (
    "could not update the login store — check profile permissions and run `lohra auth login`"
)


def _try_lock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - native Windows CI is not available
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def profile_transaction(home: Path) -> Iterator[Path]:
    """Yield the canonical profile under an exclusive, bounded OS lock."""
    fd = None
    try:
        home = home.resolve()
        home.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            home / ".auth.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("invalid lock file")
        if os.name == "nt" and os.fstat(fd).st_size == 0:  # pragma: no cover
            os.write(fd, b"\0")
        deadline = time.monotonic() + _LOCK_TIMEOUT
        while True:
            try:
                _try_lock(fd)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise SubscriptionError(
                        "login store is busy — retry after the other auth operation finishes"
                    ) from None
                time.sleep(0.02)
    except (OSError, RuntimeError):
        if fd is not None:
            os.close(fd)
        raise SubscriptionError(_STORE_ERROR) from None
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield home
    finally:
        os.close(fd)  # closing the descriptor also unlocks, including on BaseException


def atomic_write(path: Path, text: str) -> None:
    """Already-locked writer: unique 0600 sibling, complete write/fsync, replace.

    Only our temporary file is cleaned up. A crash after remote token rotation
    but before local replacement still requires re-login; no model request is
    sent unless this commit succeeds.
    """
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except OSError:
        raise SubscriptionError(_STORE_ERROR) from None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass  # preserve the original token-free failure
