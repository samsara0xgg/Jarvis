"""ADR 0194, 0198: where the user is, from this Mac on demand or from his phone's last report."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.phone_location import NoPhoneReport

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

PHONE_REPORT_RULE: Final = (
    "The phone reports when he moves about 500 m or arrives at or leaves a place, so an old"
    " report means he has not moved far since."
)
"""What the model is told about the age of a phone's report (ADR 0198)."""

_PHONE_DESCRIPTION: Final = (
    "The user's last known location, from his phone's last report (the phone is with him):"
    " a place name when the phone had one, latitude, longitude, accuracy in metres and age_s,"
    " how many seconds ago the phone reported it. " + PHONE_REPORT_RULE + " Tell him how old"
    " the report is when that matters. Call it only when his question depends on where he is"
    " right now: 'where am I', what is nearby, places, food or shops near him, how far"
    " something is from here. For bus trips use transit with origin 'here' instead. If it"
    " returns an error, no phone has reported a location: say so and ask him where he is."
)

_ASK: Final = "ask the user where he is, or use a saved place he names"


def describe_age(age_s: int) -> str:
    """A report's age in words for the model: ``under a minute``, ``12 min``, ``3 h 5 min``."""
    if age_s < 60:  # noqa: PLR2004 - the unit boundaries read as numbers
        return "under a minute"
    minutes = age_s // 60
    if minutes < 60:  # noqa: PLR2004
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:  # noqa: PLR2004
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h" if hours else f"{days} d"


def read_here(
    reader: Callable[[], Mapping[str, Any]] | None, *, phone: bool = False,
) -> dict[str, Any]:
    """One reading from ``reader``, or a tool error that tells the model to ask.

    ``phone``: the reader is the phone's last report (ADR 0198), whose reading also carries
    ``age_s``, ``source`` and ``device``; a Mac's reading is a fresh fix and has none of them.
    """
    try:
        if reader is None:
            msg = "no location reader on this machine"
            raise RuntimeError(msg)  # noqa: TRY301 - one exit for every way it can fail
        fix = reader()
        here: dict[str, Any] = {
            "lat": float(fix["lat"]),
            "lng": float(fix["lng"]),
            "accuracy_m": float(fix["accuracy_m"]),
            "place": str(fix["place"]) if fix.get("place") else None,
        }
        if phone:
            here |= {
                "age_s": int(fix["age_s"]),
                "source": "phone",
                "device": str(fix["device"]),
            }
    except Exception as exc:  # denied, timeout, helper failure, no report: all one short reason.
        if not phone:
            msg = f"this Mac's location could not be read ({exc}); {_ASK}"
        elif isinstance(exc, NoPhoneReport):
            msg = f"{exc}; {_ASK}"
        else:
            msg = f"the phone's last location report could not be read ({exc}); {_ASK}"
        raise ToolError(msg, code="location_unavailable") from exc
    return here


def phone_report(fix: Mapping[str, Any]) -> str:
    """``the phone's last report 12 min ago`` for a reading made with ``phone=True``."""
    return f"the phone's last report {describe_age(fix['age_s'])} ago"


def build_where_tool(
    reader: Callable[[], Mapping[str, Any]] | None, *, phone: bool = False,
) -> tuple[Tool, ...]:
    """``where_am_i`` over ``reader``; none when this machine cannot read a location.

    ``phone``: on a brain, ``reader`` gives the phone's last report (ADR 0198) and the tool says
    so, with its age and accuracy; otherwise ``reader`` is this Mac's fresh fix (ADR 0194).
    """
    if reader is None:
        return ()

    def where_am_i(_args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        fix = read_here(reader, phone=phone)
        if not phone:
            return {**fix, "source": "this Mac"}
        note = (
            f"{phone_report(fix)}, accurate to about {round(fix['accuracy_m'])} m. "
            + PHONE_REPORT_RULE
        )
        return {**fix, "note": note}

    return (
        Tool(
            name="where_am_i",
            description=_PHONE_DESCRIPTION if phone else _DESCRIPTION,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=where_am_i,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = ["PHONE_REPORT_RULE", "build_where_tool", "describe_age", "phone_report", "read_here"]
