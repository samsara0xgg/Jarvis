"""ADR 0234: the tools that read and keep his places: `places`, `save_place`, `forget_place`.

The program counts his stays (:mod:`jarvis.state.places`); she decides, through these tools, what
is kept, under which name and when to ask. Both writers record origin ``model``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import places
from jarvis.state.daily_report import resolve_zone
from jarvis.state.phone_location import latest_phone_fix

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import ToolContext

FIX_MAX_AGE_MS: Final = 30 * 60_000
"""How old the phone's latest fix may be and still be `here`."""
SPOTS_LISTED: Final = 10

PLACES_DESCRIPTION: Final = (
    "The places he keeps in long-term memory, and the places he goes to most that are not kept"
    " (spots), each with the days he was there in the last 28 and 84 days (today included), the"
    " last visit, the usual length of a stay in minutes and the label his phone gives the place."
    " Call it to answer how often he goes somewhere, or before you keep a place. `query` narrows"
    " the list to a kept name or a phone label ('Iron Works'). The program counted these from his"
    " phone's stays: they are facts, and they do not decide what is kept. A spot's id lasts for"
    " this conversation only."
)
SAVE_DESCRIPTION: Final = (
    "Keep a place in his long-term memory under a name, or rename one he keeps. Call it when he"
    ' names a place ("this is my home", "这里是健身房"), tells you he goes somewhere often,'
    " or answers your question about a spot. You may also keep a place on your own judgement"
    " when `places` shows he goes there regularly and it clearly matters to him, but only under"
    " a name he has used: never invent a name, and never use the phone's label as one. `where`"
    ' is "here" (where his phone is now), or a spot id or a place id from `places`. Places he'
    " goes to only now and then are not kept; they stay counted."
)
FORGET_DESCRIPTION: Final = (
    "Remove a kept place when he asks you to forget it or says it is no longer his (he moved"
    " out, quit the gym). Its visits stay counted."
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _integer(args: Mapping[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        msg = f"{key} must be a whole number"
        raise ToolError(msg, code="invalid_argument")
    return int(value)


def _refused(exc: Exception) -> ToolError:
    """A refused write as the model reads it."""
    if isinstance(exc, LookupError):
        return ToolError(str(exc), code="not_found")
    return ToolError(str(exc), code="invalid_argument")


class _PlaceTools:
    """memory.db's places and the phone's rows in the log, as three handlers."""

    def __init__(self, memory_db: Path, zone: tzinfo) -> None:
        self._db = memory_db
        self._zone = zone

    def listing(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        query = str(args.get("query") or "").strip().casefold()
        now = _now_ms()
        held = places.read_kept(self._db)
        counted = places.tally(places.read_stays(ctx.conn, now, self._zone), held, now, self._zone)

        def has_label(facts: places.Facts) -> bool:
            return query in (facts.label or "").casefold()

        spots = [
            s for s in counted.spots if (has_label(s.facts) if query else s.facts.days_28)
        ][:SPOTS_LISTED]
        return {
            "kept": [
                places.place_view(one, counted.kept[one.id]) for one in held
                if not query or query in one.name.casefold() or has_label(counted.kept[one.id])
            ],
            "spots": [{"id": s.id, **places.fact_view(s.facts)} for s in spots],
        }

    def _point(self, where: str, ctx: ToolContext, now: int) -> tuple[float, float]:
        """The centre `where` names: the phone's fix for "here", else a listed spot's."""
        if where == "here":
            fix = latest_phone_fix(ctx.conn)
            if fix is None or now - fix.at_ms > FIX_MAX_AGE_MS:
                msg = "his phone has not reported where he is in the last 30 minutes: ask him"
                raise ToolError(msg, code="location_unavailable")
            return fix.lat, fix.lng
        seen = places.read_stays(ctx.conn, now, self._zone)
        held = places.read_kept(self._db)
        spot = places.spot_at(places.tally(seen, held, now, self._zone).spots, where)
        return spot.lat, spot.lng

    def save(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        name, where = str(args.get("name", "")).strip(), str(args.get("where", "")).strip()
        role = args.get("role")
        if not name or not where:
            msg = "save_place: name and where are both required"
            raise ToolError(msg, code="invalid_argument")
        radius = _integer(args, "radius_m")
        kind = None if role is None else str(role)
        try:
            if where.startswith("place:"):
                written = places.edit(
                    self._db, where, name=name, role=kind, radius_m=radius, origin="model",
                )
            else:
                lat, lng = self._point(where, ctx, _now_ms())
                written = places.save(
                    self._db, name=name, lat=lat, lng=lng, role=kind, radius_m=radius,
                    origin="model",
                )
        except (LookupError, ValueError) as exc:
            raise _refused(exc) from exc
        return {
            "kept": places.place_view(written.place, None),
            "unchanged": written.change_id is None,
        }

    def forget(self, args: Mapping[str, Any], _ctx: ToolContext) -> dict[str, Any]:
        which = str(args.get("place", "")).strip()
        if not which:
            msg = "forget_place: place is required"
            raise ToolError(msg, code="invalid_argument")
        found = next(
            (
                p for p in places.read_kept(self._db)
                if which == p.id or p.name.casefold() == which.casefold()
            ),
            None,
        )
        if found is None:
            msg = f"no kept place {which}"
            raise ToolError(msg, code="not_found")
        try:
            places.forget(self._db, found.id, origin="model")
        except (LookupError, ValueError) as exc:
            raise _refused(exc) from exc
        return {"forgotten": found.name}


def build_place_tools(memory_db_path: Path, zone: tzinfo | None = None) -> tuple[Tool, ...]:
    """`places`, `save_place` and `forget_place` over memory.db and the phone's rows in the log.

    ``zone`` is the one days are cut in (default: the machine's).
    """
    tools = _PlaceTools(memory_db_path, zone or resolve_zone(None, None)[1])
    caller = frozenset({CallerPrincipal.JARVIS_LLM})
    return (
        Tool(
            name="places",
            description=PLACES_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "a kept place's name, or the phone's label of a spot",
                    },
                },
            },
            handler=tools.listing,
            allowed_callers=caller,
            risk_level="L1",
            read_only=True,
        ),
        Tool(
            name="save_place",
            description=SAVE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "the name he uses for it"},
                    "where": {
                        "type": "string",
                        "description": "'here', a spot id or a place id from `places`",
                    },
                    "role": {"type": "string", "enum": list(places.ROLES)},
                    "radius_m": {
                        "type": "integer",
                        "minimum": places.RADIUS_RANGE[0],
                        "maximum": places.RADIUS_RANGE[1],
                        "description": (
                            f"metres, default {places.DEFAULT_RADIUS_M}; a campus is larger"
                        ),
                    },
                },
                "required": ["name", "where"],
            },
            handler=tools.save,
            allowed_callers=caller,
            risk_level="L1",
            read_only=False,
        ),
        Tool(
            name="forget_place",
            description=FORGET_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "place": {"type": "string", "description": "a place id from `places`"},
                },
                "required": ["place"],
            },
            handler=tools.forget,
            allowed_callers=caller,
            risk_level="L1",
            read_only=False,
        ),
    )


__all__ = [
    "FORGET_DESCRIPTION",
    "PLACES_DESCRIPTION",
    "SAVE_DESCRIPTION",
    "build_place_tools",
]
