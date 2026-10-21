"""ADR 0291: his plan of a day, read and changed in conversation by the one tool ``plan``.

``read`` gives a day's items in order as compact rows (tasks with a done mark, then the time left
against the tasks left), ``change`` applies add, edit, move and drop ops as one ``plan.revised``
by him, ``settle`` says the plan is fine as it stands. The fold, the ops and the
checks are :mod:`jarvis.state.plan`; this module only reads his arguments and words the answers.
"""

from __future__ import annotations

from datetime import date, time
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import plan
from jarvis.state.plan_budget import DAY_ENDS, budget

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.execution.tools import ToolContext
    from jarvis.shared.clock import Clock

_DESCRIPTION: Final = (
    "Use when he asks to add, move, drop or change something in a day's plan, asks what is in"
    " it, or says it is fine. While today's plan is opened and not settled, you are settling it"
    " with him: ask one question at a time about what would change the day, then propose it"
    " with its buses, and settle it once he agrees. A change he states is his answer: make it at"
    " once; your proposals wait for his yes."
    " It changes only a day that already has a plan, and a change is"
    " saved only when this returns: never say it is changed without calling it. For something"
    " he has to go to or do rather than plan, use schedule."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["read", "change", "settle"],
            "description": (
                "read: the day's items in order (id, title, start, minutes, place, firmness,"
                " route, done) and the time left against the tasks left. change: apply"
                " changes. settle: he accepts the plan as it is."
            ),
        },
        "day": {"type": "string", "description": "ISO date, 2026-10-16. Default: today."},
        "changes": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "change only. Ops: add {item: {title, start, minutes, place, firmness (fixed,"
                " movable, droppable), why, prepare, private, window_from, window_to}, after (an"
                " id; default the end)}; edit {id, <any of those fields>, done}; move {id, after"
                " (an id, or null for first)}; drop {id}."
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


def _row(item: plan.PlanItem) -> dict[str, Any]:
    """An item as a few words of JSON: only what it says."""
    row: dict[str, Any] = {
        "id": item.item_id, "title": item.title, "start": item.start, "minutes": item.minutes,
        "window_from": item.window_from, "window_to": item.window_to, "place": item.place,
        "firmness": item.firmness, "why": item.why, "goal": item.goal,
        "prepare": list(item.prepare), "private": item.private, "done": item.done,
        "route": item.route and {k: item.route.get(k) for k in _ROUTE},
    }
    return {k: v for k, v in row.items() if v not in (None, "", [], False)}


class _Planner:
    def __init__(self, clock: Clock, day_ends: time) -> None:
        self._zone = clock.zone
        self._clock = clock.now
        self._day_ends = day_ends

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
                f"plan: {day} has no plan yet (a day is planned at 04:00); to add something"
                " for that day use schedule"
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

    def _state(self, day: date, held: plan.Plan | None) -> dict[str, Any]:
        if held is None:
            return {"day": day.isoformat(), "plan": False, "items": [],
                    "summary": f"No plan for {day}."}
        state: dict[str, Any] = {
            "day": day.isoformat(), "plan": True, "settled": held.status == "settled",
            "source": held.source, "items": [_row(i) for i in held.items],
        }
        now = self._clock().astimezone(self._zone)
        if day == now.date():  # the time left only means something for today
            left = budget(held, now, day_ends=self._day_ends)
            state["budget"] = {
                "tasks_left": len(left.tasks), "remaining_min": left.remaining_min,
                "free_min": left.free_min, "fits": left.fits,
                "next_task": left.next_task and left.next_task.title,
            }
        return state

    def _change(
        self, args: Mapping[str, Any], ctx: ToolContext, day: date, held: plan.Plan,
    ) -> dict[str, Any]:
        changes = args.get("changes")
        if not isinstance(changes, list) or not changes:
            msg = "plan: change needs changes, a list of add, edit, move and drop ops"
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


def build_plan_tool(day_ends: time | None = None) -> tuple[Tool, ...]:
    """The ``plan`` tool over the event log; ``day_ends`` bounds the time left."""

    def handle(args: Mapping[str, Any], ctx: ToolContext) -> dict[str, Any]:
        return _Planner(ctx.clock, day_ends or DAY_ENDS).handle(args, ctx)

    return (
        Tool(
            name="plan",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=handle,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            max_result_chars=_RESULT_CHARS,
        ),
    )


__all__ = ["build_plan_tool"]
