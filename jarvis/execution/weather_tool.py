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
    "The weather from Apple Weather at any place, from 7 days back to 9 days ahead, in one call:"
    " one call answers the whole question, so do not call it again for the same place."
    " Use it for every weather question: now, today, tonight, tomorrow, a weekday or the weekend,"
    " a day gone by, rain, wind, UV, sunrise and sunset, warnings, at home or anywhere else. The"
    " result has `now` (temperature, feels-like, wind, humidity, rain rate); `today` and"
    " `tomorrow`, or `days` when dates were asked (high and low, conditions, chance and amount"
    " of rain or snow, wind, UV index, sunrise and sunset); `next_hours`, the next 12 hours, or"
    " `hourly`;"
    " `next_hour`, minute-by-minute rain for the next 60 minutes (is it dry or raining now, when"
    ' rain starts_at or stops_at, peak intensity: use it for "will it rain soon", "when does it'
    ' start or stop raining", "do I need an umbrella now"); `alerts`, the official warnings in'
    " force or coming (headline, severity, area, effective, expires, source; [] means none: use"
    " it for any question about warnings, storms, heat or snowfall alerts); and `attribution`."
    " Leave place out for the user's home."
    " Otherwise pass the place exactly as he names it (a city, area or landmark, in his own"
    " language, without extra words), and pass it again whenever he corrects or changes the"
    " place of an earlier answer: never state one place's weather from another place's result."
    "{here} With no `when` you get now, today, tomorrow and the next 12 hours: that answers"
    " now, today, tonight and tomorrow. For any other day or days, past or later, pass `when` in"
    " his words (tomorrow, Saturday, this weekend, next Friday, the 25th, 2026-10-25, two of"
    " these joined by to): the tool works out the dates in the place's own calendar. A past day"
    " gives its daily figures only: no now, hours or alerts. Set hourly only when he wants"
    " hour-by-hour rows for those days (today or later, up to 3 days), and air_quality for US"
    " AQI, PM2.5 and PM10. Days from 7 back to 9 ahead are all one call; earlier or later ones"
    " are not available: say so, or use web_search. If other places share the name, the result"
    " lists them: mention it when it matters, and pass the coordinates given as place to read one"
    " of them. Times are the place's local time; the weather is Apple Weather, which you may name."
    " If it returns an error, say what failed, then use web_search or tell him it is"
    " unavailable."
)
_HERE: Final = (
    ' Pass place "here" for where he is right now (his location is read for it, and the result'
    " says whose report it was)."
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
            msg = (
                f"weather: the forecast could not be read ({type(exc).__name__}); tell him it is"
                " unavailable"
            )
            raise ToolError(msg, code="network_error") from exc
        return {**result, "location_from": said} if said else result

    properties: dict[str, Any] = {
        "place": {
            "type": "string",
            "description": (
                "The place as he names it; leave out for his home."
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
            "description": "Hour-by-hour rows for the days asked; only when he wants them.",
        },
        "air_quality": {"type": "boolean", "description": "Also US AQI, PM2.5 and PM10."},
    }
    return (
        Tool(
            name="weather",
            description=_DESCRIPTION.format(
                here=_HERE if here is not None else ""
            ),
            input_schema={"type": "object", "properties": properties, "required": []},
            handler=weather,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["DAYS_AHEAD", "DAYS_BACK", "HOURLY_MAX_DAYS", "WeatherRequest", "build_weather_tool"]
