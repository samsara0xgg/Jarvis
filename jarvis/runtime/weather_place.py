"""ADR 0285, 0356: where a weather request is for, from a name, a point or the home.

A name goes to Open-Meteo's geocoder, which gives up to ten matches. Of the places called that,
the one nearest where he roughly is wins when it is within :data:`NEAR_KM` of him, else the first
match stands; a region or country after a comma ("Vancouver, BC") narrows the matches first. A
point (``here`` or ``lat,lng``) takes the zone of a place the lookup listed, or the home's within
:data:`_NEAR_HOME_KM`, else the offline map's.
"""

from __future__ import annotations

import json
import math
import re
import urllib.request
from typing import TYPE_CHECKING, Any, Final, NamedTuple
from urllib.parse import urlencode

from tzfpy import get_tz

from jarvis.execution.tools import ToolError
from jarvis.shared import lang
from jarvis.shared.clock import zone_country

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.weather_tool import WeatherRequest

GEOCODE: Final = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODE_MATCHES: Final = 10
NEAR_KM: Final = 300
"""A match this close to where he is wins over a farther one of the same name (ADR 0356)."""
LAT_LNG: Final = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
TIMEOUT_S: Final = 4.0
ALTERNATIVES: Final = 4
"""How many other places of the name an answer lists."""
_NEAR_HOME_KM: Final = 100
_NAMED_AFTER: Final = re.compile(r"\s*[,\uff0c]\s*")
_NOT_LETTERS: Final = re.compile(r"[\s.]")
_COUNTRY_WORDS: Final = {"usa": "US", "uk": "GB"}
"""What he calls a country besides its name and its ISO code."""
_SHOWN_IN_ERROR: Final = 3


def _table(rows: str) -> dict[str, str]:
    return {code: name for row in rows.split(",") for code, _, name in [row.strip().partition(" ")]}


_REGION_CODES: Final = {
    "CA": _table(
        "AB Alberta, BC British Columbia, MB Manitoba, NB New Brunswick,"
        " NL Newfoundland and Labrador, NS Nova Scotia, NT Northwest Territories, NU Nunavut,"
        " ON Ontario, PE Prince Edward Island, QC Quebec, SK Saskatchewan, YT Yukon",
    ),
    "US": _table(
        "AL Alabama, AK Alaska, AZ Arizona, AR Arkansas, CA California, CO Colorado,"
        " CT Connecticut, DE Delaware, DC District of Columbia, FL Florida, GA Georgia,"
        " HI Hawaii, ID Idaho, IL Illinois, IN Indiana, IA Iowa, KS Kansas, KY Kentucky,"
        " LA Louisiana, ME Maine, MD Maryland, MA Massachusetts, MI Michigan, MN Minnesota,"
        " MS Mississippi, MO Missouri, MT Montana, NE Nebraska, NV Nevada, NH New Hampshire,"
        " NJ New Jersey, NM New Mexico, NY New York, NC North Carolina, ND North Dakota,"
        " OH Ohio, OK Oklahoma, OR Oregon, PA Pennsylvania, RI Rhode Island,"
        " SC South Carolina, SD South Dakota, TN Tennessee, TX Texas, UT Utah, VT Vermont,"
        " VA Virginia, WA Washington, WV West Virginia, WI Wisconsin, WY Wyoming",
    ),
}
"""The standard short codes of Canada's provinces and territories and of the US states."""


class Spot(NamedTuple):
    """Where a request is for: the point, its label, its time zone and country (None if unknown)."""

    latitude: float
    longitude: float
    label: str
    zone: str | None
    country: str | None


def get_json(url: str, query: Mapping[str, Any], what: str) -> dict[str, Any]:
    """One JSON GET; a ToolError that names ``what`` when it cannot be read."""
    try:
        with urllib.request.urlopen(  # noqa: S310 - a fixed https URL.
            f"{url}?{urlencode(query, safe=',')}", timeout=TIMEOUT_S,
        ) as reply:
            body: dict[str, Any] = json.load(reply)
    except Exception as exc:  # timeout, refusal, bad JSON: all one short reason.
        msg = (
            f"weather: {what} could not be read ({type(exc).__name__}); say so, or leave place"
            " out for the weather at his home"
        )
        raise ToolError(msg, code="network_error") from exc
    return body


def label(found: Mapping[str, Any]) -> str:
    """``Toronto, Ontario, Canada`` for a geocoding match."""
    return ", ".join(dict.fromkeys(
        str(one) for one in (found["name"], found.get("admin1"), found.get("country")) if one
    ))


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in km."""
    lat1, lng1, lat2, lng2 = map(math.radians, (*a, *b))
    chord = math.sin((lat2 - lat1) / 2) ** 2 + (
        math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371 * math.asin(math.sqrt(chord))


def _in(wanted: str, found: Mapping[str, Any]) -> bool:
    """Whether a region or country he named is the match's, by its name or its short code."""
    text = " ".join(wanted.casefold().split())
    code = _NOT_LETTERS.sub("", text).upper()
    country = str(found.get("country_code") or "").upper()
    region = str(found.get("admin1") or "")
    if code == country or _COUNTRY_WORDS.get(code.lower()) == country:
        return True
    coded = _REGION_CODES.get(country, {}).get(code)
    if coded is not None and coded.casefold() == region.casefold():
        return True
    chinese = lang.text_language(text) == "zh"
    names = (region.casefold(), str(found.get("country") or "").casefold())
    return any(text == name or (chinese and text in name) for name in names if name)


def _matches(base: str, regions: list[str], asked: str) -> list[dict[str, Any]]:
    """The geocoder's matches for ``base`` that lie in every region he named.

    Asks in the language of the name, then of the regions, then English; the first language that
    has a match in them answers.

    Raises:
        ToolError: Open-Meteo did not answer, knows no place of that name, or knows none in the
            region he named.
    """
    anywhere: list[dict[str, Any]] = []
    languages = (lang.text_language(base), lang.text_language(" ".join(regions)), "en")
    for language in dict.fromkeys(languages):
        query = {"name": base, "count": GEOCODE_MATCHES, "language": language, "format": "json"}
        found = get_json(GEOCODE, query, "the place lookup").get("results") or []
        anywhere = anywhere or found
        if narrowed := [one for one in found if all(_in(region, one) for region in regions)]:
            return narrowed
    if not anywhere:
        msg = f"weather: no place found named {asked!r}; ask him which place he means"
        raise ToolError(msg, code="not_found")
    known = "; ".join(dict.fromkeys(label(one) for one in anywhere[:_SHOWN_IN_ERROR]))
    msg = (
        f"weather: no place named {base!r} found in {', '.join(regions)}; the geocoder knows"
        f" {known}. Ask him which place he means"
    )
    raise ToolError(msg, code="not_found")


def geocode(name: str, near: tuple[float, float] | None) -> tuple[Spot, list[Spot]]:
    """The place called ``name`` nearest ``near``, and the other places called that (ADR 0356).

    ``name`` is a place, or a place and a region or country after a comma. Of the places called
    that, the nearest to ``near`` within :data:`NEAR_KM` wins; with none that near the geocoder's
    first stands.

    Raises:
        ToolError: Open-Meteo did not answer, or has no such place.
    """
    parts = [part for part in _NAMED_AFTER.split(name.strip()) if part]
    if not parts:
        msg = f"weather: no place found named {name!r}; ask him which place he means"
        raise ToolError(msg, code="not_found")
    base, *regions = parts
    found = _matches(base, regions, name)
    called = base.casefold() if any(one["name"].casefold() == base.casefold() for one in found) \
        else found[0]["name"].casefold()
    same = [one for one in found if one["name"].casefold() == called]

    def point(one: Mapping[str, Any]) -> tuple[float, float]:
        return one["latitude"], one["longitude"]

    chosen = same[0]
    if near is not None:
        here = near
        close = [one for one in same if km(here, point(one)) <= NEAR_KM]
        chosen = min(close, key=lambda one: km(here, point(one)), default=chosen)

    def spot(one: Mapping[str, Any]) -> Spot:
        return Spot(*point(one), label(one), one.get("timezone"), one.get("country_code"))

    others = {label(one): spot(one) for one in same if one is not chosen}
    others.pop(label(chosen), None)
    return spot(chosen), list(others.values())


def locate(
    request: WeatherRequest, home: Spot, known: dict[tuple[float, float], Spot],
) -> tuple[Spot, list[Spot]]:
    """Where the request is for, and the other places of the name a named place has.

    A point (``here`` or ``lat,lng``) takes a listed place's zone, or the home's country within
    :data:`_NEAR_HOME_KM` of it; else its zone comes from tzfpy's offline map. A name is the place
    of that name nearest ``request.near`` (else the home).
    """
    if request.here is not None:
        point, called = request.here[:2], request.here[2]
    elif request.place is None:
        return home, []
    elif pair := LAT_LNG.match(request.place):
        point, called = (float(pair[1]), float(pair[2])), request.place
    else:
        chosen, others = geocode(request.place, request.near or (home.latitude, home.longitude))
        # ponytail: grows by a few places per named lookup, until a restart.
        known.update({(one.latitude, one.longitude): one for one in (chosen, *others)})
        return chosen, others
    if (match := known.get((point[0], point[1]))) is not None:
        return match._replace(label=called), []
    return spot_at(point, called, home), []


def spot_at(point: tuple[float, float], called: str, home: Spot) -> Spot:
    """The spot of a bare point: the home's zone and country within :data:`_NEAR_HOME_KM` of it.

    Farther away the zone comes from tzfpy's offline map and the country from the zone.
    """
    if km(point, (home.latitude, home.longitude)) <= _NEAR_HOME_KM:
        return Spot(*point, called, home.zone, home.country)
    zone = get_tz(point[1], point[0])
    return Spot(*point, called, zone, zone_country(zone))
