"""One day of his for the answer's links (ADR 0391): a night, three stays, a walk, work, a call."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from jarvis.state.event_log import open_event_log
from tests.support import day_line as rows

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.state.day_line import DaySources

ZONE = ZoneInfo("America/Vancouver")
DAY = date(2026, 10, 14)
ORIGIN = int(datetime(2026, 10, 14, tzinfo=ZONE).timestamp() * 1000)
NOW = datetime(2026, 10, 14, 14, 30, tzinfo=ZONE)
NOW_MS = int(NOW.timestamp() * 1000)


def page_world(tmp_path: Path) -> tuple[Path, DaySources]:
    """The day's log and stores: the phone's night, stays and walk, his Mac's work and a call."""
    sources = rows.make_stores(
        tmp_path, ZONE, origin=ORIGIN,
        spans=(rows.Spun(550, 690, "Ghostty", "jarvis"), rows.Spun(690, 720, "Chrome")),
        calls=(rows.Call(780, 810),),
        talks=(rows.Said(555),),
    )
    log = tmp_path / "events.db"
    with closing(open_event_log(log)) as conn:
        rows.load_rows(
            conn,
            [
                rows.health("sleep-1", -30, 410, 90),
                rows.visit("stay-home", 410, 500, "Home"),
                rows.motion("walk-1", "walking", 500, 515),
                rows.visit("stay-cafe", 515, 545, "Corner Cafe"),
                rows.visit("stay-library", 560, None, "UVic Library"),
            ],
            ORIGIN,
        )
    return log, sources
