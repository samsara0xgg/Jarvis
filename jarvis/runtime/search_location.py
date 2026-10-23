"""ADR 0359: where he is, in the form Anthropic's web search takes it (``user_location``).

The search tool localizes its results by an approximate place: a city, a region, a country (ISO
3166-1 alpha-2) and an IANA time zone, each optional. Words in the question are only more search
terms. The place is roughly where the call's Here says he is, else home. The city is known only for
home's own place, so it is given only when he is within :data:`HOME_CITY_KM` of home; the country
and zone of anywhere else come from the tz database.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import TZPATH

from tzfpy import get_tz

from jarvis.shared import geo

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.shared.here import Coarse

HOME_CITY_KM: Final[float] = 30.0
"""Within this far of home he is in home's city, so home's place is his."""


@dataclass(frozen=True)
class HomePlace:
    """Home as the deep search knows it: where, its name as the city, its country and zone."""

    lat: float
    lng: float
    city: str | None
    country: str | None
    zone: str | None

    @classmethod
    def from_weather(cls, place: Mapping[str, Any], zone: str | None) -> HomePlace:
        """Home from ``home.weather``: ``latitude``, ``longitude``, ``name``, ``country_code``.

        The name and the country code are optional. ``zone`` is home's time zone, the local one;
        the country is the one named, else the zone's.
        """
        name, code = str(place.get("name") or "").strip(), str(place.get("country_code") or "")
        return cls(
            float(place["latitude"]), float(place["longitude"]), name or None,
            code.strip().upper() or _country(zone), zone,
        )


@functools.cache
def _zone_countries() -> dict[str, str]:
    """Time zone -> ISO country code, from the system tz database's ``zone.tab``."""
    for root in TZPATH:
        if (table := Path(root) / "zone.tab").is_file():
            lines = table.read_text().splitlines()
            rows = (line.split("\t") for line in lines if line and not line.startswith("#"))
            return {zone: country for country, _, zone, *_ in rows}
    return {}


def _country(zone: str | None) -> str | None:
    """The country a time zone is in (``America/Vancouver`` -> ``CA``)."""
    return _zone_countries().get(zone) if zone else None


def user_location(where: Coarse | None, home: HomePlace | None) -> dict[str, str] | None:
    """The ``user_location`` of the search tool for ``where``, else home; ``None`` when neither.

    Only the fields known are given. Within :data:`HOME_CITY_KM` of home the place is home's,
    its city included; farther, its zone is the tz map's and its country the zone's.
    """
    if where is not None:
        point = (where.lat, where.lng)
    elif home is not None:
        point = (home.lat, home.lng)
    else:
        return None
    if home is not None and geo.metres(point, (home.lat, home.lng)) <= HOME_CITY_KM * 1000:
        known = {"city": home.city, "country": home.country, "timezone": home.zone}
    else:
        zone = get_tz(point[1], point[0])
        known = {"country": _country(zone), "timezone": zone}
    fields = {key: value for key, value in known.items() if value}
    return {"type": "approximate", **fields} if fields else None
