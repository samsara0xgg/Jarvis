"""ADR 0189: the conversation's one-call read of the bus times, from Google Routes."""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.location_tool import PHONE_REPORT_RULE, phone_report, read_here
from jarvis.execution.tools import Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal, lang
from jarvis.state import departures, reminders

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_URL: Final = "https://routes.googleapis.com/directions/v2:computeRoutes"
_TIMEOUT_S: Final = 5.0
_ZONE: Final = ZoneInfo("America/Vancouver")
_MAX_OPTIONS: Final = 3
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
_LAT_LNG = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

_MAC_HERE: Final = "his location, read from this Mac, which he carries."
_PHONE_HERE: Final = (
    "his location from his phone's last report, with its age and accuracy in the result."
    " " + PHONE_REPORT_RULE
)


def _description(*, phone: bool) -> str:
    """The tool's description; ``phone``: on a brain, ``here`` is the phone's last report."""
    return (
        "Bus and transit times from one place to another, from now or from a clock time today:"
        " when to leave, the walk to the first stop, each bus (route number, direction, board"
        " stop, departure time, alight stop), arrival time and total minutes, for up to 3"
        " options. Use it for any question about the bus, transit, 'when's the next bus', 'how"
        " do I get to X by bus' or 'when should I leave'. The origin is always 'here', his real"
        f" location: {_PHONE_HERE if phone else _MAC_HERE}"
        " Use another origin only when he says outright that he starts somewhere else ('from"
        " Mayfair to ...'); never guess his start from the time of day or his routine. The"
        " destination is the word 'home' for his own home ('my bus home', going home, in any"
        " language), the word 'school' for the campus (going to school or class, in any"
        " language), or a place as an address or name, e.g. 'Mayfair Mall'. depart_at is a"
        " local time today as HH:MM, only when he names a later time; leave it out for now. If"
        " it returns an error, say so and do not guess times. To pin a trip on the notch when he"
        " asks, call pin_departure."
    )


def _here(
    reader: Callable[[], Mapping[str, Any]] | None, *, phone: bool,
) -> tuple[dict[str, Any], str]:
    """The user's location as a waypoint, and how to say it in the result."""
    fix = read_here(reader, phone=phone)
    near = f"{fix['place']}, " if fix["place"] else ""
    where = phone_report(fix) if phone else "this Mac"
    label = f"here ({where}, {near}±{round(fix['accuracy_m'])} m)"
    latlng = {"latitude": fix["lat"], "longitude": fix["lng"]}
    return {"location": {"latLng": latlng}}, label


def _waypoint(
    place: str,
    saved: Mapping[str, str],
    reader: Callable[[], Mapping[str, Any]] | None,
    *,
    phone: bool,
) -> tuple[dict[str, Any], str]:
    word = place.strip().lower()
    if word == "here":
        return _here(reader, phone=phone)
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


def _compute(api_key: str, body: Mapping[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 — a fixed https URL.
        _URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": _FIELD_MASK,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as reply:  # noqa: S310
        answer: dict[str, Any] = json.load(reply)
    return answer


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
    " leave, and ring him at leave time. Call it ONLY when he asks to pin it or put it on the"
    " notch ('pin it', 'put it on the notch', or the same in any other language); never on"
    " your own. Copy the fields from the transit option he is taking (the first option unless"
    " he picked another): leave_at; route, the number of the first bus ('28', or '28 → 12' for"
    " two buses); board_stop and departs of the first bus; arrive_at. destination is the word"
    " you gave transit. A newer pin replaces the older one. If it returns an error because"
    " leave_at is past, say so and offer to look the bus up again."
)


def _pin_departure(
    stops: Mapping[tuple[str, str], tuple[float, float]], args: Mapping[str, Any], ctx: ToolContext,
) -> dict[str, Any]:
    """Pin the trip on the notch and schedule its ring (ADR 0200)."""
    leave = _today(args.get("leave_at"), "leave_at")
    departs = _today(args.get("departs"), "departs")
    arrive = _today(args.get("arrive_at"), "arrive_at").strftime("%H:%M")
    route = str(args.get("route", "")).strip()
    board_stop = str(args.get("board_stop", "")).strip()
    if not route or not board_stop:
        msg = "route and board_stop are both required"
        raise ToolError(msg, code="invalid_argument")
    if leave < _now() - _PIN_PAST_GRACE:
        msg = f"leave_at {leave:%H:%M} has already passed; look the bus up again"
        raise ToolError(msg, code="time_in_past")
    if departs < leave:
        msg = "departs is before leave_at; copy both from the same option"
        raise ToolError(msg, code="invalid_argument")
    source = _get_running_event_uid(ctx.conn, ctx.action_id)
    # One pin at a time: the older pin's reminder must not ring for a dropped trip.
    older = departures.current(ctx.conn)
    ringing = reminders.fold(ctx.conn).get(older.reminder_id) if older else None
    if ringing and ringing.pending:
        reminders.cancel(
            ctx.conn, ringing.reminder_id, action_id=ctx.action_id, source_event_id=source,
        )
    first = route.split("→")[0].strip()
    reminder_id = reminders.schedule(
        ctx.conn,
        due=leave,
        text=lang.t("departure.go", route=first, departs=f"{departs:%H:%M}", stop=board_stop),
        action_id=ctx.action_id,
        source_event_id=source,
    )
    pin_id = departures.pin(
        ctx.conn,
        reminder_id=reminder_id,
        route=route,
        board_stop=board_stop,
        leave_at_ms=int(leave.timestamp() * 1000),
        departs_at_ms=int(departs.timestamp() * 1000),
        arrive_at=arrive,
        stop=stops.get((first, board_stop)),
        to=str(args.get("destination", "")).strip().lower()[:60],
        action_id=ctx.action_id,
        source_event_id=source,
    )
    return {
        "pin_id": pin_id,
        "pinned": True,
        "leave_at": f"{leave:%H:%M}",
        "departs": f"{departs:%H:%M}",
        "route": route,
    }


def build_transit_tool(
    api_key: str | None,
    places: Mapping[str, str],
    here: Callable[[], Mapping[str, Any]] | None = None,
    *,
    phone: bool = False,
) -> tuple[Tool, ...]:
    """``transit`` over Google Routes; none without a key.

    ``places`` maps ``home`` and ``school`` to an address or ``lat,lng``; a missing one makes
    that word a tool error rather than a guess. ``here`` reads this Mac's location when asked
    (ADR 0194), or with ``phone`` the phone's last report (ADR 0198), which the description and
    the result then say, with its age; its failure is a tool error telling the model to ask
    where the user is.
    """
    if not api_key:
        return ()
    # The first stop's place for each option last shown: the notch's live refresh finds the stop
    # in BC Transit's data by position, since the two feeds spell stop names differently.
    stops: dict[tuple[str, str], tuple[float, float]] = {}

    def transit(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        origin, destination = str(args.get("origin", "")), str(args.get("destination", ""))
        if not origin.strip() or not destination.strip():
            msg = "origin and destination are both required"
            raise ToolError(msg, code="invalid_argument")
        start, start_label = _waypoint(origin, places, here, phone=phone)
        end, end_label = _waypoint(destination, places, here, phone=phone)
        body: dict[str, Any] = {
            "origin": start,
            "destination": end,
            "travelMode": "TRANSIT",
            "computeAlternativeRoutes": True,
        }
        if args.get("depart_at"):
            body["departureTime"] = _departure(str(args["depart_at"]))
        try:
            answer = _compute(api_key, body)
        except Exception as exc:  # timeout, HTTP error, bad JSON: all one short reason.
            msg = f"transit unavailable ({type(exc).__name__})"
            raise ToolError(msg, code="network_error") from exc
        options = [o for r in answer.get("routes", ()) if (o := _option(r))][:_MAX_OPTIONS]
        if not options:
            msg = "no transit route found for that trip at that time"
            raise ToolError(msg, code="not_found")
        stops.clear()
        for found in map(_first_board, answer.get("routes", ())):
            if found:
                stops[found[0]] = found[1]
        return {"from": start_label, "to": end_label, "options": options}


    return (
        Tool(
            name="transit",
            description=_description(phone=phone),
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
                "properties": {
                    "leave_at": {"type": "string", "description": "HH:MM, from the option"},
                    "route": {"type": "string", "description": "'28', or '28 → 12'"},
                    "board_stop": {"type": "string", "description": "first bus's stop"},
                    "departs": {"type": "string", "description": "first bus, HH:MM"},
                    "arrive_at": {"type": "string", "description": "HH:MM, from the option"},
                    "destination": {"type": "string", "description": "as given to transit"},
                },
                "required": ["leave_at", "route", "board_stop", "departs", "arrive_at"],
            },
            handler=lambda args, ctx: _pin_departure(stops, args, ctx),
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["build_transit_tool"]
