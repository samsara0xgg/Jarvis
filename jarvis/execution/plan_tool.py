"""ADR 0289: his plan of a day, read and changed in conversation by the one tool ``plan``.

``read`` gives a day's items as compact rows, ``change`` applies add, edit and drop ops as one
``plan.revised`` by him, ``settle`` says the plan is fine as it stands. The fold, the ops and the
checks are :mod:`jarvis.state.plan`; this module only reads his arguments and words the answers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import plan
from jarvis.state.daily_report import resolve_zone

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Use when he asks to add, move, drop or change something in a day's plan, asks what is in"
    " the plan, or says the plan is fine. action=read: the day's items (id, title, start,"
    " minutes, place, firmness, route). action=change: changes is a list of ops, each"
    ' {"op": "add", "item": {title, start HH:MM, minutes, place, ...}}, {"op": "edit", "id":'
    ' "p3", <fields to change>} (moving an item is editing its start) or {"op": "drop", "id":'
    ' "p3"}; why is his reason in a few words. action=settle: he accepts the plan as it is.'
    " day is an ISO date, today by default. It changes only a day that already has a plan, and"
    " is saved only when this returns: never say it is changed without calling it. For"
    " something to remember rather than plan, use keep."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["read", "change", "settle"]},
        "day": {"type": "string", "description": "ISO date, 2026-10-16. Default: today."},
        "changes": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "change only. Ops: add {item: {title, start, minutes, place, firmness (fixed,"
                " movable, droppable), why, prepare, private, window_from, window_to}}; edit"
                " {id, <any of those fields>}; drop {id}."
            ),
        },
        "why": {"type": "string", "description": "change only: his reason, a few words."},
    },
    "required": ["action"],
    "additionalProperties": False,
}
_RESULT_CHARS: Final = 6000
_WHY_CHARS: Final = 200
_ROUTE: Final = ("from", "leave_at", "line", "board", "arrive_at")


def _now() -> datetime:
    return datetime.now(UTC)


def _row(item: plan.PlanItem) -> dict[str, Any]:
    """An item as a few words of JSON: only what it says."""
    row: dict[str, Any] = {
        "id": item.item_id, "title": item.title, "start": item.start, "minutes": item.minutes,
        "window_from": item.window_from, "window_to": item.window_to, "place": item.place,
        "firmness": item.firmness, "why": item.why, "goal": item.goal,
        "prepare": list(item.prepare), "private": item.private,
        "route": item.route and {k: item.route.get(k) for k in _ROUTE},
    }
    return {k: v for k, v in row.items() if v not in (None, "", [], False)}


class _Planner:
    def __init__(self, zone: tzinfo, clock: Callable[[], datetime]) -> None:
        self._zone = zone
        self._clock = clock

    def handle(self, args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        """Read, change or settle the day the arguments name."""
        action = args.get("action")
        if action not in ("read", "change", "settle"):
            msg = "plan: action must be read, change or settle"
            raise ToolError(msg, code="invalid_arguments")
        day = self._day(args.get("day"))
        held = plan.for_day(ctx.conn, day)
        if action == "read":
            return self._state(day, held)
        if held is None:
            msg = (
                f"plan: {day} has no plan yet (a day is planned at 04:00); to remember something"
                " for that day use keep"
            )
            raise ToolError(msg, code="no_plan")
        if action == "settle":
            if held.status != "settled":
                plan.settle(ctx.conn, day, "him")
            return {"summary": f"The plan for {day} is settled.", "day": day.isoformat()}
        return self._change(args, ctx, day, held)

    def _day(self, raw: object) -> date:
        if raw is None or raw == "":
            return self._clock().astimezone(self._zone).date()
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            msg = f'plan: day "{raw}" is not an ISO date like 2026-10-16'
            raise ToolError(msg, code="invalid_arguments") from None

    @staticmethod
    def _state(day: date, held: plan.Plan | None) -> dict[str, Any]:
        if held is None:
            return {"day": day.isoformat(), "plan": False, "items": [],
                    "summary": f"No plan for {day}."}
        return {
            "day": day.isoformat(), "plan": True, "settled": held.status == "settled",
            "source": held.source, "items": [_row(i) for i in held.items],
        }

    def _change(
        self, args: Mapping[str, Any], ctx: ToolContext, day: date, held: plan.Plan,
    ) -> dict[str, Any]:
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            msg = "plan: change needs changes, a list of add, edit and drop ops"
            raise ToolError(msg, code="invalid_arguments")
        why = args.get("why")
        why = " ".join(why.split())[:_WHY_CHARS] if isinstance(why, str) else ""
        items, notes = plan.apply_changes(held.items, changes)
        if items == list(held.items):
            return {"summary": "Nothing changed.", "changed": False, "notes": notes,
                    **self._state(day, held)}
        plan.revise(ctx.conn, day, items, "him", why or "his change")
        return {
            "summary": f"Plan for {day} changed: {plan.diff(held.items, items)}.",
            "changed": True, "notes": notes,
            **self._state(day, plan.for_day(ctx.conn, day)),
        }


def build_plan_tool(
    zone: tzinfo | None, clock: Callable[[], datetime] = _now,
) -> tuple[Tool, ...]:
    """The ``plan`` tool over the event log and his zone."""
    planner = _Planner(resolve_zone(None, zone)[1], clock)
    return (
        Tool(
            name="plan",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=planner.handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            max_result_chars=_RESULT_CHARS,
        ),
    )


__all__ = ["build_plan_tool"]
