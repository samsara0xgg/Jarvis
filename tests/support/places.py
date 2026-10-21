"""A visit row as the phone reports it, and the Sunday noon the place tests count from."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.state.event_log import emit_event
from jarvis.state.phone_location import VISIT_EVENT

if TYPE_CHECKING:
    import sqlite3

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=ZONE)  # a Sunday
NOW_MS = int(NOW.timestamp() * 1000)
TODAY = NOW.date()


def ms_days_ago(days_ago: int, hour: int, minute: int = 0) -> int:
    """Epoch ms of ``hour:minute`` local, ``days_ago`` days before today."""
    day = datetime.combine(TODAY - timedelta(days=days_ago), time(hour, minute), tzinfo=ZONE)
    return int(day.timestamp() * 1000)


def stay(  # noqa: PLR0913 — a visit row's own fields.
    conn: sqlite3.Connection,
    uid: str,
    lat: float,
    lng: float,
    arrived: int,
    departed: int | None = None,
    label: str | None = None,
) -> None:
    """One ``phone.visit_observed`` row, as the phone reports it."""
    payload: dict[str, Any] = {
        "lat": lat,
        "lng": lng,
        "accuracy_m": 30.0,
        "arrived_at_ms": arrived,
    }
    if departed is not None:
        payload["departed_at_ms"] = departed
    if label is not None:
        payload["place"] = label
    emit_event(
        conn,
        type=VISIT_EVENT,
        payload=payload,
        ts_epoch_ms=departed or arrived,
        event_uid=uid,
        ingestion_node="phone",
    )
