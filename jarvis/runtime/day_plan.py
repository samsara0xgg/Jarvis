"""Runtime wiring for the day's plan draft (ADR 0288).

Once a day at ``day_plan.at``, and on start when today has neither a draft nor a failed draft,
the schedule gathers the day's inputs, asks the model through the ``day-plan`` skill, keeps what
the parser accepts, lets the route planner fill in the legs and writes ``plan.drafted``. A draft
that cannot be made writes ``plan.draft_failed``, never an empty day. The work runs in a thread
off the loop. A day he already changed (the plan's last write is a revision) is not redrafted.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.day_plan import (
    DayPlanInputs,
    build_prompt,
    calendar_items,
    commitment_items,
    parse_items,
)
from jarvis.runtime.work_state import build_analyst
from jarvis.state import commitments, core_memory, places, plan, routines
from jarvis.state.day_line import day_window, fold_day
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import current_core_memory, local_now

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.runtime.work_state import LLMAnalyst
    from jarvis.state.day_line import CalendarRead, DaySources

LOGGER = logging.getLogger(__name__)

DEFAULT_AT: Final[time] = time(4, 0)
DEFAULT_PRESET: Final[str] = "sonnet-high"
DEFAULT_HORIZON_DAYS: Final[int] = 3
MAX_HORIZON_DAYS: Final[int] = 14
HISTORY_DAYS: Final[int] = 14
"""How far back his changes to plans, and whether each plan settled, are read as preferences."""
MAX_CHANGES: Final[int] = 20
RECENT_DAYS: Final[int] = 3
MAX_MEMORY_ITEMS: Final[int] = 30
MAX_UPCOMING: Final[int] = 20
MAX_TODO: Final[int] = 30
MAX_EVENTS_PER_DAY: Final[int] = 8
_ITEM_CHARS: Final[int] = 300
_CALL_TIMEOUT_S: Final[float] = 300.0
"""A draft reads a few thousand tokens and writes a day; Sonnet at high effort needs minutes."""
_EVENT_KINDS: Final[frozenset[str]] = frozenset({"event", "departure", "ride", "reminder", "call"})

type Ask = Callable[[str, str], str]
type RouteLegs = Callable[[list[plan.PlanItem], date], list[plan.PlanItem]]


@dataclass(frozen=True)
class DayPlanSettings:
    """The ``day_plan:`` block of ``config/jarvis.yaml``."""

    at: time = DEFAULT_AT
    preset: str = DEFAULT_PRESET
    horizon_days: int = DEFAULT_HORIZON_DAYS
    morning_push: bool = False

    @classmethod
    def from_config(cls, raw: object) -> DayPlanSettings | None:
        """The settings, or None (feature off) unless ``enabled`` is true and ``at`` reads."""
        block: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
        if block.get("enabled") is not True:
            return None
        try:
            at = time.fromisoformat(str(block.get("at") or DEFAULT_AT.isoformat("minutes")))
        except ValueError:
            LOGGER.warning("day_plan.at %r is not HH:MM; no plan is drafted", block["at"])
            return None
        preset = block.get("preset")
        horizon = block.get("horizon_days")
        return cls(
            at=at,
            preset=preset if isinstance(preset, str) and preset.strip() else DEFAULT_PRESET,
            horizon_days=(
                horizon if isinstance(horizon, int) and 1 <= horizon <= MAX_HORIZON_DAYS
                else DEFAULT_HORIZON_DAYS
            ),
            morning_push=block.get("morning_push") is True,
        )


@dataclass(frozen=True)
class DayPlanSources:
    """What the inputs are read from. A reader left None is a source not connected (it is noted)."""

    event_log_path: Path
    memory_db: Path
    zone: tzinfo
    day_sources: DaySources
    calendar: CalendarRead | None = None
    todos: Callable[[], list[dict[str, Any]]] | None = None
    weather: Callable[[date], dict[str, Any] | None] | None = None


def make_ask(analyst: LLMAnalyst | None, event_log_path: Path) -> Ask | None:
    """``(system, user) -> text`` over the analyst's preset, accounted as ``day_plan``."""
    if analyst is None:
        return None

    def ask(system: str, user: str) -> str:
        with closing(open_runtime_event_log(event_log_path)) as conn:
            result = analyst.analyze(
                conn, system=system, messages=[{"role": "user", "content": user}],
                tools=[], tool_choice="none",
            )
        return (result.text or "").strip()

    return ask


def _clip(text: str) -> str:
    return " ".join(text.split())[:_ITEM_CHARS]


def _memory_texts(doc: core_memory.Doc, section: str) -> tuple[str, ...]:
    items = doc.get(section, [])[:MAX_MEMORY_ITEMS]
    return tuple(_clip(str(item.get("text") or "")) for item in items if item.get("text"))


def _recent(
    conn: Any, sources: DayPlanSources, day: date, now: datetime,  # noqa: ANN401 - a sqlite conn
) -> tuple[dict[str, Any], ...]:
    """The last days summarised: their dated events and a count of every kind of item."""
    now_ms = int(now.timestamp() * 1000)
    rows: list[dict[str, Any]] = []
    for back in range(RECENT_DAYS, 0, -1):
        past = day - timedelta(days=back)
        first, last = day_window(past, sources.zone)
        line = fold_day(conn, first, last, now_ms, sources.day_sources)
        counts: dict[str, int] = {}
        events: list[str] = []
        for item in line.items:
            kind = str(item["kind"])
            counts[kind] = counts.get(kind, 0) + 1
            if kind in _EVENT_KINDS and len(events) < MAX_EVENTS_PER_DAY:
                at = datetime.fromtimestamp(item["start_ms"] / 1000, sources.zone)
                said = _clip(str(item.get("title") or item.get("text") or kind))
                events.append(f"{at:%H:%M} {said}")
        rows.append({
            "date": past.isoformat(), "counts": counts, "events": events,
            "labels": [str(label["title"]) for label in line.labels][:MAX_EVENTS_PER_DAY],
            "unreadable": line.missing,
        })
    return tuple(rows)


def _routines(conn: Any, sources: DayPlanSources, day: date) -> list[routines.Routine]:  # noqa: ANN401
    kept = places.read_kept(sources.memory_db)
    return routines.for_day(conn, day, zone=sources.zone, places=kept)


def _preferences(
    conn: Any, day: date,  # noqa: ANN401 - a sqlite conn
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """``(his_changes, past_plans)``: his revisions and how each earlier plan settled."""
    first = day - timedelta(days=HISTORY_DAYS)
    changes = tuple(
        {"date": r.day, "change": r.change, "why": r.why}
        for r in plan.revisions(conn, first, day, by="him")[:MAX_CHANGES]
    )
    held = plan.fold(conn)
    past = tuple(
        {"date": key, "settled": held[key].settled_by or "no"}
        for key in sorted(held, reverse=True)
        if first.isoformat() <= key < day.isoformat()
    )
    return changes, past


def gather(
    sources: DayPlanSources, settings: DayPlanSettings, day: date, now: datetime,
) -> DayPlanInputs:
    """The draft's inputs for ``day``; a source that cannot be read is noted, never fatal."""
    zone = sources.zone
    notes: list[str] = []
    first, last = day_window(day, zone)
    horizon_end = day_window(day + timedelta(days=settings.horizon_days), zone)[0]
    with closing(open_runtime_event_log(sources.event_log_path)) as conn:
        occurrences = commitments.occurrences(conn, first, horizon_end, zone=zone)
        fixed = commitment_items(occurrences, day, zone)
        upcoming = tuple(
            {
                "date": o.day, "title": o.commitment.title, "place": o.commitment.place,
                "start": None if o.commitment.all_day
                else datetime.fromtimestamp(o.start_ms / 1000, zone).strftime("%H:%M"),
                "confirmed": o.commitment.confirmed,
                "source": f"commitment:{o.commitment.commitment_id}",
            }
            for o in occurrences if o.day != day.isoformat()
        )[:MAX_UPCOMING]
        held = _read(partial(_routines, conn, sources, day), "routines", notes) or []
        recent = _read(partial(_recent, conn, sources, day, now), "recent_days", notes) or ()
        changes, past = _preferences(conn, day)
    now_ms = int(now.timestamp() * 1000)
    read_calendar = sources.calendar and partial(sources.calendar, first, last, now_ms)
    events = _read(read_calendar, "calendar", notes)
    extra, labels = calendar_items(events or [], day, zone, fixed)
    todo = _todos(sources, day, settings, notes)
    weather = _read(sources.weather and partial(sources.weather, day), "weather", notes)
    try:
        doc = current_core_memory(sources.memory_db).doc
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("day_plan: core memory unreadable: %s: %s", type(exc).__name__, exc)
        doc = {}
        notes.append("memory")
    all_fixed = (*fixed, *extra)
    return DayPlanInputs(
        day=day,
        fixed=all_fixed,
        upcoming=upcoming,
        calendar_all_day=tuple(labels),
        todo=todo,
        goals=_memory_texts(doc, core_memory.GOALS_SECTION),
        deadlines=_memory_texts(doc, core_memory.ID_FREE_SECTION),
        routines=tuple(
            {"start": r.start, "end": r.end, "what": r.what, "place": r.place,
             "source": f"routine:{r.routine_id}"}
            for r in held
        ),
        weather=weather,
        recent_days=recent,
        his_changes=changes,
        past_plans=past,
        notes=tuple(notes),
        sources=_sources(all_fixed, upcoming, held),
    )


def _read[T](read: Callable[[], T] | None, name: str, notes: list[str]) -> T | None:
    """``read()``, or None with ``name`` noted when there is no reader, it fails or gives none."""
    if read is not None:
        try:
            found = read()
        except Exception as exc:  # noqa: BLE001 - not connected, not answering: the plan goes on.
            LOGGER.info("day_plan: %s unreadable: %s: %s", name, type(exc).__name__, exc)
        else:
            if found is not None:
                return found
    notes.append(name)
    return None


def _todos(
    sources: DayPlanSources, day: date, settings: DayPlanSettings, notes: list[str],
) -> tuple[Mapping[str, Any], ...]:
    """Open To Do tasks due by the end of the horizon (overdue ones included)."""
    if sources.todos is None:
        notes.append("todo")
        return ()
    try:
        rows = sources.todos()
    except Exception as exc:  # noqa: BLE001
        LOGGER.info("day_plan: To Do unreadable: %s: %s", type(exc).__name__, exc)
        notes.append("todo")
        return ()
    limit = (day + timedelta(days=settings.horizon_days)).isoformat()
    due = [
        {"title": _clip(str(r.get("title") or "")), "due": str(r["due"])[:10]}
        for r in rows if r.get("due") and str(r["due"])[:10] <= limit
    ]
    return tuple(sorted(due, key=lambda r: r["due"])[:MAX_TODO])


def _sources(
    fixed: tuple[plan.PlanItem, ...], upcoming: tuple[Mapping[str, Any], ...],
    held: list[routines.Routine],
) -> tuple[str, ...]:
    names = {*plan.SOURCE_NAMES, *(i.source for i in fixed), *(str(u["source"]) for u in upcoming)}
    names |= {f"routine:{r.routine_id}" for r in held}
    return tuple(sorted(names))


def draft_day(  # noqa: PLR0913 - the sources, the knobs, the model, the clock and the planner.
    sources: DayPlanSources, settings: DayPlanSettings, ask: Ask | None, day: date,
    now: datetime, route_legs: RouteLegs | None = None,
) -> str:
    """Draft ``day``'s plan; ``drafted``, ``kept`` (he changed it already) or ``failed``.

    ``route_legs(items, day)`` computes the legs between the placed items and returns the items
    with their routes; the default leaves them as they are. Every failure writes
    ``plan.draft_failed``; nothing is ever written as an empty day.
    """
    try:
        with closing(open_runtime_event_log(sources.event_log_path)) as conn:
            held = plan.for_day(conn, day)
        if held is not None and held.source.startswith("revised"):
            return "kept"
        if ask is None:
            msg = f"no model: llm.presets.{settings.preset} is missing or malformed"
            raise RuntimeError(msg)  # noqa: TRY301 - one failure path for every cause
        inputs = gather(sources, settings, day, now)
        today = now.astimezone(sources.zone).date()
        system, user = build_prompt(inputs, zone=sources.zone, today=today)
        items, notes = parse_items(
            ask(system, user), known_sources=inputs.sources, fixed=inputs.fixed,
        )
        if not items:
            msg = f"no usable plan: {'; '.join(notes) or 'empty'}"
            raise ValueError(msg)  # noqa: TRY301
        for note in notes:
            LOGGER.info("day_plan: %s: %s", day, note)
        if route_legs is not None:
            try:
                items = route_legs(items, day)
            except Exception as exc:  # noqa: BLE001 - a plan without legs beats no plan.
                LOGGER.warning("day_plan: legs not computed: %s: %s", type(exc).__name__, exc)
        with closing(open_runtime_event_log(sources.event_log_path)) as conn:
            plan.draft(conn, day, items, inputs.digest(), settings.preset)
    except Exception as exc:  # noqa: BLE001 - the day gets a failure on record, not a crash.
        LOGGER.warning("day_plan: draft for %s failed: %s: %s", day, type(exc).__name__, exc)
        with closing(open_runtime_event_log(sources.event_log_path)) as conn:
            plan.draft_failed(conn, day, f"{type(exc).__name__}: {exc}")
        return "failed"
    return "drafted"


class DayPlanSchedule:
    """Checks every ``poll_s``: drafts once per local date after ``at``, and once on start."""

    def __init__(
        self,
        *,
        settings: DayPlanSettings,
        zone: tzinfo,
        draft: Callable[[date, datetime], str],
        has_state: Callable[[date], bool],
        poll_s: float = 60.0,
    ) -> None:
        """Bind the draft job and the test of whether a day already has a draft or a failure."""
        self._settings = settings
        self._zone = zone
        self._draft = draft
        self._has_state = has_state
        self._poll_s = poll_s
        self._last: date | None = None
        self._checked = False

    def due(self, now: datetime) -> date | None:
        """Today when it should be drafted now, else None.

        The first check after a start drafts at once if today has no draft and no failure, at any
        hour; a start after ``at`` with state on record waits for tomorrow. Later checks draft
        once per date, from ``at`` on.
        """
        local = now.astimezone(self._zone)
        today, past_at = local.date(), local.time() >= self._settings.at
        if not self._checked:
            self._checked = True
            if past_at:
                self._last = today
            return None if self._has_state(today) else today
        if not past_at or today == self._last:
            return None
        self._last = today
        return today

    async def run(self) -> None:
        """The loop; a failed run is logged and waits for the next date."""
        while True:
            now = local_now()
            today = await asyncio.to_thread(self.due, now)
            if today is not None:
                try:
                    outcome = await asyncio.to_thread(self._draft, today, now)
                except Exception:
                    LOGGER.exception("day_plan: scheduled run for %s failed", today)
                else:
                    LOGGER.info("day_plan: %s -> %s", today, outcome)
            await asyncio.sleep(self._poll_s)


def build_schedule(
    settings: DayPlanSettings, *, config: Mapping[str, Any], pricing_path: Path,
    sources: DayPlanSources, route_legs: RouteLegs | None = None,
) -> DayPlanSchedule:
    """The schedule over ``sources``; the model is the ``settings.preset`` of ``config``."""
    ask = make_ask(
        build_analyst(
            config, settings.preset, pricing_path=pricing_path, account_cost=True,
            kind="day_plan", timeout_s=_CALL_TIMEOUT_S,
        ),
        sources.event_log_path,
    )

    def has_state(day: date) -> bool:
        with closing(open_runtime_event_log(sources.event_log_path)) as conn:
            return plan.for_day(conn, day) is not None or plan.failed(conn, day) is not None

    return DayPlanSchedule(
        settings=settings, zone=sources.zone, has_state=has_state,
        draft=lambda day, now: draft_day(sources, settings, ask, day, now, route_legs),
    )


__all__ = [
    "DayPlanSchedule",
    "DayPlanSettings",
    "DayPlanSources",
    "build_schedule",
    "draft_day",
    "gather",
    "make_ask",
]
