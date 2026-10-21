"""Reference checkers for the tool layer (ADR 0317): the facts, derived from the recording.

An item's ``expect.checker`` names one. It reads what the providers answered (the recording) at the
item's moment and says what the tool's result must hold, without the tool's own code: the rest of
today's rain chance, the departures still catchable, the calendar and to-do rows of a day. Each
takes
the item, its recording and the decoded result, and returns what is wrong (empty = right).
"""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import re
import zipfile
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, NamedTuple
from zoneinfo import ZoneInfo

from google.transit import gtfs_realtime_pb2

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from scripts.tool_exam_tools import Recording

HTTP_ERROR = 400


def _bodies(recording: Recording | None, host: str, needle: str = "") -> list[dict[str, Any]]:
    """Decoded answers of the recorded HTTP calls to ``host`` whose request holds ``needle``.

    Objects only: a list (the data sets a place offers) has no rows to read.
    """
    answers = (
        json.loads(entry["body"])
        for entry in (recording.data["http"] if recording else ())
        if host in entry["url"]
        and needle in entry.get("request", "")
        and entry["status"] < HTTP_ERROR
    )
    return [answer for answer in answers if isinstance(answer, dict)]


def _now(item: Mapping[str, Any]) -> datetime:
    return datetime.fromisoformat(item["at"]).astimezone(ZoneInfo(item.get("zone") or "UTC"))


# --- the bus: Google's legs, BC Transit's export and feeds, and how a time is said ---------------

_SAME_STOP_M = 40.0
"""A Google stop and the export's are one stop this close (ADR 0342)."""
_MATCH_WINDOW = timedelta(minutes=10)
_RUN = timedelta(minutes=5)
"""A bus he misses at a walk by this much or less is one he can run for (ADR 0265)."""
_NEAR_M = 300.0
"""A stop or a bus 'near him' (ADR 0344)."""
_PER_DIRECTION = 2
_EARTH_RADIUS_M = 6_371_000.0
_SAID = re.compile(r"(?:(tomorrow) \(\w{3}\), )?(\d{1,2}):(\d{2}) ([ap]m)")
_PM = 12


class _Bus(NamedTuple):
    """The first bus of one route of Google's answer, and the walk to it."""

    leaves: datetime
    line: str
    walk: timedelta
    board: tuple[float, float] | None
    alight: tuple[float, float] | None


class _Call(NamedTuple):
    """One stop time of the recorded export, placed on the item's service day."""

    trip: str
    route: str
    headsign: str
    direction: str
    stop: str
    name: str
    point: tuple[float, float]
    seq: int
    at: datetime


def _zone(item: Mapping[str, Any]) -> ZoneInfo:
    return ZoneInfo(item.get("zone") or "UTC")


def _apart_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle metres between two points."""
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    hav = math.sin((lat2 - lat1) / 2) ** 2
    hav += math.cos(lat1) * math.cos(lat2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(hav))


def _said(at: datetime, now: datetime) -> str:
    """``at`` as a bus answer says it: ``9:31 am`` today, ``tomorrow (Tue), 12:12 am``.

    Seconds are dropped, which is the rounding of a departure (ADR 0342).
    """
    clock = f"{at.hour % _PM or _PM}:{at.minute:02d} {'am' if at.hour < _PM else 'pm'}"
    if at.date() == now.date():
        return clock
    if at.date() == now.date() + timedelta(days=1):
        return f"tomorrow ({at:%a}), {clock}"
    return f"{at:%a %b} {at.day}, {clock}"


def _heard(text: str, now: datetime) -> datetime | None:
    """A time as :func:`_said` says it, back on the clock; None unless today's or tomorrow's."""
    match = _SAID.fullmatch(text)
    if match is None:
        return None
    hour = int(match[2]) % _PM + (_PM if match[4] == "pm" else 0)
    day = now.date() + timedelta(days=1 if match[1] else 0)
    return datetime.combine(day, time(hour, int(match[3])), tzinfo=now.tzinfo)


def _up(at: datetime) -> datetime:
    """An arrival rounded up to the minute (ADR 0342)."""
    down = at.replace(second=0, microsecond=0)
    return down if down == at else down + timedelta(minutes=1)


def _lat_lng(stop: Mapping[str, Any]) -> tuple[float, float] | None:
    found = (stop.get("location") or {}).get("latLng")
    return None if not found else (float(found["latitude"]), float(found["longitude"]))


def _google_buses(item: Mapping[str, Any], recording: Recording | None) -> list[_Bus]:
    """The first bus of every route the recorded Google transit answers hold, earliest first."""
    zone = _zone(item)
    found = []
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
            detail = rides[0]["transitDetails"]
            stops = detail["stopDetails"]
            found.append(_Bus(
                datetime.fromisoformat(stops["departureTime"]).astimezone(zone),
                str(detail.get("transitLine", {}).get("nameShort", "?")),
                timedelta(seconds=walk),
                _lat_lng(stops["departureStop"]),
                _lat_lng(stops["arrivalStop"]),
            ))
    return sorted(found, key=lambda b: b.leaves)


def _catchable(item: Mapping[str, Any], recording: Recording | None) -> list[_Bus]:
    """The Google buses that leave after he can walk to them, earliest first."""
    now = _now(item)
    return [b for b in _google_buses(item, recording) if b.leaves - now >= b.walk]


def _export_calls(item: Mapping[str, Any], recording: Recording | None) -> list[_Call]:
    """Every stop time of the recorded BC Transit export on the item's day, by trip and order.

    A time is placed by its service day's start, noon minus twelve hours, as GTFS has it.
    """
    entry = next(
        (e for e in (recording.data["http"] if recording else ()) if "TdExport" in e["url"]),
        None,
    )
    if entry is None:
        return []
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(entry["body_b64"]))) as archive:

        def rows(name: str) -> list[dict[str, str]]:
            with archive.open(name) as raw:
                return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")))

        routes = {r["route_id"]: r["route_short_name"] for r in rows("routes.txt")}
        trips = {t["trip_id"]: t for t in rows("trips.txt")}
        stops = {s["stop_id"]: s for s in rows("stops.txt")}
        times = rows("stop_times.txt")
    zone = _zone(item)
    # ponytail: every trip runs on the item's day (calendar.txt is not read); read it when an
    # item needs a service that does not run that day.
    start = datetime.combine(_now(item).date(), time(_PM), zone).astimezone(UTC)
    start -= timedelta(hours=_PM)
    calls = []
    for row in times:
        trip, stop = trips[row["trip_id"]], stops[row["stop_id"]]
        hours, minutes, seconds = (int(p) for p in row["departure_time"].split(":"))
        calls.append(_Call(
            row["trip_id"], routes[trip["route_id"]], trip.get("trip_headsign", ""),
            trip.get("direction_id", "0"), row["stop_id"], stop["stop_name"],
            (float(stop["stop_lat"]), float(stop["stop_lon"])), int(row["stop_sequence"]),
            (start + timedelta(hours=hours, minutes=minutes, seconds=seconds)).astimezone(zone),
        ))
    return sorted(calls, key=lambda c: (c.trip, c.seq))


def _feed(recording: Recording | None, name: str) -> gtfs_realtime_pb2.FeedMessage:
    """The recorded GTFS-realtime feed ``name`` (``tripupdates``, ``vehicleupdates``), or empty."""
    message = gtfs_realtime_pb2.FeedMessage()
    for entry in recording.data["http"] if recording else ():
        if f"/{name}.pb" in entry["url"]:
            message.ParseFromString(base64.b64decode(entry.get("body_b64", "")))
            break
    return message


def _vehicles(recording: Recording | None) -> list[gtfs_realtime_pb2.VehiclePosition]:
    return [e.vehicle for e in _feed(recording, "vehicleupdates").entity if e.HasField("vehicle")]


def _vehicle_point(bus: gtfs_realtime_pb2.VehiclePosition) -> tuple[float, float]:
    return bus.position.latitude, bus.position.longitude


def _item_here(item: Mapping[str, Any]) -> tuple[float, float]:
    return float(item["here"]["lat"]), float(item["here"]["lng"])


def bus_catchable_departures(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """Every bus the recording departs after he can walk to it is a bus he can catch (ADR 0343).

    The result must answer (not say no bus runs). One bus he can only run for, marked
    ``run_for``, may come first; the first other option is the earliest catchable departure, and
    no other option departs before it or is a bus he cannot catch. Options are in departure
    order. Times are read as the answer says them, with their day ("tomorrow (Tue), 12:12 am"),
    from the recording's own UTC stamps, so a bus after midnight is tomorrow's, not one that left
    earlier today.
    """
    now = _now(item)
    buses, catchable = _google_buses(item, recording), _catchable(item, recording)
    if not catchable:
        return (
            []
            if "error" in out
            else ["the recording holds no catchable bus and the tool named one"]
        )
    first = catchable[0]
    floor = first.leaves.replace(second=0, microsecond=0)
    if "error" in out:
        return [
            f"{len(catchable)} departures are still catchable (first the {first.line} at"
            f" {_said(floor, now)}), the tool answered: {str(out['error'])[:100]}"
        ]
    options = out.get("options") or []
    said = [str(o["legs"][0]["departs"]) for o in options]
    heard = [_heard(s, now) for s in said]
    runs = [n for n, o in enumerate(options) if o.get("run_for")]
    rest = [n for n in range(len(options)) if n not in runs]
    gone = {_said(b.leaves, now) for b in buses} - {_said(b.leaves, now) for b in catchable}
    problems = [
        f"option {n + 1} says {s!r}, not a time today or tomorrow"
        for n, (s, h) in enumerate(zip(said, heard, strict=True))
        if h is None
    ]
    if runs not in ([], [0]):
        problems.append(f"one run-for bus may be marked, the first: marked {[n + 1 for n in runs]}")
    if not rest or said[rest[0]] != _said(floor, now):
        problems.append(
            f"the earliest catchable bus is the {first.line} at {_said(floor, now)}, the first"
            f" option he can catch says {said[rest[0]] if rest else None}"
        )
    for n in rest:
        if said[n] in gone:
            problems.append(f"option {n + 1} ({said[n]}) is a bus he cannot catch")
        elif (at := heard[n]) is not None and at < floor:
            problems.append(f"option {n + 1} ({said[n]}) leaves before the first catchable bus")
    problems += [
        f"the run-for bus ({said[n]}) is not before the first catchable bus"
        for n in runs
        if (at := heard[n]) is not None and at >= floor
    ]
    known = [h for h in heard if h is not None]
    if known != sorted(known):
        problems.append(f"the options are not in departure order: {said}")
    return problems


def bus_run_for_catch_and_next(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """The run-for bus, the first catchable and the one after, for leaving now (ADR 0343).

    The first two as :func:`bus_catchable_departures` has them, and here the run-for bus must be
    there: Google holds a bus he misses at a walk by five minutes or less, the latest of which is
    option 1. Google's answer holds no bus after the first catchable one, so the one after is the
    export's next departure of that route from a stop within 40 m of Google's board stop.
    """
    problems = bus_catchable_departures(item, recording, out)
    now, catchable = _now(item), _catchable(item, recording)
    if not catchable or "error" in out:
        return problems
    first = catchable[0]
    runs = [
        b for b in _google_buses(item, recording)
        if b.leaves < first.leaves and b.walk - (b.leaves - now) <= _RUN
    ]
    head = (out.get("options") or [{}])[0]
    if runs and (
        not head.get("run_for") or head["legs"][0]["departs"] != _said(runs[-1].leaves, now)
    ):
        problems.append(f"option 1 is not the run-for {runs[-1].line} at"
                        f" {_said(runs[-1].leaves, now)}")
    if any(b.leaves > first.leaves for b in _google_buses(item, recording)):
        return [*problems, "Google gives a bus after the first catchable one: no timetable next"]
    board = first.board
    later = sorted(
        c.at for c in _export_calls(item, recording)
        if c.route == first.line and board is not None
        and _apart_m(c.point, board) <= _SAME_STOP_M and c.at > first.leaves
    )
    if not later:
        return [*problems, f"the recorded export has no later {first.line} at that stop"]
    last = ((out.get("options") or [{}])[-1].get("legs") or [{}])[0]
    if last.get("departs") != _said(later[0], now):
        problems.append(
            f"the one after is the timetable's {first.line} at {_said(later[0], now)}, the last"
            f" option says {last.get('departs')}"
        )
    return problems


def bus_one_time_per_bus(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """One time per bus (ADR 0342): the live prediction rounded down to the minute, never Google's.

    The pin made from the listed bus says the feed's time for it; Google's own minute is nowhere
    in the result.
    """
    now, zone = _now(item), _zone(item)
    live = [
        one.departure.time
        for entity in _feed(recording, "tripupdates").entity
        for one in entity.trip_update.stop_time_update
        if one.departure.time
    ]
    buses = _google_buses(item, recording)
    if len(live) != 1 or len(buses) != 1:
        return ["the recording must hold one Google bus and one live departure for it"]
    want = _said(datetime.fromtimestamp(live[0], zone), now)
    google = _said(buses[0].leaves, now)
    pinned = (out.get("trips") or [{}])[0].get("departs")
    problems = [] if pinned == want else [f"the pin says {pinned}, the live time is {want}"]
    if f"bus at {want}" not in str(out.get("summary", "")):
        problems.append(f"the summary does not say the bus at {want}")
    if google != want and google in json.dumps(out):
        problems.append(f"Google's {google} is in the result")
    return problems


def bus_matched_direction(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """Google's bus leg is the trip that calls near its board stop and later near its alight stop.

    Near is 40 m; of those trips the one leaving nearest Google's time, within ten minutes
    (ADR 0342). A trip of the other direction at a stop 30 m away, leaving at Google's very
    minute, calls at the alight stop first, so it is not the bus. The answer's leg takes the
    matched trip's times and headsign.
    """
    now = _now(item)
    leg = next((b for b in _google_buses(item, recording) if b.board and b.alight), None)
    if leg is None or leg.board is None or leg.alight is None:
        return ["the recording holds no Google bus leg with its stops placed"]
    leg_board, leg_alight = leg.board, leg.alight
    calls = _export_calls(item, recording)
    fits: list[tuple[timedelta, _Call, _Call]] = []
    for n, call in enumerate(calls):
        off = abs(call.at - leg.leaves)
        if call.route != leg.line or off > _MATCH_WINDOW:
            continue
        if _apart_m(call.point, leg_board) > _SAME_STOP_M:
            continue
        end = next(
            (
                c for c in calls[n + 1:]
                if c.trip == call.trip and _apart_m(c.point, leg_alight) <= _SAME_STOP_M
            ),
            None,
        )
        if end is not None:
            fits.append((off, call, end))
    if not fits:
        return ["no trip of the recorded export fits Google's leg"]
    _, board, alight = min(fits, key=lambda f: f[0])
    got = ((out.get("options") or [{}])[0].get("legs") or [{}])[0]
    want = {
        "departs": _said(board.at, now), "arrives": _said(_up(alight.at), now),
        "headsign": board.headsign,
    }
    return [
        f"the leg's {key} is {got.get(key)!r}, the matched trip {board.trip} gives {value!r}"
        for key, value in want.items()
        if got.get(key) != value
    ]


def bus_board_both_ways(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """``route=`` alone is the timetable at his nearest stop of the route, 300 m at most (ADR 0344).

    Each direction's next two buses there are listed with their headsigns, so a stop served both
    ways shows both. It asks Google nothing, so the recording answers no Google request.
    """
    now = _now(item)
    problems = [
        f"the recording answers {e['url']}; the board asks Google nothing"
        for e in (recording.data["http"] if recording else ())
        if "googleapis.com" in e["url"]
    ]
    here, route = _item_here(item), str(item["args"]["route"])
    calls = [c for c in _export_calls(item, recording) if c.route.casefold() == route.casefold()]
    near = sorted((_apart_m(here, c.point), c.stop, c.name) for c in calls)
    if not near or near[0][0] > _NEAR_M:
        return [*problems, f"the export has no stop of the {route} near him"]
    _, stop, name = near[0]
    want = set()
    for direction in {c.direction for c in calls}:
        due = sorted(
            (c.at, c.headsign) for c in calls
            if c.stop == stop and c.direction == direction and c.at >= now
        )
        want |= {(head, _said(at, now)) for at, head in due[:_PER_DIRECTION]}
    shown = (out.get("stops") or [{}])[0]
    got = {(b.get("headsign"), b.get("departs")) for b in shown.get("buses") or ()}
    if shown.get("stop") != name:
        problems.append(f"the first stop is {shown.get('stop')!r}, the nearest of the {route} is"
                        f" {name!r}")
    if got != want:
        problems.append(f"the buses at {name} are {sorted(want)}, the result lists {sorted(got)}")
    return problems


def bus_nearest_branch(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """A chain's name is one Text Search ranked by distance from him (ADR 0346).

    Its first place, the nearest branch, is where the bus goes: the summary says it by name and
    address, and the result's destination is it.
    """
    searches = [
        e for e in (recording.data["http"] if recording else ())
        if "places:searchText" in e["url"] and e["status"] < HTTP_ERROR
    ]
    if len(searches) != 1:
        return [f"the recording holds {len(searches)} Text Searches, not one"]
    asked = json.loads(searches[0]["request"])
    lat, lng = _item_here(item)
    problems = []
    if asked.get("rankPreference") != "DISTANCE":
        problems.append("the search is not ranked by distance")
    if asked.get("locationBias", {}).get("circle", {}).get("center") != {
        "latitude": lat, "longitude": lng,
    }:
        problems.append("the search is not centred on him")
    branch = json.loads(searches[0]["body"])["places"][0]
    name, address = branch["displayName"]["text"], branch["formattedAddress"]
    if f"{name} ({address})" not in str(out.get("summary", "")):
        problems.append(f"the summary does not say {name} ({address})")
    shown = out.get("destination") or {}
    if (shown.get("name"), shown.get("address")) != (name, address):
        problems.append(f"the destination is {shown.get('name')!r}, {shown.get('address')!r}")
    return problems


def ride_moved_to_the_bus_near_him(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """His stop is not on the ride's trip: the ride moves to the right bus (ADR 0344).

    The right bus is the one other bus of the route within 300 m of him whose trip still calls at
    the stop he names (at or after its current stop). The ride now follows it: its headsign is the
    result's, the summary says which bus it left, and the stop is set.
    """
    calls = _export_calls(item, recording)
    trips = {c.trip: c for c in calls}
    buses = {b.vehicle.id: b for b in _vehicles(recording)}
    riding = buses.get(str(item["setup"][0]["args"]["vehicle_id"]))
    words = str(item["args"]["stop"])
    if riding is None or riding.trip.trip_id not in trips:
        return ["the recording does not hold the bus the ride started on"]
    route = trips[riding.trip.trip_id].route

    def goes(bus: gtfs_realtime_pb2.VehiclePosition) -> bool:
        return any(
            c.trip == bus.trip.trip_id and c.seq >= bus.current_stop_sequence
            and c.name.casefold() == words.casefold()
            for c in calls
        )

    if goes(riding):
        return ["the ride's own bus still calls at that stop: nothing should move"]
    right = [
        b for b in buses.values()
        if b.vehicle.id != riding.vehicle.id and b.trip.trip_id in trips
        and trips[b.trip.trip_id].route == route
        and _apart_m(_item_here(item), _vehicle_point(b)) <= _NEAR_M and goes(b)
    ]
    if len(right) != 1:
        return [f"{len(right)} other buses near him go to {words}; the item needs one"]
    want, was = trips[right[0].trip.trip_id].headsign, trips[riding.trip.trip_id].headsign
    problems = [] if out.get("headsign") == want else [
        f"the ride follows the bus toward {out.get('headsign')}, the right one goes to {want}"
    ]
    if f"toward {want}, not the one toward {was}" not in str(out.get("summary", "")):
        problems.append(f"the summary does not say it moved from the {was} bus to the {want} one")
    if out.get("alight_stop") != words:
        problems.append(f"the stop set is {out.get('alight_stop')!r}, not {words!r}")
    return problems


def ride_passed_stop_refused(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """A stop the ride's bus has passed cannot be where he gets off (ADR 0344).

    The ride's bus is the route's bus nearest him; its trip called at the stop he names before
    the bus's current stop. The call is refused, naming the stop, and nothing is set.
    """
    calls, here = _export_calls(item, recording), _item_here(item)
    trips = {c.trip: c for c in calls}
    route = str(item["setup"][0]["args"]["route"])
    mine = sorted(
        (_apart_m(here, _vehicle_point(b)), b.vehicle.id, b) for b in _vehicles(recording)
        if b.trip.trip_id in trips and trips[b.trip.trip_id].route == route
    )
    if not mine or mine[0][0] > _NEAR_M:
        return [f"the recording has no {route} bus near him"]
    bus, words = mine[0][2], str(item["args"]["stop"])
    behind = [
        c for c in calls
        if c.trip == bus.trip.trip_id and c.seq < bus.current_stop_sequence
        and c.name.casefold() == words.casefold()
    ]
    if not behind:
        return [f"{words} is not behind the {route} bus near him; the item needs a passed stop"]
    if "error" not in out:
        return [f"the tool set {words}, which the bus has already passed"]
    return [] if behind[0].name in str(out["error"]) else ["the refusal does not name the stop"]


def weather_rest_of_today(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """The result carries every hour left today whole, and keeps the morning apart from them.

    The whole day's chance includes the morning that has passed, so it is not the answer: the hours
    from now to midnight are, each with its recorded chance and the peak among them as
    ``rest_of_today``; the rain since midnight is ``earlier_today``, added up from the same rows.
    """
    zone, now = ZoneInfo(item.get("zone") or "UTC"), _now(item)
    this_hour = now.replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    expected: dict[str, int] = {}
    earlier: dict[datetime, float] = {}
    for body in _bodies(recording, "weatherkit.apple.com"):
        for row in body.get("forecastHourly", {}).get("hours", ()):
            start = datetime.fromisoformat(row["forecastStart"]).astimezone(zone)
            if this_hour <= start < midnight:
                expected[f"today {start:%H:%M}"] = math.floor(
                    row["precipitationChance"] * 100 + 0.5
                )
            elif today <= start < this_hour:
                earlier[start] = row.get("precipitationAmount", 0)
    if not expected:
        return ["the recording holds no hour left today"]
    got = {r["at"]: r.get("rain_chance_pct") for r in out.get("hourly") or ()}
    problems = [f"hour {at} is missing from the result" for at in expected if at not in got]
    problems += [
        f"hour {at}: rain chance {got[at]}, recorded {pct}"
        for at, pct in expected.items()
        if at in got and got[at] != pct
    ]
    peak = (out.get("rest_of_today") or {}).get("max_chance_pct")
    if peak != (top := max(expected.values())):
        problems.append(f"the peak of the hours left is {top}, the result: {peak}")
    fell = round(sum(earlier.values()), 1)
    said = (out.get("earlier_today") or {}).get("rain_mm", 0)
    if abs(said - fell) > 0.05:  # noqa: PLR2004 - a rounding step
        problems.append(f"{fell} mm fell before now, the result says {said}")
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


# --- places: map_search and what is around him ---------------------------------------------------

_EARTH_M = 6_371_000.0
_M_PER_DEG = 111_320.0
_DEFAULT_LIMIT = 5
_BOX_SLACK_DEG = 1e-6
_METRE_SLACK = 1.0
_MIN_ACCURACY_M = 20.0
_REACH_PAD_M = 30.0
_LINE_NAMES = 3
_GUESS_AFTER_MIN = 10
_NOON = 12


def _exchanges(recording: Recording | None, path: str) -> list[tuple[dict[str, Any], Any]]:
    """``(request, answer)`` of each recorded HTTP call whose URL holds ``path``, in order."""
    return [
        (json.loads(entry.get("request") or "{}"), json.loads(entry["body"]))
        for entry in (recording.data["http"] if recording else ())
        if path in entry["url"] and entry["status"] < HTTP_ERROR
    ]


def _metres(origin: tuple[float, float], raw: Mapping[str, Any]) -> float:
    """Great-circle metres from ``origin`` to a Places API place's recorded point."""
    lat1, lat2 = math.radians(origin[0]), math.radians(float(raw["location"]["latitude"]))
    dlng = math.radians(float(raw["location"]["longitude"]) - origin[1])
    hav = math.sin((lat2 - lat1) / 2) ** 2
    hav += math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * _EARTH_M * math.asin(math.sqrt(hav))


def _name(raw: Mapping[str, Any]) -> str:
    return str(raw["displayName"]["text"])


def _here(item: Mapping[str, Any]) -> tuple[float, float]:
    return float(item["here"]["lat"]), float(item["here"]["lng"])


def _clock(stamp: str, zone: ZoneInfo) -> str:
    """``9:00 pm``: a recorded RFC 3339 time on his clock."""
    local = datetime.fromisoformat(stamp).astimezone(zone)
    half = "am" if local.hour < _NOON else "pm"
    return f"{local.hour % _NOON or _NOON}:{local.minute:02d} {half}"


def _shown_problems(
    raws: list[dict[str, Any]],
    got: list[dict[str, Any]],
    origin: tuple[float, float],
    zone: ZoneInfo,
) -> list[str]:
    """The places shown are ``raws`` in order, each at its distance from ``origin``, open or not.

    Open or closed is the recorded ``openNow``, with the recorded time it next closes or opens.
    """
    names, want = [p.get("name") for p in got], [_name(r) for r in raws]
    if names != want:
        return [f"shown {names}, expected {want}"]
    problems = []
    for raw, place in zip(raws, got, strict=True):
        far = _metres(origin, raw)
        if abs(float(place.get("distance_m", -1)) - far) > _METRE_SLACK:
            problems.append(f"{_name(raw)}: {place.get('distance_m')} m, recorded {far:.0f} m")
        hours = raw.get("currentOpeningHours") or {}
        if "openNow" not in hours:
            continue
        is_open = bool(hours["openNow"])
        lead = "open now, closes" if is_open else "closed now, opens"
        change = hours.get("nextCloseTime" if is_open else "nextOpenTime")
        said = str(place.get("open", ""))
        if not said.startswith(lead) or (change and _clock(change, zone) not in said):
            problems.append(f"{_name(raw)}: open is {said!r}, recorded {lead} {change}")
    return problems


def _box_problems(sent: Mapping[str, Any], here: tuple[float, float], radius: float) -> list[str]:
    """The search is restricted to the box round his circle: radius metres each way."""
    box = (sent.get("locationRestriction") or {}).get("rectangle")
    if not box:
        return ["the search is not restricted to a rectangle"]
    dlat = radius / _M_PER_DEG
    dlng = dlat / math.cos(math.radians(here[0]))
    want = (here[0] - dlat, here[1] - dlng, here[0] + dlat, here[1] + dlng)
    got = (
        box["low"]["latitude"],
        box["low"]["longitude"],
        box["high"]["latitude"],
        box["high"]["longitude"],
    )
    if any(abs(float(g) - w) > _BOX_SLACK_DEG for g, w in zip(got, want, strict=True)):
        return [f"the search's rectangle is {got}, the box round {radius:.0f} m is {want}"]
    return []


def _walks(recording: Recording | None) -> dict[tuple[float, float], int]:
    """Minutes on foot the route matrix recorded, by destination point, rounded up."""
    walks = {}
    for sent, answer in _exchanges(recording, "computeRouteMatrix"):
        ends = [d["waypoint"]["location"]["latLng"] for d in sent["destinations"]]
        for one in answer:
            end = ends[int(one["destinationIndex"])]
            seconds = float(str(one["duration"]).removesuffix("s"))
            walks[float(end["latitude"]), float(end["longitude"])] = max(1, math.ceil(seconds / 60))
    return walks


def map_search_inside_radius(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """A search near him asks for the box round his radius and shows only the circle, nearest first.

    Every recorded place farther than the radius is dropped; each one shown is at its recorded
    distance, says open or closed and when that changes, and carries the recorded walk to it.
    """
    zone, here = ZoneInfo(item.get("zone") or "UTC"), _here(item)
    radius = float(item["args"]["radius_m"])
    searches = _exchanges(recording, "places:searchText")
    if len(searches) != 1:
        return [f"expected one Text Search, the recording holds {len(searches)}"]
    sent, answer = searches[0]
    raws = sorted(answer.get("places", ()), key=lambda r: _metres(here, r))
    inside = [r for r in raws if _metres(here, r) <= radius]
    inside = inside[: int(item["args"].get("limit", _DEFAULT_LIMIT))]
    got = list(out.get("places") or ())
    problems = _box_problems(sent, here, radius) + _shown_problems(inside, got, here, zone)
    walks = _walks(recording)
    for raw, place in zip(inside, got, strict=False):
        point = (float(raw["location"]["latitude"]), float(raw["location"]["longitude"]))
        if (walk := walks.get(point)) != place.get("walk_min"):
            problems.append(f"{_name(raw)}: walk {place.get('walk_min')}, recorded {walk}")
    return problems


def map_search_nearest_outside(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """Nothing inside the radius: a second, distance-ranked search gives the one nearest place.

    The recorded rectangle search holds no place inside the radius; the result is the nearest
    place of the second search (biased, not restricted), marked outside the radius.
    """
    zone, here = ZoneInfo(item.get("zone") or "UTC"), _here(item)
    radius = float(item["args"]["radius_m"])
    searches = _exchanges(recording, "places:searchText")
    boxed = [a for s, a in searches if "locationRestriction" in s]
    ranked = [
        a
        for s, a in searches
        if s.get("rankPreference") == "DISTANCE" and "locationRestriction" not in s
    ]
    if len(boxed) != 1 or len(ranked) != 1:
        return [f"expected a rectangle search, then a distance-ranked one: {len(searches)} made"]
    if any(_metres(here, r) <= radius for r in boxed[0].get("places", ())):
        return ["the item is wrong: its rectangle search holds a place inside the radius"]
    nearest = sorted(ranked[0].get("places", ()), key=lambda r: _metres(here, r))[:1]
    got = list(out.get("places") or ())
    problems = _shown_problems(nearest, got, here, zone)
    if got and got[0].get("outside_radius") is not True:
        problems.append(f"{got[0].get('name')} is not marked outside the radius")
    return problems


def map_search_in_area(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """A search in a named area is restricted to its bounds and measured from it, not from him.

    The reader's search for the area gives its point and viewport; the place search must be
    restricted to exactly that viewport, and the places shown, in Google's order, are at their
    distance from the area's point.
    """
    zone = ZoneInfo(item.get("zone") or "UTC")
    searches = _exchanges(recording, "places:searchText")
    read = [a for s, a in searches if s.get("textQuery") == item["args"]["area"]]
    found = [(s, a) for s, a in searches if s.get("textQuery") == item["args"]["query"]]
    if len(read) != 1 or len(found) != 1:
        return [f"expected one search for the area and one in it: {len(searches)} searches"]
    area = read[0]["places"][0]
    sent, answer = found[0]
    problems = []
    view = {"low": area["viewport"]["low"], "high": area["viewport"]["high"]}
    if (sent.get("locationRestriction") or {}).get("rectangle") != view:
        problems.append(f"the search is not restricted to the area's viewport {view}")
    raws = list(answer.get("places", ()))[: int(item["args"].get("limit", _DEFAULT_LIMIT))]
    origin = (float(area["location"]["latitude"]), float(area["location"]["longitude"]))
    return problems + _shown_problems(raws, list(out.get("places") or ()), origin, zone)


def around_candidates_within_reach(
    item: Mapping[str, Any],
    recording: Recording | None,
    out: Mapping[str, Any],
) -> list[str]:
    """What the "Around him" line is made of: the places within his fix's reach, and his stay.

    Within reach is the fix's accuracy (at least 20 m) plus 30 m. The recorded Nearby Search holds
    three such places, the most the line names; the result's candidates are those, nearest first,
    and he has been here the ten minutes the line waits before it guesses. The line itself is the
    state block's, which no tool returns.
    """
    here = _here(item)
    reach = max(float(item["here"].get("accuracy_m", _MIN_ACCURACY_M)), _MIN_ACCURACY_M)
    reach += _REACH_PAD_M
    nearby = _exchanges(recording, "places:searchNearby")
    if len(nearby) != 1:
        return [f"expected one Nearby Search, the recording holds {len(nearby)}"]
    raws = sorted(nearby[0][1].get("places", ()), key=lambda r: _metres(here, r))
    within = [_name(r) for r in raws if _metres(here, r) <= reach]
    if len(within) != _LINE_NAMES:
        return [f"the item is wrong: {len(within)} places within reach, not {_LINE_NAMES}"]
    around = out.get("around") or {}
    got = [c.get("name") for c in around.get("candidates") or ()]
    problems = [] if got == within else [f"candidates {got}, within reach {within}"]
    if int(around.get("here_for_min") or 0) < _GUESS_AFTER_MIN:
        problems.append(f"here for {around.get('here_for_min')} min, under {_GUESS_AFTER_MIN}")
    return problems


CHECKERS: dict[
    str, Callable[[Mapping[str, Any], Recording | None, Mapping[str, Any]], list[str]]
] = {
    "bus_catchable_departures": bus_catchable_departures,
    "bus_run_for_catch_and_next": bus_run_for_catch_and_next,
    "bus_one_time_per_bus": bus_one_time_per_bus,
    "bus_matched_direction": bus_matched_direction,
    "bus_board_both_ways": bus_board_both_ways,
    "bus_nearest_branch": bus_nearest_branch,
    "ride_moved_to_the_bus_near_him": ride_moved_to_the_bus_near_him,
    "ride_passed_stop_refused": ride_passed_stop_refused,
    "weather_rest_of_today": weather_rest_of_today,
    "schedule_day_complete": schedule_day_complete,
    "map_search_inside_radius": map_search_inside_radius,
    "map_search_nearest_outside": map_search_nearest_outside,
    "map_search_in_area": map_search_in_area,
    "around_candidates_within_reach": around_candidates_within_reach,
}
