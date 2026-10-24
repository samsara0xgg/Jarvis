"""ADR 0234: the tools that read and keep his places: `places`, `save_place`, `forget_place`.

The program counts his stays (:mod:`jarvis.state.places`); she decides, through these tools, what
is kept, under which name and when to ask. Both writers record origin ``model``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import maps_spend
from jarvis.execution.location_tool import read_here
from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state import places

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.execution.place_reader import PlaceReader
    from jarvis.execution.tools import ToolContext

SPOTS_LISTED: Final = 10

PLACES_DESCRIPTION: Final = (
    "Use to see the places he keeps and where he goes most, or how often he goes somewhere."
    " Not for finding a business (map_search) or where he is now (where_am_i). Returns kept"
    " places and unkept spots, each with the days he was there in the last 28 and 84 days, the"
    " last visit, the usual stay in minutes and the label his phone gives it; a spot's id lasts"
    " one conversation."
)
SAVE_DESCRIPTION: Final = (
    "Use to keep a place under the name he uses for it, or to rename or resize one he keeps."
    " Not for finding a business (map_search) or noting a visit (places counts those). Returns"
    " the kept place and whether anything changed."
)
FORGET_DESCRIPTION: Final = (
    "Use to remove a kept place he no longer wants or no longer has. Not for hiding a visit:"
    " visits stay counted. Returns the name removed."
)
_WHERE: Final = (
    "Where the place is: 'here' (his phone now), a spot id from places, a kept place's id, or"
    " words for a place (a name, an address, 'home')."
)
_SOURCES: Final = ("kept places", "phone stays")


def _integer(args: Mapping[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        msg = f"save_place: {key} must be a whole number of metres (got {value!r})"
        raise ToolError(msg, code="invalid_arguments")
    return int(value)


def _refused(exc: Exception, tool: str) -> ToolError:
    """A refused write as the model reads it."""
    code = "not_found" if isinstance(exc, LookupError) else "invalid_arguments"
    msg = f"{tool}: {exc}"
    return ToolError(msg, code=code)


class _PlaceTools:
    """memory.db's places and the phone's rows in the log, as three handlers."""

    def __init__(self, memory_db: Path, reader: PlaceReader | None) -> None:
        self._db = memory_db
        self._reader = reader

    def listing(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        query = str(args.get("query") or "").strip().casefold()
        now, zone = int(ctx.clock.time() * 1000), ctx.clock.zone
        held = places.read_kept(self._db)
        counted = places.tally(places.read_stays(ctx.conn, now, zone), held, now, zone)

        def has_label(facts: places.Facts) -> bool:
            return query in (facts.label or "").casefold()

        matching = [
            s for s in counted.spots if (has_label(s.facts) if query else s.facts.days_28)
        ]
        spots = matching[:SPOTS_LISTED]
        kept = [
            places.place_view(one, counted.kept[one.id]) for one in held
            if not query or query in one.name.casefold() or has_label(counted.kept[one.id])
        ]
        more = len(matching) - len(spots)
        return Receipted(
            {
                "summary": f"{len(kept)} kept places and {len(spots)} spots he goes to most.",
                "kept": kept,
                "spots": [{"id": s.id, **places.fact_view(s.facts)} for s in spots],
            },
            Receipt(
                "done", reads_only=True,
                line=f"read {len(kept)} kept places and {len(spots)} spots",
                covered=Covered(
                    _SOURCES, "the last 84 days", len(kept) + len(spots), more,
                    "name part of a place or its label in query" if more else "",
                ),
            ),
        )

    def _point(self, where: str, ctx: ToolContext, now: int) -> tuple[float, float]:
        """The centre `where` names: Here's position for "here", a listed spot's, else words."""
        if where == "here":
            fix = read_here(ctx)
            if fix.why:
                msg = (
                    "save_place: his phone's last report is old and no fresh fix came; "
                    "ask him where the place is"
                )
                raise ToolError(msg, code="location_unavailable")
            return fix.lat, fix.lng
        zone = ctx.clock.zone
        seen = places.read_stays(ctx.conn, now, zone)
        held = places.read_kept(self._db)
        try:
            spot = places.spot_at(places.tally(seen, held, now, zone).spots, where)
        except LookupError:
            if where.startswith("spot:"):
                raise  # a spot id that no spot has, not words for a place
            return self._read_words(where, ctx)
        return spot.lat, spot.lng

    def _read_words(self, where: str, ctx: ToolContext) -> tuple[float, float]:
        """The point the place reader finds for his words (ADR 0346), metered."""
        if self._reader is None:
            msg = f"save_place: no spot or place {where!r}; use 'here' or ask him where it is"
            raise ToolError(msg, code="not_found")
        meter: list[str] = []
        try:
            return self._reader.read(where, ctx, meter).point
        finally:
            maps_spend.record(ctx, meter)

    def save(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        name, where = str(args.get("name", "")).strip(), str(args.get("where", "")).strip()
        role = args.get("role")
        if not name or not where:
            msg = (
                "save_place: name (what he calls the place) and where ('here', or a spot or "
                "place id from places) are both required"
            )
            raise ToolError(msg, code="invalid_arguments")
        radius = _integer(args, "radius_m")
        kind = None if role is None else str(role)
        try:
            if where.startswith("place:"):
                written = places.edit(
                    self._db, where, name=name, role=kind, radius_m=radius, origin="model",
                )
            else:
                lat, lng = self._point(where, ctx, int(ctx.clock.time() * 1000))
                written = places.save(
                    self._db, name=name, lat=lat, lng=lng, role=kind, radius_m=radius,
                    origin="model",
                )
        except (LookupError, ValueError) as exc:
            raise _refused(exc, "save_place") from exc
        line = (
            f"{written.place.name} was already kept like this" if written.change_id is None
            else f"kept {written.place.name}"
        )
        return Receipted(
            {
                "summary": line + ".",
                "kept": places.place_view(written.place, None),
                "unchanged": written.change_id is None,
            },
            Receipt(
                "done", reads_only=False, line=line, object=written.place.id,
                undo=written.change_id,
            ),
        )

    def forget(self, args: Mapping[str, Any], _ctx: ToolContext) -> Receipted:
        which = str(args.get("place", "")).strip()
        if not which:
            msg = "forget_place: place is required; pass a kept place's id or name from places"
            raise ToolError(msg, code="invalid_arguments")
        found = next(
            (
                p for p in places.read_kept(self._db)
                if which == p.id or p.name.casefold() == which.casefold()
            ),
            None,
        )
        if found is None:
            msg = f"forget_place: no kept place {which!r}; call places to see what is kept"
            raise ToolError(msg, code="not_found")
        try:
            written = places.forget(self._db, found.id, origin="model")
        except (LookupError, ValueError) as exc:
            raise _refused(exc, "forget_place") from exc
        return Receipted({"summary": f"Forgot {found.name}.", "forgotten": found.name}, Receipt(
            "done", reads_only=False, line=f"forgot {found.name}", object=found.id,
            undo=written.change_id,
        ))


def build_place_tools(
    memory_db_path: Path, reader: PlaceReader | None = None,
) -> tuple[Tool, ...]:
    """`places`, `save_place` and `forget_place` over memory.db and the phone's rows in the log.

    Days are cut in the context clock's zone.
    """
    tools = _PlaceTools(memory_db_path, reader)
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
                    "where": {"type": "string", "description": _WHERE},
                    "role": {
                        "type": "string",
                        "enum": list(places.ROLES),
                        "description": "What the place is to him: his home, his school or other.",
                    },
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
