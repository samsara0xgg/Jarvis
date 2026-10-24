"""ADR 0388: an invented fortnight of his Health rows, as the phone sends them.

Heart rate every five minutes from 2 pm to 6 pm, a step total each hour, a night's sleep with its
stages, one workout, the watch charging through a hole in the heart rate, and a resting heart
rate this morning; the ``body`` tool and ``GET /inherent/body`` are read against it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
NOW = datetime(2026, 10, 23, 17, 36, tzinfo=ZONE)  # a Friday
TODAY = NOW.date()
MEASURE, HEALTH, STATE = "phone.measure_observed", "phone.health_observed", "phone.state_observed"
LATE = timedelta(minutes=40)  # how long after the watch took it a reading reaches the log
USUAL_HR = (68, 72, 76, 80)  # the days before, 2 pm to 6 pm; the first two while still
TODAY_HR = (84, 88, 92, 96)  # today from 2 pm; the first two while still


def epoch_ms(at: datetime) -> int:
    """``at`` in epoch milliseconds."""
    return round(at.timestamp() * 1000)


def local_at(day: date, hour: int, minute: int = 0) -> datetime:
    """``hour:minute`` of ``day`` in his zone."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZONE)


class World:
    """The rows of one invented fortnight, appended to a log in the order they arrived."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, Any], int]] = []
        self._uuid = 0

    def _next_uuid(self) -> str:
        self._uuid += 1
        return f"00000000-0000-4000-8000-{self._uuid:012d}"

    def reading(
        self, metric: str, at: datetime, value: float, *, rest: bool | None = None,
        uuid: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "metric": metric, "start_ms": epoch_ms(at), "end_ms": epoch_ms(at), "value": value,
            "unit": "count/min", "sample_uuid": uuid or self._next_uuid(), "source": "Watch",
        }
        if rest is not None:
            payload["meta"] = {"motion_context": "sedentary" if rest else "not_set"}
        self.rows.append((MEASURE, payload, epoch_ms(at + LATE)))

    def heart(self, day: date, values: tuple[int, ...], start: int, until: datetime) -> None:
        at, place = local_at(day, start), 0
        while at < until:
            self.reading("heart_rate", at, values[place % 4], rest=place % 4 < 2)
            at, place = at + timedelta(minutes=5), place + 1

    def total(self, metric: str, hour: datetime, value: float) -> None:
        end = hour + timedelta(hours=1)
        payload = {
            "metric": metric, "start_ms": epoch_ms(hour), "end_ms": epoch_ms(end), "value": value,
        }
        self.rows.append((HEALTH, payload, epoch_ms(end)))

    def span(
        self, metric: str, start: datetime, end: datetime, detail: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "metric": metric, "start_ms": epoch_ms(start), "end_ms": epoch_ms(end), "value": 0,
            "unit": "category", "detail": detail, "sample_uuid": self._next_uuid(),
        }
        if meta:
            payload["meta"] = meta
        self.rows.append((HEALTH, payload, epoch_ms(end)))

    def night(self, woke: date, minutes_late: int = 0) -> None:
        """23:30 to 07:00 (plus ``minutes_late``): core, deep, core, REM, awake, core."""
        at = local_at(woke, 0) - timedelta(minutes=30)
        for stage, minutes in (
            ("core", 90), ("deep", 60), ("core", 120), ("rem", 60), ("awake", 10),
            ("core", 120 + minutes_late),
        ):
            self.span("sleep", at, at + timedelta(minutes=minutes), stage)
            at += timedelta(minutes=minutes)

    def charge(self, at: datetime, percent: int, state: str) -> None:
        payload = {"signal": "watch_battery", "value": str(percent), "detail": state}
        self.rows.append((STATE, payload, epoch_ms(at)))

    def write(self, path: Path) -> None:
        conn = open_event_log(path)
        for kind, payload, ts in sorted(self.rows, key=lambda row: row[2]):
            if ts <= epoch_ms(NOW):
                emit_event(conn, type=kind, payload=payload, ts_epoch_ms=ts, ingestion_node="phone")
        conn.close()


def fortnight() -> World:
    world = World()
    for back in range(1, 15):
        day = TODAY - timedelta(days=back)
        if back == 1:  # yesterday the watch came off from 2:30 pm to 4:10 pm
            world.heart(day, USUAL_HR, 14, local_at(day, 14, 30))
            world.heart(day, USUAL_HR, 16, local_at(day, 18))
            world.rows = [r for r in world.rows if not (
                r[1].get("metric") == "heart_rate"
                and epoch_ms(local_at(day, 14, 30))
                <= r[1]["start_ms"]
                < epoch_ms(local_at(day, 16, 10))
            )]
            world.charge(local_at(day, 14, 40), 15, "charging")
            world.charge(local_at(day, 15, 50), 100, "full")
        else:
            world.heart(day, USUAL_HR, 14, local_at(day, 18))
        for hour in range(8, 18):
            world.total("steps", local_at(day, hour), 500)
        world.night(day)
        world.span("stand_hour", local_at(day, 15), local_at(day, 16), "stood")
    world.heart(TODAY, TODAY_HR, 14, NOW)
    for hour in range(8, 17):
        world.total("steps", local_at(TODAY, hour), 300)
    world.total("steps", local_at(TODAY, 9), 100)  # sent again later for the same hour: 100 stands
    world.night(TODAY, minutes_late=-30)  # woke at 6:30 am, half an hour short
    world.span("stand_hour", local_at(TODAY, 15), local_at(TODAY, 16), "idle")
    yesterday = TODAY - timedelta(days=1)
    world.span(
        "workout", local_at(yesterday, 18, 5), local_at(yesterday, 18, 47), "running",
        {"total_energy_kcal": 388.2, "distance_m": 6120.0, "avg_heart_rate": 148.0},
    )
    world.reading("resting_heart_rate", local_at(TODAY, 8), 55)
    return world


__all__ = ["NOW", "TODAY", "ZONE", "World", "epoch_ms", "fortnight", "local_at"]
