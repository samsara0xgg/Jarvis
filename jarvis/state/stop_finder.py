"""One way to find a bus stop: by its name or code, or by a point (ADR 0344).

A name matches by its words, in any order: ``at``, ``&`` and ``and`` are the same and dropped, and
``St``/``Street``, ``Ave``/``Avenue``, ``Rd``/``Road``, ``Dr``/``Drive`` (and the like) are the
same word. A code (the number on the pole) matches exactly. A place's stops are those within
:data:`NEAR_M` of it, nearest first; with none, the nearest stop at any distance, with that
distance. Stop objects only need a ``name``; the timetable store (:mod:`jarvis.state.timetable`)
supplies the rest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from jarvis.shared.geo import metres

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from jarvis.state.timetable import Stop, Timetable

NEAR_M: Final = 300.0
"""A place's stop is no farther than this from it."""
_WIDENING_M: Final = (1_000.0, 3_000.0, 10_000.0, 50_000.0)
_ALIKE: Final = {
    "st": "street", "ave": "avenue", "av": "avenue", "rd": "road", "dr": "drive",
    "blvd": "boulevard", "hwy": "highway", "ln": "lane", "ct": "court", "pl": "place",
    "cres": "crescent", "pkwy": "parkway",
}
_SAME_AS_AT: Final = frozenset({"at", "and", "@"})
_CODE: Final = re.compile(r"^\d{3,}$")


class Named(Protocol):
    """Anything with a stop's name."""

    @property
    def name(self) -> str:
        """The stop's name as BC Transit writes it."""
        ...


def words(text: str) -> frozenset[str]:
    """The words of a stop name or of what he said: lower case, alike words one, noise dropped."""
    tokens = re.findall(r"[a-z0-9@]+", text.casefold().replace("&", " and "))
    return frozenset(_ALIKE.get(t, t) for t in tokens if t not in _SAME_AS_AT)


def matching[T: Named](items: Sequence[T], query: str, name_of: Callable[[T], str]) -> list[T]:
    """The items whose name holds every word of ``query``, those with the fewest others first."""
    asked = words(query)
    if not asked:
        return []
    hits = [(len(have - asked), item) for item in items if asked <= (have := words(name_of(item)))]
    return [item for _, item in sorted(hits, key=lambda h: h[0])]


@dataclass(frozen=True)
class StopHit:
    """A stop, how far it is from the point asked about (None: no point), and what serves it."""

    stop: Stop
    metres: int | None
    serves: tuple[tuple[str, str], ...]
    """``(route, headsign)`` of the buses that call here."""


@dataclass(frozen=True)
class Nearby:
    """The stops of a place: those within :data:`NEAR_M`, else the nearest one."""

    hits: list[StopHit]
    nearest: StopHit | None
    """Set when ``hits`` is empty and the timetable has any stop at all."""


def _hit(table: Timetable, stop: Stop, point: tuple[float, float] | None) -> StopHit:
    return StopHit(
        stop, None if point is None else round(metres(point, (stop.lat, stop.lng))),
        tuple(table.serves(stop.stop_id)),
    )


def by_name(
    table: Timetable, text: str, near: tuple[float, float] | None = None,
) -> list[StopHit]:
    """The stops he named: a code exactly, else the name's words; nearest to ``near`` first."""
    asked = text.strip()
    found = table.stops_by_code(asked) if _CODE.match(asked) else []
    if not found:
        found = matching(table.all_stops(), asked, lambda s: s.name)
    hits = [_hit(table, s, near) for s in found]
    if near is not None:
        hits.sort(key=lambda h: h.metres if h.metres is not None else 0)
    return hits


def by_place(table: Timetable, point: tuple[float, float], radius_m: float = NEAR_M) -> Nearby:
    """The stops within ``radius_m`` of ``point``, nearest first; else the nearest anywhere."""
    close = table.stops_near(point, radius_m)
    if close:
        return Nearby([_hit(table, s, point) for s, _ in close], None)
    for wider in _WIDENING_M:
        far = table.stops_near(point, wider)
        if far:
            return Nearby([], _hit(table, far[0][0], point))
    return Nearby([], None)


def serving(hits: Sequence[StopHit], route: str) -> list[StopHit]:
    """The hits whose stop a bus of ``route`` calls at."""
    return [h for h in hits if any(r.casefold() == route.casefold() for r, _ in h.serves)]
