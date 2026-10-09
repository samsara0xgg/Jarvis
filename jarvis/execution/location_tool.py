"""ADR 0194: where the user is, read from this Mac on demand (he carries it)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "The user's current location, read from this Mac (he carries it): a place name (street"
    " and area), latitude, longitude and accuracy in metres. Call it only when his question"
    " depends on where he is right now: 'where am I', what is nearby, places, food or shops"
    " near him, how far something is from here. For bus trips use transit with origin 'here'"
    " instead. If it returns an error, say so and ask him where he is."
)


def read_here(reader: Callable[[], Mapping[str, Any]] | None) -> dict[str, Any]:
    """One fresh fix from ``reader``, or a tool error that tells the model to ask."""
    try:
        if reader is None:
            msg = "no location reader on this machine"
            raise RuntimeError(msg)  # noqa: TRY301 - one exit for every way it can fail
        fix = reader()
        return {
            "lat": float(fix["lat"]),
            "lng": float(fix["lng"]),
            "accuracy_m": float(fix["accuracy_m"]),
            "place": str(fix["place"]) if fix.get("place") else None,
        }
    except Exception as exc:  # denied, timeout, helper failure: all one short reason.
        msg = (
            f"this Mac's location could not be read ({exc}); ask the user where he is,"
            " or use a saved place he names"
        )
        raise ToolError(msg, code="location_unavailable") from exc


def build_where_tool(reader: Callable[[], Mapping[str, Any]] | None) -> tuple[Tool, ...]:
    """``where_am_i`` over ``reader``; none when this machine cannot read a location."""
    if reader is None:
        return ()

    def where_am_i(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        fix = read_here(reader)
        return {**fix, "source": "this Mac"}

    return (
        Tool(
            name="where_am_i",
            description=_DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=where_am_i,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["build_where_tool", "read_here"]
