"""ADR 0283, 0345: `map_search`, places near him or in a named area, read from Google Places (New).

One tool for the questions he used to open Google Maps for: what is near, is it open, how far, a
phone number, reviews. A search near him is restricted to the box round his radius and cut to the
circle, nearest first; one in an area is restricted to the area's own bounds. Each call is metered
against the monthly cap (``maps_spend``), and the places it returns are logged so the turn's
answer can carry them to the phone's map.
"""

from __future__ import annotations

import calendar
import json
import math
import urllib.error
import urllib.request
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import maps_spend, place_reader, places_api, transit_tool
from jarvis.execution.location_tool import read_here, said_where
from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereReading
    from jarvis.state.places import Place

_DEFAULT_RADIUS_M: Final = 2000
_MAX_RADIUS_M: Final = 50_000
_DEFAULT_LIMIT: Final = 5
_MAX_LIMIT: Final = 8
_PAGE: Final = 20
"""Text Search's largest page: a box holds corners the circle drops, so more is asked for."""
_NEAREST_PAGE: Final = 3
_WALK_WITHIN_M: Final = 3000
_MATRIX_URL: Final = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
_MATRIX_MASK: Final = "originIndex,destinationIndex,duration,condition"
_MATRIX_TIMEOUT_S: Final = 5.0
_SKU_MATRIX: Final = "routes.matrix.essentials"
_SHOWN_IN_SUMMARY: Final = 3
_REVIEWS: Final = 3
_REVIEW_CHARS: Final = 240
_KEYS: Final = frozenset({"query", "kind", "area", "radius_m", "limit", "place_id"})
_SOURCES: Final = ("Google Places",)

_DESCRIPTION: Final = (
    "Use to find places: food, shops, services or a named business, near him or in an area, or"
    " one place in detail. Not for bus stops or bus times (bus), where he is (where_am_i) or"
    " places he keeps (places). Returns each place with address, distance, walking time, whether"
    " it is open and when that changes, rating, price level, phone and place_id."
)
_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "What he is looking for, as he said it."},
        "kind": {
            "type": "string",
            "description": "A Google place type such as cafe, pharmacy or atm, for the nearest"
            " of it. Instead of query.",
        },
        "area": {
            "type": "string",
            "description": "A named area to search in rather than near him, e.g. 'Fairfield,"
            " Victoria'.",
        },
        "radius_m": {
            "type": "integer",
            "description": f"How far from him to look, in metres. Default {_DEFAULT_RADIUS_M}.",
        },
        "limit": {
            "type": "integer",
            "description": f"How many places. Default {_DEFAULT_LIMIT}, at most {_MAX_LIMIT}.",
        },
        "place_id": {
            "type": "string",
            "description": "A place_id from an earlier result: that one place in detail.",
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


def _today_hours(raw: Mapping[str, Any], now: datetime) -> str | None:
    """Today's line of Google's seven weekday lines ('Monday: 9:00 AM - 5:00 PM')."""
    lines = (raw.get("currentOpeningHours") or {}).get("weekdayDescriptions") or []
    day = calendar.day_name[now.weekday()]
    return next((str(line) for line in lines if str(line).startswith(f"{day}:")), None)


def _status(raw: Mapping[str, Any], now: datetime) -> str | None:
    """Open, closed for now or closed for good, and when that next changes, in his time."""
    state = raw.get("businessStatus")
    if state == "CLOSED_PERMANENTLY":
        return "closed for good"
    if state == "CLOSED_TEMPORARILY":
        return "closed for now"
    hours = raw.get("currentOpeningHours") or {}
    if (is_open := hours.get("openNow")) is None:
        return None
    line = "open now" if is_open else "closed now"
    if change := hours.get("nextCloseTime" if is_open else "nextOpenTime"):
        at = datetime.fromisoformat(str(change)).astimezone(now.tzinfo)
        line += f", {'closes' if is_open else 'opens'} {transit_tool.said(at, now)}"
    return line


def _shape(
    raw: Mapping[str, Any], origin: tuple[float, float] | None, now: datetime,
) -> dict[str, Any]:
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
    out |= {
        "open": _status(raw, now),
        "hours_today": _today_hours(raw, now),
        "rating": raw.get("rating"),
        "rating_count": raw.get("userRatingCount"),
        "price_level": places_api.price_word(raw.get("priceLevel")),
        "phone": raw.get("nationalPhoneNumber"),
        "maps_url": raw.get("googleMapsUri"),
        "place_id": raw.get("id"),
    }
    return {k: v for k, v in out.items() if v is not None}


def _detail(
    raw: Mapping[str, Any], origin: tuple[float, float] | None, now: datetime,
) -> dict[str, Any]:
    out = _shape(raw, origin, now)
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


def _waypoint(lat: float, lng: float) -> dict[str, Any]:
    return {"waypoint": {"location": {"latLng": {"latitude": lat, "longitude": lng}}}}


def walk_minutes(
    api_key: str, origin: tuple[float, float], places: Sequence[Mapping[str, Any]],
    meter: list[str],
) -> dict[int, int]:
    """Minutes on foot from ``origin`` to each place (by index), from one route matrix request.

    A request that fails gives none: the walking time is extra to the answer. Each element sent is
    metered unless Google refused the request.
    """
    if not places:
        return {}
    body = {
        "origins": [_waypoint(*origin)],
        "destinations": [
            _waypoint(float(p["location"]["latitude"]), float(p["location"]["longitude"]))
            for p in places
        ],
        "travelMode": "WALK",
    }
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL.
        _MATRIX_URL, data=json.dumps(body).encode(), method="POST",
        headers={
            "Content-Type": "application/json", "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": _MATRIX_MASK,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_MATRIX_TIMEOUT_S) as reply:  # noqa: S310
            elements = json.load(reply)
    except urllib.error.HTTPError:
        return {}  # a refused request is not billed.
    except (OSError, ValueError):
        meter.extend([_SKU_MATRIX] * len(places))
        return {}
    meter.extend([_SKU_MATRIX] * len(places))
    found: dict[int, int] = {}
    for one in elements if isinstance(elements, list) else ():
        try:
            seconds = float(str(one["duration"]).removesuffix("s"))
            found[int(one["destinationIndex"])] = max(1, math.ceil(seconds / 60))
        except (KeyError, TypeError, ValueError):
            continue
    return found


def _sentence(item: Mapping[str, Any]) -> str:
    """One place in a line: name, how far, how long on foot, open or not."""
    text = str(item.get("name"))
    if "distance_m" in item:
        text += f", {item['distance_m']} m {item['direction']}"
    if "walk_min" in item:
        text += f", {item['walk_min']} min on foot"
    if item.get("open"):
        text += f", {item['open']}"
    return text


class _MapSearch:
    def __init__(
        self,
        api_key: str,
        cap_usd: float,
        book: places_api.PlaceBook,
        reader: place_reader.PlaceReader,
    ) -> None:
        self._key = api_key
        self._cap = cap_usd
        self._book = book
        self._reader = reader

    def _fix(self, ctx: ToolContext, *, needed: bool) -> HereReading | None:
        """Where he is; None, when ``needed`` is false and it cannot be read."""
        try:
            return read_here(ctx)
        except ToolError:
            if needed:
                raise
            return None

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
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
        maps_spend.guard(ctx.conn, self._cap, ctx.clock.now())
        meter: list[str] = []
        try:
            if place_id:
                return self._place(place_id, fix, meter, ctx)
            return self._search(
                query, kind, area, fix, meter, ctx,
                radius=_count(given.get("radius_m"), "radius_m", 100, _MAX_RADIUS_M,
                              _DEFAULT_RADIUS_M),
                limit=_count(given.get("limit"), "limit", 1, _MAX_LIMIT, _DEFAULT_LIMIT),
            )
        finally:
            maps_spend.record(ctx, meter)

    def _place(
        self, place_id: str, fix: HereReading | None, meter: list[str], ctx: ToolContext,
    ) -> Receipted:
        raw = places_api.details(self._key, place_id, meter)
        if (one := places_api.on_map(raw)) is not None:
            self._book.remember(one)
            places_api.announce(ctx, [one])
        origin = (fix.lat, fix.lng) if fix is not None else None
        place = _detail(raw, origin, ctx.clock.now())
        return Receipted({"place": place}, Receipt(
            "done", reads_only=True, line=f"read the details of {place.get('name')}",
            covered=Covered(_SOURCES, "that one place", 1, 0),
        ))

    def _candidates(  # noqa: PLR0913 - the question, the area, the reading, the meter and limits.
        self, query: str, kind: str, area: str, fix: HereReading | None, meter: list[str],
        ctx: ToolContext, *, radius: int, limit: int,
    ) -> tuple[list[dict[str, Any]], tuple[float, float], str, str]:
        """Google's places, the point their distances are from, what was searched and where."""
        if area:
            found_area = self._reader.read(area, ctx, meter, here_fix=fix)
            box = found_area.bounds or place_reader.circle_bounds(*found_area.point, radius)
            raws = places_api.text_search(
                self._key,
                {"textQuery": query, "maxResultCount": limit, "languageCode": "en",
                 "locationRestriction": places_api.rectangle(box)},
                meter,
            )
            origin = found_area.point
            return raws, origin, repr(query), (
                f"in {area} (distances from {found_area.name})"
            )
        assert fix is not None  # noqa: S101 - `_fix` raised without a position here.
        origin = (fix.lat, fix.lng)
        if kind:
            raws = places_api.nearby_search(
                self._key,
                {
                    "includedTypes": [kind], "maxResultCount": _MAX_LIMIT, "languageCode": "en",
                    "rankPreference": "DISTANCE",
                    "locationRestriction": places_api.circle(*origin, radius),
                },
                meter,
            )
            searched = f"the nearest {kind}"
        else:
            raws = places_api.text_search(
                self._key,
                {"textQuery": query, "maxResultCount": _PAGE, "languageCode": "en",
                 "locationRestriction": places_api.rectangle(
                     place_reader.circle_bounds(*origin, radius))},
                meter,
            )
            searched = repr(query)
        return raws, origin, searched, f"within {radius} m of his position ({said_where(fix)})"

    def _search(  # noqa: PLR0913 - the question, the area, the reading, the meter and limits.
        self, query: str, kind: str, area: str, fix: HereReading | None, meter: list[str],
        ctx: ToolContext, *, radius: int, limit: int,
    ) -> Receipted:
        now = ctx.clock.now()
        raws, origin, searched, where = self._candidates(
            query, kind, area, fix, meter, ctx, radius=radius, limit=limit,
        )
        by_distance = sorted(
            raws, key=lambda r: places_api.distance_and_direction(
                origin, float(r["location"]["latitude"]), float(r["location"]["longitude"]),
            )[0],
        ) if not area else list(raws)
        inside = [
            r for r in by_distance
            if area or places_api.distance_and_direction(
                origin, float(r["location"]["latitude"]), float(r["location"]["longitude"]),
            )[0] <= radius
        ]
        shown = inside[:limit]
        nearest = False
        if not shown and not area:
            shown = self._nearest(query or kind.replace("_", " "), origin, meter)
            nearest = bool(shown)
        on_map = [one for raw in shown if (one := places_api.on_map(raw)) is not None]
        for one in on_map:
            self._book.remember(one)
        places_api.announce(ctx, on_map)
        places = [_shape(raw, origin, now) for raw in shown]
        if not area:
            close = [
                i for i, p in enumerate(places)
                if p.get("distance_m", math.inf) <= _WALK_WITHIN_M
            ]
            walks = walk_minutes(self._key, origin, [shown[i] for i in close], meter)
            for k, i in enumerate(close):
                if k in walks:
                    places[i]["walk_min"] = walks[k]
        if nearest:
            places[0]["outside_radius"] = True
        out: dict[str, Any] = {
            "summary": self._summary(searched, where, places, radius, nearest=nearest, fix=fix),
            "places": places,
        }
        more = max(0, len(inside) - len(shown)) if not nearest else 0
        return Receipted(out, Receipt(
            "done", reads_only=True, line=f"searched {searched} {where}: {len(places)} shown",
            covered=Covered(
                _SOURCES, where, len(places), more,
                "raise limit or radius_m, or name an area" if more else "",
            ),
        ))

    def _nearest(
        self, words: str, origin: tuple[float, float], meter: list[str],
    ) -> list[dict[str, Any]]:
        """The nearest match anywhere around him, for when nothing is inside the radius."""
        return places_api.text_search(
            self._key,
            {
                "textQuery": words, "maxResultCount": _NEAREST_PAGE, "languageCode": "en",
                "rankPreference": "DISTANCE",
                "locationBias": places_api.circle(*origin, place_reader.SEARCH_BIAS_M),
            },
            meter,
        )[:1]

    @staticmethod
    def _summary(  # noqa: PLR0913 - what was searched, where, the places, the radius, the reading.
        searched: str, where: str, places: list[dict[str, Any]], radius: int, *,
        nearest: bool, fix: HereReading | None,
    ) -> str:
        old = (
            "His phone's report is old, so he may have moved. " if fix is not None and fix.why
            else ""
        )
        if not places:
            return f"{old}Google found no place for {searched} {where}; say so, do not guess"
        if nearest:
            return (
                f"{old}Nothing within {radius} m for {searched}; the nearest is"
                f" {_sentence(places[0])}."
            )
        count = f"{len(places)} place{'' if len(places) == 1 else 's'}"
        lines = "; ".join(_sentence(p) for p in places[:_SHOWN_IN_SUMMARY])
        return f"{old}{count} for {searched} {where}: {lines}."


def build_map_tool(
    api_key: str | None,
    cap_usd: float,
    book: places_api.PlaceBook,
    *,
    places: Callable[[], Mapping[str, str]] | None = None,
    kept: Callable[[], list[Place]] | None = None,
) -> tuple[Tool, ...]:
    """`map_search`; none without a Google key."""
    if not api_key:
        return ()
    reader = place_reader.PlaceReader(
        api_key, book, saved=places or dict, kept=kept,
        before_request=lambda ctx: maps_spend.guard(ctx.conn, cap_usd, ctx.clock.now()),
    )
    return (
        Tool(
            name="map_search",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_MapSearch(api_key, cap_usd, book, reader).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["build_map_tool", "walk_minutes"]
