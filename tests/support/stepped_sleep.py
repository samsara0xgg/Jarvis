"""A sleep the test steps through, for a task that loops on ``await sleep(interval)``.

``asyncio.sleep`` would make the test wait the interval, and ``FakeClock.async_sleep`` returns at
once, so the loop would never rest between laps. This one parks the loop at each wait until the
test lets it go on: the test sees each lap finish, in order, and nothing waits on a clock.
"""

from __future__ import annotations

import asyncio

_GUARD_S = 5.0
"""How long a test waits for a lap before it fails; not a time the loop keeps."""


class SteppedSleep:
    """Stand in for ``asyncio.sleep``: the loop parks here, the test says when it goes on."""

    def __init__(self) -> None:
        """Start with no wait asked for."""
        self.waits: list[float] = []
        """The seconds each call asked for, in order: one per lap the loop has finished."""
        self._parked = asyncio.Event()
        self._go = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        """Park the loop until :meth:`release`."""
        self.waits.append(seconds)
        self._parked.set()
        await self._go.wait()
        self._go.clear()

    async def parked(self) -> None:
        """Return once the loop has finished a lap and is waiting for the next, or fail the test."""
        try:
            await asyncio.wait_for(self._parked.wait(), _GUARD_S)
        except TimeoutError:
            msg = "gave up waiting for the loop to finish a lap"
            raise AssertionError(msg) from None
        self._parked.clear()

    def release(self) -> None:
        """Let the loop go on to its next lap."""
        self._go.set()
