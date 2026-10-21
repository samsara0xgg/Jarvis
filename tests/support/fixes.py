"""The phone fixes the stay and move tests cut from: points near home, rows and what they expect."""

from __future__ import annotations

import math
from typing import Any, NamedTuple

from jarvis.state.phone_location import LOCATION_EVENT
from tests.support.day_line import MINUTE, Row

DAY = "2026-10-10"
HOME = (10.0, 20.0)
_M_PER_DEGREE = 111_194.9


def spot(
    north: float = 0.0,
    east: float = 0.0,
    *,
    of: tuple[float, float] = HOME,
) -> tuple[float, float]:
    """The point ``north`` metres north and ``east`` metres east of ``of``."""
    lat = of[0] + north / _M_PER_DEGREE
    return lat, of[1] + east / (_M_PER_DEGREE * math.cos(math.radians(of[0])))


def fix(
    uid: str,
    minute: float,
    at: tuple[float, float] = HOME,
    *,
    acc: float = 20.0,
    place: str | None = None,
) -> Row:
    """A ``phone.location_observed`` row at ``minute``."""
    payload: dict[str, Any] = {"lat": at[0], "lng": at[1], "accuracy_m": acc}
    if place is not None:
        payload["place"] = place
    return Row(LOCATION_EVENT, uid, payload, minute)


def trail(  # noqa: PLR0913 - a straight line of fixes: its name, span, step and ends
    name: str,
    first: float,
    last: float,
    step: float,
    start: tuple[float, float],
    end: tuple[float, float],
) -> list[Row]:
    """Fixes every ``step`` minutes from ``first`` to ``last``, on the line ``start`` to ``end``."""
    count = round((last - first) / step)
    return [
        fix(
            f"{name}{k}",
            first + k * step,
            (
                start[0] + (end[0] - start[0]) * k / count,
                start[1] + (end[1] - start[1]) * k / count,
            ),
        )
        for k in range(count + 1)
    ]


class Seen(NamedTuple):
    """An item of the day as the table expects it; ``acc`` is checked only when given."""

    kind: str
    uid: str
    start: float
    end: float | None
    place: str | None = None
    cut: bool = False
    acc: float | None = None


def cut(
    uid: str,
    start: float,
    end: float | None,
    place: str | None = None,
    acc: float | None = None,
) -> Seen:
    """An expected stay cut from fixes."""
    return Seen("stay", uid, start, end, place, cut=True, acc=acc)


def as_seen(items: list[dict[str, Any]], origin: int, expected: list[Seen]) -> list[Seen]:
    """The day's stays and moves as the table words them (``@`` ids with their start in minutes)."""
    found: list[Seen] = []
    for item, want in zip(items, expected, strict=False):
        uid = str(item["id"])
        if "@" in uid:
            head, tail = uid.split("@")
            uid = f"{head}@{(int(tail) - origin) // MINUTE}"
        found.append(
            Seen(
                item["kind"],
                uid,
                (item["start_ms"] - origin) / MINUTE,
                None if item["end_ms"] is None else (item["end_ms"] - origin) / MINUTE,
                item.get("place"),
                item.get("cut_from") == "fixes",
                item.get("accuracy_m") if want.acc is not None else None,
            )
        )
    found.extend(Seen(item["kind"], str(item["id"]), 0, None) for item in items[len(expected) :])
    return found
