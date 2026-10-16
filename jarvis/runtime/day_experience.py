"""Runtime wiring for the daily experience table (ADR 0279).

Once a day, after the configured local time, the schedule folds every local date before today
that has no ``day_experience`` row, oldest first, from the first turn on the event log. That one
rule backfills history on first deploy and catches up days the machine slept through. It calls no
model and sends nothing off the machine; the fold runs in a thread off the loop, so a turn never
waits for it. A day that fails stores nothing and is picked up again on the next run; it never
stops the days after it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Final

from jarvis.state.day_experience import compute_day, first_day
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import experience_days, local_now, put_day_experience

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.state.memory_db import MemorySettings

LOGGER = logging.getLogger(__name__)

DEFAULT_AT: Final[time] = time(4, 0)


@dataclass(frozen=True)
class DayExperienceSettings:
    """The ``day_experience:`` block of ``config/jarvis.yaml``."""

    at: time = DEFAULT_AT

    @classmethod
    def from_config(cls, raw: object) -> DayExperienceSettings | None:
        """The settings, or None (feature off) when ``enabled`` is false or ``at`` is unusable.

        A missing block or ``at`` is the default, 04:00.
        """
        block: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
        if block.get("enabled") is False:
            return None
        if block.get("at") is None:
            return cls()
        try:
            return cls(at=time.fromisoformat(str(block["at"])))
        except ValueError:
            LOGGER.warning(
                "day_experience.at %r is not HH:MM; no experience table is written", block["at"],
            )
            return None


def run_day_experience(memory_db: Path, event_log_path: Path, today: date) -> dict[str, str]:
    """Fold every local date before ``today`` that has no row; ``{date: written | failed}``."""
    outcomes: dict[str, str] = {}
    with closing(open_runtime_event_log(event_log_path)) as conn:
        day = first_day(conn)
        if day is None:
            return outcomes
        done = experience_days(memory_db)
        while day < today:
            key = day.isoformat()
            if key not in done:
                try:
                    metrics = compute_day(conn, memory_db, day)
                    put_day_experience(memory_db, key, json.dumps(metrics, ensure_ascii=False))
                except Exception:
                    LOGGER.exception("day_experience: %s failed; nothing stored", key)
                    outcomes[key] = "failed"
                else:
                    outcomes[key] = "written"
            day += timedelta(days=1)
    return outcomes


class DayExperienceSchedule:
    """Checks every ``poll_s``; runs once per local date, after ``settings.at``."""

    def __init__(
        self,
        *,
        memory: MemorySettings,
        settings: DayExperienceSettings,
        event_log_path: Path,
        poll_s: float = 60.0,
    ) -> None:
        """Bind the stores and the knobs; nothing runs until :meth:`run`."""
        self._memory = memory
        self._settings = settings
        self._event_log_path = event_log_path
        self._poll_s = poll_s
        self._last: date | None = None

    def due(self, now: datetime) -> date | None:
        """Today (this machine's zone) once ``at`` has passed and today was not tried yet."""
        local = now.astimezone()
        if local.time() < self._settings.at or local.date() == self._last:
            return None
        return local.date()

    def write(self, today: date) -> dict[str, str]:
        """One attempt per local date: a day that fails waits for tomorrow's run."""
        self._last = today
        return run_day_experience(self._memory.db_path, self._event_log_path, today)

    async def run(self) -> None:
        """A machine asleep at ``at`` writes on its first check after waking or after a start."""
        while True:
            await asyncio.sleep(self._poll_s)
            today = self.due(local_now())
            if today is None:
                continue
            try:
                outcomes = await asyncio.to_thread(self.write, today)
            except Exception:
                LOGGER.exception("day_experience: scheduled run for %s failed", today)
            else:
                LOGGER.info("day_experience: scheduled %s -> %s", today, outcomes)


__all__ = [
    "DayExperienceSchedule",
    "DayExperienceSettings",
    "run_day_experience",
]
