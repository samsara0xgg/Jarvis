"""ADR 0283: `map_search`, places near him or in a named area, read from Google Places (New).

One tool for the questions he used to open Google Maps for: what is near, is it open, how far, a
phone number, reviews. Each call is metered against the monthly cap (``maps_spend``), and the
places it returns are logged so the turn's answer can carry them to the phone's map.
"""

from __future__ import annotations

import calendar
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution import maps_spend, places_api
from jarvis.execution.location_tool import read_here, said_where
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_ZONE: Final = ZoneInfo("America/Vancouver")
_DEFAULT_RADIUS_M: Final = 2000
_MAX_RADIUS_M: Final = 50_000
_DEFAULT_LIMIT: Final = 5
_MAX_LIMIT: Final = 8
_REVIEWS: Final = 3
_REVIEW_CHARS: Final = 240
# A phone report older than this is not where he is now; the result says so (as `bus` does).
_OLD_FIX_S: Final = 30 * 60
_KEYS: Final = frozenset({"query", "kind", "area", "radius_m", "limit", "place_id"})

_DESCRIPTION: Final = (
    "Find places (food, shops, services, a named business) and say how far each is: address,"
    " distance and direction from him, open now, today's hours, rating, price level, phone,"
    " maps link and place_id. query= is what he wants ('ramen', 'Shoppers Drug Mart',"
    " 'pharmacy open late'); kind=<Google place type> (cafe, restaurant, pharmacy, atm,"
    " gas_station, supermarket) lists the nearest of a type. Both look near his current position"
    " (his phone's last report, which the result dates) unless area= names another area"
    " ('Fairfield, Victoria'). place_id= (from an earlier result) gives that one place in"
    " detail: weekly hours, review highlights, website. When a place name is vague or may be"
    " misheard, search first, name the specific match (name and street) and let him confirm"
    " before relying on it; never invent a place, an address or opening hours, and say so when"
    " nothing is found. Distance is in a straight line, not walking. For the bus to a place"
    " found here, pass its place_id to bus(place_id=...). Every call costs money against a"
    " monthly limit: do not repeat a search that already has an answer, and if the result says"
    " the limit is reached, tell him."
)
_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What he is looking for, as he said it."},
        "kind": {
            "type": "string",
            "description": "A Google place type, e.g. cafe, pharmacy, atm; the nearest of it."
            " Not with query.",
        },
        "area": {
            "type": "string",
            "description": "Search in this named area instead of near him, e.g. 'Fairfield,"
            " Victoria'.",
        },
        "radius_m": {
            "type": "integer",
            "description": f"How far from him to look, metres; default {_DEFAULT_RADIUS_M}.",
        },
        "limit": {
            "type": "integer",
            "description": f"How many places, default {_DEFAULT_LIMIT}, at most {_MAX_LIMIT}.",
        },
        "place_id": {
            "type": "string",
            "description": "A place_id from an earlier result: that place in detail. Alone.",
        },
    },
    "additionalProperties": False,
}


def _bad(message: str) -> ToolError:
    return ToolError(f"map_search: {message}", code="invalid_arguments")


def _count(value: object, name: str, low: int, high: int, default: int) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        msg = f"{name} must be a whole number"
        raise _bad(msg)
    return max(low, min(high, int(value)))


def _now() -> datetime:
    return datetime.now(_ZONE)


def _today_hours(raw: Mapping[str, Any]) -> str | None:
    """Today's line of Google's seven weekday lines ('Monday: 9:00 AM - 5:00 PM')."""
    lines = (raw.get("currentOpeningHours") or {}).get("weekdayDescriptions") or []
    day = calendar.day_name[_now().weekday()]
    return next((str(line) for line in lines if str(line).startswith(f"{day}:")), None)


def _shape(raw: Mapping[str, Any], origin: tuple[float, float] | None) -> dict[str, Any]:
    """One place as the model reads it; what Google did not give is left out, never guessed."""
    name = raw.get("displayName")
    out: dict[str, Any] = {
        "name": name.get("text") if isinstance(name, dict) else name,
        "address": raw.get("formattedAddress"),
    }
    where = raw.get("location")
    if origin is not None and isinstance(where, dict):
        metres, direction = places_api.distance_and_direction(
            origin, float(where["latitude"]), float(where["longitude"]),
        )
        out |= {"distance_m": metres, "direction": direction}
    hours = raw.get("currentOpeningHours") or {}
    out |= {
        "open_now": hours.get("openNow"),
        "hours_today": _today_hours(raw),
        "rating": raw.get("rating"),
        "rating_count": raw.get("userRatingCount"),
        "price_level": places_api.price_word(raw.get("priceLevel")),
        "phone": raw.get("nationalPhoneNumber"),
        "maps_url": raw.get("googleMapsUri"),
        "place_id": raw.get("id"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _detail(raw: Mapping[str, Any], origin: tuple[float, float] | None) -> dict[str, Any]:
    out = _shape(raw, origin)
    out["hours_week"] = list((raw.get("currentOpeningHours") or {}).get("weekdayDescriptions", []))
    if raw.get("websiteUri"):
        out["website"] = raw["websiteUri"]
    reviews = []
    for one in raw.get("reviews", [])[:_REVIEWS]:
        text = one.get("text")
        said = text.get("text") if isinstance(text, dict) else text
        reviews.append({
            "rating": one.get("rating"),
            "text": str(said or "")[:_REVIEW_CHARS],
            "when": one.get("relativePublishTimeDescription"),
        })
    out["reviews"] = reviews
    return out


class _MapSearch:
    def __init__(
        self,
        api_key: str,
        cap_usd: float,
        book: places_api.PlaceBook,
        here: Callable[[], Mapping[str, Any]] | None,
        phone_here: Callable[[], Mapping[str, Any]] | None,
    ) -> None:
        self._key = api_key
        self._cap = cap_usd
        self._book = book
        self._here = here
        self._phone = phone_here

    def _fix(self, ctx: ToolContext, *, needed: bool) -> dict[str, Any] | None:
        """Where he is; None, when ``needed`` is false and it cannot be read."""
        try:
            return read_here(self._here, self._phone, ctx)
        except ToolError:
            if needed:
                raise
            return None

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """One search, or one place in detail; refused at the monthly cap."""
        given = {k: v for k, v in args.items() if v is not None and v != ""}
        if unknown := sorted(set(given) - _KEYS):
            msg = f"{', '.join(unknown)} is not an argument; they are {', '.join(sorted(_KEYS))}"
            raise _bad(msg)
        place_id = str(given.get("place_id") or "").strip()
        query, kind = str(given.get("query") or "").strip(), str(given.get("kind") or "").strip()
        if place_id and len(given) > 1:
            msg = "place_id stands alone"
            raise _bad(msg)
        if (not place_id and not (query or kind)) or (query and kind):
            msg = "give query=<what he wants> or kind=<place type>, or place_id=<from a result>"
            raise _bad(msg)
        area = str(given.get("area") or "").strip()
        if kind and area:
            query, kind = kind.replace("_", " "), ""
        fix = self._fix(ctx, needed=not (area or place_id))
        origin = (fix["lat"], fix["lng"]) if fix is not None else None
        maps_spend.guard(ctx.conn, self._cap)
        meter: list[str] = []
        try:
            if place_id:
                return self._place(place_id, origin, meter, ctx)
            return self._search(
                query, kind, area, origin, fix, meter, ctx,
                radius=_count(given.get("radius_m"), "radius_m", 100, _MAX_RADIUS_M,
                              _DEFAULT_RADIUS_M),
                limit=_count(given.get("limit"), "limit", 1, _MAX_LIMIT, _DEFAULT_LIMIT),
            )
        finally:
            maps_spend.record(ctx, meter)

    def _place(
        self, place_id: str, origin: tuple[float, float] | None, meter: list[str],
        ctx: ToolContext,
    ) -> dict[str, Any]:
        raw = places_api.details(self._key, place_id, meter)
        if (one := places_api.on_map(raw)) is not None:
            self._book.remember(one)
            places_api.announce(ctx, [one])
        return {"place": _detail(raw, origin)}

    def _search(  # noqa: PLR0913 - the question, where from, the reading, the meter and the limits.
        self,
        query: str,
        kind: str,
        area: str,
        origin: tuple[float, float] | None,
        fix: Mapping[str, Any] | None,
        meter: list[str],
        ctx: ToolContext,
        *,
        radius: int,
        limit: int,
    ) -> dict[str, Any]:
        if area:
            found = places_api.text_search(
                self._key,
                {"textQuery": f"{query} in {area}", "maxResultCount": limit,
                 "languageCode": "en"},
                meter,
            )
            searched = f"{query!r} in {area}"
        elif origin is not None and kind:
            found = places_api.nearby_search(
                self._key,
                {
                    "includedTypes": [kind], "maxResultCount": limit, "languageCode": "en",
                    "rankPreference": "DISTANCE",
                    "locationRestriction": places_api.circle(*origin, radius),
                },
                meter,
            )
            searched = f"the nearest {kind}"
        else:
            assert origin is not None  # noqa: S101 - `_fix` raised without a position here.
            found = places_api.text_search(
                self._key,
                {
                    "textQuery": query, "maxResultCount": limit, "languageCode": "en",
                    "locationBias": places_api.circle(*origin, radius),
                },
                meter,
            )
            searched = repr(query)
        on_map = [one for raw in found if (one := places_api.on_map(raw)) is not None]
        for one in on_map:
            self._book.remember(one)
        places_api.announce(ctx, on_map)
        out: dict[str, Any] = {
            "summary": (
                f"{len(found)} place{'' if len(found) == 1 else 's'} for {searched}"
                if found else f"Google found no place for {searched}; say so, do not guess"
            ),
            "places": [_shape(raw, origin) for raw in found],
        }
        if fix is not None:
            out["near"] = f"his position ({said_where(fix)})"
            if fix["source"] == "phone" and fix["age_s"] > _OLD_FIX_S:
                out["warning"] = (
                    "His phone's report is old, so he may have moved: say that first, or ask"
                    " where he is, before sending him to a place by its distance."
                )
        return out


def build_map_tool(
    api_key: str | None,
    cap_usd: float,
    book: places_api.PlaceBook,
    here: Callable[[], Mapping[str, Any]] | None = None,
    *,
    phone_here: Callable[[], Mapping[str, Any]] | None = None,
) -> tuple[Tool, ...]:
    """`map_search`; none without a Google key."""
    if not api_key:
        return ()
    return (
        Tool(
            name="map_search",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_MapSearch(api_key, cap_usd, book, here, phone_here).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["build_map_tool"]
