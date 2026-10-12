"""ADR 0246: everything about his bus is one tool, `bus`, and the server remembers the rest.

It answers the questions the model used to spread over `transit`, `pin_departure`,
`watch_for_boarding`, `start_ride`, `set_ride_stop` and `end_ride`: no arguments say how the ride he
is on is going (else the buses home), `to` lists numbered options, `pin` puts a listed option on the
notch, `ride` starts, watches for or ends a ride, `get_off` sets his stop. The last list
(:class:`~jarvis.execution.transit_tool.TransitOffers`) and the ride (``jarvis.runtime.rides``) are
state the server holds, so no call copies a field from an earlier result. The runtime hands the ride
in as a :class:`RideControl` of plain callables, as it hands the planner its live departure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import transit_tool
from jarvis.execution.tools import Tool, ToolError, _get_running_event_uid
from jarvis.shared import CallerPrincipal
from jarvis.state import departures

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.execution.transit_tool import TransitOffers


@dataclass(frozen=True)
class RideControl:
    """What the runtime does for the tool; each raises ``ToolError`` for a refusal."""

    watch: Callable[[str], dict[str, Any]]
    start: Callable[[str | None, str | None, str | None], dict[str, Any]]
    set_stop: Callable[[str], dict[str, Any]]
    end: Callable[[], dict[str, Any]]
    status: Callable[[], dict[str, Any] | None]
    """The ride he is on as the runtime describes it, None when he is not on one."""


_DESCRIPTION: Final = (
    "Use for anything about the bus: times, getting somewhere by bus, pinning a trip, the bus he"
    " is on; not for where he is (where_am_i). No arguments: the ride he is on now (next stop,"
    " his stop, minutes left), else the next buses home. to= (home, school, a place) lists up to"
    " 3 numbered options with leave time, walk, buses and arrival; at=HH:MM for later today,"
    " route=39 asks about that one bus. pin=[N] puts listed option N on the notch. ride=start: he"
    " boarded; ride=watch: he is heading out; ride=end: he got off. get_off=<stop> sets his stop"
    " and warns him before it."
)
_FROM_MAC: Final = "here: his location, read from this Mac, which he carries."
_FROM_PHONE: Final = (
    "here: his location from his phone's last report; the result gives its age, and an old one"
    " means he has not moved far."
)
_FROM_BOTH: Final = (
    "here: his phone's last report when he is talking from his phone (the result gives its age;"
    " an old one means he has not moved far), otherwise read from this Mac, which he carries."
)
_STARTS: Final = ("start", "watch", "end")
_KEYS: Final = frozenset(
    {"to", "from", "at", "route", "pin", "ride", "option", "get_off", "vehicle_id"},
)
_HOME: Final = "home"
_RIDE_ONLY: Final = {
    "option": "ride=start", "vehicle_id": "ride=start",
}
_DROPPED: Final = frozenset({"ride_id", "eta_ms", "started_at_ms", "warned", "note"})
"""Fields of the ride follower's answers that are the program's, or that the sentence says."""
_EASY_SPARE_MIN: Final = 2


def _schema(here: str) -> dict[str, Any]:
    """The arguments; ``here`` says whose location 'here' is (ADR 0194, 0198, 0212)."""
    return {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Where to: 'home' (default), 'school' (UVic too), or a place or"
                " address. With ride=watch, where he is going.",
            },
            "from": {
                "type": "string",
                "description": "Another start, only when he says he starts elsewhere; never guess"
                f" one from the time of day. Default {here}",
            },
            "at": {"type": "string", "description": "Local time later today, HH:MM; omit for now."},
            "route": {
                "type": "string",
                "description": "The bus number he names ('39', '26A'; digits only if speech was"
                " garbled). With to or alone: can he still catch THAT bus. Say a bus can or"
                " cannot be caught only when the result shows it. With ride=start: the bus he"
                " boarded.",
            },
            "pin": {
                "type": "array",
                "items": {"type": "integer"},
                "maxItems": departures.MAX_TRIPS,
                "description": "Option numbers of the last list to put on the notch, e.g. [1] or"
                " [1,2] for two. Only when he asks ('pin it'); never offer: the chat card"
                " already has a button per bus.",
            },
            "ride": {
                "type": "string",
                "enum": list(_STARTS),
                "description": "start: he says he boarded ('I'm on the 28'), never a guess. watch:"
                " he says he is heading out now to go by bus; needs to=. end: he got off.",
            },
            "option": {
                "type": "integer",
                "description": "With ride=start: the number of the listed option he took.",
            },
            "get_off": {
                "type": "string",
                "description": "The stop he gets off at (its name, or a place); needs a started"
                " ride. If several stops match, ask which.",
            },
            "vehicle_id": {
                "type": "string",
                "description": "With ride=start, only after an ambiguous result and his answer:"
                " the vehicle_id of the candidate he picked.",
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


def _buses(option: Mapping[str, Any]) -> str:
    return ", then ".join(f"the {leg['route']}" for leg in option["legs"])


def _leave(option: Mapping[str, Any]) -> str:
    if option["spare_min"] <= 0:
        return "leave now"
    return f"leave {transit_tool.said_hhmm(option['leave_at'], soon=True)}"


def _where(label: str) -> str:
    """A waypoint label as a sentence reads it: 'here (the phone's last report ...)' is 'here'."""
    return "here" if label.startswith("here (") else label


def _named_line(named: Mapping[str, Any]) -> str:
    """The sentence that answers 'can I still catch the 39': catchable or not, how tight, next."""
    route = named["route"]
    if named["verdict"] == "none":
        return f"Google has no route {route} bus then."
    at = transit_tool.said_hhmm(named["departs"], soon=True)
    first = f"The {route} leaves {named['board_stop']} at {at}"
    if named["verdict"] == "easy":
        first += f"; he has room ({named['spare_min']} min to spare after the walk)."
    elif named["verdict"] == "tight":
        first += f"; it is tight, he must walk fast ({named['spare_min']} min to spare)."
    else:
        first += "; he has missed it."
    later = named.get("next")
    if later and named["verdict"] == "missed":
        first += f" The next one is at {transit_tool.said_hhmm(later['departs'])}."
    return first


def _plan_line(raw: Mapping[str, Any]) -> str:
    """The plan in a few words: the named bus if one was asked about, else the first catchable."""
    options = raw["options"]
    if named := raw.get("named_route"):
        text = _named_line(named)
        return text + (" Other options are listed." if len(options) > 1 else "")
    pick = next((o for o in options if o["spare_min"] > -_EASY_SPARE_MIN), options[0])
    leg = pick["legs"][0]
    text = (
        f"To {_where(raw['to'])}, option {options.index(pick) + 1}: {_leave(pick)}, take"
        f" {_buses(pick)} from {leg['board_stop']} at {transit_tool.said_hhmm(leg['departs'])},"
        f" arrive {transit_tool.said_hhmm(pick['arrive_at'])} ({pick['total_min']} min)."
    )
    if pick["spare_min"] <= -_EASY_SPARE_MIN:
        text += " It is probably too late to catch."
    return text + (f" {len(options)} options are listed." if len(options) > 1 else "")


def _ride_line(ride: Mapping[str, Any]) -> str:
    """How the ride is going, in words: the bus, the next stop, his stop and when."""
    text = f"On the {ride['route']}"
    if ride.get("board_stop"):
        text += f" from {ride['board_stop']}"
    text += f"; next stop {ride['next_stop']}." if ride.get("next_stop") else "."
    stop = ride.get("alight_stop")
    if not stop:
        return text + " Where he gets off is not set: ask him once, then bus(get_off=<stop>)."
    text += f" He gets off at {stop}"
    if ride.get("eta_ms") is not None and ride.get("stops_left") is not None:
        left = ride["stops_left"]
        text += f": {left} stop{'' if left == 1 else 's'} left, about"
        text += f" {transit_tool.said_ms(ride['eta_ms'], soon=True)}."
    else:
        text += "."
    if ride.get("next_stop") is None and ride.get("stale", True):
        return text + " There is no bus data for this trip now: say you cannot give times."
    if ride.get("stale"):
        text += " No live bus position now: these are timetable times and may be off."
    return text


def _ride_view(summary: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """A ride tool's answer: the sentence first, then what the program told, minus its own ids."""
    return {"summary": summary, **{k: v for k, v in raw.items() if k not in _DROPPED}}


class _Bus:
    """The tool's handler: dispatch on the arguments to a plan, a pin, a ride or a stop."""

    def __init__(
        self,
        plan: Callable[[Mapping[str, Any], ToolContext], dict[str, Any]] | None,
        offers: TransitOffers,
        control: RideControl | None,
    ) -> None:
        self._plan = plan
        self._offers = offers
        self._control = control

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Which of the five things was asked, then do it; mixing them is an error."""
        given = {k: v for k, v in args.items() if v is not None and v != ""}
        if unknown := sorted(set(given) - _KEYS):
            msg = (
                f"{', '.join(unknown)} is not an argument. The arguments are"
                f" {', '.join(sorted(_KEYS))}; times are to=, at= and route="
            )
            raise _bad(msg)
        if "ride" in given:
            return self._ride(given)
        if "pin" in given:
            self._alone(given, "pin")
            return self._pin(_numbers(given["pin"]), ctx)
        if "get_off" in given:
            self._alone(given, "get_off")
            return self._get_off(str(given["get_off"]))
        for name, needs in _RIDE_ONLY.items():
            if name in given:
                msg = f"{name} goes with {needs}"
                raise _bad(msg)
        if given:
            return self._times(given, ctx)
        ride = self._control.status() if self._control is not None else None
        if ride is not None:
            return _ride_view(self._riding(ride), ride)
        return self._times({}, ctx)

    def _riding(self, ride: Mapping[str, Any]) -> str:
        """How the ride is going; with no stop set, where the last plan lets this bus off."""
        text = _ride_line(ride)
        if not ride.get("alight_stop") and (
            hit := self._offers.alight_for(str(ride["route"]))
        ):
            text += (
                f" The last plan, to {self._offers.to}, lets him off the {ride['route']} at"
                f" {hit[0]}; set it with bus(get_off=...) if he takes that."
            )
        return text

    @staticmethod
    def _alone(given: Mapping[str, Any], name: str) -> None:
        if len(given) > 1:
            msg = f"{name} stands alone; for times use to=..., for a ride use ride=..."
            raise _bad(msg)

    # --- times -------------------------------------------------------------------------------

    def _times(self, given: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Up to three numbered options, from Google Routes; the list is kept for ``pin``."""
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
        if at := _text(given.get("at")):
            asked["depart_at"] = at
        if route := _text(given.get("route"), 8):
            asked["route"] = route
        raw = self._plan(asked, ctx)
        options = [{"option": n, **o} for n, o in enumerate(raw["options"], 1)]
        return {"summary": _plan_line(raw), **raw, "options": options}

    # --- the pin -----------------------------------------------------------------------------

    def _pin(self, numbers: list[int], ctx: ToolContext) -> dict[str, Any]:
        trips, new = transit_tool.pin_listed(
            ctx.conn, self._offers, numbers, action_id=ctx.action_id,
            source_event_id=_get_running_event_uid(ctx.conn, ctx.action_id),
        )
        said = "; ".join(
            f"the {t.route} from {t.board_stop}: leave"
            f" {transit_tool.said_ms(t.leave_at_ms, soon=True)}, bus at"
            f" {transit_tool.said_ms(t.departs_at_ms)}"
            for t in trips
        )
        summary = (
            f"Pinned on the notch: {said}. It rings at the leave time." if new
            else f"Already pinned, nothing changed: {said}."
        )
        return {
            "summary": summary,
            "pinned": True,
            "already_pinned": not new,
            "trips": [
                {
                    "route": t.route,
                    "board_stop": t.board_stop,
                    "leave": transit_tool.said_ms(t.leave_at_ms, soon=True),
                    "departs": transit_tool.said_ms(t.departs_at_ms),
                }
                for t in trips
            ],
        }

    # --- the ride ----------------------------------------------------------------------------

    def _rides(self) -> RideControl:
        if self._control is None:
            msg = "Following a bus ride is not set up on this machine; tell him."
            raise ToolError(msg, code="unavailable")
        return self._control

    def _ride(self, given: Mapping[str, Any]) -> dict[str, Any]:
        what = given["ride"]
        if what not in _STARTS:
            msg = f"ride must be start, watch or end, not {what!r}"
            raise _bad(msg)
        if what == "start":
            return self._start(given)
        extra = set(given) - {"ride", "to"}
        if extra or (what == "end" and "to" in given):
            msg = f"ride={what} takes {'to= only' if what == 'watch' else 'no other arguments'}"
            raise _bad(msg)
        if what == "end":
            return self._end()
        return self._watch(_text(given.get("to")))

    def _watch(self, place: str | None) -> dict[str, Any]:
        if place is None:
            msg = "ride=watch needs to=<where he is going>, as he said it"
            raise _bad(msg)
        raw = self._rides().watch(place)
        if not raw.get("watching"):
            return _ride_view("He is already on a bus ride, so there is nothing to watch for.", raw)
        summary = (
            f"Watching for him to board a bus to {place} for the next {raw.get('minutes')}"
            " minutes. If his phone moves with a bus, he is asked on his phone whether he boarded."
        )
        return _ride_view(summary, raw)

    def _start(self, given: Mapping[str, Any]) -> dict[str, Any]:
        if {"from", "at", "pin", "get_off"} & set(given):
            msg = "ride=start takes option= or route=, and to= and vehicle_id= if needed"
            raise _bad(msg)
        if "option" in given and "route" in given:
            msg = "pass option=<number> or route=<bus number>, not both"
            raise _bad(msg)
        route, place = _text(given.get("route"), 8), _text(given.get("to"))
        if "option" in given:
            row = self._offers.listed_row(_number(given["option"], "option"))
            route, place = str(row["route"]).split("→")[0].strip(), str(row["to"])
        raw = self._rides().start(route, place, _text(given.get("vehicle_id"), 64))
        if raw.get("ambiguous"):
            names = " or ".join(
                f"the one heading for {c.get('next_stop') or 'an unknown stop'}"
                for c in raw["candidates"]
            )
            summary = (
                f"Two buses are about equally near him: {names}. Ask him which, then call"
                " bus(ride='start', route=..., vehicle_id=...) with that candidate's vehicle_id."
            )
            return _ride_view(summary, raw)
        lead = "Already following this ride; nothing changed. " if raw.get("already_riding") else ""
        return _ride_view(lead + _ride_line(raw), raw)

    def _get_off(self, stop: str) -> dict[str, Any]:
        raw = self._rides().set_stop(stop.strip()[:80])
        if raw.get("ambiguous"):
            summary = (
                f"More than one stop matches: {', '.join(raw['candidates'])}. Ask him which, then"
                " call bus(get_off=<that stop>)."
            )
            return _ride_view(summary, raw)
        return _ride_view(f"Set. {_ride_line(raw)} He is warned one stop before it.", raw)

    def _end(self) -> dict[str, Any]:
        raw = self._rides().end()
        if raw.get("ended"):
            return _ride_view(f"Stopped following the {raw.get('route')}.", raw)
        return _ride_view("He was not on a followed ride, so nothing was ended.", raw)


def build_bus_tool(  # noqa: PLR0913 - the key, places, two readers, offers, live, the ride.
    api_key: str | None,
    places: Callable[[], Mapping[str, str]],
    here: Callable[[], Mapping[str, Any]] | None = None,
    *,
    phone_here: Callable[[], Mapping[str, Any]] | None = None,
    offers: TransitOffers | None = None,
    bus_live: Callable[[str, tuple[float, float], int], int | None] | None = None,
    control: RideControl | None = None,
) -> tuple[Tool, ...]:
    """``bus``; none without a Google Routes key and a ride to follow.

    Without the key it still follows rides, and says times cannot be read; without ``control`` it
    still plans and pins. ``offers`` is the last list, shared with the chat card's routes and the
    ride follower, which finds his stop in it. The other arguments are the planner's
    (:func:`~jarvis.execution.transit_tool.build_planner`).
    """
    if not api_key and control is None:
        return ()
    offers = offers if offers is not None else transit_tool.TransitOffers()
    plan = transit_tool.build_planner(
        api_key, places, here, phone_here=phone_here, offers=offers, bus_live=bus_live,
    )
    return (
        Tool(
            name="bus",
            description=_DESCRIPTION,
            input_schema=_schema(
                _FROM_BOTH if here is not None and phone_here is not None
                else _FROM_PHONE if phone_here is not None
                else _FROM_MAC,
            ),
            handler=_Bus(plan, offers, control).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = ["RideControl", "build_bus_tool"]
