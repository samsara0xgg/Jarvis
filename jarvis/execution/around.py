"""ADR 0284: what is around him, for ``where_am_i`` and for one line of the state block.

Facts only; the model judges. :meth:`Surroundings.lookup` is ``where_am_i``'s: a street and
nearest intersection (one reverse-geocoding request), the places within 300 m with direction,
distance and open now (one Nearby Search), how long he has been at this spot, the kept place he
is in, the past stays there, and the places he may be at. Both requests go through the monthly
cap and are recorded (:mod:`jarvis.execution.maps_spend`).

A reading is held by position: a point within :data:`CELL_M` of a held one, less than
:data:`TTL_S` old, pays nothing. :meth:`Surroundings.line` is the state block's and never makes a
request: it shows what is held for his position, and otherwise only his kept place, or nothing.
A part that failed (the API is not enabled, the cap) is not held, and the next lookup tries it
again; what did come is returned with the reason.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import geocode, maps_spend, places_api
from jarvis.execution.location_tool import describe_age
from jarvis.execution.tools import ToolError
from jarvis.shared.geo import metres
from jarvis.state import places
from jarvis.state.phone_location import fixes_since

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereReading
    from jarvis.state.phone_day import Span

CELL_M: Final = 50.0
"""A held reading answers any point this close to the one it was made at."""
TTL_S: Final = 3 * 3600.0
RADIUS_M: Final = 300
NOTABLE: Final = 10
CANDIDATES: Final = 5
_HELD: Final = 32
_REACH_PAD_M: Final = 30.0
_MIN_ACCURACY_M: Final = 20.0
_PAST_STAY_M: Final = 60.0
_DWELL_LOOKBACK_MS: Final = 6 * 3_600_000
_GUESS_AFTER_MIN: Final = 10
_LINE_CHARS: Final = 160

_NOTE: Final = (
    "These are facts, not a conclusion: you judge which place he is at from how long he has been"
    " here, the kept place, the candidates and what he said, and ask him when unsure. When he"
    " confirms a place that is not kept, keep it with save_place(name=..., where='here')."
)


@dataclass
class _Held:
    lat: float
    lng: float
    at: float
    street: dict[str, str] | None = None
    nearby: list[dict[str, Any]] | None = None


def _nearby(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """One Places API place as held: name, kind, address, point, open now, id."""
    one = places_api.on_map(raw)
    if one is None or not one["name"]:
        return None
    kind = raw.get("primaryTypeDisplayName")
    return {
        "name": one["name"], "lat": one["lat"], "lng": one["lng"], "address": one["address"],
        "kind": kind.get("text") if isinstance(kind, dict) else None,
        "open_now": (raw.get("currentOpeningHours") or {}).get("openNow"),
        "place_id": one["place_id"],
    }


def stay_minutes(
    conn: sqlite3.Connection, point: tuple[float, float], now_ms: int, zone: tzinfo,
) -> int | None:
    """Minutes he has been within a spot's radius of ``point``, as far back as six hours of fixes.

    The unbroken run of the phone's location fixes ending now, and the open stay if it began
    earlier; None when neither puts him there. A phone that stays put sends little, so it is
    at least this long.
    """
    start: int | None = None
    for fix in reversed(fixes_since(conn, now_ms - _DWELL_LOOKBACK_MS)):
        if metres(point, (fix.lat, fix.lng)) > places.SPOT_RADIUS_M:
            break
        start = fix.at_ms
    seen = places.read_stays(conn, now_ms, zone, days=1)
    if seen and seen[-1].end is None and metres(
        point, (seen[-1].fields["lat"], seen[-1].fields["lng"]),
    ) <= places.SPOT_RADIUS_M:
        start = seen[-1].start if start is None else min(start, seen[-1].start)
    return None if start is None else max(0, now_ms - start) // 60_000


def _ranked(
    nearby: list[dict[str, Any]], origin: tuple[float, float], accuracy_m: float,
    past: list[Span] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(candidates, notable)``: the places he may be at, then the other near ones."""
    measured = []
    for one in nearby:
        metres_away, direction = places_api.distance_and_direction(origin, one["lat"], one["lng"])
        measured.append((metres_away, direction, one))
    measured.sort(key=lambda m: m[0])
    reach = max(accuracy_m, _MIN_ACCURACY_M) + _REACH_PAD_M
    candidates: list[dict[str, Any]] = []
    notable: list[dict[str, Any]] = []
    for metres_away, direction, one in measured:
        if metres_away > RADIUS_M:
            continue
        view: dict[str, Any] = {
            "name": one["name"], "kind": one["kind"], "distance_m": metres_away,
            "direction": direction, "open_now": one["open_now"],
        }
        if metres_away <= reach and len(candidates) < CANDIDATES:
            view |= {"address": one["address"], "place_id": one["place_id"]}
            if past is not None:
                view["past_stays"] = sum(
                    1 for s in past
                    if metres((s.fields["lat"], s.fields["lng"]), (one["lat"], one["lng"]))
                    <= _PAST_STAY_M
                )
            candidates.append(view)
        elif len(notable) < NOTABLE:
            notable.append(view)
    return (
        [{k: v for k, v in c.items() if v is not None} for c in candidates],
        [{k: v for k, v in n.items() if v is not None} for n in notable],
    )


class Surroundings:
    """The held readings by position, and the two ways to read them."""

    def __init__(
        self, api_key: str | None, cap_usd: float, memory_db: Path | None, zone: tzinfo,
    ) -> None:
        """``api_key`` None is allowed: a lookup then returns the free parts and says why not."""
        self._key = api_key
        self._cap = cap_usd
        self._memory_db = memory_db
        self._zone = zone
        self._held: list[_Held] = []
        self._lock = threading.Lock()

    def _find(self, lat: float, lng: float, now: float) -> _Held | None:
        with self._lock:
            self._held = [h for h in self._held if now - h.at < TTL_S]
            near = [h for h in self._held if metres((lat, lng), (h.lat, h.lng)) <= CELL_M]
            return min(near, key=lambda h: metres((lat, lng), (h.lat, h.lng)), default=None)

    def _hold(self, lat: float, lng: float, now: float) -> _Held:
        held = _Held(lat, lng, now)
        with self._lock:
            self._held = [*self._held[-(_HELD - 1):], held]
        return held

    def _key_or_raise(self) -> str:
        if not self._key:
            msg = (
                "There is no Google key, so the street and the places around him cannot be"
                " looked up; tell him. Do not guess them."
            )
            raise ToolError(msg, code="places_not_enabled")
        return self._key

    def _street(self, held: _Held, ctx: ToolContext, meter: list[str]) -> None:
        key = self._key_or_raise()
        maps_spend.guard(ctx.conn, self._cap, ctx.clock.now())
        try:
            held.street = geocode.reverse(held.lat, held.lng, key)
        except geocode.Unplaced as exc:
            msg = (
                f"The street could not be looked up (Google Geocoding API: {exc}); tell him the"
                " Geocoding API may not be enabled. Do not guess the street."
            )
            raise ToolError(msg, code="geocoding_unavailable") from None
        meter.append("geocoding.reverse")

    def _places(self, held: _Held, ctx: ToolContext, meter: list[str]) -> None:
        key = self._key_or_raise()
        maps_spend.guard(ctx.conn, self._cap, ctx.clock.now())
        found = places_api.nearby_around(
            key,
            {
                "maxResultCount": 20, "languageCode": "en", "rankPreference": "DISTANCE",
                "locationRestriction": places_api.circle(held.lat, held.lng, RADIUS_M),
            },
            meter,
        )
        held.nearby = [one for raw in found if (one := _nearby(raw)) is not None]

    def lookup(self, ctx: ToolContext, fix: HereReading) -> dict[str, Any]:
        """Everything around the fix; what could not be got is named in ``unavailable``."""
        point = (fix.lat, fix.lng)
        now = ctx.clock.time()
        held = self._find(*point, now) or self._hold(*point, now)
        looked_up = describe_age(int(now - held.at))
        unavailable: list[str] = []
        meter: list[str] = []
        try:
            for missing, fill in (
                (held.street is None, self._street), (held.nearby is None, self._places),
            ):
                if not missing:
                    continue
                try:
                    fill(held, ctx, meter)
                except ToolError as exc:
                    unavailable.append(str(exc))
        finally:
            maps_spend.record(ctx, meter)
        now_ms = int(now * 1000)
        kept_all = [] if self._memory_db is None else places.read_kept(self._memory_db)
        seen = places.read_stays(ctx.conn, now_ms, self._zone)
        counted = places.tally(seen, kept_all, now_ms, self._zone)
        inside = places.containing(kept_all, *point)
        out: dict[str, Any] = dict(held.street or {})
        stayed = stay_minutes(ctx.conn, point, now_ms, self._zone)
        if stayed is not None:
            out["here_for_min"] = stayed
        if inside is not None:
            out["kept_place"] = places.place_view(inside, counted.kept[inside.id])
        else:
            spot = next(
                (s for s in counted.spots if metres(point, (s.lat, s.lng)) <= places.SPOT_RADIUS_M),
                None,
            )
            if spot is not None:
                out["unkept_spot"] = places.fact_view(spot.facts)
        if held.nearby is not None:
            out["candidates"], out["nearby"] = _ranked(
                held.nearby, point, fix.accuracy_m, seen,
            )
        if held.street is not None or held.nearby is not None:
            out["looked_up"] = looked_up if looked_up != "under a minute" else "just now"
        if unavailable:
            out["unavailable"] = unavailable
        out["note"] = _NOTE
        return out

    def line(
        self, conn: sqlite3.Connection, point: tuple[float, float], accuracy_m: float, now_ms: int,
    ) -> str | None:
        """The state block's line for ``point``; no request is ever made.

        The street or intersection and the place he is at, from what is held for this position:
        his kept place, else the nearest place when he has stayed here for ten minutes. Where
        nothing is held, only the kept place; None when there is not even that.
        """
        kept_all = [] if self._memory_db is None else places.read_kept(self._memory_db)
        inside = places.containing(kept_all, *point)
        held = self._find(*point, now_ms / 1000)
        street = None if held is None or not held.street else (
            held.street.get("intersection") or held.street.get("address")
            or held.street.get("street")
        )
        where = None if inside is None else f"at {inside.name}"
        if (
            where is None and held is not None and held.nearby
            and (stayed := stay_minutes(conn, point, now_ms, self._zone)) is not None
            and stayed >= _GUESS_AFTER_MIN
        ):
            near, _ = _ranked(held.nearby, point, accuracy_m, None)
            if near:
                where = f"maybe at {near[0]['name']}"
        parts = [p for p in (street, where) if p]
        return f"Around him: {'; '.join(parts)}."[:_LINE_CHARS] if parts else None


__all__ = ["Surroundings", "stay_minutes"]
