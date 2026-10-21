"""A call that must come back while the test still holds what it might wait for.

A test that holds an event never sets it before the call returns, so a call that returns has not
waited for it. Setting the event is the test's own step after the call; ``HANG_S`` only turns a call
that does wait into a failure and is no bound on how long a call may take.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

HANG_S = 10.0
"""How long a wait that only a real hang outlasts is given: a wait for an event, a join."""


@contextlib.contextmanager
def never_waits_for(held: threading.Event) -> Iterator[None]:
    """Fail the test if a call inside is still waiting for ``held`` after ``HANG_S``.

    The call is let go then, by setting ``held``, so the thread under test ends and the failure is
    the test's, not a hung run.
    """
    hung = threading.Event()

    def _let_go() -> None:
        hung.set()
        held.set()

    watchdog = threading.Timer(HANG_S, _let_go)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        watchdog.cancel()
        if hung.is_set():
            pytest.fail("the call waited for what the test holds")
