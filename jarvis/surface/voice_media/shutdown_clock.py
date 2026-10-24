"""The media owner's shutdown deadline: one absolute moment that only moves earlier."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from typing import Any


class ShutdownClock:
    """The only writer of the shutdown deadline; callable from any thread (ADR 0383).

    Every bounded wait of the owner is capped by what is left of it, so a close that asks for
    a short bound is not outlived by a step that budgets more.
    """

    def __init__(self) -> None:
        """No deadline until a shutdown asks for one."""
        self._lock = threading.Lock()
        self._deadline = float("inf")

    @property
    def deadline(self) -> float:
        """The ``time.monotonic()`` moment in force; ``inf`` until a shutdown is asked for."""
        with self._lock:
            return self._deadline

    def lower_to(self, deadline: float) -> None:
        """Atomically make ``deadline`` the one in force, unless an earlier one already is."""
        with self._lock:
            self._deadline = min(self._deadline, deadline)

    def remaining_s(self, cap_s: float) -> float:
        """``cap_s``, or less when the deadline is nearer; never below zero."""
        deadline = self.deadline
        if deadline == float("inf"):
            return cap_s
        return min(cap_s, max(0.0, deadline - time.monotonic()))

    async def wait_task_bounded(self, task: asyncio.Task[Any], *, timeout_s: float) -> bool:
        """Wait for ``task`` within the bound; cancel it and answer False if it is not done."""
        done, pending = await asyncio.wait({task}, timeout=self.remaining_s(timeout_s))
        if pending:
            task.cancel()
            return False
        if done:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                task.result()
        return True
