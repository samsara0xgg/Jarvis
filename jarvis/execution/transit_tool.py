"""ADR 0189: the bus times from Google Routes, the offer they leave, and the pins made from it.

The model reaches all of it through the one ``bus`` tool (``jarvis.execution.bus_tool``, ADR 0246).
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.execution import maps_spend, places_api
from jarvis.execution.location_tool import describe_age, read_here, said_where
from jarvis.execution.tools import ToolError, _get_running_event_uid, _turn_of
from jarvis.shared import lang
from jarvis.state import departures, reminders
from jarvis.state import places as place_state
from jarvis.state.event_log import MAC_NODE, emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_URL: Final = "https://routes.googleapis.com/directions/v2:computeRoutes"
_TIMEOUT_S: Final = 5.0
_MAX_OPTIONS: Final = 3
# A far-fetched option (ADR 0203): it arrives this long after the earliest, or rides this much
# longer.
_LATER_MIN: Final = 15
_SLOWER: Final = 1.5
# Every search without `at` asks Google from this long before now: a bus whose leave time
# (departure minus Google's walking estimate) just passed is still returned, and he can walk
# faster than Google assumes (ADR 0221 for a named route, ADR 0265 for all).
_LOOKBACK_MIN: Final = 10
# A bus this many minutes short of a normal walk is gone for good: no run makes it up.
_RUN_MIN: Final = 5
_LIVE_TIMEOUT_S: Final = 4.0
# spare_min: at or above this he has room (easy); at or below its negative the bus is gone.
_EASY_SPARE_MIN: Final = 2
_SCHOOL_NAMES: Final = frozenset({"uvic", "u vic", "university of victoria"})
# ADR 0288: a plan asks for a day this far ahead at most.
_MAX_DAYS_AHEAD: Final = 30
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
) + ",routes.legs.endLocation,geocodingResults"
# The last two: the point Google put the destination at, and the place_id it resolved a text to
# (ADR 0283), so the answer can say what Google actually understood.
# DRIVE and WALK need only how long and how far; the default routing preference is the cheap one.
_MODE_MASK: Final = "routes.duration,routes.distanceMeters"
_MODES: Final = (("drive", "DRIVE"), ("walk", "WALK"))
_POSTAL = re.compile(r"\s*[A-Z]\d[A-Z]\s?\d[A-Z]\d\s*$")
_LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

def _here(
    ctx: ToolContext, kept: Callable[[], list[place_state.Place]] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """The user's location as a waypoint, how to say it in the result, and a warning if it is old.

    A fix inside a kept place's circle is said as that place, not as the street the phone named.
    The warning is ``{}`` unless Here could not confirm the report with a fresh fix (the report is
    past its age limit); then it holds ``from_age_min`` and the ``warning`` line the model must
    act on before giving any times.
    """
    fix = read_here(ctx)
    inside = place_state.containing(kept(), fix.lat, fix.lng) if kept else None
    near = f"at {inside.name}, " if inside is not None else f"{fix.place}, " if fix.place else ""
    label = f"here ({said_where(fix)}, {near}±{round(fix.accuracy_m)} m)"
    latlng = {"latitude": fix.lat, "longitude": fix.lng}
    old: dict[str, Any] = {}
    if fix.why and fix.device != MAC_NODE:
        where = fix.place or "no place name"
        old = {
            "from_age_min": fix.age_s // 60,
            "warning": (
                f"The starting point is his phone's report from {describe_age(fix.age_s)} ago"
                f" ({where}); he may have moved. Say that first, or ask where he is, before"
                " giving times from it."
            ),
        }
    return {"location": {"latLng": latlng}}, label, old


def _waypoint(
    place: str,
    saved: Mapping[str, str],
    ctx: ToolContext,
    kept: Callable[[], list[place_state.Place]] | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    word = " ".join(place.lower().split())
    if word in _SCHOOL_NAMES:
        word = "school"
    if word == "here":
        return _here(ctx, kept)
    text = saved.get(word, "") if word in ("home", "school") else place.strip()
    if word not in ("home", "school") and (one := _kept_named(word, kept)):
        text = f"{one.lat},{one.lng}" if one.lat is not None and one.lng is not None else (
            one.address or text
        )
    if not text:
        msg = (
            f"His {word} address isn't saved yet; ask him for it and save it with save_place, or"
            " use a place name or address instead"
        )
        raise ToolError(msg, code="not_configured")
    if match := _LAT_LNG.match(text):
        latlng = {"latitude": float(match[1]), "longitude": float(match[2])}
        return {"location": {"latLng": latlng}}, place.strip(), {}
    return {"address": text}, place.strip(), {}


def _kept_named(
    word: str, kept: Callable[[], list[place_state.Place]] | None,
) -> place_state.Place | None:
    """The kept place he calls ``word`` (its name, any case), when it has a point or an address."""
    for one in kept() if kept else ():
        if " ".join(one.name.lower().split()) == word and (
            one.address or (one.lat is not None and one.lng is not None)
        ):
            return one
    return None


class _When(NamedTuple):
    """The day asked for, and the Routes time field to send with its UTC stamp (None: now)."""

    day: date
    field: str | None
    stamp: str | None


def _when(args: Mapping[str, Any], now: datetime) -> _When:
    """``day`` with ``depart_at`` or ``arrive_by`` (HH:MM local on that day); ADR 0288.

    ToolError ``invalid_arguments`` for both times, a day not ISO, past or over 30 days ahead, an
    arrive-by time that has passed, or a day other than today with neither time. A ``depart_at``
    earlier than now is an error only when ``day`` is given; without it the older behavior stands.
    """
    depart, arrive = args.get("depart_at"), args.get("arrive_by")
    if depart and arrive:
        msg = "give at= or arrive_by=, not both"
        raise ToolError(msg, code="invalid_arguments")
    today = now.date()
    try:
        day = date.fromisoformat(str(args["day"]).strip()) if args.get("day") else today
    except ValueError as exc:
        msg = f"day must be a date as YYYY-MM-DD, not {args['day']!r}; leave it out for today"
        raise ToolError(msg, code="invalid_arguments") from exc
    if day < today or day > today + timedelta(days=_MAX_DAYS_AHEAD):
        msg = (
            f"day {day} is past or more than {_MAX_DAYS_AHEAD} days ahead; times can be read"
            " from today up to then"
        )
        raise ToolError(msg, code="invalid_arguments")
    given, name = (arrive, "arrive_by") if arrive else (depart, "at")
    if not given:
        if day != today:
            msg = f"day={day} needs at=HH:MM or arrive_by=HH:MM"
            raise ToolError(msg, code="invalid_arguments")
        return _When(day, None, None)
    at = _today(given, name, now, day)
    if at < now.replace(second=0, microsecond=0) and (arrive or args.get("day")):
        msg = f"{name}={given} on {day} has already passed"
        raise ToolError(msg, code="invalid_arguments")
    return _When(day, "arrivalTime" if arrive else "departureTime", _utc(at))


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone(now: datetime) -> tzinfo:
    """The zone of an aware ``now``: the clock's, in which his times are said."""
    return now.tzinfo or UTC


def _clock(stamp: str, zone: tzinfo) -> datetime:
    """Google's absolute ``stamp`` as a time in his ``zone``."""
    return datetime.fromisoformat(stamp).astimezone(zone)


def _seconds(step: Mapping[str, Any]) -> float:
    return float(str(step.get("staticDuration", "0s")).removesuffix("s"))


def _option(route: Mapping[str, Any], zone: tzinfo) -> dict[str, Any] | None:
    """One route in plain words, or None when it has no bus ride in it."""
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
        legs.append(
            {
                "route": line.get("nameShort") or line.get("name", "?"),
                "headsign": detail.get("headsign", ""),
                "board_stop": stops["departureStop"]["name"],
                "departs": _clock(stops["departureTime"], zone).strftime("%H:%M"),
                "alight_stop": stops["arrivalStop"]["name"],
                "arrives": _clock(stops["arrivalTime"], zone).strftime("%H:%M"),
            }
        )
    leave = _clock(rides[0]["transitDetails"]["stopDetails"]["departureTime"], zone) - walk_to
    arrive = _clock(rides[-1]["transitDetails"]["stopDetails"]["arrivalTime"], zone) + walk_from
    return {
        "leave_at": leave.strftime("%H:%M"),
        "walk_to_first_stop_min": round(walk_to.total_seconds() / 60),
        "legs": legs,
        "walk_at_end_min": round(walk_from.total_seconds() / 60),
        "arrive_at": arrive.strftime("%H:%M"),
        "total_min": round((arrive - leave).total_seconds() / 60),
    }


def _spare(departs: datetime, walk_min: int, now: datetime) -> int:
    """Whole minutes he has left beyond a normal walk to catch the bus (negative: late)."""
    return math.floor((departs - now).total_seconds() / 60) - walk_min


def _verdict(spare: int) -> str:
    """``easy`` with room, ``missed`` when a normal walk is too late, else ``tight``."""
    if spare >= _EASY_SPARE_MIN:
        return "easy"
    return "missed" if spare <= -_EASY_SPARE_MIN else "tight"


def _pairs(
    answer: Mapping[str, Any], now: datetime, day: date | None = None,
) -> list[tuple[dict[str, Any], Any]]:
    """Each bus route of Google's answer as ``(option with spare_min, Google's route)``.

    ``day`` is the day the answer is for (default today): the options' ``HH:MM`` are on it.
    """
    pairs = [(o, r) for r in answer.get("routes", ()) if (o := _option(r, _zone(now)))]
    for o, _ in pairs:
        at = _today(o["legs"][0]["departs"], "departs", now, day)
        o["spare_min"] = _spare(at, o["walk_to_first_stop_min"], now)
        o["verdict"] = _verdict(o["spare_min"])
    return pairs


def _ahead(
    pairs: list[tuple[dict[str, Any], Any]], wanted: str, now: datetime, day: date | None = None,
) -> list[tuple[dict[str, Any], Any]]:
    """The routes whose first bus is ``wanted`` and not gone yet, earliest first."""
    floor = now.replace(second=0, microsecond=0)
    mine = [
        p for p in pairs
        if p[0]["legs"][0]["route"].casefold() == wanted.casefold()
        and _today(p[0]["legs"][0]["departs"], "departs", now, day) >= floor
    ]
    return sorted(mine, key=lambda p: p[0]["legs"][0]["departs"])  # ponytail: one day, no midnight


def _live_ms(
    live: Callable[[str, tuple[float, float], int], int | None],
    route: str,
    stop: tuple[float, float],
    around_ms: int,
) -> int | None:
    """BC Transit's live departure for the bus; None when it has none, fails or is too slow."""
    pool = ThreadPoolExecutor(max_workers=1)  # a cold first download is left to finish alone
    try:
        return pool.submit(live, route, stop, around_ms).result(timeout=_LIVE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - the Google time stands.
        return None
    finally:
        pool.shutdown(wait=False)


def _named_route(
    wanted: str,
    hits: list[tuple[dict[str, Any], Any]],
    now: datetime,
    live: Callable[[str, tuple[float, float], int], int | None] | None,
    day: date | None = None,
) -> dict[str, Any]:
    """The answer to 'can I still catch bus ``wanted``': how tight, and the next one.

    ``day`` other than today: no live lookup, BC Transit's feed is for the buses running now.
    """
    if not hits:
        return {"route": wanted, "verdict": "none", "note": f"no route {wanted} bus found then"}
    option, raw = hits[0]
    leg = option["legs"][0]
    at, fresh = _today(leg["departs"], "departs", now, day), False
    stop = _first_board(raw)
    if live is not None and stop is not None and (day or now.date()) == now.date():
        ms = _live_ms(live, leg["route"], stop[1], int(at.timestamp() * 1000))
        if ms:
            at, fresh = datetime.fromtimestamp(ms / 1000, _zone(now)), True
    walk = option["walk_to_first_stop_min"]
    spare = _spare(at, walk, now)
    named: dict[str, Any] = {
        "route": leg["route"],
        "board_stop": leg["board_stop"],
        "departs": at.strftime("%H:%M"),
        "walk_min": walk,
        "spare_min": spare,
        "verdict": _verdict(spare),
    }
    if fresh:
        named["live"] = True
    if len(hits) > 1:
        later = hits[1][0]
        named["next"] = {
            "board_stop": later["legs"][0]["board_stop"],
            "departs": later["legs"][0]["departs"],
            "leave_at": later["leave_at"],
        }
    return named


def _with_city(end: Mapping[str, Any], places: Mapping[str, str]) -> dict[str, Any] | None:
    """A text destination with the home's city added (Victoria, BC if none); None for the rest.

    'UVic' or 'CARSA' alone is not found; 'CARSA, University of Victoria' is.
    """
    address = end.get("address")
    if not address or address in places.values():
        return None
    home = places.get("home", "")
    city = "" if _LAT_LNG.match(home) else ", ".join(p.strip() for p in home.split(",")[1:3])
    city = _POSTAL.sub("", city).strip(", ")
    return {"address": f"{address}, {city or 'Victoria, BC'}"}


def _search(  # noqa: PLR0913 - the trip, how it was asked and the clock.
    api_key: str,
    start: Mapping[str, Any],
    end: Mapping[str, Any],
    wanted: str,
    now: datetime,
    when: _When,
    meter: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[dict[str, Any], Any]], dict[str, Any]]:
    """Ask Google and the other ways: ``(answer, options, hits of the named route, modes)``.

    Without ``depart_at`` it asks from ten minutes ago, so a bus whose leave time just passed
    still shows, with its true spare minutes; if a named route is not found that way, the usual
    'now' answer is used. With ``arrive_by`` it sends ``arrivalTime`` instead of a departure and
    keeps the last three buses by leave time, the one nearest the deadline last (ADR 0288).
    DRIVE and WALK carry no time either way. ToolError ``not_found`` when no bus route is left.
    """
    body: dict[str, Any] = {
        "origin": start,
        "destination": end,
        "travelMode": "TRANSIT",
        "computeAlternativeRoutes": True,
    }
    plain = dict(body)
    if when.field:
        body[when.field] = when.stamp
    else:
        ago = now.replace(second=0, microsecond=0) - timedelta(minutes=_LOOKBACK_MIN)
        body["departureTime"] = _utc(ago)
    shifted = when.field is None
    day = when.day
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
        pairs = _pairs(answer, now, day)
        hits = _ahead(pairs, wanted, now, day) if wanted else []
        if wanted and shifted and not hits:
            try:
                later = pool.submit(_compute, api_key, plain, _FIELD_MASK, meter).result()
            except Exception:  # noqa: BLE001 - the first answer stands.
                later = None
            if later is not None:
                answer, pairs = later, _pairs(later, now, day)
                hits = _ahead(pairs, wanted, now, day)
    modes = {name: found for name, way in ways.items() if (found := way.result())}
    # An earlier start returns buses he could not reach at a normal pace: only the named one stays.
    first = hits[0][0] if hits else None
    rest = [o for o, _ in pairs if o is not first]
    near: list[dict[str, Any]] = []
    if shifted:
        rest.sort(key=lambda o: o["legs"][0]["departs"])  # ponytail: one day, no midnight
        if wanted:
            rest = [o for o in rest if o["spare_min"] >= 0]
        else:
            # Only the latest bus he missed by a run's worth stays, marked; it is no yardstick.
            near = [
                o for o in rest if o["verdict"] == "missed" and o["spare_min"] >= -_RUN_MIN
            ][-1:]
            rest = [o for o in rest if o["verdict"] != "missed"]
    # The named bus is first however late or slow it is; it does not set the others' yardstick.
    if when.field == "arrivalTime":
        late = sorted(rest, key=lambda o: o["legs"][0]["departs"])  # ponytail: one day
        options = ([first] if first else []) + late[-(_MAX_OPTIONS - bool(first)) :]
    else:
        options = ([first] if first else []) + near + _sensible(rest)
    if not options:
        msg = (
            "No bus route found for that trip at that time; tell him, or try another time with"
            " at=HH:MM or another place name"
        )
        raise ToolError(msg, code="not_found")
    return answer, options[:_MAX_OPTIONS], hits, modes


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


def _today(value: object, field: str, now: datetime, day: date | None = None) -> datetime:
    """``HH:MM`` as a local time on ``day`` (default today), or a tool error naming the field."""
    try:
        at = time.fromisoformat(str(value).strip())
    except ValueError as exc:
        msg = f"{field} must be a local time as HH:MM, not {value!r}"
        raise ToolError(msg, code="invalid_arguments") from exc
    return datetime.combine(
        day or now.date(), at.replace(second=0, microsecond=0), tzinfo=_zone(now),
    )


_PIN_PAST_GRACE: Final = timedelta(minutes=1)
_SAME_BUS_MS: Final = 5 * 60_000
_SAY_AHEAD_MIN: Final = 90
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
        return [
            {
                "index": i,
                "route": " → ".join(leg["route"] for leg in o["legs"]),
                "board_stop": o["legs"][0]["board_stop"],
                "leave_at": o["leave_at"],
                "departs": o["legs"][0]["departs"],
                "arrive_at": o["arrive_at"],
                "to": to,
            }
            for i, o in enumerate(options)
        ]

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
        self, route: str, board_stop: str, after: datetime, now: datetime,
    ) -> tuple[datetime, datetime, str] | None:
        """``(leave, departs, arrive_at)`` of the earliest option on that route and stop."""
        found = [
            (_today(r["departs"], "", now), _today(r["leave_at"], "", now), r["arrive_at"])
            for r in self._rows
            if r["route"].split("→")[0].strip() == route and r["board_stop"] == board_stop
        ]
        later = sorted(f for f in found if f[0] > after and f[1] >= now - _PIN_PAST_GRACE)
        return (later[0][1], later[0][0], later[0][2]) if later else None


def _leaves(made_at: datetime, row: Mapping[str, Any]) -> datetime:
    """A row's leave time: its ``HH:MM`` on the day the answer was made.

    The next day when that is more than an hour before the moment it was made.
    """
    at = datetime.combine(made_at.date(), time.fromisoformat(str(row["leave_at"])), made_at.tzinfo)
    return at + timedelta(days=1) if at < made_at - timedelta(hours=1) else at


def live_card(trip: Mapping[str, Any], now: datetime) -> dict[str, Any] | None:
    """The card's trip as served now: no row whose leave time has passed; None if none is left.

    A row's ``HH:MM`` is on the day the answer was made (the next day when it is more than an hour
    before that moment), so a card does not come back after midnight.
    """
    made_at = datetime.fromisoformat(str(trip["made_at"]))
    floor = now.replace(second=0, microsecond=0)
    rows = [r for r in trip["options"] if _leaves(made_at, r) >= floor]
    return {**trip, "options": rows} if rows else None


def first_leave_ms(trip: Mapping[str, Any]) -> int | None:
    """When the card's first option says to leave, in epoch ms; None if it cannot be read.

    The day is :func:`live_card`'s: the day the answer was made, as the card was kept.
    """
    try:
        made_at = datetime.fromisoformat(str(trip["made_at"]))
        return int(_leaves(made_at, trip["options"][0]).timestamp() * 1000)
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
        int(leave.timestamp() * 1000),
        int(departs.timestamp() * 1000),
        arrive_at,
        None if stop is None else stop[0],
        None if stop is None else stop[1],
        to,
        None if alight is None else alight[0],
        None if alight is None else alight[1],
        None if alight is None else alight[2],
    )


def _hhmm(ms: int, zone: tzinfo) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


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
    if target.leave_at_ms < now_ms - int(_PIN_PAST_GRACE.total_seconds() * 1000):
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
        conn, trips, now_ms=int(now.timestamp() * 1000), zone=_zone(now), reminder_id=None,
        ring_tid=None, action_id=action_id, source_event_id=source_event_id,
    )
    pin_id = departures.pin(
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
    now_ms = int(now.timestamp() * 1000)
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
        pin = departures.active(conn, int(now.timestamp() * 1000))
        if add and pin is not None and pin.offer_id == offer_id:
            add_trip(conn, pin, trip, action_id=offer_id, now=now)
            return {"pin_id": pin.pin_id}
        return pin_trips(conn, [trip], offer_id=offer_id, action_id=offer_id, now=now)
    except ToolError as exc:
        if exc.code == "full":
            return {"reason": "full"}
        raise LookupError(str(exc)) from exc


def _sensible(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the far-fetched: arriving over 15 min after the earliest, or 1.5x the shortest ride."""
    if not options:
        return options
    def minutes(o: dict[str, Any]) -> int:
        h, m = str(o["arrive_at"]).split(":")
        return int(h) * 60 + int(m)
    first = min(map(minutes, options))
    shortest = min(int(o["total_min"]) for o in options)
    kept = [
        o for o in options
        if minutes(o) - first <= _LATER_MIN and o["total_min"] <= _SLOWER * shortest
    ]
    return (kept or options[:1])[:_MAX_OPTIONS]




def _by_id(place_id: str, book: places_api.PlaceBook) -> tuple[dict[str, Any], str]:
    """A ``map_search`` place as the route's end, and the name to say it by."""
    known = book.get(place_id)
    return {"placeId": place_id}, str(known["name"]) if known else "that place"


def _destination(  # noqa: PLR0913 - the answer, the end asked, who he named, the key, the book.
    answer: Mapping[str, Any],
    end: Mapping[str, Any],
    label: str,
    saved: Mapping[str, str],
    api_key: str,
    book: places_api.PlaceBook,
    meter: list[str],
) -> dict[str, Any] | None:
    """The place Google took the destination to be, as the phone's map draws it (ADR 0283).

    The point is Google's own end of the route. Name and address are the remembered result of a
    ``map_search`` for a place_id, else Google's Place Details for the id it resolved a text to;
    a destination he kept (home, school) is said as he named it, and one whose name cannot be
    read is said as he asked it. None when the answer has no end point.
    """
    try:
        at = answer["routes"][0]["legs"][-1]["endLocation"]["latLng"]
        lat, lng = float(at["latitude"]), float(at["longitude"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    address = end.get("address")
    kept = (address is None and "placeId" not in end) or address in saved.values()
    found: Mapping[str, Any] | None = None
    resolved = (answer.get("geocodingResults") or {}).get("destination") or {}
    place_id = end.get("placeId") or (None if kept else resolved.get("placeId"))
    if place_id:
        found = book.get(str(place_id))
        if found is None:
            try:
                found = places_api.on_map(places_api.name_of(api_key, str(place_id), meter))
            except ToolError:
                found = None  # the name is a bonus; the route stands.
            if found is not None:
                book.remember(found)
    return {
        "name": (found or {}).get("name") or label,
        "lat": lat,
        "lng": lng,
        "address": (found or {}).get("address"),
        "place_id": str(place_id) if place_id else None,
        "kind": "place",
    }


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


def build_planner(  # noqa: PLR0913, C901 - the key, places, offers, live; one plan.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    *,
    offers: TransitOffers,
    bus_live: Callable[[str, tuple[float, float], int], int | None] | None,
    kept: Callable[[], list[place_state.Place]] | None = None,
    cap_usd: float = maps_spend.DEFAULT_MONTHLY_USD,
    book: places_api.PlaceBook | None = None,
    quiet: bool = False,
) -> Callable[[Mapping[str, Any], ToolContext], dict[str, Any]] | None:
    """The bus read over Google Routes behind ``bus(to=...)``; None without a key.

    Its arguments are ``origin`` and ``destination`` (both required), ``depart_at`` or
    ``arrive_by`` (HH:MM) on ``day`` (ISO date, default today; ADR 0288),
    ``route`` and ``place_id`` (a ``map_search`` result standing for the destination). ``places``
    reads, at each call, the kept ``home`` and ``school`` as an address or
    ``lat,lng`` (ADR 0234); a missing one makes that word a tool error rather than a guess.
    "here" is the call's Here (ADR 0322), whose age the result then says. A failed read is a tool
    error telling the model to ask where the user is.
    ``offers`` is where each answer leaves its options, for the chat card (ADR 0205) and for
    ``bus(pin=...)``; the runtime shares it with the routes that pin them. ``bus_live`` is BC
    Transit's live departure of a route at a stop near ``(lat, lng)`` around an epoch ms (the
    runtime wires it); when it answers, a named route's ``departs`` and
    ``spare_min`` use it. ``kept`` reads every kept place, so a "here" inside one is said as it.
    Every Google request is counted against the month's ``cap_usd`` (ADR 0283); ``book`` holds the
    places ``map_search`` found, so a place_id has a name. ``quiet`` is for a background caller
    (:func:`build_leg_planner`): no card, no offer kept, no destination announced or named (that
    would be a Place Details request); the cap guard and the spend record stay.
    """
    if not api_key:
        return None
    book = book if book is not None else places_api.PlaceBook()

    def plan(args: Mapping[str, Any], ctx: ToolContext, meter: list[str]) -> dict[str, Any]:
        origin, destination = str(args.get("origin", "")), str(args.get("destination", ""))
        if not origin.strip() or not destination.strip():
            msg = "origin and destination are both required"
            raise ToolError(msg, code="invalid_arguments")
        saved = places()
        start, start_label, old_start = _waypoint(origin, saved, ctx, kept)
        place_id = str(args.get("place_id") or "").strip()
        if place_id:
            end, end_label = _by_id(place_id, book)
            destination = end_label
        else:
            end, end_label, _ = _waypoint(destination, saved, ctx, kept)
        now = ctx.clock.now()
        when = _when(args, now)
        wanted = re.sub(r"[^0-9A-Za-z]", "", str(args.get("route") or ""))
        try:
            answer, options, hits, modes = _search(
                api_key, start, end, wanted, now, when, meter,
            )
        except ToolError as exc:
            retry = _with_city(end, saved) if exc.code == "not_found" else None
            if retry is None:
                raise
            end = retry
            end_label = retry["address"]
            answer, options, hits, modes = _search(
                api_key, start, end, wanted, now, when, meter,
            )
        # The card, the list `pin` reads and the notch's stops are today's buses.
        if not quiet and when.day == now.date():
            asked = end_label.strip().lower()[:60] if place_id else destination.strip().lower()[:60]
            _offer(offers, ctx, answer, options, modes, asked)
        extra: dict[str, Any] = {"modes": modes} if modes else {}
        extra |= {k: str(args[k]).strip() for k in ("day", "arrive_by") if args.get(k)}
        if wanted:
            extra["named_route"] = _named_route(wanted, hits, now, bus_live, when.day)
        if not quiet and (
            dest := _destination(answer, end, end_label, saved, api_key, book, meter)
        ):
            extra["destination"] = dest
            places_api.announce(ctx, [dest])
        return {"from": start_label, "to": end_label, **old_start, "options": options, **extra}

    def transit(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        maps_spend.guard(ctx.conn, cap_usd, ctx.clock.now())
        meter: list[str] = []
        try:
            return plan(args, ctx, meter)
        finally:
            maps_spend.record(ctx, meter)

    return transit


def build_leg_planner(  # noqa: PLR0913 - the key, places, the context, kept, cap, book.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    ctx: ToolContext,
    *,
    kept: Callable[[], list[place_state.Place]] | None = None,
    cap_usd: float = maps_spend.DEFAULT_MONTHLY_USD,
    book: places_api.PlaceBook | None = None,
) -> Callable[..., dict[str, Any]] | None:
    """One leg for a background job (ADR 0288), or None without a key.

    ``plan_leg(origin, destination, *, day, arrive_by=None, depart_at=None)`` plans a leg on the
    ISO date ``day`` for HH:MM local ``arrive_by`` or ``depart_at`` (exactly one). ``ctx`` is the
    job's own (its connection takes the cap guard and the spend rows). It shows no card and keeps
    no offer; "here" is not available. The answer is :func:`build_planner`'s plus ``chosen`` and
    ``backup`` (or None): for ``arrive_by`` the latest bus that arrives in time and the one
    before it; for ``depart_at`` the first he can still catch and the one after it. ToolError
    ``spend_cap`` when the month's cap is reached, ``invalid_arguments`` for a time that has
    passed or a day over 30 days ahead, ``not_found`` for no bus route.
    """
    plan = build_planner(
        api_key, places, offers=TransitOffers(), bus_live=None,
        kept=kept, cap_usd=cap_usd, book=book, quiet=True,
    )
    if plan is None:
        return None

    def plan_leg(
        origin: str, destination: str, *, day: str,
        arrive_by: str | None = None, depart_at: str | None = None,
    ) -> dict[str, Any]:
        if bool(arrive_by) == bool(depart_at):
            msg = "give exactly one of arrive_by and depart_at"
            raise ToolError(msg, code="invalid_arguments")
        args: dict[str, Any] = {"origin": origin, "destination": destination, "day": day}
        args |= {"arrive_by": arrive_by} if arrive_by else {"depart_at": depart_at}
        raw = plan(args, ctx)
        options = raw["options"]
        if arrive_by:
            fits = [o for o in options if o["arrive_at"] <= arrive_by.strip()] or options[:1]
            at = options.index(fits[-1])
            backup = options[at - 1] if at else None
        else:
            at = next((i for i, o in enumerate(options) if o["spare_min"] > -_EASY_SPARE_MIN), 0)
            backup = options[at + 1] if at + 1 < len(options) else None
        return {**raw, "chosen": options[at], "backup": backup}

    return plan_leg


def said_clock(at: datetime, now: datetime, *, soon: bool = False) -> str:
    """``at`` as he hears it: ``5:06 pm``; ``soon`` adds `` (in 6 min)`` within 90 minutes."""
    local = at.astimezone(_zone(now))
    hour = local.hour % 12 or 12
    text = f"{hour}:{local.minute:02d} {'am' if local.hour < 12 else 'pm'}"  # noqa: PLR2004
    ahead = math.floor((at - now).total_seconds() / 60)
    return f"{text} (in {ahead} min)" if soon and 0 < ahead <= _SAY_AHEAD_MIN else text


def said_hhmm(
    hhmm: str, now: datetime, *, soon: bool = False, day: date | None = None,
) -> str:
    """A ``HH:MM`` of today, as :func:`said_clock` says it; on another ``day`` the date leads."""
    text = said_clock(_today(hhmm, "time", now, day), now, soon=soon)
    return text if day is None or day == now.date() else f"{day:%a %b} {day.day}, {text}"


def said_ms(ms: int, now: datetime, *, soon: bool = False) -> str:
    """An epoch ms, as :func:`said_clock` says it."""
    return said_clock(datetime.fromtimestamp(ms / 1000, _zone(now)), now, soon=soon)


def _row_trip(
    offers: TransitOffers, row: Mapping[str, Any], tid: str, now: datetime,
) -> departures.Trip:
    """The trip a listed row stands for, with the stop and drop-off positions Google gave."""
    first = str(row["route"]).split("→")[0].strip()
    return trip_of(
        tid=tid,
        route=row["route"],
        board_stop=row["board_stop"],
        leave=_today(row["leave_at"], "leave_at", now),
        departs=_today(row["departs"], "departs", now),
        arrive_at=row["arrive_at"],
        to=row["to"],
        stop=offers.stops.get((first, row["board_stop"])),
        now=now,
        alight=offers.alights.get((first, row["board_stop"])),
    )


def _pinned_already(
    held: departures.Departure | None, rows: list[dict[str, Any]], now: datetime,
) -> bool:
    """Whether the pin in force holds exactly these rows' buses (same route and stop, same bus).

    A pinned bus's time moves with the live feed, so the bus counts as the same within five
    minutes of the listed one.
    """
    if held is None or len(held.trips) != len(rows):
        return False
    return all(
        any(
            t.route == r["route"] and t.board_stop == r["board_stop"]
            and abs(t.departs_at_ms - _today(r["departs"], "departs", now).timestamp() * 1000)
            <= _SAME_BUS_MS
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
) -> tuple[list[departures.Trip], bool]:
    """Pin options ``numbers`` (from 1) of the last list as a new set; a newer set replaces.

    Returns the pinned trips and whether this call pinned them: ``False`` when the pin in force
    already holds exactly these buses, which is left as it is (no second ring). ToolError
    ``not_found`` for a number the list has not got, ``time_in_past`` for a leave time gone.
    """
    rows = [offers.listed_row(n) for n in numbers]
    held = departures.active(conn, int(now.timestamp() * 1000))
    if held is not None and _pinned_already(held, rows, now):
        return held.trips, False
    trips = []
    for n, row in zip(numbers, rows, strict=True):
        try:
            trips.append(_row_trip(offers, row, departures.new_tid(), now))
        except ToolError as exc:
            if exc.code != "time_in_past":
                raise
            msg = (
                f"Option {n} (leave {said_hhmm(row['leave_at'], now)}) is already past; ask for the"
                " times again with bus(to=...)"
            )
            raise ToolError(msg, code="time_in_past") from exc
    pin_trips(
        conn, trips, offer_id=None, action_id=action_id, now=now, source_event_id=source_event_id,
    )
    return trips, True


__all__ = [
    "TransitOffers",
    "add_trip",
    "build_planner",
    "live_card",
    "pin_listed",
    "pin_offered",
    "pin_trips",
    "ring",
    "said_clock",
    "said_hhmm",
    "said_ms",
    "show_card",
    "trip_of",
]
