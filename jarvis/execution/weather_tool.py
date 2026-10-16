"""ADR 0279: the conversation's one-call read of the weather at any place and day."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.location_tool import read_here, said_where
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

DAYS_BACK: Final = 7
DAYS_AHEAD: Final = 16
HOURLY_MAX_DAYS: Final = 3
_DESCRIPTION: Final = (
    "The weather at any place for any day from 7 days back to 16 days ahead, in one call:"
    " current conditions (temperature, feels-like, wind, humidity, rain), and for each day the"
    " high and low, conditions, chance and amount of rain or snow, wind, UV index and"
    " sunrise/sunset. Use it for every weather question: now, today, tonight, tomorrow, the"
    " weekend or a later day, at home or anywhere else. Leave place out for the user's home."
    " Otherwise pass the place exactly as he names it (a city, area or landmark, in his own"
    " language, without extra words), and pass it again whenever he corrects or changes the"
    " place of an earlier answer: never state one place's weather from another place's result."
    "{here} Dates are YYYY-MM-DD in the place's own calendar: start_date, and end_date for a"
    " range (a day alone needs only start_date). With no dates you get now, today, tomorrow and"
    " the next 12 hours. Set hourly for hour-by-hour rows of the days asked (up to 3 days) and"
    " air_quality for US AQI, PM2.5 and PM10. If other places share the name, the result lists"
    " them: mention it when it matters, and pass the coordinates given as place to read one of"
    " them. Times are the place's local time. web_search is only for dates beyond this range or"
    " for alerts and news. If it returns an error, say what failed, then use web_search or tell"
    " him it is unavailable."
)
_HERE: Final = (
    ' Pass place "here" for where he is right now (his location is read for it, and the result'
    " says whose report it was)."
)


@dataclass(frozen=True)
class WeatherRequest:
    """What one call asks of the lookup, checked: the place, the days and the extras.

    ``place`` is a name or ``lat,lng``; with ``here`` (latitude, longitude, label) set it is where
    he is; with neither it is the home. ``start`` and ``end`` are None for the default
    (now, today and tomorrow).
    """

    place: str | None = None
    here: tuple[float, float, str] | None = None
    start: date | None = None
    end: date | None = None
    hourly: bool = False
    air_quality: bool = False


def _day(args: Mapping[str, Any], key: str) -> date | None:
    value = args.get(key)
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        msg = f"weather: {key} must be a date as YYYY-MM-DD (got {value!r})"
        raise ToolError(msg, code="invalid_argument") from exc


def _dates(args: Mapping[str, Any], *, hourly: bool) -> tuple[date | None, date | None]:
    start, end = _day(args, "start_date"), _day(args, "end_date")
    if start is None:
        if end is not None:
            msg = "weather: end_date needs a start_date"
            raise ToolError(msg, code="invalid_argument")
        return None, None
    end = end or start
    # ponytail: judged against the UTC date, one day of grace each way, because the place's own
    # zone is not known before the request; Open-Meteo refuses what is still out of range.
    today = datetime.now(UTC).date()
    low, high = today - timedelta(days=DAYS_BACK + 1), today + timedelta(days=DAYS_AHEAD + 1)
    if end < start or start < low or end > high:
        msg = (
            f"weather: dates must run from {DAYS_BACK} days back to {DAYS_AHEAD} days ahead"
            f" ({(today - timedelta(days=DAYS_BACK)).isoformat()} to"
            f" {(today + timedelta(days=DAYS_AHEAD)).isoformat()}) with end_date not before"
            " start_date; use web_search beyond that"
        )
        raise ToolError(msg, code="invalid_argument")
    if hourly and (end - start).days >= HOURLY_MAX_DAYS:
        msg = f"weather: hourly rows are for at most {HOURLY_MAX_DAYS} days; ask for fewer days"
        raise ToolError(msg, code="invalid_argument")
    return start, end


def build_weather_tool(
    lookup: Callable[[WeatherRequest], Mapping[str, Any]] | None,
    mac: Callable[[], Mapping[str, Any]] | None = None,
    phone: Callable[[], Mapping[str, Any]] | None = None,
) -> tuple[Tool, ...]:
    """``weather`` over ``lookup``; none when the home has no location.

    ``mac`` and ``phone`` are the location readers ``where_am_i`` has (ADR 0194, 0198); either
    one gives the tool its ``place: "here"``.
    """
    if lookup is None:
        return ()

    def weather(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        place = str(args.get("place") or "").strip() or None
        hourly = args.get("hourly") is True
        start, end = _dates(args, hourly=hourly)
        here = None
        said = ""
        if place is not None and place.casefold() == "here":
            fix = read_here(mac, phone, ctx)
            here = (fix["lat"], fix["lng"], fix["place"] or "where he is now")
            said, place = said_where(fix), None
        request = WeatherRequest(
            place, here, start, end, hourly,
            args.get("air_quality") is True,
        )
        try:
            result = dict(lookup(request))
        except ToolError:
            raise
        except Exception as exc:  # any other failure: say so, and offer the search.
            msg = (
                f"weather: the forecast could not be read ({type(exc).__name__}); "
                "use web_search, or tell him it is unavailable"
            )
            raise ToolError(msg, code="network_error") from exc
        return {**result, "location_from": said} if said else result

    properties: dict[str, Any] = {
        "place": {
            "type": "string",
            "description": (
                "The place as he names it; leave out for his home."
                + (' "here" for where he is now.' if mac is not None or phone is not None else "")
            ),
        },
        "start_date": {"type": "string", "description": "First day, YYYY-MM-DD."},
        "end_date": {"type": "string", "description": "Last day, YYYY-MM-DD; default start_date."},
        "hourly": {"type": "boolean", "description": "Hour-by-hour rows for the days asked."},
        "air_quality": {"type": "boolean", "description": "Also US AQI, PM2.5 and PM10."},
    }
    return (
        Tool(
            name="weather",
            description=_DESCRIPTION.format(
                here=_HERE if mac is not None or phone is not None else ""
            ),
            input_schema={"type": "object", "properties": properties, "required": []},
            handler=weather,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["DAYS_AHEAD", "DAYS_BACK", "WeatherRequest", "build_weather_tool"]
