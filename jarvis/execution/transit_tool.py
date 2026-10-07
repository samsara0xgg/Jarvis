"""ADR 0189: the conversation's one-call read of the bus times, from Google Routes."""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Mapping

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

_DESCRIPTION: Final = (
    "Bus and transit times from one place to another, from now or from a clock time today:"
    " when to leave, the walk to the first stop, each bus (route number, direction, board"
    " stop, departure time, alight stop), arrival time and total minutes, for up to 3"
    " options. Use it for any question about the bus, transit, 'when's the next bus', 'how"
    " do I get to X by bus' or 'when should I leave'. Pass the word 'home' for the user's own"
    " home ('my bus home', going home, in any language) and the word 'school' for the campus"
    " (going to school or class, leaving class or school, 'from campus', in any language);"
    " leaving school to go home is origin 'school', destination"
    " 'home'. Otherwise pass the place as an address or name, e.g. 'Mayfair Mall'. depart_at"
    " is a local time today as HH:MM; leave it out for now. If it returns an error, say so"
    " and do not guess times."
)


def _waypoint(place: str, saved: Mapping[str, str]) -> dict[str, Any]:
    word = place.strip().lower()
    text = saved.get(word, "") if word in ("home", "school") else place.strip()
    if not text:
        msg = f"the {word} address isn't saved yet"
        raise ToolError(msg, code="not_configured")
    if match := _LAT_LNG.match(text):
        return {"location": {"latLng": {"latitude": float(match[1]), "longitude": float(match[2])}}}
    return {"address": text}


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


def build_transit_tool(api_key: str | None, places: Mapping[str, str]) -> tuple[Tool, ...]:
    """``transit`` over Google Routes; none without a key.

    ``places`` maps ``home`` and ``school`` to an address or ``lat,lng``; a missing one makes
    that word a tool error rather than a guess.
    """
    if not api_key:
        return ()

    def transit(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        origin, destination = str(args.get("origin", "")), str(args.get("destination", ""))
        if not origin.strip() or not destination.strip():
            msg = "origin and destination are both required"
            raise ToolError(msg, code="invalid_argument")
        body: dict[str, Any] = {
            "origin": _waypoint(origin, places),
            "destination": _waypoint(destination, places),
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
        return {"from": origin.strip(), "to": destination.strip(), "options": options}

    return (
        Tool(
            name="transit",
            description=_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "'home', 'school', or a place"},
                    "destination": {
                        "type": "string",
                        "description": "'home', 'school', or a place",
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
    )


__all__ = ["build_transit_tool"]
