"""ADR 0194, 0198, 0212, 0281, 0322: where the user is, from the one Here a call carries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.here import HereReading, HereUnavailable
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state.attachments import short_id
from jarvis.state.event_log import MAC_NODE
from jarvis.state.phone_location import NoPhoneReport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.around import Surroundings
    from jarvis.execution.tools import ToolContext
    from jarvis.shared.here import HereSource

_DESCRIPTION: Final = (
    "Use for where he is right now and what is around him: a place name, street, nearby places"
    " and how long he has stayed. Not for a bus trip (bus) or finding a business (map_search)."
    " Returns latitude, longitude, accuracy, the age of the position and whether the device took"
    " a fresh fix (it can take ten seconds; if not, the last report), and with a Google key the"
    " street, his kept place and the places he may be at. His phone reports after about 200 m"
    " and a rest, so the last report can be old."
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


def read_here(ctx: ToolContext, *, fresh: bool = False) -> HereReading:
    """Where he is for this call (ADR 0322), or a tool error that tells the model to ask.

    ``fresh`` always asks for a new fix (``where_am_i``); otherwise a report recent enough is used
    as it is. A reading that carries ``why`` is the last report, with no fix behind it.
    """
    try:
        if ctx.here is None:
            msg = "no location reader on this machine"
            raise HereUnavailable(msg)  # noqa: TRY301 - one exit for every way it can fail
        return ctx.here.fresh() if fresh else ctx.here.read()
    except (NoPhoneReport, HereUnavailable) as exc:
        msg = f"{exc}; {_ASK}"
    except Exception as exc:  # a log that cannot be read, a helper failure: one short reason.
        msg = f"his location could not be read ({exc}); {_ASK}"
        raise ToolError(msg, code="location_unavailable") from exc
    raise ToolError(msg, code="location_unavailable")


def phone_report(fix: HereReading) -> str:
    """``the phone's last report 12 min ago`` for a reading from a phone."""
    return f"the phone's last report {describe_age(fix.age_s)} ago"


def said_where(fix: HereReading) -> str:
    """Whose reading ``fix`` is, in words: this Mac, or the phone's report with its age.

    A report no fresh fix could confirm says so, since he may have moved.
    """
    if fix.device == MAC_NODE:
        return "this Mac, no phone has reported yet" if fix.why else "this Mac"
    said = phone_report(fix)
    return f"{said}, no fresh fix came" if fix.why else said


def _note(fix: HereReading) -> str:
    """What the model is told about a position: how fresh, how accurate, what it holds."""
    accuracy = f"accurate to about {round(fix.accuracy_m)} m"
    if fix.fresh:
        age = f"{fix.age_s} s" if fix.age_s < 60 else describe_age(fix.age_s)  # noqa: PLR2004
        note = f"a fix his phone just took, {age} old, {accuracy}."
        if fix.snapshot is not None:
            note += (
                " The phone also sent a picture of where he is: look at it with look_at_picture,"
                f" picture {short_id(fix.snapshot)}, if what he asks needs it."
            )
        return note
    note = f"{phone_report(fix)}, {accuracy}."
    if fix.why:
        note += (
            f" A fresh fix was asked for and did not come ({fix.why}), so he may not be"
            " where this says now."
        )
    return note


def build_where_tool(
    here: HereSource | None, around: Surroundings | None = None,
) -> tuple[Tool, ...]:
    """``where_am_i`` over the context's Here; none without a source (``here`` is ``None``).

    With ``around`` the result also carries what is around the fix (ADR 0284).
    """
    if here is None:
        return ()

    def where_am_i(_args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        fix = read_here(ctx, fresh=True)
        mac = fix.device == MAC_NODE
        out: dict[str, Any] = {
            "summary": f"He is at {fix.place or 'an unnamed place'} ({said_where(fix)}).",
            "lat": fix.lat, "lng": fix.lng, "accuracy_m": fix.accuracy_m,
            "place": fix.place, "source": "this Mac" if mac else "phone",
        }
        if mac and fix.why:
            out["note"] = (
                "no phone has reported a location yet, so this is this Mac's, which may not be"
                " where he is."
            )
        elif not mac:
            out |= {"age_s": fix.age_s, "device": fix.device, "fresh": fix.fresh}
            if fix.why:
                out["not_fresh"] = fix.why
            if fix.snapshot is not None:
                out["snapshot"] = fix.snapshot
            out["note"] = _note(fix)
        if around is not None:
            out["around"] = around.lookup(ctx, fix)
        sources: tuple[str, ...] = ("phone location",)
        if around is not None:
            sources += ("Google Places", "Google Geocoding")
        return Receipted(out, Receipt(
            "done", reads_only=True, line=f"read where he is: {said_where(fix)}",
            covered=Covered(sources, "his position now", 1, 0),
        ))

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


__all__ = [
    "build_where_tool",
    "describe_age",
    "phone_report",
    "read_here",
    "said_where",
]
