"""ADR 0224: the four words that move a bus ride, as model-only tools.

The ride itself lives in the runtime (``jarvis.runtime.rides``), which holds the feeds and the
clock; the tool layer may not import it, so the runtime hands in a :class:`RideControl` of plain
callables, as it hands ``transit`` its live departure. The model is the only thing that hears
"I'm heading out", "上车了" and "I got off", which is why these are tools and not word matchers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext


@dataclass(frozen=True)
class RideControl:
    """What the runtime does for the tools; each raises ``ToolError`` for a refusal."""

    watch: Callable[[str], dict[str, Any]]
    start: Callable[[str | None, str | None, str | None], dict[str, Any]]
    set_stop: Callable[[str], dict[str, Any]]
    end: Callable[[], dict[str, Any]]


_WATCH: Final = (
    "Start watching for him getting on a bus. Call it when he says he is heading out now to go"
    " somewhere by bus and names the place ('I'm leaving for the campus', 'heading to Mayfair', or"
    " the same in any other language)."
    " It is quiet and lasts about 45 minutes; when his phone then moves with a bus, he gets a"
    " notification asking whether he boarded. destination is the place as he said it ('home' and"
    " 'school' are his saved places). Do NOT call it for a question about times (use transit),"
    " and not when he already says he is on the bus (use start_ride)."
)
_START: Final = (
    "Start following his bus ride. Call it when he says he has boarded ('I'm on the 28', 'got on',"
    " or the same in any other language); never to guess he is riding. It finds the bus nearest"
    " his phone's last position on the route he named, or the pinned one. Pass route (the number"
    " he says), destination (the place he is going, if he said), and vehicle_id only after an"
    " ambiguous result and his answer. If the result says ambiguous, ask him which bus from the"
    " candidates (their next stops) instead of choosing. If it says his stop is unknown, ask him"
    " once where he gets off and then call set_ride_stop."
)
_STOP: Final = (
    "Set where he gets off the bus he is riding (the ride must have started). stop is the stop's"
    " name or the place he names ('the campus exchange'). If it is ambiguous the result lists the"
    " candidates: ask which."
)
_END: Final = (
    "End the ride he is on. Call it when he says he got off, or is no longer on the bus."
)


def _text(args: Mapping[str, Any], key: str, limit: int = 80) -> str | None:
    value = args.get(key)
    if value is None or not str(value).strip():
        return None
    return str(value).strip()[:limit]


def build_ride_tools(control: RideControl | None) -> tuple[Tool, ...]:
    """``watch_for_boarding``, ``start_ride``, ``set_ride_stop`` and ``end_ride``; none without."""
    if control is None:
        return ()

    def watch(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        place = _text(args, "destination")
        if place is None:
            msg = "destination is required"
            raise ToolError(msg, code="invalid_argument")
        return control.watch(place)

    def start(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return control.start(
            _text(args, "route", 8), _text(args, "destination"), _text(args, "vehicle_id", 64),
        )

    def set_stop(args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        stop = _text(args, "stop")
        if stop is None:
            msg = "stop is required"
            raise ToolError(msg, code="invalid_argument")
        return control.set_stop(stop)

    def end(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        return control.end()

    def make(name: str, description: str, props: dict[str, Any], required: list[str],
             handler: Any) -> Tool:  # noqa: ANN401 - the handlers are the closures above.
        return Tool(
            name=name, description=description,
            input_schema={"type": "object", "properties": props, "required": required},
            handler=handler, allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1", read_only=False,
        )

    place = {"type": "string", "description": "the place as he said it"}
    route = {"type": "string", "description": "bus number he named, e.g. '28' or '26A'"}
    return (
        make("watch_for_boarding", _WATCH, {"destination": place}, ["destination"], watch),
        make("start_ride", _START, {
            "route": route, "destination": place,
            "vehicle_id": {"type": "string", "description": "from an ambiguous result's candidate"},
        }, [], start),
        make("set_ride_stop", _STOP,
             {"stop": {"type": "string", "description": "stop name or place"}}, ["stop"], set_stop),
        make("end_ride", _END, {}, [], end),
    )


__all__ = ["RideControl", "build_ride_tools"]
