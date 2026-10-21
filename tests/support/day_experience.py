"""Log rows for the day-experience tests, timed by the clock of one fixed day."""

from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

DAY = date(2026, 10, 14)


def write_log(path: Path, rows: Sequence[tuple[str, datetime, dict[str, Any], str]]) -> None:
    with closing(open_event_log(path)) as conn:
        for kind, when, payload, node in rows:
            emit_event(
                conn,
                type=kind,
                payload=payload,
                ingestion_node=node,
                ts_epoch_ms=int(when.timestamp() * 1000),
                correlation={"turn_id": payload["turn_id"]} if "turn_id" in payload else None,
            )


def noon_of(day: date, minute: int = 0) -> datetime:
    return datetime.combine(day, time(12, minute)).astimezone()
