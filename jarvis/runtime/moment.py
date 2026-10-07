"""The moment (ADR 0161): Allen's current situation as TimeSink last recorded it, read on demand.

One object, built at boot from the ``moment:`` block, answers three questions for any alert
source: should alerts wait now (``hold``), what is the situation (``facts``), and what does the
short 现况 doc say (``snapshot``, which is what a decision snapshot stores). Facts are read when
asked and kept ``CACHE_S`` seconds, so a burst of snapshots costs one read. A store that is off,
missing, unreadable or stale makes every fact ``unknown`` and nothing is held.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import moment as rules
from jarvis.state import job_ledger, job_time, timesink_moment

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)

CACHE_S: Final[float] = 5.0


@dataclass(frozen=True)
class MomentSettings:
    """The ``moment:`` block of ``config/jarvis.yaml`` once validated."""

    fields: dict[str, bool]


class Moment:
    """Reads the situation from TimeSink at the time it is asked; never a background poll."""

    def __init__(
        self,
        settings: MomentSettings,
        timesink_path: Path | None,
        db_path: Path,
        device: DeviceLink | None = None,
    ) -> None:
        """``timesink_path`` is None while ``observer.timesink`` is off; ``db_path``: memory.db.

        ``device`` (ADR 0170: this is a brain) is the link to the terminal whose TimeSink it
        is; the facts are read there, and must be asked for off the event loop.
        """
        self._settings = settings
        self._path = timesink_path
        self._device = device
        self._db = db_path
        self._cached: tuple[datetime, dict[str, Any]] | None = None
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        # Allen's watch list, goals and rules: the doc's third part, empty until they are written.
        self.situation = ""

    def facts(self) -> dict[str, Any]:
        """The facts now (each possibly ``unknown``), from the cache when under ``CACHE_S`` old."""
        now = self.now()
        if self._cached is not None and now - self._cached[0] < timedelta(seconds=CACHE_S):
            return self._cached[1]
        try:
            known = job_time.companies(job_ledger.company_sites(self._db))
            found = timesink_moment.moment_facts(self._path, now, known, self._device)
        except Exception:
            LOGGER.exception("moment: reading the situation failed; treated as unknown")
            found = timesink_moment.moment_facts(None, now)
        self._cached = (now, found)
        return found

    def hold(self) -> str | None:
        """Why every card and sound should wait now (``call``, ``idle``, ...), or None."""
        return rules.hold_reason(self.facts())

    def client_hold(self) -> str | None:
        """What a client is told to hold for: ``call``, ``away`` or None (ADR 0163)."""
        return rules.client_hold(self.facts())

    def snapshot(self) -> dict[str, Any]:
        """``{"facts", "doc"}``: what a decision snapshot stores so it can be replayed."""
        facts = self.facts()
        return {
            "facts": facts,
            "doc": rules.render_doc(facts, self._settings.fields, self.situation),
        }
