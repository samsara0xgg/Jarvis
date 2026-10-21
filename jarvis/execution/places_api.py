"""ADR 0283: Google's Places API (New) over HTTPS, and the places an answer shows on a map.

The key goes in the ``X-Goog-Api-Key`` header, never in a URL, and no exception leaves this module
holding the request. Each request asks for the cheapest field mask that covers what ``map_search``
says, and appends the SKU it was billed under to ``meter`` (``maps_spend`` prices it).
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import ToolError, _turn_of
from jarvis.state.event_log import emit_event, iter_events_for_turn

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping

    from jarvis.execution.tools import ToolContext

_BASE: Final = "https://places.googleapis.com/v1/"
_TIMEOUT_S: Final = 6.0
_MAX_ON_MAP: Final = 8
_REMEMBERED: Final = 64
_ERROR_BODY_BYTES: Final = 4096
EVENT_TYPE: Final = "map.places_found"

_ESSENTIAL_FIELDS: Final = ("id", "displayName", "formattedAddress", "location", "googleMapsUri")
_ENTERPRISE_FIELDS: Final = (
    "rating", "userRatingCount", "priceLevel", "nationalPhoneNumber",
    "currentOpeningHours.openNow", "currentOpeningHours.weekdayDescriptions", "businessStatus",
    "currentOpeningHours.nextOpenTime", "currentOpeningHours.nextCloseTime",
)
# Text and Nearby Search: Enterprise tier, because rating, price level, hours and phone are.
SEARCH_MASK: Final = ",".join(f"places.{f}" for f in (*_ESSENTIAL_FIELDS, *_ENTERPRISE_FIELDS))
# What is around a point (ADR 0284): name, address, point, kind and open now. Open now is an
# Enterprise field, so the request is billed as Nearby Search Enterprise.
AROUND_MASK: Final = ",".join(f"places.{f}" for f in (
    "id", "displayName", "formattedAddress", "location", "primaryTypeDisplayName",
    "currentOpeningHours.openNow",
))
# Place Details with review highlights: Enterprise + Atmosphere (reviews are Atmosphere fields).
DETAILS_MASK: Final = ",".join((
    *_ESSENTIAL_FIELDS, *_ENTERPRISE_FIELDS, "websiteUri",
    "reviews.rating", "reviews.text", "reviews.relativePublishTimeDescription",
))
# Name and address of a place by id: Pro tier (displayName is a Pro field).
NAME_MASK: Final = "id,displayName,formattedAddress,location,viewport"
# What the place reader asks a Text Search for (ADR 0346): only name, address, point and bounds,
# which is the Pro tier.
READER_MASK: Final = ",".join(
    f"places.{f}" for f in ("id", "displayName", "formattedAddress", "location", "viewport")
)

SKU_TEXT: Final = "places.text_search.enterprise"
SKU_NEARBY: Final = "places.nearby_search.enterprise"
SKU_DETAILS: Final = "places.details.enterprise_atmosphere"
SKU_NAME: Final = "places.details.pro"
SKU_TEXT_PRO: Final = "places.text_search.pro"

_COMPASS: Final = (
    "north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west",
)
_EARTH_M: Final = 6_371_000.0
_PRICE: Final = {
    "PRICE_LEVEL_FREE": "free", "PRICE_LEVEL_INEXPENSIVE": "$", "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$", "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


def _failure(exc: urllib.error.HTTPError) -> ToolError:
    """The reason Google refused, in words for the model; never the request, key or full body."""
    try:
        text = exc.read(_ERROR_BODY_BYTES).decode("utf-8", "replace")
    except OSError:
        text = ""
    if exc.code == 403:  # noqa: PLR2004 - HTTP status
        if "SERVICE_DISABLED" in text:
            msg = (
                "Places API (New) is not enabled for the Google key's project, so place search"
                " cannot work yet; tell him it has to be switched on in Google Cloud. Do not"
                " guess places."
            )
            return ToolError(msg, code="places_not_enabled")
        if "API_KEY_SERVICE_BLOCKED" in text:
            msg = (
                "The Google key is not allowed to call Places API (New); tell him its API"
                " restrictions need to include it. Do not guess places."
            )
            return ToolError(msg, code="places_not_enabled")
    if exc.code == 404:  # noqa: PLR2004
        msg = "Google has no place with that place_id (they can expire); search again by name"
        return ToolError(msg, code="not_found")
    if exc.code == 429:  # noqa: PLR2004
        msg = "Google's place search is over its rate limit; try again later"
        return ToolError(msg, code="rate_limited")
    msg = f"Place search failed just now (Google Places: HTTP {exc.code}); do not guess places"
    return ToolError(msg, code="network_error")


def _call(  # noqa: PLR0913 - the key, where, what to read, its price class, the meter, the body.
    api_key: str, path: str, mask: str, sku: str, meter: list[str], body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    request = urllib.request.Request(  # noqa: S310 - a fixed https URL.
        _BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": mask,
        },
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as reply:  # noqa: S310
            answer: dict[str, Any] = json.load(reply)
    except urllib.error.HTTPError as exc:
        raise _failure(exc) from None  # a 4xx is not billed, so it is not metered.
    except Exception as exc:  # noqa: BLE001 - timeout, bad JSON: may have been billed.
        meter.append(sku)
        msg = (
            f"Place search failed just now (Google Places: {type(exc).__name__}); do not guess"
            " places"
        )
        raise ToolError(msg, code="network_error") from None
    meter.append(sku)
    return answer


def circle(lat: float, lng: float, radius_m: float) -> dict[str, Any]:
    """A Places API circle around a point."""
    return {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}}


def text_search(
    api_key: str, body: Mapping[str, Any], meter: list[str],
    *, mask: str = SEARCH_MASK, sku: str = SKU_TEXT,
) -> list[dict[str, Any]]:
    """Text Search: the places Google found for ``body`` (``textQuery`` and optional bias).

    ``mask`` and its ``sku`` default to the search tier that says hours, rating and phone.
    """
    found = _call(api_key, "places:searchText", mask, sku, meter, body)
    return [p for p in found.get("places", []) if isinstance(p, dict)]


def bounds_of(raw: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    """A place's viewport as ``(low lat, low lng, high lat, high lng)``, or None without one."""
    view = raw.get("viewport")
    try:
        low, high = view["low"], view["high"]  # type: ignore[index]
        return (
            float(low["latitude"]), float(low["longitude"]),
            float(high["latitude"]), float(high["longitude"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def rectangle(bounds: tuple[float, float, float, float]) -> dict[str, Any]:
    """A Places API rectangle from ``(low lat, low lng, high lat, high lng)``."""
    return {"rectangle": {
        "low": {"latitude": bounds[0], "longitude": bounds[1]},
        "high": {"latitude": bounds[2], "longitude": bounds[3]},
    }}


def nearby_search(
    api_key: str, body: Mapping[str, Any], meter: list[str],
) -> list[dict[str, Any]]:
    """Nearby Search: the places of a type inside a circle, nearest first."""
    found = _call(api_key, "places:searchNearby", SEARCH_MASK, SKU_NEARBY, meter, body)
    return [p for p in found.get("places", []) if isinstance(p, dict)]


def nearby_around(
    api_key: str, body: Mapping[str, Any], meter: list[str],
) -> list[dict[str, Any]]:
    """Nearby Search with no type filter and :data:`AROUND_MASK`: what is around a point."""
    found = _call(api_key, "places:searchNearby", AROUND_MASK, SKU_NEARBY, meter, body)
    return [p for p in found.get("places", []) if isinstance(p, dict)]


def details(api_key: str, place_id: str, meter: list[str]) -> dict[str, Any]:
    """Place Details with weekly hours and review highlights."""
    return _call(api_key, f"places/{place_id}", DETAILS_MASK, SKU_DETAILS, meter, None)


def name_of(api_key: str, place_id: str, meter: list[str]) -> dict[str, Any]:
    """Only the name, address and point of a place."""
    return _call(api_key, f"places/{place_id}", NAME_MASK, SKU_NAME, meter, None)


def distance_and_direction(
    origin: tuple[float, float], lat: float, lng: float,
) -> tuple[int, str]:
    """Metres and an eight-point compass word from ``origin`` to the point."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*origin, lat, lng))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    metres = 2 * _EARTH_M * math.asin(math.sqrt(hav))
    bearing = math.degrees(math.atan2(
        math.sin(dlng) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng),
    ))
    return round(metres), _COMPASS[round((bearing % 360) / 45) % 8]


def price_word(level: object) -> str | None:
    """``$`` to ``$$$$`` for Google's price level enum, else None."""
    return _PRICE.get(str(level))


def on_map(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """A Places API place as the phone's map card draws it; None without a point."""
    where = raw.get("location")
    try:
        lat, lng = float(where["latitude"]), float(where["longitude"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("displayName")
    return {
        "name": str(name.get("text", "") if isinstance(name, dict) else name or "")[:80],
        "lat": lat,
        "lng": lng,
        "address": str(raw["formattedAddress"]) if raw.get("formattedAddress") else None,
        "place_id": str(raw["id"]) if raw.get("id") else None,
        "kind": "place",
    }


class PlaceBook:
    """The places this process found lately, by place_id: a place_id sent to `bus` has a name."""

    def __init__(self) -> None:
        """Start empty; the oldest are dropped past a small cap."""
        self._by_id: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def remember(self, place: Mapping[str, Any]) -> None:
        """Keep a place that has an id."""
        if pid := place.get("place_id"):
            self._by_id[str(pid)] = dict(place)
            self._by_id.move_to_end(str(pid))
            while len(self._by_id) > _REMEMBERED:
                self._by_id.popitem(last=False)

    def get(self, place_id: str) -> dict[str, Any] | None:
        """The remembered place, or None."""
        return self._by_id.get(place_id)


def announce(ctx: ToolContext, places: list[dict[str, Any]]) -> None:
    """Log the places a tool returned, so the turn's answer can carry them to the phone's map."""
    if not places:
        return
    turn = _turn_of(ctx.action_id)
    emit_event(
        ctx.conn,
        type=EVENT_TYPE,
        payload={"turn_id": turn or "", "action_id": ctx.action_id, "places": places},
        correlation={"turn_id": turn} if turn else {"action_id": ctx.action_id},
    )


def turn_places(conn: sqlite3.Connection, turn_id: str) -> list[dict[str, Any]]:
    """The places this turn's tools returned, at most eight, the latest kept, none repeated."""
    found: dict[Any, dict[str, Any]] = {}
    for event in iter_events_for_turn(conn, turn_id, (EVENT_TYPE,)):
        for one in event.payload.get("places", []):
            key = one.get("place_id") or (one["name"], one["lat"], one["lng"])
            found.pop(key, None)
            found[key] = one
    return list(found.values())[-_MAX_ON_MAP:]


__all__ = [
    "AROUND_MASK",
    "DETAILS_MASK",
    "EVENT_TYPE",
    "NAME_MASK",
    "READER_MASK",
    "SEARCH_MASK",
    "SKU_TEXT_PRO",
    "PlaceBook",
    "announce",
    "bounds_of",
    "circle",
    "details",
    "distance_and_direction",
    "name_of",
    "nearby_around",
    "nearby_search",
    "on_map",
    "price_word",
    "rectangle",
    "text_search",
    "turn_places",
]
