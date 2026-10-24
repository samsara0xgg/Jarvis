"""ADR 0346: his words for a place, turned into a point by one reader.

Every tool that takes a place (`bus` for where to and from, the ride for where he gets off,
`map_search` for an area, `save_place`) reads it here, so the copies cannot drift. In order:

- ``here``: the call's Here (ADR 0322);
- a place he keeps, by name in any case and spacing, home and school among them;
- a place id from a ``map_search`` result;
- otherwise one Google Text Search, biased to Here and ranked by distance, so a chain's name gives
  its nearest branch. It asks only for name, address, point and bounds (the Pro tier) and is
  metered under its own SKU.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import places_api
from jarvis.execution.location_tool import read_here
from jarvis.execution.tools import ToolError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereReading
    from jarvis.state.places import Place

SEARCH_BIAS_M: Final = 50_000.0
"""Google's largest bias circle."""
_SCHOOL_NAMES: Final = frozenset({"uvic", "u vic", "university of victoria"})
_LAT_LNG: Final = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_PLACE_ID: Final = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_M_PER_DEG: Final = 111_320.0


@dataclass(frozen=True)
class Found:
    """A place he named: a point, its bounds when it has them, and what to say back."""

    lat: float
    lng: float
    name: str
    how: str
    """``here``, ``kept``, ``point`` (he gave coordinates), ``place_id`` or ``search``."""
    address: str | None = None
    place_id: str | None = None
    bounds: tuple[float, float, float, float] | None = None
    fix: HereReading | None = None
    """Set when ``how`` is ``here``: the reading, with its age and device."""
    unbiased: bool = False
    """A search that had no position to be biased to."""

    @property
    def point(self) -> tuple[float, float]:
        """``(lat, lng)``."""
        return self.lat, self.lng

    @property
    def said(self) -> str:
        """The place as he is told it: name, and the address when there is one."""
        return f"{self.name} ({self.address})" if self.address else self.name

    @property
    def waypoint(self) -> dict[str, Any]:
        """The point as a Routes API waypoint."""
        return {"location": {"latLng": {"latitude": self.lat, "longitude": self.lng}}}

    def on_map(self) -> dict[str, Any]:
        """The place as the phone's map card draws it."""
        return {
            "name": self.name, "lat": self.lat, "lng": self.lng, "address": self.address,
            "place_id": self.place_id, "kind": "place",
        }


def _words(text: str) -> str:
    return " ".join(text.lower().split())


def circle_bounds(lat: float, lng: float, radius_m: float) -> tuple[float, float, float, float]:
    """The box round a kept place's circle, for an area search."""
    dlat = radius_m / _M_PER_DEG
    dlng = dlat / max(0.01, math.cos(math.radians(lat)))
    return lat - dlat, lng - dlng, lat + dlat, lng + dlng


class PlaceReader:
    """Reads his words for a place; built once with the key, the finds so far and his places."""

    def __init__(
        self,
        api_key: str | None,
        book: places_api.PlaceBook,
        *,
        saved: Callable[[], Mapping[str, str]],
        kept: Callable[[], list[Place]] | None = None,
        before_request: Callable[[ToolContext], None] | None = None,
    ) -> None:
        """Hold the key (None: no search) and how his places are read.

        ``saved`` reads the kept ``home`` and ``school`` (``lat,lng`` or an address) at each use;
        ``kept`` reads every kept place. ``before_request`` runs before each Google request (the
        monthly cap guard) and raises ToolError to stop it.
        """
        self._key = api_key
        self._book = book
        self._saved = saved
        self._kept = kept
        self._before = before_request

    def read(  # noqa: PLR0911 - one return per step of the order the docstring gives.
        self, words: str, ctx: ToolContext, meter: list[str],
        *, here_fix: HereReading | None = None, search: bool = True,
    ) -> Found:
        """The place ``words`` name; ToolError when it cannot be read.

        ``here_fix`` is a reading of Here already taken for the call, so that "here" and the
        search's bias agree with the call's other uses of it. ``search=False`` answers only from
        what needs no Google request (here, a point, his places, a place id this process knows) and
        raises ToolError ``not_found`` otherwise.
        """
        text = words.strip()
        word = _words(text)
        if not word:
            msg = "a place needs words: a name, an address, here, home or school"
            raise ToolError(msg, code="invalid_arguments")
        if word == "here":
            fix = here_fix or read_here(ctx)
            return Found(fix.lat, fix.lng, "here", "here", fix=fix)
        if match := _LAT_LNG.match(text):
            return Found(float(match[1]), float(match[2]), text, "point")
        if kept := self._kept_named(word, ctx, meter, here_fix, search=search):
            return kept
        if word in _SCHOOL_NAMES:
            word = "school"
        if word in ("home", "school"):
            return self._role(word, ctx, meter, here_fix, search=search)
        if (known := self._book.get(text)) is not None:
            return self._from_book(known)
        if _PLACE_ID.match(text):
            return self._by_id(text, ctx, meter, search=search)
        return self._search(text, ctx, meter, here_fix, search=search)

    def _kept_named(
        self, word: str, ctx: ToolContext, meter: list[str], here_fix: HereReading | None,
        *, search: bool,
    ) -> Found | None:
        """The place he keeps under that name: its point, else its address looked up as words."""
        for one in self._kept() if self._kept else ():
            if _words(one.name) != word:
                continue
            if one.lat is not None and one.lng is not None:
                bounds = circle_bounds(one.lat, one.lng, one.radius_m)
                return Found(
                    one.lat, one.lng, one.name, "kept", address=one.address, bounds=bounds,
                )
            if one.address:
                found = self._search(one.address, ctx, meter, here_fix, search=search)
                return Found(
                    found.lat, found.lng, one.name, "kept", address=one.address,
                    place_id=found.place_id, bounds=found.bounds,
                )
        return None

    def _role(
        self, word: str, ctx: ToolContext, meter: list[str], here_fix: HereReading | None,
        *, search: bool,
    ) -> Found:
        """His home or school: a kept point, else a kept address looked up as words."""
        text = self._saved().get(word, "")
        if not text:
            msg = (
                f"His {word} address isn't saved yet; ask him for it and save it with save_place,"
                " or use a place name or address instead"
            )
            raise ToolError(msg, code="not_configured")
        if match := _LAT_LNG.match(text):
            return Found(float(match[1]), float(match[2]), word, "kept")
        found = self._search(text, ctx, meter, here_fix, search=search)
        return Found(
            found.lat, found.lng, word, "kept", address=found.address, place_id=found.place_id,
            bounds=found.bounds,
        )

    def _from_book(self, known: Mapping[str, Any]) -> Found:
        return Found(
            float(known["lat"]), float(known["lng"]), str(known["name"]), "place_id",
            address=known.get("address"), place_id=known.get("place_id"),
        )

    def _by_id(self, place_id: str, ctx: ToolContext, meter: list[str], *, search: bool) -> Found:
        """A place id Google gave earlier that this process no longer remembers."""
        key = self._ask(ctx, place_id, search=search)
        raw = places_api.name_of(key, place_id, meter)
        return self._from_raw(raw, "place_id", words=place_id)

    def _search(
        self, text: str, ctx: ToolContext, meter: list[str], here_fix: HereReading | None,
        *, search: bool,
    ) -> Found:
        """One Text Search near Here, nearest first; unbiased, and saying so, without Here."""
        key = self._ask(ctx, text, search=search)
        fix = here_fix
        if fix is None:
            try:
                fix = read_here(ctx)
            except ToolError:
                fix = None
        body: dict[str, Any] = {"textQuery": text, "maxResultCount": 1, "languageCode": "en"}
        if fix is not None:
            body["rankPreference"] = "DISTANCE"
            body["locationBias"] = places_api.circle(fix.lat, fix.lng, SEARCH_BIAS_M)
        found = places_api.text_search(
            key, body, meter, mask=places_api.READER_MASK, sku=places_api.SKU_TEXT_PRO,
        )
        if not found:
            msg = (
                f"Google found no place for {text!r}; ask him for a street address or a nearby"
                " landmark"
            )
            raise ToolError(msg, code="not_found")
        return self._from_raw(found[0], "search", words=text, unbiased=fix is None)

    def _from_raw(
        self, raw: Mapping[str, Any], how: str, *, words: str, unbiased: bool = False,
    ) -> Found:
        one = places_api.on_map(raw)
        if one is None:
            msg = f"Google gave no point for {words!r}; ask him for a street address"
            raise ToolError(msg, code="not_found")
        self._book.remember(one)
        return Found(
            one["lat"], one["lng"], one["name"] or words, how, address=one["address"],
            place_id=one["place_id"], bounds=places_api.bounds_of(raw), unbiased=unbiased,
        )

    def _ask(self, ctx: ToolContext, words: str, *, search: bool) -> str:
        """The key, once a Google request is allowed and under the cap; else ToolError."""
        if not search:
            msg = f"{words!r} is not a place he keeps; no lookup was made"
            raise ToolError(msg, code="not_found")
        key = self._need_key()
        if self._before is not None:
            self._before(ctx)
        return key

    def _need_key(self) -> str:
        if not self._key:
            msg = "Place lookups are not set up on this machine (no Google key); tell him"
            raise ToolError(msg, code="unavailable")
        return self._key


__all__ = ["Found", "PlaceReader", "circle_bounds"]
