"""ADR 0282, 0285: the conversation's one-call read of the weather at any place and day."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.location_tool import read_here, said_where
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereSource

DAYS_AHEAD: Final = 9
DAYS_BACK: Final = 7
HOURLY_MAX_DAYS: Final = 3
_DESCRIPTION: Final = (
    "Use it for any question about the weather outside: now, today, tonight, rain, wind,"
    " warnings, UV, sunrise and sunset or air quality, at his home or any place he names, for any"
    " day from 7 days back to 9 ahead. Not for days outside that range, climate or weather news"
    " (web_search), or for where he is (where_am_i). It returns a summary line, now, today in"
    " parts (what fell since midnight, the rest of the day, tonight), tomorrow or the days he"
    " asked for, hourly rows, the next hour's rain, official warnings and the place's own time"
    " zone."
)


@dataclass(frozen=True)
class WeatherRequest:
    """What one call asks of the lookup, checked: the place, the days in his words and the extras.

    ``place`` is a name or ``lat,lng``; with ``here`` (latitude, longitude, label) set it is where
    he is; with neither it is the home. ``near`` is roughly where he is, which picks the place
    called that nearest him. ``when`` is his words for the day or days (the lookup resolves them
    in the place's zone); None is now, today and tomorrow.
    """

    place: str | None = None
    here: tuple[float, float, str] | None = None
    when: str | None = None
    hourly: bool = False
    air_quality: bool = False
    near: tuple[float, float] | None = None


def build_weather_tool(
    lookup: Callable[[WeatherRequest], Mapping[str, Any]] | None, here: HereSource | None = None,
) -> tuple[Tool, ...]:
    """``weather`` over ``lookup``; none when the home has no location.

    ``here`` says the runtime has a Here (ADR 0322), which gives the tool its ``place: "here"``.
    """
    if lookup is None:
        return ()

    def weather(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        if any(args.get(name) is not None for name in ("start_date", "end_date")):
            msg = (
                "weather: start_date and end_date are gone; pass when in his words (tomorrow,"
                " Saturday, this weekend, 2026-10-25, 2026-10-25 to 2026-10-27)"
            )
            raise ToolError(msg, code="invalid_arguments")
        place = str(args.get("place") or "").strip() or None
        when = str(args.get("when") or "").strip() or None
        hourly = args.get("hourly") is True
        at = near = None
        said = ""
        if place is not None and place.casefold() == "here":
            fix = read_here(ctx)
            at = (fix.lat, fix.lng, fix.place or "where he is now")
            said, place = said_where(fix), None
        elif place is not None and ctx.here is not None and (rough := ctx.here.coarse()):
            near = (rough.lat, rough.lng)
        request = WeatherRequest(
            place, at, when, hourly, args.get("air_quality") is True, near,
        )
        try:
            result = dict(lookup(request))
        except ToolError:
            raise
        except Exception as exc:  # any other failure: say so.
            msg = f"weather is unavailable: the forecast could not be read ({type(exc).__name__})"
            raise ToolError(msg, code="network_error", kind="dont_retry") from exc
        return {**result, "location_from": said} if said else result

    properties: dict[str, Any] = {
        "place": {
            "type": "string",
            "description": (
                "The place in his words (a city, an area, a landmark, Vancouver, BC); leave out"
                " for his home. Another place of the same name: the use_place the result gave."
                + (' "here" for where he is now.' if here is not None else "")
            ),
        },
        "when": {
            "type": "string",
            "description": (
                "The day or days in his words: today, tonight, tomorrow, Saturday, this weekend,"
                " next Friday, the 25th, 2026-10-25, or two joined by to; from 7 days back to 9"
                " ahead. Leave out for now, today and tomorrow."
            ),
        },
        "hourly": {
            "type": "boolean",
            "description": (
                "Hour-by-hour rows for the days asked, today or later, up to 3 days; only when"
                " he wants them."
            ),
        },
        "air_quality": {
            "type": "boolean",
            "description": "Also US AQI, PM2.5 and PM10 for the days asked; only when he asks.",
        },
    }
    return (
        Tool(
            name="weather",
            description=_DESCRIPTION,
            input_schema={"type": "object", "properties": properties, "required": []},
            handler=weather,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["DAYS_AHEAD", "DAYS_BACK", "HOURLY_MAX_DAYS", "WeatherRequest", "build_weather_tool"]
