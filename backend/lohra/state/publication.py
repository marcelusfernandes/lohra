"""Nonblocking DB/run guard for outcome publication versus acquisition.

This is deliberately separate from state mutexes, SQLite transactions and lease
renewal. A publisher keeps this guard across its file/callback effects; a new
acquisition answers busy until they finish. A dead process releases native locks.
Lock inodes are never unlinked. A stuck, live callback cannot be bypassed by TTL.
"""

from contextlib import contextmanager
import errno
import hashlib
import logging
import os
from pathlib import Path
import stat
import threading
from typing import Iterator, Literal
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)
Access = Literal["acquired", "busy", "storage_error"]


def _try_lock(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - branch simulated; no Windows host
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


class PublicationGuard:
    def __init__(self, database: str):
        # SQLite's main filename is empty for a private in-memory/temporary DB.
        # Its SessionDB object is then the shared identity, not ':memory:'.
        self._path = Path(database).resolve() if database else None
        self._memory: WeakValueDictionary = WeakValueDictionary()
        self._registry_lock = threading.Lock()

    @contextmanager
    def hold(self, run_id: str) -> Iterator[Access]:
        if self._path is None:
            with self._registry_lock:
                lock = self._memory.get(run_id)
                if lock is None:
                    lock = threading.Lock()
                    self._memory[run_id] = lock
            won = lock.acquire(blocking=False)
            try:
                yield "acquired" if won else "busy"
            finally:
                if won:
                    lock.release()
            return
        fd = None
        access: Access = "storage_error"
        try:
            # Device/inode also unifies existing filename case aliases. SQLite
            # itself does not support accessing one WAL DB via arbitrary hardlinks.
            identity = self._path.stat()
            key = hashlib.sha256(
                f"{identity.st_dev}:{identity.st_ino}:{run_id}".encode()
            ).hexdigest()
            directory = self._path.parent / ".lohra-publication-locks"
            directory.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(directory / key,
                         os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("invalid publication lock file")
            if os.name == "nt" and os.fstat(fd).st_size == 0:  # pragma: no cover
                os.write(fd, b"\0")
            try:
                _try_lock(fd)
                access = "acquired"
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                access = "busy"
        except OSError:
            logger.warning("workflow: publication guard unavailable", exc_info=True)
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise
        try:
            yield access
        finally:
            if fd is not None:
                os.close(fd)  # releases native lock even on a callback exception
