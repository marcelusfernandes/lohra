"""Short Service admission/closing gates; preparation and cleanup stay outside.

A pool may enqueue and then raise. Only the ticket whose actual Future was
published may enter the run; an old refusal cannot borrow a later acceptance.
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Condition, Lock, get_ident
from typing import Any


@dataclass(eq=False)
class Submission:
    accepted: bool = False
    owner: int = field(default_factory=get_ident)
    key: str | None = None
    error: str | None = None


class LaunchAdmission:
    def __init__(self):
        self.lock = Lock()
        self._condition = Condition(self.lock)
        self._preparing: set[Submission] = set()
        self._closing = False

    @contextmanager
    def reserve(self, key: str | None = None):
        with self._condition:
            error = ("workflow service is shutting down" if self._closing else
                     f"workflow run {key!r} is being prepared; retry after its launch finishes"
                     if key is not None and any(t.key == key for t in self._preparing) else None)
            ticket = Submission(key=key, error=error)
            if error is None:
                self._preparing.add(ticket)
        try:
            yield ticket
        finally:
            if ticket.error is None:
                with self._condition:
                    self._retire(ticket)

    def _retire(self, ticket: Submission) -> None:
        self._preparing.discard(ticket)
        self._condition.notify_all()

    def submit(self, ticket: Submission, pool: Any, state: Any, run: Any, *args, **kwargs):
        with self._condition:
            if self._closing:
                raise RuntimeError("workflow service is shutting down; launch was not submitted")
            future = pool.submit(run, *args, submission=ticket, **kwargs)
            state.future = future
            self._retire(ticket)
            ticket.accepted = True

    def accepted(self, ticket: Submission) -> bool:
        with self._condition:
            return ticket.accepted

    def close(self) -> None:
        with self._condition:
            if any(ticket.owner == get_ident() for ticket in self._preparing):
                raise RuntimeError("cannot shut down from inside workflow launch preparation")
            self._closing = True
            # wait_for releases the mutex while preparation/cleanup finishes.
            self._condition.wait_for(lambda: not self._preparing)
