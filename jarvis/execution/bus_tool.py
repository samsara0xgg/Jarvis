"""ADR 0246: everything about his bus is one tool, `bus`, and the server remembers the rest.

It answers the questions the model used to spread over `transit`, `pin_departure`,
`watch_for_boarding`, `start_ride`, `set_ride_stop` and `end_ride`: no arguments say how the ride he
is on is going (else the buses home), `to` lists numbered options, `stop` (or a `route` alone) lists
the next buses at a stop (ADR 0344), `pin` puts a listed option on the notch, `ride` starts, watches
for, ends or sets the stop of a ride. The last list
(:class:`~jarvis.execution.transit_tool.TransitOffers`) and the ride (``jarvis.runtime.rides``) are
state the server holds, so no call copies a field from an earlier result. The runtime hands the ride
in as a :class:`RideControl` of plain callables, as it hands the planner its live departure.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import maps_spend, place_reader, places_api, transit_tool
from jarvis.execution.location_tool import read_here
from jarvis.execution.tools import Receipted, Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal
from jarvis.shared.geo import metres
from jarvis.shared.receipts import Covered, Receipt
from jarvis.shared.rounding import departure_ms
from jarvis.state import departures, stop_finder, timetable

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.execution.transit_tool import BusTimes, TransitOffers
    from jarvis.shared.here import HereReading
    from jarvis.state.places import Place


@dataclass(frozen=True)
class RideControl:
    """What the runtime does for the tool; each raises ``ToolError`` for a refusal.

    ``start`` and ``set_stop`` take a ``place`` that reads, only when it is needed, where he said
    he is going (the tool's place reader, ADR 0346) and ``here``, his position when it is fresh.
    """

    watch: Callable[[str], dict[str, Any]]
    start: Callable[
        [str | None, str | None, str | None, HereReading | None,
         Callable[[], tuple[float, float] | None]],
        dict[str, Any],
    ]
    set_stop: Callable[
        [str, HereReading | None, Callable[[], tuple[float, float] | None]], dict[str, Any],
    ]
    end: Callable[[], dict[str, Any]]
    status: Callable[[], dict[str, Any] | None]
    """The ride he is on as the runtime describes it, None when he is not on one."""


_DESCRIPTION: Final = (
    "Use for anything about the bus: when it comes, getting somewhere by bus, the next buses at a"
    " stop, pinning a trip, the bus he is on. Not for where he is (where_am_i) or finding places"
    " (map_search). Returns the buses with leave time, walk, arrival and whether each time is live"
    " or scheduled; with no arguments the ride he is on, else the buses home."
)
_FROM: Final = "his current location"
_RIDES: Final = ("start", "watch", "end", "get_off")
_KEYS: Final = frozenset(
    {"to", "at", "route", "stop", "pin", "ride", "from", "arrive_by", "option", "vehicle_id"},
)
_MOVED: Final = {
    "get_off": 'ride="get_off", stop=<stop>',
    "day": "at=, which takes a local date and time such as 2026-10-22 09:00",
    "place_id": "to=, which takes a place_id from map_search",
}
_HOME = "home"
_RIDE_ONLY: Final = {"option": "ride=start", "vehicle_id": "ride=start"}
_WRITES: Final = ("pin", "ride")
"""The arguments that change something; a call with none of them only reads (ADR 0319)."""
_DROPPED: Final = frozenset({
    "ride_id", "eta_ms", "started_at_ms", "warned", "note", "event_uid", "watch_id",
    "alight_lat", "alight_lng",
})
"""Fields of the ride follower's answers that are the program's, or that the sentence says."""
_BOARD_WINDOW: Final = timedelta(hours=3)
_BOARD_WIDE: Final = timedelta(hours=24)
_PER_DIRECTION: Final = 2
_NEAR_ROUTE_M: Final = 300.0
_ROUTE_REACH_M: Final = (1_000.0, 3_000.0, 10_000.0)
_KIND_PIN: Final = "pin"


def _schema(here: str) -> dict[str, Any]:
    """The arguments; ``here`` says whose location 'here' is (ADR 0194, 0198, 0212)."""
    return {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Where to: home (default), school, a place or address, or a"
                " place_id from map_search.",
            },
            "at": {
                "type": "string",
                "description": "When to leave: a local time like 09:00, or a local date and time"
                " like 2026-10-22 09:00. Default now.",
            },
            "route": {
                "type": "string",
                "description": "A bus number he names ('39', '26A'). With to: can he catch that"
                " bus. Alone or with stop: its next buses near him or at the stop. With"
                " ride=start: the bus he boarded.",
            },
            "stop": {
                "type": "string",
                "description": "A stop by name or code ('Fort & Cook', '10001'): the next buses"
                " there. With ride=get_off: the stop he gets off at.",
            },
            "ride": {
                "type": "string",
                "enum": list(_RIDES),
                "description": "start: he boarded. watch: he is heading out, needs to. end: he"
                " got off. get_off: his stop, needs stop.",
            },
            "pin": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": departures.MAX_TRIPS,
                "description": "Option numbers of the last list to put on the notch, e.g. [1] or"
                " [1,2].",
            },
            "from": {
                "type": "string",
                "description": f"Where he starts if not here: a place or address. Default {here}.",
            },
            "arrive_by": {
                "type": "string",
                "description": "When he must be there, in the same forms as at. Not with at.",
            },
            "option": {
                "type": "integer",
                "description": "With ride=start: the number of the listed option he took.",
            },
            "vehicle_id": {
                "type": "string",
                "description": "With ride=start: the vehicle_id of the candidate he picked.",
            },
        },
        "additionalProperties": False,
    }


def _text(value: object, limit: int = 80) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _bad(message: str) -> ToolError:
    return ToolError(f"bus: {message}", code="invalid_arguments")


def _number(value: object, name: str) -> int:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        msg = f"{name} must be a whole number from 1, e.g. {name}=1"
        raise _bad(msg)
    return value


def _numbers(value: object) -> list[int]:
    """``pin`` as a list of distinct option numbers: ``1`` and ``[1]`` are the same."""
    many = value if isinstance(value, list) else [value]
    numbers = list(dict.fromkeys(_number(one, "pin") for one in many))
    if not numbers or len(numbers) > departures.MAX_TRIPS:
        msg = f"pin takes one to {departures.MAX_TRIPS} option numbers, e.g. pin=[1] or pin=[1,2]"
        raise _bad(msg)
    return numbers


def sources_said(*, position_live: bool, arrival_live: bool, has_arrival: bool) -> str:
    """Where the ride's two facts come from, in words: the bus's place and his arrival, ADR 0261."""
    if not position_live:
        also = " and when he arrives" if has_arrival else ""
        return (
            f"There is no live bus position now: the timetable gives where the bus is{also},"
            " and may be off."
        )
    if not has_arrival:
        return "The bus position is live from BC Transit."
    if arrival_live:
        return "The bus position and the arrival at his stop are live from BC Transit."
    return (
        "The bus position is live from BC Transit; the arrival at his stop is a timetable"
        " estimate and may be off by a few minutes."
    )


def _the_bus(ride: Mapping[str, Any]) -> str:
    """The bus by its route and where it is going: ``the 7 toward Harbour``."""
    heading = f" toward {ride['headsign']}" if ride.get("headsign") else ""
    return f"the {ride['route']}{heading}"


def _ride_line(ride: Mapping[str, Any], now: datetime) -> str:
    """How the ride is going, in words: the bus, the next stop, his stop and when."""
    text = f"On {_the_bus(ride)}"
    if ride.get("board_stop"):
        text += f" from {ride['board_stop']}"
    text += f"; next stop {ride['next_stop']}." if ride.get("next_stop") else "."
    stop = ride.get("alight_stop")
    if not stop:
        return text + " Where he gets off is not set; bus(ride='get_off', stop=<stop>) sets it."
    text += f" He gets off at {stop}"
    if ride.get("eta_ms") is not None and ride.get("stops_left") is not None:
        left = ride["stops_left"]
        text += f": {left} stop{'' if left == 1 else 's'} left, about"
        text += f" {transit_tool.said_ms(ride['eta_ms'], now, soon=True)}."
    else:
        text += "."
    position = bool(ride.get("position_live"))
    if ride.get("next_stop") is None and not position:
        return text + " There is no bus data for this trip now: say you cannot give times."
    return text + " " + sources_said(
        position_live=position, arrival_live=bool(ride.get("arrival_live")),
        has_arrival=ride.get("eta_ms") is not None,
    )


def _reads(args: Mapping[str, Any]) -> bool:
    """Whether a ``bus`` call only reads: times, a board, or the ride as it stands."""
    return not any(args.get(name) not in (None, "") for name in _WRITES)


def _ride_view(summary: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """A ride tool's answer: the sentence first, then what the program told, minus its own ids."""
    return {"summary": summary, **{k: v for k, v in raw.items() if k not in _DROPPED}}


def _short(identifier: str) -> str:
    """The short id she sees: the part of ``ride-3fa91c02`` after the dash."""
    return identifier.rsplit("-", 1)[-1]


def _needs_choice(view: dict[str, Any], line: str, *, reads_only: bool = False) -> Receipted:
    """A call that stopped to ask which of several candidates he means (ADR 0319)."""
    return Receipted(view, Receipt("needs_choice", reads_only=reads_only, line=line))


def _read(answer: dict[str, Any], line: str, covered: Covered) -> Receipted:
    return Receipted(answer, Receipt("done", reads_only=True, line=line, covered=covered))


def _ride_write(answer: dict[str, Any], raw: Mapping[str, Any], line: str) -> Receipted:
    """A ride write with its receipt: the ride it is about and the event an undo reverses."""
    ride_id = raw.get("ride_id")
    return Receipted(answer, Receipt(
        "done", reads_only=False, line=line,
        object=f"ride:{_short(str(ride_id))}" if ride_id else None, undo=raw.get("event_uid"),
    ))


class _Bus:
    """The tool's handler: dispatch on the arguments to a plan, a board, a pin or a ride."""

    def __init__(  # noqa: PLR0913 - the planner, the list, the ride, the places and the cap.
        self,
        plan: Callable[[Mapping[str, Any], ToolContext], dict[str, Any]] | None,
        offers: TransitOffers,
        control: RideControl | None,
        bus_live: BusTimes | None,
        reader: place_reader.PlaceReader,
        cap_usd: float,
    ) -> None:
        self._plan = plan
        self._offers = offers
        self._control = control
        self._bus = bus_live
        self._reader = reader
        self._cap = cap_usd

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Which of the things was asked, then do it; mixing them is an error."""
        given = {k: v for k, v in args.items() if v is not None and v != ""}
        if unknown := sorted(set(given) - _KEYS):
            moved = "; ".join(f"{name} is {_MOVED[name]}" for name in unknown if name in _MOVED)
            msg = (
                f"{', '.join(unknown)} is not an argument. The arguments are"
                f" {', '.join(sorted(_KEYS))}" + (f" ({moved})" if moved else "")
            )
            raise _bad(msg)
        if "ride" in given:
            return self._ride(given, ctx)
        if "pin" in given:
            self._alone(given, "pin")
            return self._pin(_numbers(given["pin"]), ctx)
        for name, needs in _RIDE_ONLY.items():
            if name in given:
                msg = f"{name} goes with {needs}"
                raise _bad(msg)
        if self._wants_board(given):
            return self._board(given, ctx)
        if given:
            return self._times(given, ctx)
        ride = self._control.status() if self._control is not None else None
        if ride is not None:
            return self._riding(ride, ctx)
        return self._times({}, ctx)

    @staticmethod
    def _wants_board(given: Mapping[str, Any]) -> bool:
        """A stop, or a route with no destination, asks for the next buses at a stop."""
        if "stop" in given:
            if {"to", "from", "arrive_by"} & set(given):
                msg = "stop= asks for the buses at a stop; to= asks for a trip, so not both"
                raise _bad(msg)
            return True
        return "route" in given and not ({"to", "from", "arrive_by"} & set(given))

    @staticmethod
    def _alone(given: Mapping[str, Any], name: str) -> None:
        if len(given) > 1:
            msg = f"{name} stands alone; for times use to=..., for a ride use ride=..."
            raise _bad(msg)

    def _riding(self, ride: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """How the ride is going; with no stop set, where the last plan lets this bus off."""
        text = _ride_line(ride, ctx.clock.now())
        if not ride.get("alight_stop") and (
            hit := self._offers.alight_for(str(ride["route"]))
        ):
            text += (
                f" The last plan, to {self._offers.to}, lets him off the {ride['route']} at"
                f" {hit[0]}; set it with bus(ride='get_off', stop=...) if he takes that."
            )
        return _read(
            _ride_view(text, ride), f"read the ride on the {ride['route']}",
            Covered(("BC Transit live", "BC Transit timetable"), "the ride now", 1, 0),
        )

    # --- times -------------------------------------------------------------------------------

    def _sources(self) -> tuple[str, ...]:
        return ("Google Routes", "BC Transit timetable", "BC Transit live")

    def _times(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Up to three numbered options, from Google Routes; the list is kept for ``pin``."""
        if (heard := self._asked_of_ride(given, ctx)) is not None:
            return heard
        if self._plan is None:
            msg = (
                "Bus times are not set up on this machine (no Google Routes key); tell him they"
                " cannot be read."
            )
            raise ToolError(msg, code="unavailable")
        asked: dict[str, Any] = {
            "origin": _text(given.get("from")) or "here",
            "destination": _text(given.get("to")) or _HOME,
        }
        for name, key in (("at", "depart_at"), ("arrive_by", "arrive_by")):
            if value := _text(given.get(name)):
                asked[key] = value
        if route := _text(given.get("route"), 8):
            asked["route"] = route
        raw = self._plan(asked, ctx)
        window = str(raw.get("at") or raw.get("arrive_by") or "now")
        shown = len(raw["options"])
        return _read(
            raw, f"read the buses to {asked['destination']}",
            Covered(self._sources(), f"buses from {window}", shown, 0),
        )

    def _asked_of_ride(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted | None:
        """A times question whose destination is the ride's own stop: the ride answers (ADR 0344).

        No Google request: the stop's name, the place he said when he boarded or a place of his
        own (home, a kept one) that is within a walk of his stop is his destination.
        """
        ride = self._control.status() if self._control is not None else None
        words = _text(given.get("to"))
        if ride is None or not words or {"at", "arrive_by", "route", "from"} & set(given):
            return None
        stop = ride.get("alight_stop")
        if not stop:
            return None
        lat, lng = ride.get("alight_lat"), ride.get("alight_lng")
        mine = stop_finder.words(words)
        by_name = bool(mine) and (
            mine <= stop_finder.words(str(stop))
            or words.casefold() == str(ride.get("destination") or "").casefold()
        )
        walk: int | None = None
        if not by_name:
            try:
                found = self._reader.read(words, ctx, [], search=False)
            except ToolError:
                return None
            if lat is None or lng is None or metres(found.point, (lat, lng)) > stop_finder.NEAR_M:
                return None
            walk = round(metres(found.point, (lat, lng)))
        now = ctx.clock.now()
        line = _ride_line(ride, now)
        if walk is not None:
            line += f" From there it is about {walk} m on foot to {words}, in a straight line."
        return _read(
            _ride_view(line, ride), f"read the ride toward {words}",
            Covered(("BC Transit live", "BC Transit timetable"), "the ride now", 1, 0),
        )

    # --- the next buses at a stop ----------------------------------------------------------------

    def _board(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """The next buses at a stop he names, or at the stops near him that serve a route."""
        if self._bus is None:
            msg = "Bus timetables are not set up on this machine; tell him they cannot be read."
            raise ToolError(msg, code="unavailable")
        table = self._bus.table()
        now = ctx.clock.now()
        route = _text(given.get("route"), 8)
        words = _text(given.get("stop"))
        try:
            fix: HereReading | None = read_here(ctx)
        except ToolError:
            fix = None
        near = None if fix is None else (fix.lat, fix.lng)
        if words:
            hits = stop_finder.by_name(table, words, near)
            if not hits:
                msg = (
                    f"No stop matches {words!r}; ask him for the street names of the corner or"
                    " the number on the pole"
                )
                raise ToolError(msg, code="not_found")
            names = list(dict.fromkeys(h.stop.name for h in hits))
            if len(names) > 1:
                return self._which_stop(hits, names)
            hits = hits[:3]
        else:
            if fix is None:
                msg = "His location could not be read; ask him where he is, or for a stop's name"
                raise ToolError(msg, code="location_unavailable")
            hits = self._stops_for(table, (fix.lat, fix.lng), str(route))
        start = transit_tool_local(given.get("at"), now)
        return self._buses(table, hits, route, start, now)

    def _which_stop(self, hits: list[stop_finder.StopHit], names: list[str]) -> Receipted:
        best = {h.stop.name: h for h in reversed(hits)}
        shown = [
            {"stop": n, **({"metres": best[n].metres} if best[n].metres is not None else {})}
            for n in names[:5]
        ]
        view = {
            "summary": (
                f"More than one stop matches: {', '.join(names[:5])}. Ask him which, then call"
                " bus(stop=...) with that stop's name."
            ),
            "candidates": shown,
        }
        line = "More than one stop matches; no buses were read"
        return _needs_choice(view, line, reads_only=True)

    def _stops_for(
        self, table: Any, point: tuple[float, float], route: str,  # noqa: ANN401
    ) -> list[stop_finder.StopHit]:
        """The stops within 300 m of him that serve ``route``; ToolError naming the nearest else."""
        near = stop_finder.by_place(table, point, _NEAR_ROUTE_M)
        found = stop_finder.serving(near.hits, route)
        if found:
            return found[:3]
        for reach in _ROUTE_REACH_M:
            wider = stop_finder.serving(stop_finder.by_place(table, point, reach).hits, route)
            if wider:
                msg = (
                    f"No stop of the {route} is within {round(_NEAR_ROUTE_M)} m of him; the"
                    f" nearest is {wider[0].stop.name}, {wider[0].metres} m away. Offer that one"
                    " with bus(stop=...)"
                )
                raise ToolError(msg, code="not_found")
        msg = f"No stop of the {route} was found near him"
        raise ToolError(msg, code="not_found")

    def _buses(
        self, table: Any, hits: list[stop_finder.StopHit], route: str | None,  # noqa: ANN401
        start: datetime, now: datetime,
    ) -> Receipted:
        assert self._bus is not None  # noqa: S101 - `_board` raised without a timetable.
        try:
            feed = self._bus.snapshot()
        except Exception:  # noqa: BLE001 - the schedule alone still names the next buses.
            feed = None
        start_ms = int(start.timestamp() * 1000)
        shown: list[dict[str, Any]] = []
        total = 0
        searched = _BOARD_WINDOW
        for hit in hits:
            for window in (_BOARD_WINDOW, _BOARD_WIDE):
                # A bus scheduled a little before the start may be late and still to come.
                calls = table.calls_at(
                    hit.stop.stop_id, start_ms - timetable.LIVE_SLACK_MS,
                    start_ms + int(window.total_seconds() * 1000), route,
                )
                buses = self._per_direction(calls, feed, start)
                if len(buses) >= 2 * _PER_DIRECTION:
                    break
            searched = max(searched, window)
            total += len(buses)
            shown.append({
                "stop": hit.stop.name,
                **({"code": hit.stop.code} if hit.stop.code else {}),
                **({"metres": hit.metres} if hit.metres is not None else {}),
                "buses": [self._bus_view(b, now) for b in buses],
            })
        hours = int(searched.total_seconds() // 3600)
        label = f"the next {hours} h from {transit_tool.said(start, now)}"
        lines = [self._stop_line(s) for s in shown]
        summary = " ".join(lines) if lines else "No buses were found."
        if total == 0:
            summary = f"No buses at {shown[0]['stop'] if shown else 'that stop'} in {label}."
        return _read(
            {"summary": summary, "stops": shown}, f"read the next buses at {len(shown)} stop(s)",
            Covered(("BC Transit timetable", "BC Transit live"), label, total, 0),
        )

    def _per_direction(
        self, calls: list[Any], feed: Any, start: datetime,  # noqa: ANN401
    ) -> list[dict[str, Any]]:
        """Two buses per direction, each with its one time: the live prediction, else scheduled."""
        timed = []
        for call in calls:
            live = None
            if feed is not None and self._bus is not None:
                live = self._bus.live_at(feed, call)
            when = departure_ms(live if live is not None else call.depart_ms)
            if when >= departure_ms(int(start.timestamp() * 1000)):
                timed.append({
                    "call": call, "ms": when, "source": "live" if live is not None else "scheduled",
                })
        timed.sort(key=lambda t: t["ms"])
        counts: dict[int, int] = {}
        kept = []
        for one in timed:
            direction = one["call"].direction
            if counts.get(direction, 0) < _PER_DIRECTION:
                counts[direction] = counts.get(direction, 0) + 1
                kept.append(one)
        return kept

    @staticmethod
    def _bus_view(one: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        call = one["call"]
        at = datetime.fromtimestamp(one["ms"] / 1000, now.tzinfo)
        return {
            "route": call.route, "headsign": call.headsign,
            "departs": transit_tool.said(at, now), "source": one["source"],
            "in_min": max(0, math.floor((at - now).total_seconds() / 60)),
        }

    @staticmethod
    def _stop_line(stop: Mapping[str, Any]) -> str:
        far = f" ({stop['metres']} m away)" if "metres" in stop else ""
        if not stop["buses"]:
            return f"No buses are listed at {stop['stop']}{far}."
        buses = "; ".join(
            f"the {b['route']} toward {b['headsign'] or '?'} at {b['departs']} ({b['source']})"
            for b in stop["buses"]
        )
        return f"Next at {stop['stop']}{far}: {buses}."

    # --- the pin -----------------------------------------------------------------------------

    def _pin(self, numbers: list[int], ctx: ToolContext) -> Receipted:
        now = ctx.clock.now()
        trips, new, made = transit_tool.pin_listed(
            ctx.conn, self._offers, numbers, action_id=ctx.action_id, now=now,
            source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
        )
        said = "; ".join(
            f"the {t.route} from {t.board_stop}: leave"
            f" {transit_tool.said_ms(t.leave_at_ms, now, soon=True)}, bus at"
            f" {transit_tool.said_ms(t.departs_at_ms, now)}"
            for t in trips
        )
        summary = (
            f"Pinned on the notch: {said}. It rings at the leave time." if new
            else f"Already pinned, nothing changed: {said}."
        )
        answer = {
            "summary": summary,
            "pinned": True,
            "already_pinned": not new,
            "trips": [
                {
                    "route": t.route,
                    "board_stop": t.board_stop,
                    "leave": transit_tool.said_ms(t.leave_at_ms, now, soon=True),
                    "departs": transit_tool.said_ms(t.departs_at_ms, now),
                }
                for t in trips
            ],
        }
        return Receipted(answer, Receipt(
            "done", reads_only=False, line=summary,
            object=f"{_KIND_PIN}:{_short(made['pin_id'])}",
            undo=made["event_uid"] if new else None,
        ))

    # --- the ride ----------------------------------------------------------------------------

    def _rides(self) -> RideControl:
        if self._control is None:
            msg = "Following a bus ride is not set up on this machine; tell him."
            raise ToolError(msg, code="unavailable")
        return self._control

    def _ride(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        what = given["ride"]
        if what not in _RIDES:
            msg = f"ride must be start, watch, end or get_off, not {what!r}"
            raise _bad(msg)
        if what == "start":
            return self._start(given, ctx)
        extra = set(given) - {"ride", "to", "stop"}
        if extra or (what == "end" and ({"to", "stop"} & set(given))):
            msg = f"ride={what} takes {_TAKES[what]}"
            raise _bad(msg)
        if what == "end":
            return self._end()
        if what == "get_off":
            if "to" in given or "stop" not in given:
                msg = "ride=get_off takes stop=<the stop he gets off at>"
                raise _bad(msg)
            return self._get_off(str(given["stop"]), ctx)
        if "stop" in given:
            msg = "ride=watch takes to= only"
            raise _bad(msg)
        return self._watch(_text(given.get("to")))

    def _watch(self, place: str | None) -> Receipted:
        if place is None:
            msg = "ride=watch needs to=<where he is going>, as he said it"
            raise _bad(msg)
        raw = self._rides().watch(place)
        if not raw.get("watching"):
            return Receipted(
                _ride_view("He is already on a bus ride, so there is nothing to watch for.", raw),
                Receipt("done", reads_only=False, line="already on a ride; no watch set"),
            )
        summary = (
            f"Watching for him to board a bus to {place} for the next {raw.get('minutes')}"
            " minutes. If his phone moves with a bus, he is asked on his phone whether he boarded."
        )
        return Receipted(_ride_view(summary, raw), Receipt(
            "done", reads_only=False, line=summary,
            object=f"watch:{_short(str(raw['watch_id']))}" if raw.get("watch_id") else None,
            undo=raw.get("event_uid"),
        ))

    def _place(
        self, ctx: ToolContext, words: str | None,
    ) -> Callable[[], tuple[float, float] | None]:
        """A reader of where he said he is going, run only when the ride needs it."""
        cache: list[tuple[float, float] | None] = []

        def point() -> tuple[float, float] | None:
            if not words:
                return None
            if not cache:
                meter: list[str] = []
                try:
                    cache.append(self._reader.read(words, ctx, meter).point)
                finally:
                    maps_spend.record(ctx, meter)
            return cache[0]

        return point

    @staticmethod
    def _fresh_here(ctx: ToolContext) -> HereReading | None:
        try:
            fix: HereReading | None = read_here(ctx)
        except ToolError:
            return None  # no report: the ride is found by the pin or the route he names
        if fix is not None and fix.why:
            return None  # a report no fresh fix confirmed does not say which bus he is on
        return fix

    def _start(self, given: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        if {"from", "at", "pin", "stop", "arrive_by"} & set(given):
            msg = "ride=start takes option= or route=, and to= and vehicle_id= if needed"
            raise _bad(msg)
        if "option" in given and "route" in given:
            msg = "pass option=<number> or route=<bus number>, not both"
            raise _bad(msg)
        route, place = _text(given.get("route"), 8), _text(given.get("to"))
        if "option" in given:
            row = self._offers.listed_row(_number(given["option"], "option"))
            route, place = str(row["route"]).split("→")[0].strip(), str(row["to"])
        rides = self._rides()
        raw = rides.start(
            route, place, _text(given.get("vehicle_id"), 64), self._fresh_here(ctx),
            self._place(ctx, place),
        )
        if raw.get("ambiguous"):
            names = " or ".join(
                f"the {c['route']} toward {c.get('headsign') or '?'}"
                f" (next stop {c.get('next_stop') or 'unknown'})"
                for c in raw["candidates"]
            )
            summary = (
                f"Buses are about equally near him: {names}. Ask him which, then call"
                " bus(ride='start', route=..., vehicle_id=...) with that candidate's vehicle_id."
            )
            return _needs_choice(
                _ride_view(summary, raw), "Buses are about equally near; not following one yet",
            )
        lead = "Already following this ride; nothing changed. " if raw.get("already_riding") else ""
        line = lead + _ride_line(raw, ctx.clock.now())
        return _ride_write(_ride_view(line, raw), raw, line)

    def _get_off(self, stop: str, ctx: ToolContext) -> Receipted:
        raw = self._rides().set_stop(
            stop.strip()[:80], self._fresh_here(ctx), self._place(ctx, stop.strip()[:80]),
        )
        if raw.get("ambiguous"):
            summary = (
                f"More than one stop matches: {', '.join(raw['candidates'])}. Ask him which, then"
                " call bus(ride='get_off', stop=...) with that stop's name."
            )
            return _needs_choice(
                _ride_view(summary, raw), "More than one stop matches; the get-off stop is not set",
            )
        lead = "Already set to this stop, nothing changed." if raw.get("already") else "Set."
        if raw.get("moved"):
            lead = (
                f"The ride was on the wrong bus: it now follows the one toward"
                f" {raw.get('headsign') or 'its end'}, not the one toward {raw['moved']}. Set."
            )
        line = f"{lead} {_ride_line(raw, ctx.clock.now())} He is warned one stop before it."
        return _ride_write(_ride_view(line, raw), raw, line)

    def _end(self) -> Receipted:
        raw = self._rides().end()
        if raw.get("ended"):
            line = f"Stopped following {_the_bus(raw)}."
            return _ride_write(_ride_view(line, raw), raw, line)
        line = "He was not on a followed ride, so nothing was ended."
        return Receipted(_ride_view(line, raw), Receipt("done", reads_only=False, line=line))


_TAKES: Final = {
    "watch": "to= only", "end": "no other arguments", "get_off": "stop= only",
}


def transit_tool_local(value: object, now: datetime) -> datetime:
    """``at`` for a board: a local time like 09:00 or a local date and time; default ``now``."""
    text = _text(value)
    if text is None:
        return now
    return transit_tool.local_time(text, "at", now)


def build_bus_tool(  # noqa: PLR0913 - the key, places, offers, live, the ride.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    *,
    offers: TransitOffers | None = None,
    bus_live: BusTimes | None = None,
    control: RideControl | None = None,
    kept: Callable[[], list[Place]] | None = None,
    cap_usd: float = maps_spend.DEFAULT_MONTHLY_USD,
    book: places_api.PlaceBook | None = None,
) -> tuple[Tool, ...]:
    """``bus``; none without a Google Routes key and a ride to follow.

    Without the key it still follows rides, and says times cannot be read; without ``control`` it
    still plans and pins. ``offers`` is the last list, shared with the chat card's routes and the
    ride follower, which finds his stop in it. The other arguments are the planner's
    (:func:`~jarvis.execution.transit_tool.build_planner`); ``bus_live`` also reads the board.
    """
    if not api_key and control is None:
        return ()
    offers = offers if offers is not None else transit_tool.TransitOffers()
    book = book if book is not None else places_api.PlaceBook()
    plan = transit_tool.build_planner(
        api_key, places, offers=offers, bus_live=bus_live,
        kept=kept, cap_usd=cap_usd, book=book,
    )
    reader = place_reader.PlaceReader(
        api_key, book, saved=places, kept=kept,
        before_request=lambda ctx: maps_spend.guard(ctx.conn, cap_usd, ctx.clock.now()),
    )
    return (
        Tool(
            name="bus",
            description=_DESCRIPTION,
            input_schema=_schema(_FROM),
            handler=_Bus(plan, offers, control, bus_live, reader, cap_usd).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            reads=_reads,
            # The six old names are gone; a search that names one ("use transit") is told the
            # call that took its place.
            supersedes={
                "transit": "bus(to=...)", "pin_departure": "bus(pin=[N])",
                "watch_for_boarding": 'bus(ride="watch", to=...)',
                "start_ride": 'bus(ride="start")',
                "set_ride_stop": 'bus(ride="get_off", stop=<stop>)',
                "end_ride": 'bus(ride="end")',
            },
        ),
    )


__all__ = ["RideControl", "build_bus_tool"]
