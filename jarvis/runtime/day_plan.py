"""Runtime wiring for the day's plan draft (ADR 0288).

Once a day at ``day_plan.at``, and on a start after ``at`` and before ``day_ends`` when today has
neither a draft nor a failed draft, the schedule gathers the day's inputs, asks the model through
the ``day-plan`` skill, keeps what the parser accepts, lets the route planner fill in the legs and
writes ``plan.drafted``. A draft that cannot be made writes ``plan.draft_failed``, never an empty
day. The work runs in a thread off the loop. A day he already changed (the plan's last write is a
revision) is not redrafted.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.day_plan import (
    DayPlanInputs,
    build_prompt,
    candidate_things,
    day_labels,
    fixed_items,
    parse_items,
)
from jarvis.runtime.work_state import build_analyst
from jarvis.state import core_memory, places, plan, routines, things, work_state
from jarvis.state.day_line import day_window, fold_day
from jarvis.state.event_log import iter_events_of_types, open_runtime_event_log
from jarvis.state.memory_db import current_core_memory, local_now
from jarvis.state.plan_budget import DAY_ENDS

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.runtime.work_state import LLMAnalyst
    from jarvis.state.day_line import DaySources

LOGGER = logging.getLogger(__name__)

DEFAULT_AT: Final[time] = time(4, 0)
DEFAULT_PRESET: Final[str] = "sonnet-high"
DEFAULT_DECIDE_PRESET: Final[str] = "haiku"
DEFAULT_TOLD_PER_DAY: Final[int] = 8
DEFAULT_TOLD_PER_HOUR: Final[int] = 2
DEFAULT_HORIZON_DAYS: Final[int] = 3
MAX_HORIZON_DAYS: Final[int] = 14
HISTORY_DAYS: Final[int] = 14
"""How far back his changes to plans, and whether each plan settled, are read as preferences."""
MAX_CHANGES: Final[int] = 20
RECENT_DAYS: Final[int] = 7
"""A week of lived days, so weekday rhythm shows."""
MAX_MEMORY_ITEMS: Final[int] = 30
MAX_UPCOMING: Final[int] = 20
MAX_THINGS: Final[int] = 30
MAX_LINES_PER_DAY: Final[int] = 200
MAX_TOPS: Final[int] = 4
HOUR_MS: Final[int] = 3_600_000
_ITEM_CHARS: Final[int] = 300
_CALL_TIMEOUT_S: Final[float] = 300.0
"""A draft reads a few thousand tokens and writes a day; Sonnet at high effort needs minutes."""

type Ask = Callable[[str, str], str]
type RouteLegs = Callable[[list[plan.PlanItem], date], list[plan.PlanItem]]


def _count(value: object, default: int) -> int:
    """``value`` when it is a whole number of 1 or more, else ``default``."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return default


def day_ends_of(raw: object) -> time:
    """``day_plan.day_ends`` of the block ``raw`` (``HH:MM``), else the default."""
    block: Mapping[str, object] = raw if isinstance(raw, Mapping) else {}
    try:
        return time.fromisoformat(str(block["day_ends"]))
    except (KeyError, ValueError):
        return DAY_ENDS


@dataclass(frozen=True)
class DayPlanSettings:
    """The ``day_plan:`` block of ``config/jarvis.yaml``."""

    at: time = DEFAULT_AT
    preset: str = DEFAULT_PRESET
    horizon_days: int = DEFAULT_HORIZON_DAYS
    morning_push: bool = False
    watch: bool = False
    decide_preset: str = DEFAULT_DECIDE_PRESET
    told_per_day: int = DEFAULT_TOLD_PER_DAY
    told_per_hour: int = DEFAULT_TOLD_PER_HOUR
    day_ends: time = DAY_ENDS

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
        decide = block.get("decide_preset")
        horizon = block.get("horizon_days")
        return cls(
            at=at,
            preset=preset if isinstance(preset, str) and preset.strip() else DEFAULT_PRESET,
            horizon_days=(
                horizon if isinstance(horizon, int) and 1 <= horizon <= MAX_HORIZON_DAYS
                else DEFAULT_HORIZON_DAYS
            ),
            morning_push=block.get("morning_push") is True,
            watch=block.get("watch") is True,
            decide_preset=(
                decide if isinstance(decide, str) and decide.strip() else DEFAULT_DECIDE_PRESET
            ),
            told_per_day=_count(block.get("told_per_day"), DEFAULT_TOLD_PER_DAY),
            told_per_hour=_count(block.get("told_per_hour"), DEFAULT_TOLD_PER_HOUR),
            day_ends=day_ends_of(block),
        )


@dataclass(frozen=True)
class DayPlanSources:
    """What the inputs are read from. A reader left None is a source not connected (it is noted)."""

    event_log_path: Path
    memory_db: Path
    zone: tzinfo
    day_sources: DaySources
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


def _clock(ms: int | None, zone: tzinfo) -> str:
    return "now" if ms is None else f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def _minutes(seconds: float) -> str:
    hours, minutes = divmod(round(seconds / 60), 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes}m"


def _named(tops: list[dict[str, Any]]) -> str:
    return ", ".join(f"{top['name']} {_minutes(top['seconds'])}" for top in tops[:MAX_TOPS])


def _where(item: Mapping[str, Any]) -> str:
    kept = item.get("kept_place")
    if kept:
        return str(kept["name"])
    if item.get("place"):
        return str(item["place"])
    return f"{item['lat']:.3f},{item['lng']:.3f}"


def _work(item: Mapping[str, Any]) -> str:
    what = f"on the Mac, {_minutes(item['active_s'])} active"
    if item.get("projects"):
        what += f"; projects {_named(item['projects'])}"
    if item.get("apps"):
        what += f"; apps {_named(item['apps'])}"
    return what


def _screen(item: Mapping[str, Any]) -> str:
    apps = ", ".join(f"{one['name']} {one['minutes']}m" for one in item["apps"][:MAX_TOPS])
    return f"on his {item['device']}, {_minutes(item['active_s'])} active; apps {apps}"


_SAID: Final[dict[str, Callable[[Mapping[str, Any]], str]]] = {
    "work": _work,
    "screen": _screen,
    "media": lambda item: (
        f"played {item['app']} on his {item['device']}, {_minutes(item['active_s'])}"
    ),
    "earbuds": lambda item: f"{item['device_name']} connected to his {item['device']}",
    "messages": lambda item: (
        f"messages in {item['app']}: {item['sent']} sent, {item['received']} received"
    ),
    "phone_call": lambda item: f"phone call ({item['direction']}, {_minutes(item['duration_s'])})",
    "stay": lambda item: f"at {_where(item)}",
    "move": lambda item: str(item.get("activity") or "moving"),
    "call": lambda item: f"call ({item.get('app') or 'app unknown'})",
    "talk": lambda item: f"talked with Jarvis, {item.get('turns', 0)} turns",
    "payment": lambda item: (
        f"{item.get('direction')} {item.get('amount')} {item.get('currency')} "
        f"{item.get('counterparty') or ''} {item.get('text') or ''}"
    ),
    "claude_chat": lambda item: f"Claude chat: {item.get('text')}, {item.get('messages')} messages",
    "picture": lambda item: f"sent a picture: {item.get('note') or 'no note'}",
}


def lived_line(item: Mapping[str, Any]) -> str:
    """What one item of a lived day was, in a few words."""
    kind = str(item["kind"])
    say = _SAID.get(kind)
    what = say(item) if say else f"{kind}: {item.get('title') or item.get('text') or ''}"
    return _clip(what.strip())


def _doing(conn: Any, first: int, last: int, zone: tzinfo) -> list[str]:  # noqa: ANN401
    """What the work state said he was doing (ADR 0201's screen reading), hour by hour.

    The last revision of each hour stands for it; an activity already said earlier that day is not
    said again.
    """
    hourly: dict[int, list[str]] = {}
    for event in iter_events_of_types(conn, (work_state.EVENT_TYPE,), since_epoch_ms=first):
        if event.ts_epoch_ms >= last:
            continue
        item = event.payload.get("item")
        if isinstance(item, dict):
            hourly[event.ts_epoch_ms // HOUR_MS] = [
                str(one.get("text") or "") for one in item.get("activities") or []
            ]
    said: dict[str, str] = {}
    for hour in sorted(hourly):
        at = f"{datetime.fromtimestamp(hour * HOUR_MS / 1000, zone):%H:00}"
        for text in hourly[hour]:
            if text and text not in said:
                said[text] = f"{at} {_clip(text)}"
    return list(said.values())


def _story(memory_db: Path, day: date) -> str | None:
    """Her account of that day, written the night after (ADR 0235's prose), or None."""
    try:
        with closing(sqlite3.connect(f"file:{memory_db}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT text FROM day_prose WHERE day = ? AND draft = 0 ORDER BY ts DESC LIMIT 1",
                (day.isoformat(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None


def _recent(
    conn: Any, sources: DayPlanSources, day: date, now: datetime,  # noqa: ANN401 - a sqlite conn
) -> tuple[dict[str, Any], ...]:
    """The last days as he lived them: each day's items in time order, one line each."""
    now_ms = int(now.timestamp() * 1000)
    rows: list[dict[str, Any]] = []
    for back in range(RECENT_DAYS, 0, -1):
        past = day - timedelta(days=back)
        first, last = day_window(past, sources.zone)
        line = fold_day(conn, first, last, now_ms, sources.day_sources)
        timeline = [
            f"{_clock(item['start_ms'], sources.zone)}-{_clock(item.get('end_ms'), sources.zone)} "
            f"{lived_line(item)}"
            for item in line.items
            if item.get("source") != "plan"  # the plan is not what he lived
        ]
        rows.append({
            "date": past.isoformat(),
            "weekday": f"{past:%A}",
            "all_day": [str(label["title"]) for label in line.labels],
            "story": _story(sources.memory_db, past),
            "timeline": timeline[:MAX_LINES_PER_DAY],
            "doing": _doing(conn, first, last, sources.zone)[:MAX_LINES_PER_DAY],
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
    first = day_window(day, zone)[0]
    horizon_end = day_window(day + timedelta(days=settings.horizon_days), zone)[0]
    with closing(open_runtime_event_log(sources.event_log_path)) as conn:
        occurrences = things.occurrences(conn, first, horizon_end, zone=zone)
        open_now = things.open_things(conn)
        fixed = fixed_items(occurrences, day, zone)
        upcoming = _upcoming(occurrences, day, zone)
        held = _read(partial(_routines, conn, sources, day), "routines", notes) or []
        recent = _read(partial(_recent, conn, sources, day, now), "recent_days", notes) or ()
        changes, past = _preferences(conn, day)
    labels = day_labels(occurrences, day, zone)
    ahead = candidate_things(
        open_now, occurrences, day, zone, day + timedelta(days=settings.horizon_days), MAX_THINGS,
    )
    weather = _read(sources.weather and partial(sources.weather, day), "weather", notes)
    try:
        doc = current_core_memory(sources.memory_db).doc
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("day_plan: core memory unreadable: %s: %s", type(exc).__name__, exc)
        doc = {}
        notes.append("memory")
    return DayPlanInputs(
        day=day,
        fixed=tuple(fixed),
        upcoming=upcoming,
        calendar_all_day=tuple(labels),
        things=tuple(ahead),
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
        sources=_sources(tuple(fixed), upcoming, ahead, held),
    )


def _upcoming(
    occurrences: Sequence[things.Occurrence], day: date, zone: tzinfo,
) -> tuple[Mapping[str, Any], ...]:
    """What he goes to or does on the days after ``day`` within the horizon, soonest first."""
    rows: list[Mapping[str, Any]] = []
    for occ in occurrences:
        local = datetime.fromtimestamp(occ.start_ms / 1000, zone)
        if local.date() > day:
            one = occ.thing
            rows.append({
                "date": local.date().isoformat(), "title": one.title, "place": one.place,
                "start": None if occ.all_day else f"{local:%H:%M}",
                "confirmed": one.confirmed, "source": f"{plan.ITEM_PREFIX}{one.thing_id}",
            })
    return tuple(rows[:MAX_UPCOMING])


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


def _sources(
    fixed: tuple[plan.PlanItem, ...], upcoming: tuple[Mapping[str, Any], ...],
    ahead: list[dict[str, Any]], held: list[routines.Routine],
) -> tuple[str, ...]:
    names = {*plan.SOURCE_NAMES, *(i.source for i in fixed), *(str(u["source"]) for u in upcoming)}
    names |= {str(a["source"]) for a in ahead}
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

        A start after ``at`` catches up at once when today has no draft and no failure and is not
        over (``day_ends``), and otherwise waits for tomorrow; a start before ``at`` waits for it,
        so a day is drafted once. Later checks draft once per date, from ``at`` on.
        """
        local = now.astimezone(self._zone)
        today, past_at = local.date(), local.time() >= self._settings.at
        if not self._checked:
            self._checked = True
            if not past_at:
                return None
            self._last = today
            over = local.time() >= self._settings.day_ends
            return None if over or self._has_state(today) else today
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
    "day_ends_of",
    "draft_day",
    "gather",
    "make_ask",
]
