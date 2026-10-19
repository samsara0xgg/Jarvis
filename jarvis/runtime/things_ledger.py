"""The ledger of his things at run time (ADR 0299): the one-time carry-in and the rings pass.

Once at boot, before anything reads the ledger, :meth:`ThingsLedger.carry_in` takes his memos,
reminders, commitments and core memory's 承诺和待办 into it (``things.carry_in``) and then writes a
core memory version without that section. :meth:`ThingsLedger.run` then makes the pending ring
reminders match the rings of the next seven days at once and every ``PASS_S``, the way the
commitment poller's reminder pass did; the reminder tick (ADR 0179) fires them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from jarvis.state import memory_db, things
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# A ring is armed when a thing is written; this pass catches the horizon moving on, a clock
# change and a thing written by another process.
PASS_S: Final[float] = 15 * 60
_LOG_WAIT_S: Final[float] = 5.0


class ThingsLedger:
    """The carry-in and the rings pass; built once at boot."""

    def __init__(
        self, event_log: Path, memory_db_path: Path, zone: tzinfo, before_min: int,
    ) -> None:
        """``event_log`` is the daemon's log and ``memory_db_path`` memory.db.

        A carried-in commitment rings ``before_min`` minutes before it
        (``commitments.remind_before_min``).
        """
        self._event_log, self._memory, self._zone = event_log, memory_db_path, zone
        self._before_min = before_min
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._carried = False

    def carry_in(self) -> dict[str, int] | None:
        """Carry the older stores in once; its counts, or None when that was done before.

        The 承诺和待办 items are read raw from memory.db before the ledger takes them, and the
        version of core memory without the section is written after, so a start that was cut
        between the two finishes the job the next time.
        """
        items = memory_db.due_items(self._memory)
        with contextlib.closing(self._log()) as conn:
            counts = things.carry_in(
                conn, zone=self._zone, now=self.now(), core_items=items,
                before_min=self._before_min,
            )
        memory_db.drop_due_section(self._memory)
        self._carried = True
        if counts is not None:
            LOGGER.info("things: carried in %s", counts)
        return counts

    def sync(self) -> None:
        """Make the pending rings match the next seven days; running it twice changes nothing."""
        with contextlib.closing(self._log()) as conn:
            things.sync_rings(conn, zone=self._zone, now=self.now())

    async def run(self) -> None:
        """Carry in if boot did not, then sync at once and every ``PASS_S``; a fault is logged."""
        LOGGER.info("things started (rings every %.0f s)", PASS_S)
        try:
            while True:
                try:
                    if not self._carried:
                        await asyncio.to_thread(self.carry_in)
                    await asyncio.to_thread(self.sync)
                except Exception:
                    LOGGER.exception("things: the pass failed; trying again next time")
                await asyncio.sleep(PASS_S)
        except asyncio.CancelledError:
            LOGGER.info("things cancelled")
            raise

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)
