"""Reference checkers for the tool layer (ADR 0317): the facts, derived from the recording.

An item's ``expect.checker`` names one. It reads what the providers answered (the recording) at the
item's moment and says what the tool's result must hold, without the tool's own code: the rest of
today's rain chance, the departures still catchable, the calendar and to-do rows of a day. Each
takes
the item, its recording and the decoded result, and returns what is wrong (empty = right).
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from scripts.tool_exam_tools import Recording

HTTP_ERROR = 400


def _bodies(recording: Recording | None, host: str, needle: str = "") -> list[dict[str, Any]]:
    """Decoded answers of the recorded HTTP calls to ``host`` whose request holds ``needle``."""
    return [
        json.loads(entry["body"])
        for entry in (recording.data["http"] if recording else ())
        if host in entry["url"]
        and needle in entry.get("request", "")
        and entry["status"] < HTTP_ERROR
    ]


def _now(item: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(item["at"]).astimezone(ZoneInfo(item.get("zone") or "UTC"))


def bus_catchable_departures(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """Every bus the recording departs after he can walk to it is a bus he can catch.

    The result must answer (not say no bus runs) and show the earliest such departure first;
    no option may depart before it. Departures are the recording's own UTC times, so a bus after
    midnight counts as tomorrow's, not as one that left earlier today.
    """
    zone, now = ZoneInfo(item.get("zone") or "UTC"), _now(item)
    catchable: list[tuple[datetime, str]] = []
    for answer in _bodies(recording, "routes.googleapis.com", '"TRANSIT"'):
        for route in answer.get("routes", ()):
            steps = [s for leg in route.get("legs", ()) for s in leg.get("steps", ())]
            rides = [s for s in steps if s.get("transitDetails")]
            if not rides:
                continue
            walk = sum(
                float(str(s.get("staticDuration", "0s")).removesuffix("s"))
                for s in steps[: steps.index(rides[0])]
            )
            leaves = datetime.fromisoformat(
                rides[0]["transitDetails"]["stopDetails"]["departureTime"],
            ).astimezone(zone)
            if leaves - now >= timedelta(seconds=walk):
                line = rides[0]["transitDetails"].get("transitLine", {}).get("nameShort", "?")
                catchable.append((leaves, f"{line} {leaves:%H:%M}"))
    if not catchable:
        return (
            []
            if "error" in out
            else ["the recording holds no catchable bus and the tool named one"]
        )
    catchable.sort()
    if "error" in out:
        return [
            f"{len(catchable)} departures are still catchable (first {catchable[0][1]}),"
            f" the tool answered: {str(out['error'])[:100]}"
        ]
    options = out.get("options") or []
    first = options[0]["legs"][0]["departs"] if options else None
    problems = []
    if first != f"{catchable[0][0]:%H:%M}":
        problems.append(
            f"the earliest catchable bus is {catchable[0][1]}, the first option is {first}"
        )
    allowed = {f"{when:%H:%M}" for when, _ in catchable}
    problems += [
        f"option departing {o['legs'][0]['departs']} is not catchable"
        for o in options
        if o["legs"][0]["departs"] not in allowed and o.get("verdict") != "missed"
    ]
    return problems


def weather_rest_of_today(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """The result carries every hour left today, each with its recorded chance of rain.

    The whole day's chance includes the morning that has passed, so it is not the answer: the hours
    from now to midnight are, with the peak among them.
    """
    zone, now = ZoneInfo(item.get("zone") or "UTC"), _now(item)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    expected: dict[str, int] = {}
    for body in _bodies(recording, "weatherkit.apple.com"):
        for row in body.get("forecastHourly", {}).get("hours", ()):
            start = datetime.fromisoformat(row["forecastStart"]).astimezone(zone)
            if now.replace(minute=0, second=0, microsecond=0) <= start < midnight:
                expected[f"{start:%Y-%m-%d %H:%M}"] = math.floor(
                    row["precipitationChance"] * 100 + 0.5
                )
    if not expected:
        return ["the recording holds no hour left today"]
    got = {r["at"]: r.get("rain_chance_pct") for r in out.get("hourly") or ()}
    problems = [f"hour {at} is missing from the result" for at in expected if at not in got]
    problems += [
        f"hour {at}: rain chance {got[at]}, recorded {pct}"
        for at, pct in expected.items()
        if at in got and got[at] != pct
    ]
    return problems


def schedule_day_complete(
    _item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """Every calendar event and unfinished to-do the providers returned is in the read."""
    wanted = set()
    for entry in recording.data["mcp"] if recording else ():
        for row in (entry.get("result") or {}).get("value", ()):
            if not isinstance(row, dict) or row.get("status") == "completed":
                continue
            title = row.get("subject") or row.get("title")
            if title:
                wanted.add(str(title))
    text = json.dumps(out, ensure_ascii=False)
    return [
        f"{title!r} is in the providers' answer and not in the read"
        for title in sorted(wanted)
        if title not in text
    ]


CHECKERS: dict[
    str, Callable[[Mapping[str, Any], Recording | None, Mapping[str, Any]], list[str]]
] = {
    "bus_catchable_departures": bus_catchable_departures,
    "weather_rest_of_today": weather_rest_of_today,
    "schedule_day_complete": schedule_day_complete,
}
