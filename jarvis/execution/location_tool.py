"""ADR 0194, 0198, 0212, 0281: where the user is, from this Mac or from his phone."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError, _turn_of
from jarvis.shared import CallerPrincipal
from jarvis.state.attachments import short_id
from jarvis.state.phone_location import NoPhoneReport, turn_from_phone

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from jarvis.execution.around import Surroundings
    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "The user's current location, read from this Mac (he carries it): a place name (street"
    " and area), latitude, longitude and accuracy in metres. Never say you do not know where he"
    " is without calling it. Call it only when his question depends on where he is right now:"
    " 'where am I', what he is doing or just did away from the computer, what is nearby,"
    " places, food or shops near him, how far something is from here. For bus trips use"
    " bus instead (it starts from here). If it returns an error, say so and ask him where he"
    " is."
)

PHONE_REPORT_RULE: Final = (
    "The phone reports when he moves about 500 m or arrives at or leaves a place, so an old"
    " report means he has not moved far since."
)
"""What the model is told about the age of a phone's report (ADR 0198)."""

_FRESH_RULE: Final = (
    "It asks his phone for a fresh fix, which can take up to ten seconds; `fresh` says whether"
    " the phone answered, and if not this is its last report."
)

_PHONE_DESCRIPTION: Final = (
    "The user's location, from his phone (the phone is with him): a place name when known,"
    " latitude, longitude, accuracy in metres and age_s, how many seconds old the position is."
    " " + _FRESH_RULE + " " + PHONE_REPORT_RULE + " Tell him how old the position is when that"
    " matters. Never say you do not know where he is without calling"
    " it. Call it only when his question depends on where he is right now: 'where am I', what"
    " he is doing or just did away from the computer, what is nearby, places, food or shops"
    " near him, how far"
    " something is from here. For bus trips use bus instead (it starts from here). If it"
    " returns an error, no phone has reported a location: say so and ask him where he is."
)

_BOTH_DESCRIPTION: Final = (
    "The user's current location: a place name when known, latitude, longitude and accuracy in"
    " metres. When he is talking from his phone it is the phone's position (the phone is"
    " with him), which also carries age_s, how many seconds old the position is. "
    + _FRESH_RULE + " " + PHONE_REPORT_RULE + " Tell him how old the position is when that"
    " matters. Otherwise it is"
    " read fresh from this Mac, which he carries. Never say you do not know where he is without"
    " calling it. Call it only when his question depends on where he is right now: 'where am"
    " I', what he is doing or just did away from the computer, what is nearby, places, food or"
    " shops near him, how"
    " far something is from here. For bus trips use bus instead (it starts from here). If it"
    " returns an error, say so and ask him where he is."
)

_AROUND_RULE: Final = (
    " The result also has `around` (ADR 0284): the street and nearest intersection, how long he"
    " has been at this spot, his kept place if he is in one, candidate places he may be at and"
    " up to 10 other places within 300 m with direction, distance and open now. They are facts"
    " for you to judge, and each new spot costs money, so do not call it again for the same"
    " spot."
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


def _read(
    reader: Callable[[], Mapping[str, Any]] | None, *, phone: bool = False,
) -> dict[str, Any]:
    """One reading from ``reader``, or a tool error that tells the model to ask.

    ``phone``: the reader is the phone (ADR 0198, 0281), whose reading also carries ``age_s``
    and ``device``, and ``fresh`` (with ``not_fresh`` and ``snapshot``) when it asked the phone
    for a fix; either way the result names its ``source``.
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
            "source": "phone" if phone else "this Mac",
        }
        if phone:
            here |= {"age_s": int(fix["age_s"]), "device": str(fix["device"])}
            here |= {key: fix[key] for key in ("fresh", "not_fresh", "snapshot") if key in fix}
    except Exception as exc:  # denied, timeout, helper failure, no report: all one short reason.
        if not phone:
            msg = f"this Mac's location could not be read ({exc}); {_ASK}"
        elif isinstance(exc, NoPhoneReport):
            msg = f"{exc}; {_ASK}"
        else:
            msg = f"the phone's last location report could not be read ({exc}); {_ASK}"
        raise ToolError(msg, code="location_unavailable") from exc
    return here


def read_here(
    mac: Callable[[], Mapping[str, Any]] | None,
    phone: Callable[[], Mapping[str, Any]] | None,
    ctx: ToolContext,
) -> dict[str, Any]:
    """Where he is for this call: the phone's last report, or this Mac's fresh fix (ADR 0212).

    ``phone`` answers a turn a paired phone opened (its opening row is under the device's name),
    and every turn where there is no ``mac`` to read, as on a brain. A turn of the Mac's own
    gets ``mac``. A phone turn when no phone has reported yet falls back to ``mac``, with
    ``phone_unreported`` set so the result can say so; with no ``mac`` it is a tool error.
    """
    if phone is None:
        return _read(mac)
    if mac is not None and not turn_from_phone(ctx.conn, _turn_of(ctx.action_id)):
        return _read(mac)
    try:
        return _read(phone, phone=True)
    except ToolError as exc:
        if mac is None or not isinstance(exc.__cause__, NoPhoneReport):
            raise
    return {**_read(mac), "phone_unreported": True}


def phone_report(fix: Mapping[str, Any]) -> str:
    """``the phone's last report 12 min ago`` for a reading from the phone."""
    return f"the phone's last report {describe_age(fix['age_s'])} ago"


def said_where(fix: Mapping[str, Any]) -> str:
    """Whose reading ``fix`` is, in words: the phone's report with its age, or this Mac."""
    if fix["source"] == "phone":
        return phone_report(fix)
    return "this Mac, no phone has reported yet" if fix.get("phone_unreported") else "this Mac"


def _phone_note(fix: Mapping[str, Any]) -> str:
    """What the model is told about a phone's position: how fresh, how accurate, what it holds."""
    accuracy = f"accurate to about {round(fix['accuracy_m'])} m"
    if fix.get("fresh"):
        age = f"{fix['age_s']} s" if fix["age_s"] < 60 else describe_age(fix["age_s"])  # noqa: PLR2004
        note = f"a fix his phone just took, {age} old, {accuracy}."
        if "snapshot" in fix:
            note += (
                " The phone also sent a picture of where he is: look at it with look_at_picture,"
                f" picture {short_id(fix['snapshot'])}, if what he asks needs it."
            )
        return note
    note = f"{phone_report(fix)}, {accuracy}. {PHONE_REPORT_RULE}"
    if "not_fresh" in fix:
        note += f" A fresh fix was asked for and did not come ({fix['not_fresh']})."
    return note


def build_where_tool(
    mac: Callable[[], Mapping[str, Any]] | None,
    phone: Callable[[], Mapping[str, Any]] | None = None,
    around: Surroundings | None = None,
) -> tuple[Tool, ...]:
    """``where_am_i`` over ``mac`` and ``phone``; none when this machine has neither.

    With ``around`` the result also carries what is around the fix (ADR 0284).

    ``mac`` is this Mac's fresh fix (ADR 0194), ``phone`` the phone's last report (ADR 0198).
    With both, :func:`read_here` picks by where the turn came from (ADR 0212) and the
    description says both; with only ``phone``, as on a brain, it is always the phone's.
    """
    if mac is None and phone is None:
        return ()

    def where_am_i(_args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        fix = read_here(mac, phone, ctx)
        if fix["source"] == "phone":
            here = {**fix, "note": _phone_note(fix)}
        elif fix.pop("phone_unreported", False):
            note = "no phone has reported a location yet, so this is this Mac's, which may not be"
            here = {**fix, "note": note + " where he is."}
        else:
            here = fix
        return here if around is None else {**here, "around": around.lookup(ctx, fix)}

    description = (
        _BOTH_DESCRIPTION if mac is not None and phone is not None
        else _PHONE_DESCRIPTION if phone is not None
        else _DESCRIPTION
    ) + ("" if around is None else _AROUND_RULE)
    return (
        Tool(
            name="where_am_i",
            description=description,
            input_schema={"type": "object", "properties": {}, "required": []},
            handler=where_am_i,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=True,
        ),
    )


__all__ = [
    "PHONE_REPORT_RULE",
    "build_where_tool",
    "describe_age",
    "phone_report",
    "read_here",
    "said_where",
]
