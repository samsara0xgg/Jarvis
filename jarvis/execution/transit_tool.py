"""ADR 0189, 0342, 0343: the bus times from Google Routes, the offer they leave, and the pins.

The model reaches all of it through the one ``bus`` tool (``jarvis.execution.bus_tool``, ADR 0246).

A Google answer is read into options whose every time is an aware local datetime. Each bus leg is
matched to one trip of BC Transit's timetable (:meth:`BusTimes.timed`), against one realtime
snapshot shared by the whole answer, and gets one time: the live prediction, else the schedule, else
Google's, labelled ``live`` or ``scheduled`` and rounded one way (a departure down, an arrival up;
:mod:`jarvis.shared.rounding`). A bus answer for leaving now gives the bus he can run for, the first
he can catch and the one after (ADR 0343).
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta, tzinfo
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final, NamedTuple, Protocol

from jarvis.execution import maps_spend, place_reader, places_api, speakable
from jarvis.execution.location_tool import describe_age, said_where
from jarvis.execution.tools import ToolError, _get_running_event_uid, _turn_of
from jarvis.shared import lang
from jarvis.shared import rounding as rounded
from jarvis.state import departures, reminders
from jarvis.state import places as place_state
from jarvis.state.event_log import MAC_NODE, emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereReading
    from jarvis.state.timetable import Call, Timed, Timetable

_URL: Final = "https://routes.googleapis.com/directions/v2:computeRoutes"
_TIMEOUT_S: Final = 5.0
_MAX_OPTIONS: Final = 3
# A far-fetched option (ADR 0203): it rides this much longer than the shortest. The cut by arrival
# time went with ADR 0343: a route every 20 minutes always arrives about 20 minutes later.
_SLOWER: Final = 1.5
# Every search without `at` asks Google from this long before now: a bus whose leave time
# (departure minus Google's walking estimate) just passed is still returned, and he can walk
# faster than Google assumes (ADR 0221 for a named route, ADR 0265 for all).
_LOOKBACK_MIN: Final = 10
# A bus this many minutes short of a normal walk is gone for good: no run makes it up.
_RUN_MIN: Final = 5
_LIVE_TIMEOUT_S: Final = 6.0
# spare_min: at or above this he has room (easy); at or below its negative the bus is gone.
_EASY_SPARE_MIN: Final = 2
# ADR 0288: a plan asks for a day this far ahead at most.
_MAX_DAYS_AHEAD: Final = 30
_CLOCK_GRACE: Final = timedelta(hours=1)
"""A clock time alone is today's until it is this long gone, then tomorrow's (ADR 0343)."""
_FIELD_MASK: Final = ",".join(
    f"routes.legs.steps.{path}"
    for path in (
        "travelMode",
        "staticDuration",
        "transitDetails.stopDetails",
        "transitDetails.headsign",
        "transitDetails.transitLine.nameShort",
        "transitDetails.transitLine.name",
    )
) + ",routes.legs.endLocation"
# DRIVE and WALK need only how long and how far; the default routing preference is the cheap one.
_MODE_MASK: Final = "routes.duration,routes.distanceMeters"
_MODES: Final = (("drive", "DRIVE"), ("walk", "WALK"))


class BusTimes(Protocol):
    """What the runtime's BC Transit reader offers the planner (``bus_live.BusLive``)."""

    def snapshot(self) -> Any:  # noqa: ANN401 - a GTFS-realtime FeedMessage.
        """The realtime feed, read once."""
        ...

    def timed(
        self, route: str, board: tuple[float, float], alight: tuple[float, float], around_ms: int,
        feed: Any = None,  # noqa: ANN401
    ) -> Timed | None:
        """The trip a Google bus leg is, with the feed's times, or None."""
        ...

    def following(
        self, after: Timed, feed: Any = None, not_before_ms: int | None = None,  # noqa: ANN401
    ) -> Timed | None:
        """The next trip of the same route, stop and way, leaving by ``not_before_ms`` if given."""
        ...

    def last_gone(self, after: Timed, now_ms: int) -> Call | None:
        """The last bus of that route, stop and direction that had left by ``now_ms``."""
        ...

    def table(self) -> Timetable:
        """The timetable store for the newest export."""
        ...

    def live_at(self, feed: Any, call: Call) -> int | None:  # noqa: ANN401
        """When the feed says ``call``'s bus leaves its stop (epoch ms), None when it is silent."""
        ...


def here_warning(fix: HereReading) -> dict[str, Any]:
    """``{}`` unless Here could not confirm the phone's report with a fresh fix (it is too old).

    Then it holds ``from_age_min`` and the ``warning`` line the model must act on before giving
    any times from that position. The bus planner and the board both carry it.
    """
    if not fix.why or fix.device == MAC_NODE:
        return {}
    return {
        "from_age_min": fix.age_s // 60,
        "warning": (
            f"The starting point is his phone's report from {describe_age(fix.age_s)} ago"
            f" ({fix.place or 'no place name'}); he may have moved. Say that first, or ask where"
            " he is, before giving times from it."
        ),
    }


def _here_label(
    fix: HereReading, kept: Callable[[], list[place_state.Place]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """How to say Here in the result, and a warning if it is old (:func:`here_warning`).

    A fix inside a kept place's circle is said as that place, not as the street the phone named.
    """
    inside = place_state.containing(kept(), fix.lat, fix.lng) if kept else None
    near = f"at {inside.name}, " if inside is not None else f"{fix.place}, " if fix.place else ""
    return f"here ({said_where(fix)}, {near}±{round(fix.accuracy_m)} m)", here_warning(fix)


class _When(NamedTuple):
    """The Routes time field to send (None: now) and the local moment it holds."""

    field: str | None
    at: datetime | None


def _zone(now: datetime) -> tzinfo:
    """The zone of an aware ``now``: the clock's, in which his times are said."""
    return now.tzinfo or UTC


def local_time(raw: str, name: str, now: datetime) -> datetime:
    """``2026-10-22 09:00`` (his zone unless it says its own), or a clock time ``09:00`` alone.

    A clock time alone is today's, or tomorrow's once today's is more than an hour gone.
    """
    zone = _zone(now)
    example = f"{now.astimezone(zone) + timedelta(days=1):%Y-%m-%d} 09:00"
    text = raw.strip()
    try:
        clock = time.fromisoformat(text)
    except ValueError:
        clock = None
    if clock is not None:
        at = datetime.combine(now.astimezone(zone).date(), clock.replace(second=0), tzinfo=zone)
        return at + timedelta(days=1) if at < now - _CLOCK_GRACE else at
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        msg = (
            f"{name} must be a local time like 09:00 or a local date and time like {example},"
            f" not {raw!r}"
        )
        raise ToolError(msg, code="invalid_arguments") from None
    return at.replace(tzinfo=zone) if at.tzinfo is None else at.astimezone(zone)


def _when(args: Mapping[str, Any], now: datetime) -> _When:
    """``depart_at`` or ``arrive_by`` as a local moment (ADR 0288, 0343).

    ToolError ``invalid_arguments`` for both times, one not a time, past or over 30 days ahead, or
    an arrive-by that has passed. A ``depart_at`` a few minutes gone is sent as it is.
    """
    depart, arrive = args.get("depart_at"), args.get("arrive_by")
    if depart and arrive:
        msg = "give at= or arrive_by=, not both"
        raise ToolError(msg, code="invalid_arguments")
    given, name = (arrive, "arrive_by") if arrive else (depart, "at")
    if not given:
        return _When(None, None)
    at = local_time(str(given), name, now)
    floor = now.replace(second=0, microsecond=0)
    if at > now + timedelta(days=_MAX_DAYS_AHEAD):
        msg = (
            f"{name}={given} is more than {_MAX_DAYS_AHEAD} days ahead; times can be read from"
            " today up to then"
        )
        raise ToolError(msg, code="invalid_arguments")
    if at < floor and (arrive or _has_date(str(given))):
        msg = f"{name}={given} has already passed"
        raise ToolError(msg, code="invalid_arguments")
    return _When("arrivalTime" if arrive else "departureTime", at)


def _has_date(text: str) -> bool:
    return not re.fullmatch(r"\s*\d{1,2}:\d{2}(:\d{2})?\s*", text)


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clock(stamp: str, zone: tzinfo) -> datetime:
    """Google's absolute ``stamp`` as a time in his ``zone``."""
    return datetime.fromisoformat(stamp).astimezone(zone)


def _seconds(step: Mapping[str, Any]) -> float:
    return float(str(step.get("staticDuration", "0s")).removesuffix("s"))


def _point(stop: Mapping[str, Any]) -> tuple[float, float] | None:
    found = (stop.get("location") or {}).get("latLng")
    if not found:
        return None
    return float(found["latitude"]), float(found["longitude"])


def _option(route: Mapping[str, Any], zone: tzinfo) -> dict[str, Any] | None:
    """One route in plain words, or None when it has no bus ride in it.

    Every time is an aware local datetime; Google's own, until a timetable match replaces it.
    """
    steps = [s for leg in route.get("legs", ()) for s in leg.get("steps", ())]
    rides = [s for s in steps if s.get("transitDetails")]
    if not rides:
        return None
    first = steps.index(rides[0])
    last = len(steps) - 1 - steps[::-1].index(rides[-1])
    walk_to = timedelta(seconds=sum(_seconds(s) for s in steps[:first]))
    walk_from = timedelta(seconds=sum(_seconds(s) for s in steps[last + 1 :]))
    legs = []
    for ride in rides:
        detail = ride["transitDetails"]
        stops, line = detail["stopDetails"], detail.get("transitLine", {})
        departs = _clock(stops["departureTime"], zone)
        arrives = _clock(stops["arrivalTime"], zone)
        legs.append({
            "route": line.get("nameShort") or line.get("name", "?"),
            "headsign": detail.get("headsign", ""),
            "board_stop": stops["departureStop"]["name"],
            "board_pt": _point(stops["departureStop"]),
            "departs": departs,
            "alight_stop": stops["arrivalStop"]["name"],
            "alight_pt": _point(stops["arrivalStop"]),
            "arrives": arrives,
            "google_departs": departs,
            "source": "scheduled",
            "timed": None,
        })
    return {
        "legs": legs,
        "walk_to": walk_to,
        "walk_to_first_stop_min": round(walk_to.total_seconds() / 60),
        "walk_from": walk_from,
        "walk_at_end_min": round(walk_from.total_seconds() / 60),
    }


def _settle(option: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The option's own times from its legs' (one rounding, ADR 0342) and how late he is."""
    first, last = option["legs"][0], option["legs"][-1]
    option["leave"] = rounded.departure(first["departs"] - option["walk_to"])
    option["arrive"] = rounded.arrival(last["arrives"] + option["walk_from"])
    option["total_min"] = round((option["arrive"] - option["leave"]).total_seconds() / 60)
    option["spare_min"] = _spare(first["departs"], option["walk_to_first_stop_min"], now)
    option["verdict"] = _verdict(option["spare_min"])
    return option


def _spare(departs: datetime, walk_min: int, now: datetime) -> int:
    """Whole minutes he has left beyond a normal walk to catch the bus (negative: late)."""
    return math.floor((departs - now).total_seconds() / 60) - walk_min


def _verdict(spare: int) -> str:
    """``easy`` with room, ``missed`` when a normal walk is too late, else ``tight``."""
    if spare >= _EASY_SPARE_MIN:
        return "easy"
    return "missed" if spare <= -_EASY_SPARE_MIN else "tight"


class _Times:
    """One answer's realtime: the snapshot is taken once, for every leg and the follow-ons."""

    def __init__(self, bus: BusTimes | None) -> None:
        self._bus = bus
        self.feed: Any = None
        self._taken = False

    def _snapshot(self) -> Any:  # noqa: ANN401
        if self._bus is not None and not self._taken:
            self._taken = True
            try:
                self.feed = self._bus.snapshot()
            except Exception:  # noqa: BLE001 - the schedule alone still gives each bus a time.
                self.feed = None
        return self.feed

    def _match(self, legs: list[dict[str, Any]]) -> list[Timed | None]:
        assert self._bus is not None  # noqa: S101 - `apply` returns first without one.
        feed = self._snapshot()
        found: list[Timed | None] = []
        for leg in legs:
            try:
                found.append(
                    self._bus.timed(
                        leg["route"], leg["board_pt"], leg["alight_pt"],
                        int(leg["google_departs"].timestamp() * 1000), feed,
                    ) if leg["board_pt"] and leg["alight_pt"] else None
                )
            except Exception:  # noqa: BLE001 - one leg without a match keeps Google's time.
                found.append(None)
        return found

    def apply(self, options: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        """Match every bus leg to its trip and give it its one time; settle each option.

        An option whose later bus leaves a change before the bus before it gets there takes that
        route's next bus from the change, or is dropped when there is none. Returns the options
        kept.
        """
        legs = [leg for o in options for leg in o["legs"]]
        zone = _zone(now)
        if self._bus is not None and legs:
            pool = ThreadPoolExecutor(max_workers=1)  # a cold first download is left to finish
            try:
                found = pool.submit(self._match, legs).result(timeout=_LIVE_TIMEOUT_S)
            except Exception:  # noqa: BLE001 - Google's times stand.
                found = [None] * len(legs)
            finally:
                pool.shutdown(wait=False)
            for leg, timed in zip(legs, found, strict=True):
                if timed is not None:
                    _timed_leg(leg, timed, zone)
        kept = [o for o in options if self._connected(o, zone)]
        for option in kept:
            _settle(option, now)
        return kept

    def _connected(self, option: dict[str, Any], zone: tzinfo) -> bool:
        """Whether each bus of ``option`` leaves its stop after the bus before it gets there.

        A bus that leaves too soon is replaced by its route's next one from that stop, read to
        the same stop as before; False when the timetable has none.
        """
        legs = option["legs"]
        for before, leg in pairwise(legs):
            if leg["departs"] >= before["arrives"]:
                continue
            nxt = self._catching(leg, int(before["arrives"].timestamp() * 1000))
            if nxt is None:
                return False
            _timed_leg(leg, nxt, zone)
        return True

    def _catching(self, leg: dict[str, Any], not_before_ms: int) -> Timed | None:
        """The next trip after the leg's own that leaves by ``not_before_ms``, or None."""
        if self._bus is None or leg["timed"] is None:
            return None
        try:
            return self._bus.following(leg["timed"], self._snapshot(), not_before_ms)
        except Exception:  # noqa: BLE001 - without the timetable there is no next bus.
            return None

    def following(self, option: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        """The option for the next trip of this one's route, stop and direction, or None."""
        leg = option["legs"][0]
        if self._bus is None or leg["timed"] is None or len(option["legs"]) != 1:
            return None
        try:
            nxt = self._bus.following(leg["timed"], self._snapshot())
        except Exception:  # noqa: BLE001 - no timetable, no next bus from it.
            return None
        if nxt is None:
            return None
        made = {k: v for k, v in option.items() if k != "run_for"}
        made["legs"] = [{**leg}]
        _timed_leg(made["legs"][0], nxt, _zone(now))
        return _settle(made, now)

    def last_gone(self, option: dict[str, Any], now: datetime) -> datetime | None:
        """When the last bus of the option's route, stop and direction left before ``now``."""
        timed = option["legs"][0]["timed"]
        if self._bus is None or timed is None:
            return None
        try:
            call = self._bus.last_gone(timed, int(now.timestamp() * 1000))
        except Exception:  # noqa: BLE001 - without the timetable there is no such fact.
            return None
        if call is None:
            return None
        return datetime.fromtimestamp(call.depart_ms / 1000, _zone(now))


def _timed_leg(leg: dict[str, Any], timed: Timed, zone: tzinfo) -> None:
    """Give a leg the matched trip's time: live or scheduled, departure down, arrival up."""
    board = timed.match.board
    leg["departs"] = rounded.departure(datetime.fromtimestamp(timed.depart_ms / 1000, zone))
    leg["arrives"] = rounded.arrival(datetime.fromtimestamp(timed.arrive_ms / 1000, zone))
    leg["source"] = timed.source
    leg["timed"] = timed
    leg["bus_trip_id"] = board.trip_id
    leg["headsign"] = board.headsign or leg["headsign"]


def _first(option: Mapping[str, Any]) -> datetime:
    departs: datetime = option["legs"][0]["departs"]
    return departs


def _options(
    answer: Mapping[str, Any], now: datetime, times: _Times,
) -> list[dict[str, Any]]:
    """Each bus route of Google's answer as an option with its times matched and settled."""
    options = [o for r in answer.get("routes", ()) if (o := _option(r, _zone(now)))]
    return times.apply(options, now)


def _ahead(options: list[dict[str, Any]], wanted: str, now: datetime) -> list[dict[str, Any]]:
    """The options whose first bus is ``wanted`` and not gone yet, earliest first."""
    floor = now.replace(second=0, microsecond=0)
    mine = [
        o for o in options
        if o["legs"][0]["route"].casefold() == wanted.casefold() and _first(o) >= floor
    ]
    return sorted(mine, key=_first)


def _sensible(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the far-fetched: a ride over 1.5x the shortest (ADR 0203, 0343)."""
    if not options:
        return options
    shortest = min(int(o["total_min"]) for o in options)
    kept = [o for o in options if o["total_min"] <= _SLOWER * shortest]
    return (kept or options[:1])[:_MAX_OPTIONS]


def _three(
    options: list[dict[str, Any]], times: _Times, now: datetime,
) -> list[dict[str, Any]]:
    """The bus he can run for, the first he can catch and the one after, in departure order.

    The run-for bus is the latest he would miss at a normal walk by no more than five minutes
    (ADR 0265). The one after is the next from any route whose ride is at most 1.5x the shortest;
    when Google has none, the timetable's next trip of the same route and stop.
    """
    ordered = sorted(options, key=_first)
    run_for = [
        {**o, "run_for": True} for o in ordered
        if o["verdict"] == "missed" and o["spare_min"] >= -_RUN_MIN
    ][-1:]
    catchable = [o for o in ordered if o["verdict"] != "missed"]
    if not catchable:
        after = times.following(run_for[0], now) if run_for else None
        return [*run_for, *([after] if after else [])]
    shortest = min(int(o["total_min"]) for o in catchable)
    after = next((o for o in catchable[1:] if o["total_min"] <= _SLOWER * shortest), None)
    if after is None:
        after = times.following(catchable[0], now)
    return [*run_for, catchable[0], *([after] if after else [])][:_MAX_OPTIONS]


def _search(  # noqa: PLR0913 - the trip, how it was asked, the clock and the realtime.
    api_key: str,
    start: Mapping[str, Any],
    end: Mapping[str, Any],
    wanted: str,
    now: datetime,
    when: _When,
    times: _Times,
    meter: list[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], Mapping[str, Any]]:
    """Ask Google and the other ways: ``(options, hits of the named route, modes, answer)``.

    Without ``at`` it asks from ten minutes ago, so a bus whose leave time just passed still
    shows, with its true spare minutes; if a named route is not found that way, the usual 'now'
    answer is used. With ``arrive_by`` it sends ``arrivalTime`` instead of a departure and keeps
    the last three buses by leave time, the one nearest the deadline last (ADR 0288). DRIVE and
    WALK carry no time either way. The options are Google's buses, empty when it found none.
    """
    body: dict[str, Any] = {
        "origin": start,
        "destination": end,
        "travelMode": "TRANSIT",
        "computeAlternativeRoutes": True,
    }
    plain = dict(body)
    if when.field and when.at:
        body[when.field] = _utc(when.at)
    else:
        ago = now.replace(second=0, microsecond=0) - timedelta(minutes=_LOOKBACK_MIN)
        body["departureTime"] = _utc(ago)
    shifted = when.field is None
    # The bus answer and, concurrently, the other ways there (ADR 0205): one request each.
    with ThreadPoolExecutor(max_workers=1 + len(_MODES)) as pool:
        ways = {
            name: pool.submit(_mode, api_key, start, end, travel, meter)
            for name, travel in _MODES
        }
        try:
            answer = pool.submit(_compute, api_key, body, _FIELD_MASK, meter).result()
        except Exception as exc:  # timeout, HTTP error, bad JSON: all one short reason.
            msg = (
                f"Bus times could not be read just now (Google Routes: {type(exc).__name__});"
                " tell him this lookup failed, do not guess times"
            )
            raise ToolError(msg, code="network_error") from exc
        options = _options(answer, now, times)
        hits = _ahead(options, wanted, now) if wanted else []
        if wanted and shifted and not hits:
            try:
                later = pool.submit(_compute, api_key, plain, _FIELD_MASK, meter).result()
            except Exception:  # noqa: BLE001 - the first answer stands.
                later = None
            if later is not None:
                answer, options = later, _options(later, now, times)
                hits = _ahead(options, wanted, now)
    modes = {name: found for name, way in ways.items() if (found := way.result())}
    return options, hits, modes, answer


def _pick(  # noqa: PLR0913 - the options, the named hits, the ask, the times and the clock.
    options: list[dict[str, Any]], hits: list[dict[str, Any]], wanted: str,
    when: _When, times: _Times, now: datetime,
) -> list[dict[str, Any]]:
    """The options to list, from every bus Google gave."""
    first = hits[0] if hits else None
    rest = sorted((o for o in options if o is not first), key=_first)
    if when.field == "arrivalTime":
        # The named bus is first however late or slow it is; it does not set the others' yardstick.
        return ([first] if first else []) + rest[-(_MAX_OPTIONS - bool(first)) :]
    if wanted:
        if when.field is None:  # from ten minutes ago: only the named bus may be one he missed
            rest = [o for o in rest if o["spare_min"] >= 0]
        return (([first] if first else []) + _sensible(rest))[:_MAX_OPTIONS]
    if when.field == "departureTime":
        return _sensible(sorted(options, key=_first))
    return _three(options, times, now)


def _compute(
    api_key: str, body: Mapping[str, Any], mask: str = _FIELD_MASK,
    meter: list[str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 — a fixed https URL.
        _URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": mask,
        },
        method="POST",
    )
    billed = True
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as reply:  # noqa: S310
            answer: dict[str, Any] = json.load(reply)
    except urllib.error.HTTPError:
        billed = False  # a refused request is not billed.
        raise
    finally:
        if billed and meter is not None:  # a list append, safe from the pool's threads.
            meter.append(
                "routes.transit.pro" if body.get("travelMode") == "TRANSIT"
                else "routes.basic.essentials",
            )
    return answer


def _mode(
    api_key: str, start: Mapping[str, Any], end: Mapping[str, Any], travel: str,
    meter: list[str] | None = None,
) -> Any:  # noqa: ANN401
    """``{minutes, km}`` of the trip by ``travel`` (DRIVE or WALK); None when Google has none."""
    try:
        body = {"origin": start, "destination": end, "travelMode": travel}
        route = _compute(api_key, body, _MODE_MASK, meter)["routes"][0]
        seconds = float(str(route["duration"]).removesuffix("s"))
        km = round(route["distanceMeters"] / 1000, 1)
        return {"minutes": max(1, round(seconds / 60)), "km": km}
    except Exception:  # noqa: BLE001 - a mode that fails is left out; the bus answer stands.
        return None


def _first_board(route: Mapping[str, Any]) -> tuple[tuple[str, str], tuple[float, float]] | None:
    """A route's first bus as ``((number, board stop), (lat, lng))``, when Google gave its place."""
    for leg in route.get("legs", ()):
        for step in leg.get("steps", ()):
            if detail := step.get("transitDetails"):
                line, stop = detail.get("transitLine", {}), detail["stopDetails"]["departureStop"]
                found = stop.get("location", {}).get("latLng")
                if not found:
                    return None
                key = (line.get("nameShort") or line.get("name", "?"), stop["name"])
                return key, (float(found["latitude"]), float(found["longitude"]))
    return None


def _first_alight(
    route: Mapping[str, Any],
) -> tuple[tuple[str, str], tuple[str, float, float]] | None:
    """Where the first bus lets him off, keyed like :func:`_first_board`, when Google placed it."""
    for leg in route.get("legs", ()):
        for step in leg.get("steps", ()):
            if detail := step.get("transitDetails"):
                line, stops = detail.get("transitLine", {}), detail["stopDetails"]
                found = stops.get("arrivalStop", {}).get("location", {}).get("latLng")
                if not found:
                    return None
                key = (line.get("nameShort") or line.get("name", "?"),
                       stops["departureStop"]["name"])
                where = (stops["arrivalStop"]["name"], float(found["latitude"]),
                         float(found["longitude"]))
                return key, where
    return None


_PIN_PAST_GRACE: Final = timedelta(minutes=1)
_SAME_BUS_MS: Final = 5 * 60_000
_SAY_AHEAD_MIN: Final = 90
_MS: Final = 1000


def _hhmm(ms: int, zone: tzinfo) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def _at_ms(at: datetime) -> int:
    return int(at.timestamp() * _MS)


class TransitOffers:
    """What the last ``bus`` plan lists and offers, and the first stops it kept.

    ``keep`` holds every option the plan listed, numbered from 1 as the model was told them:
    ``bus(pin=N)`` and ``bus(ride="start", option=N)`` read them back, so nothing is copied
    from one call to the next. The conversation card (ADR 0205) lists the catchable ones for as
    long as the card stays; a click pins one through :func:`pin_offered`. The newest plan
    replaces the older. The card's options stay in memory after the card is gone, for the pin
    card's "next bus" (same route and stop). ``stops`` is each option's first stop place, keyed
    ``(first route, board stop)``: the notch's live refresh finds the stop in BC Transit's data
    by position, as the two feeds spell names differently.

    A row carries its times twice: ``leave_at``, ``departs`` and ``arrive_at`` as ``HH:MM`` for the
    phone's display, and ``leave_ms``, ``departs_ms`` and ``arrive_ms`` (epoch) for everything that
    reads them (ADR 0343). Its ``bus_trip_id`` is the BC Transit trip it was matched to, if any.
    """

    def __init__(self) -> None:
        """Start with no answer."""
        self.stops: dict[tuple[str, str], tuple[float, float]] = {}
        # Where each option's first bus lets him off (name, lat, lng), same key (ADR 0224).
        self.alights: dict[tuple[str, str], tuple[str, float, float]] = {}
        self.to = ""
        self._id = ""
        self._rows: list[dict[str, Any]] = []
        self._listed: list[dict[str, Any]] = []

    @staticmethod
    def _table(options: list[dict[str, Any]], to: str) -> list[dict[str, Any]]:
        rows = []
        for i, o in enumerate(options):
            first = o["legs"][0]
            zone = o["leave"].tzinfo or UTC
            rows.append({
                "index": i,
                "route": " → ".join(leg["route"] for leg in o["legs"]),
                "board_stop": first["board_stop"],
                "leave_at": f"{o['leave']:%H:%M}",
                "leave_ms": _at_ms(o["leave"]),
                "departs": f"{first['departs']:%H:%M}",
                "departs_ms": _at_ms(first["departs"]),
                "arrive_at": _hhmm(_at_ms(o["arrive"]), zone),
                "arrive_ms": _at_ms(o["arrive"]),
                "to": to,
                "bus_trip_id": first.get("bus_trip_id"),
            })
        return rows

    def keep(self, options: list[dict[str, Any]], to: str) -> None:
        """Take a plan's listed options (the numbers the model was told) and where they go."""
        self.to = to
        self._listed = self._table(options, to)

    def listed_row(self, number: int) -> dict[str, Any]:
        """Option ``number`` (from 1) of the last plan; ToolError when there is none."""
        if not self._listed:
            msg = "There is no bus list to use yet; ask for the times first with bus(to=...)"
            raise ToolError(msg, code="no_plan")
        if not 1 <= number <= len(self._listed):
            have = f"1-{len(self._listed)}" if len(self._listed) > 1 else "1"
            msg = (
                f"There is no option {number}: the last list has option {have}. Use one of those"
                " numbers, or ask for the times again with bus(to=...)"
            )
            raise ToolError(msg, code="not_found")
        return self._listed[number - 1]

    def put(self, options: list[dict[str, Any]], to: str, now: datetime) -> dict[str, Any]:
        """Take a fresh answer's options as the offer; ``to`` is the destination as asked.

        Returns the card's trip: ``{offer_id, to, options}``, one row per option.
        """
        self._id = "offer-" + uuid.uuid4().hex[:8]
        self.to = to
        self._rows = self._table(options, to)
        made_at = now.isoformat(timespec="minutes")
        return {"offer_id": self._id, "to": to, "options": self._rows, "made_at": made_at}

    def row(self, offer_id: str, index: int) -> dict[str, Any]:
        """The offered option; LookupError when the offer is unknown or has no such row."""
        mine = [r for r in self._rows if r["index"] == index]
        if offer_id != self._id or not mine:
            msg = "that offer is gone"
            raise LookupError(msg)
        return mine[0]

    def remember(self, answer: Mapping[str, Any]) -> None:
        """Keep each route's first stop and where its first bus lets him off (ADR 0224)."""
        self.stops.clear()
        self.alights.clear()
        for found in map(_first_board, answer.get("routes", ())):
            if found:
                self.stops[found[0]] = found[1]
        for dropped in map(_first_alight, answer.get("routes", ())):
            if dropped:
                self.alights[dropped[0]] = dropped[1]

    def alight_for(
        self, route: str, destination: str | None = None,
    ) -> tuple[str, float, float] | None:
        """Where the last answer lets him off the bus ``route``, if it was asked for that place.

        ``destination`` None accepts the last answer whatever it was for.
        """
        if destination and destination.strip().casefold() != self.to.casefold():
            return None
        return next((where for (num, _), where in self.alights.items() if num == route), None)

    def later(
        self, route: str, board_stop: str, after_ms: int, now_ms: int,
    ) -> tuple[int, int, str] | None:
        """``(leave ms, departs ms, arrive_at)`` of the earliest option on that route and stop."""
        grace = int(_PIN_PAST_GRACE.total_seconds() * _MS)
        found = sorted(
            (r["departs_ms"], r["leave_ms"], r["arrive_at"])
            for r in self._rows
            if r["route"].split("→")[0].strip() == route and r["board_stop"] == board_stop
            and r["departs_ms"] > after_ms and r["leave_ms"] >= now_ms - grace
        )
        return (found[0][1], found[0][0], found[0][2]) if found else None


def live_card(trip: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """The card's trip as served now: no row whose leave time has passed; None if none is left."""
    floor = _at_ms(now.replace(second=0, microsecond=0))
    rows = [r for r in trip["options"] if r["leave_ms"] >= floor]
    return {**trip, "options": rows} if rows else None


def first_leave_ms(trip: Mapping[str, Any]) -> int | None:
    """When the card's first option says to leave, in epoch ms; None if it cannot be read."""
    try:
        return int(trip["options"][0]["leave_ms"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def show_card(ctx: ToolContext, trip: Mapping[str, Any]) -> None:
    """Put the answer up as the conversation's card (ADR 0205): an ask card (ADR 0066) with rows."""
    emit_event(
        ctx.conn,
        type="clarification.requested",
        payload={
            "clarification_id": trip["offer_id"],
            "question": trip["to"],
            "fields": [],
            "turn_id": _turn_of(ctx.action_id) or "",
            "action_id": ctx.action_id,
            "trip": trip,
        },
        source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
        correlation={"action_id": ctx.action_id},
    )


def trip_of(  # noqa: PLR0913 — one keyword per trip field.
    *,
    tid: str,
    route: str,
    board_stop: str,
    leave: datetime,
    departs: datetime,
    arrive_at: str,
    to: str,
    stop: tuple[float, float] | None,
    now: datetime,
    alight: tuple[str, float, float] | None = None,
    bus_trip_id: str | None = None,
) -> departures.Trip:
    """A trip to pin, checked: its leave time not past (a minute's grace), its bus after it."""
    if leave < now - _PIN_PAST_GRACE:
        msg = f"leave_at {leave:%H:%M} has already passed; look the bus up again"
        raise ToolError(msg, code="time_in_past")
    if departs < leave:
        msg = "departs is before leave_at; copy both from the same option"
        raise ToolError(msg, code="invalid_arguments")
    return departures.Trip(
        tid,
        route,
        board_stop,
        _at_ms(leave),
        _at_ms(departs),
        arrive_at,
        None if stop is None else stop[0],
        None if stop is None else stop[1],
        to,
        None if alight is None else alight[0],
        None if alight is None else alight[1],
        None if alight is None else alight[2],
        bus_trip_id,
    )


def ring(  # noqa: PLR0913 — the older ring, the set, the clock and who is asking.
    conn: sqlite3.Connection,
    trips: list[departures.Trip],
    *,
    now_ms: int,
    zone: tzinfo,
    reminder_id: str | None,
    ring_tid: str | None,
    action_id: str,
    source_event_id: str | None = None,
) -> tuple[str, str]:
    """Make the pin's one reminder ring for its earliest catchable trip (ADR 0204).

    It names that trip and, after it, the others still catchable. ``reminder_id`` and
    ``ring_tid`` are the older ring and the trip it was for (None for a new set). A ring that
    already says this stays; one that says something else is cancelled and scheduled again; one
    that already rang for this trip is not rung twice, and a trip whose leave time is long past
    gets no ring. Returns ``(reminder id, trip it is for)`` for the event that records them.
    """
    catchable = (t for t in trips if now_ms < t.departs_at_ms)
    live = sorted(catchable, key=lambda t: (t.leave_at_ms, t.departs_at_ms)) or trips
    target = live[0]
    text = lang.t(
        "departure.go", route=target.first_route, departs=_hhmm(target.departs_at_ms, zone),
        stop=target.board_stop,
    ) + "".join(
        lang.t("departure.also", route=t.first_route, departs=_hhmm(t.departs_at_ms, zone))
        for t in live[1:]
    )
    old = reminders.fold(conn).get(reminder_id) if reminder_id else None
    if old is not None and old.pending:
        if (old.due_at_ms, old.text, ring_tid) == (target.leave_at_ms, text, target.tid):
            return old.reminder_id, target.tid
        reminders.cancel(
            conn, old.reminder_id, action_id=action_id, source_event_id=source_event_id,
        )
    elif old is not None and ring_tid == target.tid:
        return old.reminder_id, target.tid
    if target.leave_at_ms < now_ms - int(_PIN_PAST_GRACE.total_seconds() * _MS):
        return reminder_id or "", target.tid
    new = reminders.schedule(
        conn,
        due=datetime.fromtimestamp(target.leave_at_ms / 1000, zone),
        text=text,
        action_id=action_id,
        source_event_id=source_event_id,
    )
    return new, target.tid


def _cancel_ring(
    conn: sqlite3.Connection, pin: departures.Departure | None, action_id: str,
    source_event_id: str | None = None,
) -> None:
    ringing = reminders.fold(conn).get(pin.reminder_id) if pin else None
    if ringing and ringing.pending:
        reminders.cancel(
            conn, ringing.reminder_id, action_id=action_id, source_event_id=source_event_id,
        )


def pin_trips(  # noqa: PLR0913 - the log, the set, the offer, the action, the moment, the source.
    conn: sqlite3.Connection,
    trips: list[departures.Trip],
    *,
    offer_id: str | None,
    action_id: str,
    now: datetime,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    """Pin these trips as a new set and schedule its ring (ADR 0200, 0204); a newer set replaces."""
    if not 1 <= len(trips) <= departures.MAX_TRIPS:
        msg = f"pin between 1 and {departures.MAX_TRIPS} trips"
        raise ToolError(msg, code="invalid_arguments")
    # One pin at a time: the older pin's reminder must not ring for a dropped set.
    _cancel_ring(conn, departures.current(conn), action_id, source_event_id)
    reminder_id, ring_tid = ring(
        conn, trips, now_ms=_at_ms(now), zone=_zone(now), reminder_id=None,
        ring_tid=None, action_id=action_id, source_event_id=source_event_id,
    )
    pin_id, event_uid = departures.pin(
        conn,
        reminder_id=reminder_id,
        ring=ring_tid,
        trips=trips,
        offer_id=offer_id,
        action_id=action_id,
        source_event_id=source_event_id,
    )
    first = min(trips, key=lambda t: t.leave_at_ms)
    return {
        "pin_id": pin_id,
        "event_uid": event_uid,
        "pinned": True,
        "leave_at": _hhmm(first.leave_at_ms, _zone(now)),
        "departs": _hhmm(first.departs_at_ms, _zone(now)),
        "route": first.route,
        "trips": [
            {
                "route": t.route, "leave_at": _hhmm(t.leave_at_ms, _zone(now)),
                "departs": _hhmm(t.departs_at_ms, _zone(now)),
            }
            for t in trips
        ],
    }


def add_trip(
    conn: sqlite3.Connection, pin: departures.Departure, trip: departures.Trip, *, action_id: str,
    now: datetime,
) -> None:
    """Add a trip to the set (once); ToolError ``full`` when it already holds three catchable."""
    if pin.trip(trip.tid):
        return
    now_ms = _at_ms(now)
    if len(pin.catchable(now_ms)) >= departures.MAX_TRIPS:
        msg = f"{departures.MAX_TRIPS} trips are already pinned"
        raise ToolError(msg, code="full")
    reminder_id, ring_tid = ring(
        conn, [*pin.trips, trip], now_ms=now_ms, zone=_zone(now), reminder_id=pin.reminder_id,
        ring_tid=pin.ring_tid, action_id=action_id,
    )
    departures.add(conn, pin.pin_id, trip, reminder_id=reminder_id, ring=ring_tid)


def pin_offered(  # noqa: PLR0913 - the log, the offers, which one, add or not, the moment.
    conn: sqlite3.Connection, offers: TransitOffers, offer_id: str, index: int, *, add: bool,
    now: datetime,
) -> dict[str, Any]:
    """The card's click: pin that option; ``add`` puts it on the set this offer already started.

    LookupError when the offer cannot be pinned; ``{"reason": "full"}`` when the set is full.
    """
    row = offers.row(offer_id, index)
    try:
        trip = _row_trip(offers, row, f"{offer_id}-{index}", now)
        pin = departures.active(conn, _at_ms(now))
        if add and pin is not None and pin.offer_id == offer_id:
            add_trip(conn, pin, trip, action_id=offer_id, now=now)
            return {"pin_id": pin.pin_id}
        return pin_trips(conn, [trip], offer_id=offer_id, action_id=offer_id, now=now)
    except ToolError as exc:
        if exc.code == "full":
            return {"reason": "full"}
        raise LookupError(str(exc)) from exc


def _offer(  # noqa: PLR0913 - the offers, the context, the answer, its options, modes, the ask.
    offers: TransitOffers, ctx: ToolContext, answer: Mapping[str, Any],
    options: list[dict[str, Any]], modes: Mapping[str, Any], asked: str,
) -> None:
    """Keep today's answer as the offer: the stops, the list `pin` reads, and the chat card."""
    offers.remember(answer)
    offers.keep(options, asked)
    # The card offers only buses he can still catch: a missed one is in the answer.
    catchable = [o for o in options if o["spare_min"] > -_EASY_SPARE_MIN]
    if catchable:
        show_card(ctx, {**offers.put(catchable, asked, ctx.clock.now()), "modes": modes})


# --- what is said --------------------------------------------------------------------------


def said(at: datetime, now: datetime, *, soon: bool = False) -> str:
    """``at`` as he hears it: ``5:06 pm`` today (``soon`` adds ``(in 6 min)``), else with its day.

    Another day is ``tomorrow (Thu), 12:10 am`` or ``Mon Sep 22, 2:40 pm`` (speakable).
    """
    zone = _zone(now)
    local = at.astimezone(zone)
    if local.date() == now.astimezone(zone).date():
        return said_clock(at, now, soon=soon)
    return speakable.when(at, now, zone)


def said_clock(at: datetime, now: datetime, *, soon: bool = False) -> str:
    """``at`` as he hears it: ``5:06 pm``; ``soon`` adds `` (in 6 min)`` within 90 minutes."""
    local = at.astimezone(_zone(now))
    hour = local.hour % 12 or 12
    text = f"{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"  # noqa: PLR2004
    ahead = math.floor((at - now).total_seconds() / 60)
    return f"{text} (in {ahead} min)" if soon and 0 < ahead <= _SAY_AHEAD_MIN else text


def said_ms(ms: int, now: datetime, *, soon: bool = False) -> str:
    """An epoch ms, as :func:`said` says it."""
    return said(datetime.fromtimestamp(ms / 1000, _zone(now)), now, soon=soon)


def _buses(option: Mapping[str, Any]) -> str:
    return ", then ".join(f"the {leg['route']}" for leg in option["legs"])


def _view(option: Mapping[str, Any], number: int, now: datetime) -> dict[str, Any]:
    """One option as the model reads it: every time said with its day, every bus with its source."""
    view: dict[str, Any] = {
        "option": number,
        "leave_at": said(option["leave"], now),
        "walk_to_first_stop_min": option["walk_to_first_stop_min"],
        "legs": [
            {
                "route": leg["route"], "headsign": leg["headsign"],
                "board_stop": leg["board_stop"], "departs": said(leg["departs"], now),
                "source": leg["source"], "alight_stop": leg["alight_stop"],
                "arrives": said(leg["arrives"], now),
            }
            for leg in option["legs"]
        ],
        "walk_at_end_min": option["walk_at_end_min"],
        "arrive_at": said(option["arrive"], now),
        "total_min": option["total_min"],
        "spare_min": option["spare_min"],
        "verdict": option["verdict"],
    }
    if option.get("run_for"):
        view["run_for"] = True
    return view


def _leave(option: Mapping[str, Any], now: datetime) -> str:
    if option["spare_min"] <= 0:
        return "leave now"
    return f"leave {said(option['leave'], now, soon=True)}"


def _from(option: Mapping[str, Any], now: datetime) -> str:
    leg = option["legs"][0]
    return (
        f"{_buses(option)} from {leg['board_stop']} at"
        f" {said(leg['departs'], now, soon=True)} ({leg['source']})"
    )


def _named(
    wanted: str, hits: list[dict[str, Any]], times: _Times, now: datetime,
) -> dict[str, Any]:
    """The answer to 'can I still catch bus ``wanted``': how tight, and the next one."""
    if not hits:
        return {"route": wanted, "verdict": "none", "note": f"no route {wanted} bus found then"}
    option = hits[0]
    leg = option["legs"][0]
    named: dict[str, Any] = {
        "route": leg["route"],
        "board_stop": leg["board_stop"],
        "departs": said(leg["departs"], now),
        "source": leg["source"],
        "walk_min": option["walk_to_first_stop_min"],
        "spare_min": option["spare_min"],
        "verdict": option["verdict"],
        "_at": leg["departs"],
    }
    later = hits[1] if len(hits) > 1 else times.following(option, now)
    if later is not None:
        named["next"] = {
            "board_stop": later["legs"][0]["board_stop"],
            "departs": said(later["legs"][0]["departs"], now),
            "source": later["legs"][0]["source"],
            "leave_at": said(later["leave"], now),
            "_at": later["legs"][0]["departs"],
        }
    return named


def _named_line(named: Mapping[str, Any], now: datetime) -> str:
    """The sentence that answers 'can I still catch the 39': catchable or not, how tight, next."""
    route = named["route"]
    if named["verdict"] == "none":
        return f"Google has no route {route} bus then."
    at = said(named["_at"], now, soon=True)
    first = f"The {route} leaves {named['board_stop']} at {at} ({named['source']})"
    if named["verdict"] == "easy":
        first += f"; he has room ({named['spare_min']} min to spare after the walk)."
    elif named["verdict"] == "tight":
        first += f"; it is tight, he must walk fast ({named['spare_min']} min to spare)."
    else:
        first += "; he has missed it."
    later = named.get("next")
    if later and named["verdict"] == "missed":
        first += f" The next one is at {said(later['_at'], now)} ({later['source']})."
    return first


def _summary(  # noqa: PLR0913 - the plan's parts, in the order they are said (ADR 0343).
    selected: list[dict[str, Any]], *, to: str, old: Mapping[str, Any],
    named: Mapping[str, Any] | None, arrive_by: datetime | None, modes: Mapping[str, Any],
    last_today: tuple[str, datetime] | None, now: datetime,
) -> str:
    """The start if it is old, the run-for bus, the first he can catch, then the one after.

    Each bus comes with its time and whether that is live or scheduled.
    """
    parts: list[str] = [str(old["warning"])] if old else []
    if named is not None:
        others = " Other options are listed." if len(selected) > 1 else ""
        parts.append(_named_line(named, now) + others)
    elif arrive_by is not None:
        pick = next(
            (o for o in reversed(selected) if o["arrive"] <= arrive_by), selected[0],
        )
        parts.append(
            f"To {to}, option {selected.index(pick) + 1}: {_leave(pick, now)}, take"
            f" {_from(pick, now)}, arrive {said(pick['arrive'], now)} ({pick['total_min']} min)."
        )
    else:
        parts.extend(_ordered(selected, to, now))
    if last_today is not None:
        parts.append(f"The last {last_today[0]} had left at {said(last_today[1], now)}.")
    if walk := modes.get("walk"):
        parts.append(f"Walking all the way is {walk['minutes']} min.")
    if len(selected) > 1 and named is None and arrive_by is not None:
        parts.append(f"{len(selected)} options are listed.")
    return " ".join(parts)


def _ordered(selected: list[dict[str, Any]], to: str, now: datetime) -> list[str]:
    """The run-for bus, the first he can catch, the one after."""
    lines = []
    catch_seen = False
    for n, option in enumerate(selected, 1):
        if option.get("run_for"):
            lines.append(
                f"Option {n}: {_from(option, now)}, only by running."
            )
        elif not catch_seen:
            catch_seen = True
            lines.append(
                f"To {to}, option {n}: {_leave(option, now)}, take {_from(option, now)}, arrive"
                f" {said(option['arrive'], now)} ({option['total_min']} min)."
            )
        else:
            lines.append(
                f"Then option {n}: {_from(option, now)}, arrive {said(option['arrive'], now)}."
            )
    return lines


# --- the planner ---------------------------------------------------------------------------


def build_planner(  # noqa: PLR0913, PLR0915, C901 - the key, places, offers, live; one plan.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    *,
    offers: TransitOffers,
    bus_live: BusTimes | None,
    kept: Callable[[], list[place_state.Place]] | None = None,
    cap_usd: float = maps_spend.DEFAULT_MONTHLY_USD,
    book: places_api.PlaceBook | None = None,
    quiet: bool = False,
) -> Callable[[Mapping[str, Any], ToolContext], dict[str, Any]] | None:
    """The bus read over Google Routes behind ``bus(to=...)``; None without a key.

    Its arguments are ``origin`` and ``destination`` (both required, as he said the places),
    ``depart_at`` or ``arrive_by`` (a local date and time like ``2026-10-22 09:00``, or a clock
    time alone; ADR 0288, 0343) and ``route``. ``places`` reads, at each call, the kept ``home``
    and ``school`` as an address or ``lat,lng`` (ADR 0234); a missing one makes that word a tool
    error rather than a guess. Every place is read by the one reader (ADR 0346); "here" is the
    call's Here (ADR 0322), whose age the result then says. A failed read is a tool error telling
    the model to ask where the user is.
    ``offers`` is where each answer leaves its options, for the chat card (ADR 0205) and for
    ``bus(pin=...)``; the runtime shares it with the routes that pin them. ``bus_live`` is BC
    Transit's reader (the runtime wires it): each bus leg is matched to a trip of its timetable
    and takes its live or scheduled time (ADR 0342). ``kept`` reads every kept place, so a "here"
    inside one is said as it. Every Google request is counted against the month's ``cap_usd``
    (ADR 0283); ``book`` holds the places ``map_search`` found, so a place id has a name.
    ``quiet`` is for a background caller (:func:`build_leg_planner`): no card, no offer kept, no
    destination announced; the cap guard and the spend record stay.
    """
    if not api_key:
        return None
    book = book if book is not None else places_api.PlaceBook()
    reader = place_reader.PlaceReader(api_key, book, saved=places, kept=kept)

    def plan(  # noqa: C901 - read, ask, pick, say; one pass.
        args: Mapping[str, Any], ctx: ToolContext, meter: list[str],
    ) -> dict[str, Any]:
        origin, destination = str(args.get("origin", "")), str(args.get("destination", ""))
        if not origin.strip() or not destination.strip():
            msg = "origin and destination are both required"
            raise ToolError(msg, code="invalid_arguments")
        now = ctx.clock.now()
        when = _when(args, now)
        start = reader.read(origin, ctx, meter)
        end = reader.read(
            destination, ctx, meter, here_fix=start.fix if start.how == "here" else None,
        )
        old: dict[str, Any] = {}
        start_label = origin.strip()
        if start.fix is not None:
            start_label, old = _here_label(start.fix, kept)
        wanted = re.sub(r"[^0-9A-Za-z]", "", str(args.get("route") or ""))
        times = _Times(bus_live)
        options, hits, modes, answer = _search(
            api_key, start.waypoint, end.waypoint, wanted, now, when, times, meter,
        )
        selected = _pick(options, hits, wanted, when, times, now)
        extra: dict[str, Any] = {"modes": modes} if modes else {}
        named = _named(wanted, hits, times, now) if wanted else None
        if named is not None:
            extra["named_route"] = {k: v for k, v in named.items() if not k.startswith("_")} | (
                {"next": {k: v for k, v in named["next"].items() if not k.startswith("_")}}
                if "next" in named else {}
            )
        last_today = None
        first_catch = next((o for o in selected if not o.get("run_for")), None)
        if (
            first_catch is not None and not wanted and when.field is None
            and _first(first_catch).date() != now.date()
        ):
            gone = times.last_gone(first_catch, now)
            if gone is not None:
                last_today = (first_catch["legs"][0]["route"], gone)
                extra["last_today"] = {"route": last_today[0], "left_at": said(gone, now)}
        end_label = destination.strip()
        place = {
            "name": end.name, "lat": end.lat, "lng": end.lng, "address": end.address,
            "place_id": end.place_id, "kind": "place",
        }
        if end.unbiased:
            extra["note"] = (
                "He has no position to look near, so the place was searched for without a bias"
                " to where he is."
            )
        if not selected:
            if not modes:
                msg = (
                    "No bus route found for that trip at that time; tell him, or try another time"
                    " with at= or another place name"
                )
                raise ToolError(msg, code="not_found")
            summary = " ".join(filter(None, [
                old.get("warning"),
                f"No bus route to {end.said} at that time. "
                + " ".join(
                    f"By {name} it is {m['minutes']} min." for name, m in modes.items()
                    if name in ("walk", "drive")
                ),
            ]))
            return {
                "summary": summary, "from": start_label, "to": end_label, **old, "options": [],
                **extra, "destination": place,
            }
        # The card, the list `pin` reads and the notch's stops are for buses from now on.
        if not quiet and (when.at is None or when.at.date() == now.date()):
            _offer(offers, ctx, answer, selected, modes, destination.strip().lower()[:60])
        out_options = [_view(o, n, now) for n, o in enumerate(selected, 1)]
        if when.field and when.at:
            extra["at" if when.field == "departureTime" else "arrive_by"] = said(when.at, now)
        if not quiet:
            extra["destination"] = place
            places_api.announce(ctx, [place])
        summary = _summary(
            selected, to=end.said, old=old, named=named,
            arrive_by=when.at if when.field == "arrivalTime" else None, modes=modes,
            last_today=last_today, now=now,
        )
        return {
            "summary": summary, "from": start_label, "to": end_label, **old,
            "options": out_options, **extra,
        } | ({"_selected": selected, "_when": when} if quiet else {})

    def transit(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        maps_spend.guard(ctx.conn, cap_usd, ctx.clock.now())
        meter: list[str] = []
        try:
            return plan(args, ctx, meter)
        finally:
            maps_spend.record(ctx, meter)

    return transit


def _clock_view(option: Mapping[str, Any]) -> dict[str, Any]:
    """An option for a background job (ADR 0288): its times as ``HH:MM`` and as epoch ms."""
    return {
        "leave_at": f"{option['leave']:%H:%M}",
        "leave_ms": _at_ms(option["leave"]),
        "arrive_at": f"{option['arrive']:%H:%M}",
        "arrive_ms": _at_ms(option["arrive"]),
        "total_min": option["total_min"],
        "spare_min": option["spare_min"],
        "verdict": option["verdict"],
        "legs": [
            {
                "route": leg["route"], "headsign": leg["headsign"],
                "board_stop": leg["board_stop"], "departs": f"{leg['departs']:%H:%M}",
                "source": leg["source"], "alight_stop": leg["alight_stop"],
                "arrives": f"{leg['arrives']:%H:%M}",
            }
            for leg in option["legs"]
        ],
    }


def build_leg_planner(  # noqa: PLR0913 - the key, places, the context, kept, cap, book.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    ctx: ToolContext,
    *,
    kept: Callable[[], list[place_state.Place]] | None = None,
    cap_usd: float = maps_spend.DEFAULT_MONTHLY_USD,
    book: places_api.PlaceBook | None = None,
    bus_live: BusTimes | None = None,
) -> Callable[..., dict[str, Any]] | None:
    """One leg for a background job (ADR 0288), or None without a key.

    ``plan_leg(origin, destination, *, arrive_by=None, depart_at=None)`` plans a leg for a local
    date and time like ``2026-10-22 09:00`` (or a clock time alone, as ``bus`` takes it); exactly
    one of ``arrive_by`` and ``depart_at``. ``ctx`` is the job's own (its connection takes the
    cap guard and the spend rows). It shows no card and keeps no offer; "here" is not available.
    The answer is :func:`build_planner`'s plus ``chosen`` and ``backup`` (or None), as ``HH:MM``
    times: for ``arrive_by`` the latest bus that arrives in time and the one before it; for
    ``depart_at`` the first he can still catch and the one after it. ToolError ``spend_cap`` when
    the month's cap is reached, ``invalid_arguments`` for a time that has passed or one over 30
    days ahead, ``not_found`` for no bus route.
    """
    plan = build_planner(
        api_key, places, offers=TransitOffers(), bus_live=bus_live,
        kept=kept, cap_usd=cap_usd, book=book, quiet=True,
    )
    if plan is None:
        return None

    def plan_leg(
        origin: str, destination: str, *,
        arrive_by: str | None = None, depart_at: str | None = None,
    ) -> dict[str, Any]:
        if bool(arrive_by) == bool(depart_at):
            msg = "give exactly one of arrive_by and depart_at"
            raise ToolError(msg, code="invalid_arguments")
        args: dict[str, Any] = {"origin": origin, "destination": destination}
        args |= {"arrive_by": arrive_by} if arrive_by else {"depart_at": depart_at}
        raw = plan(args, ctx)
        selected: list[dict[str, Any]] = raw.pop("_selected")
        when: _When = raw.pop("_when")
        options = [_clock_view(o) for o in selected]
        if arrive_by:
            assert when.at is not None  # noqa: S101 - `_when` returned the arrive-by moment.
            fits = [o for o in selected if o["arrive"] <= when.at] or selected[:1]
            at = selected.index(fits[-1])
            backup = options[at - 1] if at else None
        else:
            at = next(
                (i for i, o in enumerate(selected) if o["spare_min"] > -_EASY_SPARE_MIN), 0,
            )
            backup = options[at + 1] if at + 1 < len(options) else None
        return {**raw, "options": options, "chosen": options[at], "backup": backup}

    return plan_leg


def _row_trip(
    offers: TransitOffers, row: Mapping[str, Any], tid: str, now: datetime,
) -> departures.Trip:
    """The trip a listed row stands for, with the stop and drop-off positions Google gave."""
    first = str(row["route"]).split("→")[0].strip()
    zone = _zone(now)
    return trip_of(
        tid=tid,
        route=row["route"],
        board_stop=row["board_stop"],
        leave=datetime.fromtimestamp(row["leave_ms"] / _MS, zone),
        departs=datetime.fromtimestamp(row["departs_ms"] / _MS, zone),
        arrive_at=row["arrive_at"],
        to=row["to"],
        stop=offers.stops.get((first, row["board_stop"])),
        now=now,
        alight=offers.alights.get((first, row["board_stop"])),
        bus_trip_id=row.get("bus_trip_id"),
    )


def _pinned_already(held: departures.Departure | None, rows: list[dict[str, Any]]) -> bool:
    """Whether the pin in force holds exactly these rows' buses (same route and stop, same bus).

    A pinned bus's time moves with the live feed, so the bus counts as the same within five
    minutes of the listed one.
    """
    if held is None or len(held.trips) != len(rows):
        return False
    return all(
        any(
            t.route == r["route"] and t.board_stop == r["board_stop"]
            and abs(t.departs_at_ms - r["departs_ms"]) <= _SAME_BUS_MS
            for t in held.trips
        )
        for r in rows
    )


def pin_listed(  # noqa: PLR0913 - the log, the offers, the numbers, the action, the moment.
    conn: sqlite3.Connection,
    offers: TransitOffers,
    numbers: list[int],
    *,
    action_id: str,
    now: datetime,
    source_event_id: str | None = None,
) -> tuple[list[departures.Trip], bool, dict[str, Any]]:
    """Pin options ``numbers`` (from 1) of the last list as a new set; a newer set replaces.

    Returns the pinned trips, whether this call pinned them and the pin (``pin_id``,
    ``event_uid``): ``False`` when the pin in force already holds exactly these buses, which is
    left as it is (no second ring; ``event_uid`` None). ToolError
    ``not_found`` for a number the list has not got, ``time_in_past`` for a leave time gone.
    """
    rows = [offers.listed_row(n) for n in numbers]
    held = departures.active(conn, _at_ms(now))
    if held is not None and _pinned_already(held, rows):
        return held.trips, False, {"pin_id": held.pin_id, "event_uid": None}
    trips = []
    for n, row in zip(numbers, rows, strict=True):
        try:
            trips.append(_row_trip(offers, row, departures.new_tid(), now))
        except ToolError as exc:
            if exc.code != "time_in_past":
                raise
            msg = (
                f"Option {n} (leave {said_ms(row['leave_ms'], now)}) is already past; ask for the"
                " times again with bus(to=...)"
            )
            raise ToolError(msg, code="time_in_past") from exc
    made = pin_trips(
        conn, trips, offer_id=None, action_id=action_id, now=now, source_event_id=source_event_id,
    )
    return trips, True, made


__all__ = [
    "BusTimes",
    "TransitOffers",
    "add_trip",
    "build_leg_planner",
    "build_planner",
    "first_leave_ms",
    "here_warning",
    "live_card",
    "pin_listed",
    "pin_offered",
    "pin_trips",
    "ring",
    "said",
    "said_clock",
    "said_ms",
    "show_card",
    "trip_of",
]
