"""ADR 0189: the conversation's one-call read of the bus times, from Google Routes."""

from __future__ import annotations

import json
import math
import re
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.location_tool import PHONE_REPORT_RULE, read_here, said_where
from jarvis.execution.tools import Tool, ToolError, _get_running_event_uid, _turn_of
from jarvis.shared import CallerPrincipal, lang
from jarvis.state import departures, reminders
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_URL: Final = "https://routes.googleapis.com/directions/v2:computeRoutes"
_TIMEOUT_S: Final = 5.0
_ZONE: Final = ZoneInfo("America/Vancouver")
_MAX_OPTIONS: Final = 3
# A far-fetched option (ADR 0203): it arrives this long after the earliest, or rides this much
# longer.
_LATER_MIN: Final = 15
_SLOWER: Final = 1.5
# `route` asks Google from this long before now: a bus whose leave time (departure minus Google's
# walking estimate) just passed is still returned, and he can walk faster than Google assumes.
_ROUTE_LOOKBACK_MIN: Final = 10
_LIVE_TIMEOUT_S: Final = 4.0
# spare_min: at or above this he has room (easy); at or below its negative the bus is gone.
_EASY_SPARE_MIN: Final = 2
_SCHOOL_NAMES: Final = frozenset({"uvic", "u vic", "university of victoria"})
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
)
# DRIVE and WALK need only how long and how far; the default routing preference is the cheap one.
_MODE_MASK: Final = "routes.duration,routes.distanceMeters"
_MODES: Final = (("drive", "DRIVE"), ("walk", "WALK"))
_POSTAL = re.compile(r"\s*[A-Z]\d[A-Z]\s?\d[A-Z]\d\s*$")
_LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

_MAC_HERE: Final = "his location, read from this Mac, which he carries."
_PHONE_HERE: Final = (
    "his location from his phone's last report, with its age and accuracy in the result."
    " " + PHONE_REPORT_RULE
)
_BOTH_HERE: Final = (
    "his location: from his phone's last report when he is talking from his phone, with its age"
    " and accuracy in the result (" + PHONE_REPORT_RULE + "), otherwise read from this Mac,"
    " which he carries."
)


def _description(here: str) -> str:
    """The tool's description; ``here`` says whose location 'here' is (ADR 0194, 0198, 0212)."""
    return (
        "Bus and transit times from one place to another, from now or from a clock time today:"
        " when to leave, the walk to the first stop, each bus (route number, direction, board"
        " stop, departure time, alight stop), arrival time and total minutes, for up to 3"
        " options. Use it for any question about the bus, transit, 'when's the next bus', 'how"
        " do I get to X by bus' or 'when should I leave'. The origin is always 'here', his real"
        f" location: {here}"
        " Use another origin only when he says outright that he starts somewhere else ('from"
        " Mayfair to ...'); never guess his start from the time of day or his routine. The"
        " destination is the word 'home' for his own home ('my bus home', going home, in any"
        " language), the word 'school' for the campus (going to school or class, in any"
        " language; 'UVic' is the campus too), or a place as an address or name, e.g. 'Mayfair"
        " Mall'. depart_at is a"
        " local time today as HH:MM, only when he names a later time; leave it out for now. Each"
        " call asks Google again and may come back with a different set of options; the options"
        " already on the card stay valid until their leave time. When he names a bus number"
        " ('can I still catch the 39?'), pass it as route and answer about THAT bus first, from"
        " named_route: catchable or not, how tight (verdict: easy has room; tight means he must"
        " walk fast; missed means it is gone; none means Google has no such bus then), and the"
        " next one (next) if it is missed. Speech recognition may mishear the number or the word"
        " after it (route 39 heard as '39 degrees', or as nonsense words before a 9): use the"
        " number. One call with route answers it: never call"
        " transit again with shifting depart_at to probe, and never invent a target arrival"
        " time he did not ask for. Say a bus can or cannot be caught only when this turn's"
        " result shows that bus; do not infer it from the bus being absent."
        " spare_min on each option is how many minutes he has beyond a normal walk to"
        " the first stop (negative: late). If it returns an error, say only that"
        " this lookup failed, do not guess times, and do not contradict an earlier result."
        " `modes` gives the driving and"
        " walking minutes and km for the same trip when Google answered them; mention an"
        " alternative in a few words only when it matters (walking under about 20 minutes, or"
        " driving much faster), never read them all out. Do not ask whether to pin the trip: a"
        " card in the chat already lists the buses with a button each. Call pin_departure only"
        " when he asks for it by voice ('pin it', 'put it on the notch')."
    )


def _here(
    mac: Callable[[], Mapping[str, Any]] | None,
    phone: Callable[[], Mapping[str, Any]] | None,
    ctx: ToolContext,
) -> tuple[dict[str, Any], str]:
    """The user's location as a waypoint, and how to say it in the result."""
    fix = read_here(mac, phone, ctx)
    near = f"{fix['place']}, " if fix["place"] else ""
    label = f"here ({said_where(fix)}, {near}±{round(fix['accuracy_m'])} m)"
    latlng = {"latitude": fix["lat"], "longitude": fix["lng"]}
    return {"location": {"latLng": latlng}}, label


def _waypoint(
    place: str,
    saved: Mapping[str, str],
    mac: Callable[[], Mapping[str, Any]] | None,
    phone: Callable[[], Mapping[str, Any]] | None,
    ctx: ToolContext,
) -> tuple[dict[str, Any], str]:
    word = " ".join(place.lower().split())
    if word in _SCHOOL_NAMES:
        word = "school"
    if word == "here":
        return _here(mac, phone, ctx)
    text = saved.get(word, "") if word in ("home", "school") else place.strip()
    if not text:
        msg = f"the {word} address isn't saved yet"
        raise ToolError(msg, code="not_configured")
    if match := _LAT_LNG.match(text):
        latlng = {"latitude": float(match[1]), "longitude": float(match[2])}
        return {"location": {"latLng": latlng}}, place.strip()
    return {"address": text}, place.strip()


def _departure(depart_at: str) -> str:
    try:
        at = time.fromisoformat(depart_at.strip())
    except ValueError as exc:
        msg = f"depart_at must be a local time today as HH:MM, not {depart_at!r}"
        raise ToolError(msg, code="invalid_argument") from exc
    when = datetime.combine(_now().date(), at.replace(second=0, microsecond=0), tzinfo=_ZONE)
    return _utc(when)


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(_ZONE)


def _clock(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).astimezone(_ZONE)


def _seconds(step: Mapping[str, Any]) -> float:
    return float(str(step.get("staticDuration", "0s")).removesuffix("s"))


def _option(route: Mapping[str, Any]) -> dict[str, Any] | None:
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
                "departs": _clock(stops["departureTime"]).strftime("%H:%M"),
                "alight_stop": stops["arrivalStop"]["name"],
                "arrives": _clock(stops["arrivalTime"]).strftime("%H:%M"),
            }
        )
    leave = _clock(rides[0]["transitDetails"]["stopDetails"]["departureTime"]) - walk_to
    arrive = _clock(rides[-1]["transitDetails"]["stopDetails"]["arrivalTime"]) + walk_from
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


def _pairs(answer: Mapping[str, Any], now: datetime) -> list[tuple[dict[str, Any], Any]]:
    """Each bus route of Google's answer as ``(option with spare_min, Google's route)``."""
    pairs = [(o, r) for r in answer.get("routes", ()) if (o := _option(r))]
    for o, _ in pairs:
        at = _today(o["legs"][0]["departs"], "departs")
        o["spare_min"] = _spare(at, o["walk_to_first_stop_min"], now)
    return pairs


def _ahead(
    pairs: list[tuple[dict[str, Any], Any]], wanted: str, now: datetime,
) -> list[tuple[dict[str, Any], Any]]:
    """The routes whose first bus is ``wanted`` and not gone yet, earliest first."""
    floor = now.replace(second=0, microsecond=0)
    mine = [
        p for p in pairs
        if p[0]["legs"][0]["route"].casefold() == wanted.casefold()
        and _today(p[0]["legs"][0]["departs"], "departs") >= floor
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
) -> dict[str, Any]:
    """The answer to 'can I still catch bus ``wanted``': how tight, and the next one."""
    if not hits:
        return {"route": wanted, "verdict": "none", "note": f"no route {wanted} bus found then"}
    option, raw = hits[0]
    leg = option["legs"][0]
    at, fresh = _today(leg["departs"], "departs"), False
    stop = _first_board(raw)
    if live is not None and stop is not None:
        ms = _live_ms(live, leg["route"], stop[1], int(at.timestamp() * 1000))
        if ms:
            at, fresh = datetime.fromtimestamp(ms / 1000, _ZONE), True
    walk = option["walk_to_first_stop_min"]
    spare = _spare(at, walk, now)
    verdict = (
        "easy" if spare >= _EASY_SPARE_MIN else "missed" if spare <= -_EASY_SPARE_MIN else "tight"
    )
    named: dict[str, Any] = {
        "route": leg["route"],
        "board_stop": leg["board_stop"],
        "departs": at.strftime("%H:%M"),
        "walk_min": walk,
        "spare_min": spare,
        "verdict": verdict,
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
    args: Mapping[str, Any],
    wanted: str,
    now: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[dict[str, Any], Any]], dict[str, Any]]:
    """Ask Google and the other ways: ``(answer, options, hits of the named route, modes)``.

    A named route asks from ten minutes ago, so a bus whose leave time just passed still shows;
    if that finds no such bus, the usual 'now' answer is used. ToolError ``not_found`` when no
    bus route is left.
    """
    body: dict[str, Any] = {
        "origin": start,
        "destination": end,
        "travelMode": "TRANSIT",
        "computeAlternativeRoutes": True,
    }
    plain = dict(body)
    if args.get("depart_at"):
        body["departureTime"] = _departure(str(args["depart_at"]))
    elif wanted:
        ago = now.replace(second=0, microsecond=0) - timedelta(minutes=_ROUTE_LOOKBACK_MIN)
        body["departureTime"] = _utc(ago)
    shifted = "departureTime" in body and not args.get("depart_at")
    # The bus answer and, concurrently, the other ways there (ADR 0205): one request each.
    with ThreadPoolExecutor(max_workers=1 + len(_MODES)) as pool:
        ways = {name: pool.submit(_mode, api_key, start, end, travel) for name, travel in _MODES}
        try:
            answer = pool.submit(_compute, api_key, body).result()
        except Exception as exc:  # timeout, HTTP error, bad JSON: all one short reason.
            msg = f"transit unavailable ({type(exc).__name__})"
            raise ToolError(msg, code="network_error") from exc
        pairs = _pairs(answer, now)
        hits = _ahead(pairs, wanted, now) if wanted else []
        if wanted and shifted and not hits:
            try:
                later = pool.submit(_compute, api_key, plain).result()
            except Exception:  # noqa: BLE001 - the first answer stands.
                later = None
            if later is not None:
                answer, pairs = later, _pairs(later, now)
                hits = _ahead(pairs, wanted, now)
    modes = {name: found for name, way in ways.items() if (found := way.result())}
    # An earlier start returns buses he could not reach at a normal pace: only the named one stays.
    first = hits[0][0] if hits else None
    rest = [o for o, _ in pairs if o is not first and (not shifted or o["spare_min"] >= 0)]
    # The named bus is first however late or slow it is; it does not set the others' yardstick.
    options = ([first] if first else []) + _sensible(rest)
    if not options:
        msg = "no transit route found for that trip at that time"
        raise ToolError(msg, code="not_found")
    return answer, options[:_MAX_OPTIONS], hits, modes


def _compute(
    api_key: str, body: Mapping[str, Any], mask: str = _FIELD_MASK,
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
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as reply:  # noqa: S310
        answer: dict[str, Any] = json.load(reply)
    return answer


def _mode(api_key: str, start: Mapping[str, Any], end: Mapping[str, Any], travel: str) -> Any:  # noqa: ANN401
    """``{minutes, km}`` of the trip by ``travel`` (DRIVE or WALK); None when Google has none."""
    try:
        body = {"origin": start, "destination": end, "travelMode": travel}
        route = _compute(api_key, body, _MODE_MASK)["routes"][0]
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


def _today(value: object, field: str) -> datetime:
    """``HH:MM`` as a local time today, or a tool error naming the field."""
    try:
        at = time.fromisoformat(str(value).strip())
    except ValueError as exc:
        msg = f"{field} must be a local time today as HH:MM, not {value!r}"
        raise ToolError(msg, code="invalid_argument") from exc
    return datetime.combine(_now().date(), at.replace(second=0, microsecond=0), tzinfo=_ZONE)


_PIN_PAST_GRACE: Final = timedelta(minutes=1)
_PIN_DESCRIPTION: Final = (
    "Pin the bus trip he just got from transit on the notch, as a countdown to when he must"
    " leave, and ring him at leave time. Call it ONLY when he asks to pin it by voice ('pin it',"
    " 'put it on the notch', or the same in any other language); never on your own, and never to"
    " offer it: a card in the chat does that. It is already on your menu: no tool_search, and"
    " do not call transit again first; copy from the transit result already in the conversation."
    " Copy the fields from the transit option he is taking (the first option unless"
    " he picked another): leave_at; route, the number of the first bus ('28', or '28 → 12' for"
    " two buses); board_stop and departs of the first bus; arrive_at. destination is the word"
    " you gave transit. When he wants several ('pin both', up to 3), put one such object per"
    " option in options instead. A newer pin replaces the older. If it returns an error because"
    " leave_at is past, say so and offer to look the bus up again."
)


_TRIP_FIELDS: Final = {
    "leave_at": {"type": "string", "description": "HH:MM, from the option"},
    "route": {"type": "string", "description": "'28', or '28 → 12'"},
    "board_stop": {"type": "string", "description": "first bus's stop"},
    "departs": {"type": "string", "description": "first bus, HH:MM"},
    "arrive_at": {"type": "string", "description": "HH:MM, from the option"},
    "destination": {"type": "string", "description": "as given to transit"},
}
_TRIP_REQUIRED: Final = ["leave_at", "route", "board_stop", "departs", "arrive_at"]


class TransitOffers:
    """What the last ``transit`` answer offers, and the first stops it kept.

    The conversation card (ADR 0205) lists the options for as long as the card stays; a click pins
    one through :func:`pin_trip`, as the tool does. The newest answer replaces the older. The
    options stay in memory after the card is gone, for the pin card's "next bus" (same route and
    stop). ``stops`` is each option's first stop place, keyed ``(first route, board stop)``: the
    notch's live refresh finds the stop in BC Transit's data by position, as the two feeds spell
    names differently.
    """

    def __init__(self) -> None:
        """Start with no answer."""
        self.stops: dict[tuple[str, str], tuple[float, float]] = {}
        self._id = ""
        self._rows: list[dict[str, Any]] = []

    def put(self, options: list[dict[str, Any]], to: str) -> dict[str, Any]:
        """Take a fresh answer's options as the offer; ``to`` is the destination as asked.

        Returns the card's trip: ``{offer_id, to, options}``, one row per option.
        """
        self._id = "offer-" + uuid.uuid4().hex[:8]
        self._rows = [
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
        made_at = _now().isoformat(timespec="minutes")
        return {"offer_id": self._id, "to": to, "options": self._rows, "made_at": made_at}

    def row(self, offer_id: str, index: int) -> dict[str, Any]:
        """The offered option; LookupError when the offer is unknown or has no such row."""
        mine = [r for r in self._rows if r["index"] == index]
        if offer_id != self._id or not mine:
            msg = "that offer is gone"
            raise LookupError(msg)
        return mine[0]

    def later(
        self, route: str, board_stop: str, after: datetime,
    ) -> tuple[datetime, datetime, str] | None:
        """``(leave, departs, arrive_at)`` of the earliest option on that route and stop."""
        found = [
            (_today(r["departs"], ""), _today(r["leave_at"], ""), r["arrive_at"])
            for r in self._rows
            if r["route"].split("→")[0].strip() == route and r["board_stop"] == board_stop
        ]
        later = sorted(f for f in found if f[0] > after and f[1] >= _now() - _PIN_PAST_GRACE)
        return (later[0][1], later[0][0], later[0][2]) if later else None


def _leaves(made_at: datetime, row: Mapping[str, Any]) -> datetime:
    """A row's leave time: its ``HH:MM`` on the day the answer was made.

    The next day when that is more than an hour before the moment it was made.
    """
    at = datetime.combine(made_at.date(), time.fromisoformat(str(row["leave_at"])), _ZONE)
    return at + timedelta(days=1) if at < made_at - timedelta(hours=1) else at


def live_card(trip: Mapping[str, Any]) -> dict[str, Any] | None:
    """The card's trip as served now: no row whose leave time has passed; None if none is left.

    A row's ``HH:MM`` is on the day the answer was made (the next day when it is more than an hour
    before that moment), so a card does not come back after midnight. A card without ``made_at``
    is from before it was kept and is gone.
    """
    if not trip.get("made_at"):
        return None
    made_at = datetime.fromisoformat(str(trip["made_at"]))
    floor = _now().replace(second=0, microsecond=0)
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
) -> departures.Trip:
    """A trip to pin, checked: its leave time not past (a minute's grace), its bus after it."""
    if leave < _now() - _PIN_PAST_GRACE:
        msg = f"leave_at {leave:%H:%M} has already passed; look the bus up again"
        raise ToolError(msg, code="time_in_past")
    if departs < leave:
        msg = "departs is before leave_at; copy both from the same option"
        raise ToolError(msg, code="invalid_argument")
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
    )


def _hhmm(ms: int) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, _ZONE):%H:%M}"


def _now_ms() -> int:
    return int(_now().timestamp() * 1000)


def ring(  # noqa: PLR0913 — the older ring, the set, the clock and who is asking.
    conn: sqlite3.Connection,
    trips: list[departures.Trip],
    *,
    now_ms: int,
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
        "departure.go", route=target.first_route, departs=_hhmm(target.departs_at_ms),
        stop=target.board_stop,
    ) + "".join(
        lang.t("departure.also", route=t.first_route, departs=_hhmm(t.departs_at_ms))
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
        due=datetime.fromtimestamp(target.leave_at_ms / 1000, _ZONE),
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


def pin_trips(
    conn: sqlite3.Connection,
    trips: list[departures.Trip],
    *,
    offer_id: str | None,
    action_id: str,
    source_event_id: str | None = None,
) -> dict[str, Any]:
    """Pin these trips as a new set and schedule its ring (ADR 0200, 0204); a newer set replaces."""
    if not 1 <= len(trips) <= departures.MAX_TRIPS:
        msg = f"pin between 1 and {departures.MAX_TRIPS} trips"
        raise ToolError(msg, code="invalid_argument")
    # One pin at a time: the older pin's reminder must not ring for a dropped set.
    _cancel_ring(conn, departures.current(conn), action_id, source_event_id)
    reminder_id, ring_tid = ring(
        conn, trips, now_ms=_now_ms(), reminder_id=None, ring_tid=None, action_id=action_id,
        source_event_id=source_event_id,
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
        "leave_at": _hhmm(first.leave_at_ms),
        "departs": _hhmm(first.departs_at_ms),
        "route": first.route,
        "trips": [
            {"route": t.route, "leave_at": _hhmm(t.leave_at_ms), "departs": _hhmm(t.departs_at_ms)}
            for t in trips
        ],
    }


def add_trip(
    conn: sqlite3.Connection, pin: departures.Departure, trip: departures.Trip, *, action_id: str,
) -> None:
    """Add a trip to the set (once); ToolError ``full`` when it already holds three catchable."""
    if pin.trip(trip.tid):
        return
    now_ms = _now_ms()
    if len(pin.catchable(now_ms)) >= departures.MAX_TRIPS:
        msg = f"{departures.MAX_TRIPS} trips are already pinned"
        raise ToolError(msg, code="full")
    reminder_id, ring_tid = ring(
        conn, [*pin.trips, trip], now_ms=now_ms, reminder_id=pin.reminder_id,
        ring_tid=pin.ring_tid, action_id=action_id,
    )
    departures.add(conn, pin.pin_id, trip, reminder_id=reminder_id, ring=ring_tid)


def pin_offered(
    conn: sqlite3.Connection, offers: TransitOffers, offer_id: str, index: int, *, add: bool,
) -> dict[str, Any]:
    """The card's click: pin that option; ``add`` puts it on the set this offer already started.

    LookupError when the offer cannot be pinned; ``{"reason": "full"}`` when the set is full.
    """
    row = offers.row(offer_id, index)
    first = row["route"].split("→")[0].strip()
    try:
        trip = trip_of(
            tid=f"{offer_id}-{index}",
            route=row["route"],
            board_stop=row["board_stop"],
            leave=_today(row["leave_at"], "leave_at"),
            departs=_today(row["departs"], "departs"),
            arrive_at=row["arrive_at"],
            to=row["to"],
            stop=offers.stops.get((first, row["board_stop"])),
        )
        pin = departures.active(conn, _now_ms())
        if add and pin is not None and pin.offer_id == offer_id:
            add_trip(conn, pin, trip, action_id=offer_id)
            return {"pin_id": pin.pin_id}
        return pin_trips(conn, [trip], offer_id=offer_id, action_id=offer_id)
    except ToolError as exc:
        if exc.code == "full":
            return {"reason": "full"}
        raise LookupError(str(exc)) from exc


def _pin_departure(
    offers: TransitOffers, args: Mapping[str, Any], ctx: ToolContext,
) -> dict[str, Any]:
    """The ``pin_departure`` tool: the model's copied fields, pinned through :func:`pin_trips`."""
    several = args.get("options")
    trips = []
    for one in several if isinstance(several, list) and several else [args]:
        leave = _today(one.get("leave_at"), "leave_at")
        departs = _today(one.get("departs"), "departs")
        arrive = _today(one.get("arrive_at"), "arrive_at").strftime("%H:%M")
        route = str(one.get("route", "")).strip()
        board_stop = str(one.get("board_stop", "")).strip()
        if not route or not board_stop:
            msg = "route and board_stop are both required"
            raise ToolError(msg, code="invalid_argument")
        trips.append(
            trip_of(
                tid=departures.new_tid(),
                route=route,
                board_stop=board_stop,
                leave=leave,
                departs=departs,
                arrive_at=arrive,
                to=str(one.get("destination", "")).strip().lower()[:60],
                stop=offers.stops.get((route.split("→")[0].strip(), board_stop)),
            )
        )
    return pin_trips(
        ctx.conn,
        trips,
        offer_id=None,
        action_id=ctx.action_id,
        source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
    )


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


def build_transit_tool(  # noqa: PLR0913 - the key, places, two readers, offers, live.
    api_key: str | None,
    places: Mapping[str, str],
    here: Callable[[], Mapping[str, Any]] | None = None,
    *,
    phone_here: Callable[[], Mapping[str, Any]] | None = None,
    offers: TransitOffers | None = None,
    bus_live: Callable[[str, tuple[float, float], int], int | None] | None = None,
) -> tuple[Tool, ...]:
    """``transit`` over Google Routes; none without a key.

    ``places`` maps ``home`` and ``school`` to an address or ``lat,lng``; a missing one makes
    that word a tool error rather than a guess. ``here`` reads this Mac's location when asked
    (ADR 0194) and ``phone_here`` the phone's last report (ADR 0198), whose age the result then
    says; with both, a turn a phone opened gets the phone's (ADR 0212), and the description says
    so. A failed read is a tool error telling the model to ask where the user is. ``offers``
    is where each answer leaves its options for the chat card (ADR 0205); the runtime shares
    it with the routes that pin them. ``bus_live`` is BC Transit's live departure of a route at
    a stop near ``(lat, lng)`` around an epoch ms (the runtime wires it, as it does ``here``);
    when it answers, a named route's ``departs`` and ``spare_min`` use it.
    """
    if not api_key:
        return ()
    offers = offers if offers is not None else TransitOffers()

    def transit(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        origin, destination = str(args.get("origin", "")), str(args.get("destination", ""))
        if not origin.strip() or not destination.strip():
            msg = "origin and destination are both required"
            raise ToolError(msg, code="invalid_argument")
        start, start_label = _waypoint(origin, places, here, phone_here, ctx)
        end, end_label = _waypoint(destination, places, here, phone_here, ctx)
        now = _now()
        wanted = re.sub(r"[^0-9A-Za-z]", "", str(args.get("route") or ""))
        try:
            answer, options, hits, modes = _search(api_key, start, end, args, wanted, now)
        except ToolError as exc:
            retry = _with_city(end, places) if exc.code == "not_found" else None
            if retry is None:
                raise
            end = retry
            end_label = retry["address"]
            answer, options, hits, modes = _search(api_key, start, end, args, wanted, now)
        offers.stops.clear()
        for found in map(_first_board, answer.get("routes", ())):
            if found:
                offers.stops[found[0]] = found[1]
        show_card(ctx, {**offers.put(options, destination.strip().lower()[:60]), "modes": modes})
        extra: dict[str, Any] = {"modes": modes} if modes else {}
        if wanted:
            extra["named_route"] = _named_route(wanted, hits, now, bus_live)
        return {"from": start_label, "to": end_label, "options": options, **extra}

    return (
        Tool(
            name="transit",
            description=_description(
                _BOTH_HERE if here is not None and phone_here is not None
                else _PHONE_HERE if phone_here is not None
                else _MAC_HERE,
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "'here', 'home', 'school', or a place",
                    },
                    "destination": {
                        "type": "string",
                        "description": "'here', 'home', 'school', or a place",
                    },
                    "depart_at": {"type": "string", "description": "local time today, HH:MM"},
                    "route": {
                        "type": "string",
                        "description": "the bus number he names, e.g. '39' or '26A'; digits only"
                        " when speech recognition garbled the rest",
                    },
                },
                "required": ["origin", "destination"],
            },
            handler=transit,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
        Tool(
            name="pin_departure",
            description=_PIN_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {**_TRIP_FIELDS, "options": {
                    "type": "array",
                    "maxItems": departures.MAX_TRIPS,
                    "description": "several trips to pin at once, each with the fields above",
                    "items": {
                        "type": "object", "properties": _TRIP_FIELDS, "required": _TRIP_REQUIRED,
                    },
                }},
            },
            handler=lambda args, ctx: _pin_departure(offers, args, ctx),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = [
    "TransitOffers",
    "add_trip",
    "build_transit_tool",
    "live_card",
    "pin_offered",
    "pin_trips",
    "ring",
    "show_card",
    "trip_of",
]
