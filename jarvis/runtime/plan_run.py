"""The plan watcher at run time (ADR 0385): her unprompted lines, due by the log, never chased.

Off unless ``day_plan.watch`` is true. Once a minute, in a thread, for a date that has a plan (or a
failed draft), the watcher reads the facts the log holds (:func:`jarvis.state.plan_facts.facts_at`),
what she has said today and what he switched off, and asks :func:`jarvis.decision.plan_gate.due`
which lines are due. Each is one of four kinds:

- ``morning`` and ``evening``: the ``line_preset`` model writes the words from the plan, the
  weather, the goals and what the log saw of the day; a model that fails is tried again the next
  minute, three times a day, and then the day goes without it. The evening also says where each
  task left undone goes, logged as ``plan.closed``.
- ``new``: a fixed line, no model (a thing with an hour that mail or the calendar added).

Every line is written into the conversation as her record ``plan-<date>-<key>`` and logged as
``plan.said``; it is never followed up. It is delivered by where he is: ``no-pop`` and ``dnd`` keep
it in the conversation; away from the Mac a silent push carries it; at the Mac nothing is spoken.
A call holds the line to the next minute, before anything is written.

Legs: :func:`make_route_legs` is the ``route_legs`` the 04:00 draft takes, and the watcher computes
the legs of items that lack one (a draft whose legs failed, an item he or she moved) when the
plan's places and times change.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import plan_gate
from jarvis.decision.day_plan import (
    build_evening_prompt,
    build_morning_prompt,
    parse_evening,
    parse_message,
)
from jarvis.execution.tools import ToolContext
from jarvis.runtime.day_plan import (
    DayPlanSettings,
    DayPlanSources,
    lived_lines,
    make_ask,
    memory_texts,
)
from jarvis.runtime.work_state import build_analyst
from jarvis.shared import lang
from jarvis.state import core_memory, places, plan, plan_budget, things
from jarvis.state.day_line import day_window
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import append_record, current_core_memory
from jarvis.state.plan_facts import facts_at

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from jarvis.execution.tools import RuntimePathsLike
    from jarvis.runtime.day_plan import Ask, RouteLegs
    from jarvis.shared.clock import Clock

LOGGER = logging.getLogger(__name__)

TICK_S: Final[float] = 60.0
LINE_ATTEMPTS: Final[int] = 3
"""How often the morning or the evening line is tried when the model fails, before the day goes
without it."""
MAX_OUTINGS: Final[int] = 6
"""The outings, soonest first, whose own weather the morning line is given."""
_CALL_TIMEOUT_S: Final[float] = 120.0
_HELD: Final[frozenset[str]] = frozenset({"no-pop", "dnd"})
_PUSH_TITLE: Final[str] = "Jarvis"
_PLACE_SHORT: Final[int] = 40
"""A place longer than this (a whole address) is left out of the new-thing line, as rings do."""
_PUSH_THREAD: Final[str] = "talk"
"""A push with this thread opens the conversation on the phone."""

type PlanLeg = Callable[..., dict[str, Any]]
type OpenLeg = Callable[[str], AbstractContextManager[PlanLeg | None]]
"""``open_leg(action_id)`` gives a ``plan_leg`` bound to its own event-log connection, or None."""
type LegFactory = Callable[[ToolContext], PlanLeg | None]


def leg_session(
    factory: LegFactory, event_log_path: Path, runtime_paths: RuntimePathsLike, clock: Clock,
) -> OpenLeg:
    """An :data:`OpenLeg` over ``factory`` (``build_leg_planner`` with the key and places bound)."""

    @contextmanager
    def session(action_id: str) -> Iterator[PlanLeg | None]:
        with closing(open_runtime_event_log(event_log_path)) as conn:
            yield factory(ToolContext(conn, runtime_paths, action_id, clock))

    return session


def _said(name: str) -> str:
    return " ".join(name.casefold().split())


def _shape(option: dict[str, Any]) -> dict[str, Any]:
    rides = option["legs"]
    return {
        "leave_at": option["leave_at"], "line": " > ".join(str(r["route"]) for r in rides),
        "board": rides[0]["board_stop"], "arrive_at": option["arrive_at"],
    }


def _route(origin: str, answer: dict[str, Any]) -> dict[str, Any]:
    """A plan item's ``route`` from a ``plan_leg`` answer: the chosen bus and its backup."""
    backup = answer.get("backup")
    return {"from": origin, **_shape(answer["chosen"]),
            "backup": None if backup is None else _shape(backup)}


def route_day(
    plan_leg: PlanLeg, items: Sequence[plan.PlanItem], day: date, home: frozenset[str],
) -> list[plan.PlanItem]:
    """``items`` with a ``route`` on each placed item that lacks one and is elsewhere.

    The day starts at home (``home``: the names it goes by). A leg arrives by the item's start;
    one that fails leaves ``route`` None. Only an item's own time and place clear its leg, so a
    dropped item before it does not recompute it.
    """
    # ponytail: the origin is the previous item's place, not where he will really be; add a
    # re-route on a dropped item when a wrong origin shows up in the replay.
    done = list(items)
    previous: str | None = None
    placed = sorted(
        (n for n, i in enumerate(items) if i.place and i.start), key=lambda n: str(items[n].start),
    )
    for n in placed:  # in time order: the list's order is the plan's, not the clock's
        item = items[n]
        same = _said(item.place) in home if previous is None else (
            _said(item.place) == _said(previous)
        )
        if item.route is None and not same:
            origin = previous or "home"
            try:
                answer = plan_leg(origin, item.place, arrive_by=f"{day.isoformat()} {item.start}")
                done[n] = replace(item, route=_route(origin, answer))
            except Exception as exc:  # noqa: BLE001 - a plan without a leg beats no plan.
                LOGGER.info("plan: no leg to %s: %s: %s", item.title, type(exc).__name__, exc)
        previous = item.place
    return done


def make_route_legs(open_leg: OpenLeg, memory_db: Path) -> RouteLegs:
    """The ``route_legs(items, day)`` the 04:00 draft takes (``draft_day``)."""

    def route_legs(items: list[plan.PlanItem], day: date) -> list[plan.PlanItem]:
        home = _home_names(places.read_kept(memory_db))
        with open_leg(f"plan-{day}-legs") as plan_leg:
            return items if plan_leg is None else route_day(plan_leg, items, day, home)

    return route_legs


@dataclass
class Hooks:
    """What the daemon wires in for telling him; until then nothing is held or pushed."""

    quiet: Callable[[], str] = lambda: "off"
    at_mac: Callable[[], bool] = lambda: False
    on_call: Callable[[], bool] = lambda: False
    push: Callable[..., object] | None = None
    """``Push.send``: blocking, so it is only called from the watcher's thread."""


class PlanWatch:
    """The watcher; :meth:`tick` is one minute's work, :meth:`run` the loop."""

    def __init__(  # noqa: PLR0913 - the settings, the stores, the model, legs, hooks, the clock.
        self,
        *,
        settings: DayPlanSettings,
        sources: DayPlanSources,
        ask: Ask | None,
        open_leg: OpenLeg | None = None,
        hooks: Hooks | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the model (None: its lines fail and are said so), the legs and the hooks."""
        self._settings = settings
        self._sources = sources
        self._ask = ask
        self._open_leg = open_leg
        self._hooks = hooks or Hooks()
        self._zone = sources.zone
        self._clock = clock or (lambda: datetime.now(sources.zone))
        self._failures: dict[tuple[date, str], int] = {}
        self._routed: dict[date, frozenset[tuple[str, str | None, str]]] = {}

    async def run(self) -> None:
        """Tick every :data:`TICK_S`; a failed tick is logged and the loop goes on."""
        LOGGER.info("plan watch started (every %.0f s)", TICK_S)
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                LOGGER.exception("plan watch: tick failed; trying again next time")
            await asyncio.sleep(TICK_S)

    # --- one tick ---------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> list[str]:
        """Do one minute's work at ``now``; the keys of the lines it said."""
        now = (now or self._clock()).astimezone(self._zone)
        day = now.date()
        said: list[str] = []
        with closing(open_runtime_event_log(self._sources.event_log_path)) as conn:
            held = plan.for_day(conn, day)
            if held is None and plan.failed(conn, day) is None:
                return []
            facts = facts_at(
                conn, day, int(now.timestamp() * 1000), self._zone,
                places.read_kept(self._sources.memory_db),
            )
            told = plan.said(conn, day)
            morning = told.get("morning")
            lines = plan_gate.due(
                held or plan.Plan(day, ()), facts, now, told.keys(), plan.muted(conn),
                day_ends=self._settings.day_ends,
                home=_home_names(places.read_kept(self._sources.memory_db)),
                first_said_ms=None if morning is None else morning.at_ms,
            )
            for line in lines:
                try:
                    if self._say(conn, now, held, line):
                        said.append(line.key)
                except Exception:
                    LOGGER.exception("plan watch: line %s failed", line.key)
            try:
                self._route(conn, day, held)
            except Exception:
                LOGGER.exception("plan watch: the legs failed")
            try:
                said += self._leave(conn, now, plan.for_day(conn, day))
            except Exception:
                LOGGER.exception("plan watch: the leave rings failed")
        return said

    # --- a line -----------------------------------------------------------------------

    def _say(
        self, conn: sqlite3.Connection, now: datetime, held: plan.Plan | None, line: plan_gate.Line,
    ) -> bool:
        """Write ``line`` and deliver it; False when it waits for the next minute.

        A call holds it before anything is written, and so does a model that fails: the record
        and ``plan.said`` go in together, once.
        """
        if self._hooks.on_call():
            return False
        day = now.date()
        unfinished: list[plan.Unfinished] = []
        if line.kind == "new":
            text = self._new_text(line)
        else:
            asked = self._asked(day, line.kind, lambda: self._words(conn, now, held, line))
            if asked is None:
                return False
            text, unfinished = asked
        record_id = f"plan-{day}-{line.key}"
        if line.kind == "evening":
            plan.close_day(conn, day, unfinished)
        append_record(self._sources.memory_db, record_id=record_id, source="jarvis", text=text)
        plan.say(conn, day, line.key, line.kind, text, record_id, int(now.timestamp() * 1000))
        self._deliver(text)
        return True

    def _asked[T](self, day: date, kind: str, build: Callable[[], T]) -> T | None:
        """``build()``, or None when it fails; not tried after :data:`LINE_ATTEMPTS` failures."""
        failed = self._failures.get((day, kind), 0)
        if failed >= LINE_ATTEMPTS:
            return None
        try:
            return build()
        except Exception as exc:  # noqa: BLE001 - a model that fails is tried again next minute.
            self._failures[day, kind] = failed + 1
            LOGGER.warning("plan watch: %s line failed: %s: %s", kind, type(exc).__name__, exc)
            if failed + 1 >= LINE_ATTEMPTS:
                LOGGER.warning("plan watch: %s: no %s line today", day, kind)
            return None

    def _words(
        self, conn: sqlite3.Connection, now: datetime, held: plan.Plan | None, line: plan_gate.Line,
    ) -> tuple[str, list[plan.Unfinished]]:
        """The model's words for the morning or the evening, and where the undone tasks go."""
        if self._ask is None:
            msg = f"no model: llm.presets.{self._settings.line_preset} is missing or malformed"
            raise RuntimeError(msg)
        today = held or plan.Plan(now.date(), ())
        if line.kind == "morning":
            system, user = build_morning_prompt(
                today, weather=self._weather(now.date()), goals=self._goals(), now=now,
                zone=self._zone, draft_failed=held is None, outings=self._outings(held),
            )
            return parse_message(self._ask(system, user)), []
        system, user = build_evening_prompt(
            today, lived=lived_lines(conn, self._sources, now.date(), now),
            tomorrow=self._tomorrow(conn, now.date()), now=now, zone=self._zone,
        )
        evening = parse_evening(self._ask(system, user), today)
        for note in evening.notes:
            LOGGER.info("plan watch: evening: %s", note)
        titles = {i.item_id: i.title for i in today.items}
        return evening.message, [
            plan.Unfinished(item_id, titles[item_id], to) for item_id, to in evening.unfinished
        ]

    @staticmethod
    def _new_text(line: plan_gate.Line) -> str:
        """The fixed line for a new hard thing; a private one is named by its time alone."""
        facts = line.facts
        if facts["private"]:
            return lang.t("plan.private", time=facts["start"])
        when = f"{lang.t('plan.new_' + facts['day'])} {facts['start']}"
        short = facts["place"] and len(facts["place"]) <= _PLACE_SHORT
        place = lang.t("plan.new_place", place=facts["place"]) if short else ""
        return lang.t("plan.new_thing", when=when, title=facts["title"], place=place)

    def _deliver(self, text: str) -> None:
        """Her line is in the conversation; away from the Mac a silent push carries it too.

        ``no-pop`` and ``dnd`` keep it in the conversation only; at the Mac nothing is spoken.
        """
        push = self._hooks.push
        if push is None or self._hooks.at_mac() or self._hooks.quiet() in _HELD:
            return
        try:
            push(_PUSH_TITLE, text, thread=_PUSH_THREAD, time_sensitive=False, sound=False)
        except Exception:
            LOGGER.exception("plan watch: the push failed")

    def _weather(self, day: date) -> dict[str, Any] | None:
        """The forecast of ``day``; a missing or failing source is None."""
        read = self._sources.weather
        if read is None:
            return None
        try:
            return read(day)
        except Exception as exc:  # noqa: BLE001 - the forecast is a courtesy.
            LOGGER.info("plan watch: weather unreadable: %s: %s", type(exc).__name__, exc)
            return None

    def _outing(
        self, held: plan.Plan, item: plan.PlanItem, kept: Sequence[places.Place],
    ) -> dict[str, Any] | None:
        """The weather of ``item``'s own hours, leave time to end, at its place (ADR 0355).

        The place's position is the kept place's; without it the hours are home's and the answer
        says so. None when there is no reader or it fails.
        """
        read = self._sources.outing_weather
        span = plan_budget.span_of(held, item, self._zone)
        if read is None or span is None or not item.place:
            return None
        leave = plan_budget.at(held, (item.route or {}).get("leave_at"), self._zone)
        point = places.position_of(kept, item.place)
        try:
            found = read(point, min(leave or span[0], span[0]), span[1])
        except Exception as exc:  # noqa: BLE001 - the forecast is a courtesy.
            LOGGER.info("plan watch: weather of %s unreadable: %s: %s", item.title,
                        type(exc).__name__, exc)
            return None
        if found is None:
            return None
        if point is None:
            return {"at": "home", "why_home": f"the position of {item.place} is not known", **found}
        return {"at": item.place, **found}

    def _outings(self, held: plan.Plan | None) -> list[dict[str, Any]]:
        """Each timed outing away from home, soonest first, with the weather of its own hours.

        A private outing is left out: the morning names it by its time alone.
        """
        if held is None or self._sources.outing_weather is None:
            return []
        kept = places.read_kept(self._sources.memory_db)
        home = _home_names(kept)
        timed = sorted(
            ((span[0], item) for item in held.items
             if item.place and not item.done and not item.private
             and _said(item.place) not in home
             and (span := plan_budget.span_of(held, item, self._zone))),
            key=lambda row: row[0],
        )
        found = ((item, self._outing(held, item, kept)) for _, item in timed[:MAX_OUTINGS])
        return [
            {"item_id": item.item_id, "place": item.place, "weather": weather}
            for item, weather in found if weather is not None
        ]

    def _goals(self) -> tuple[str, ...]:
        """His long-term goals from core memory; none when it cannot be read."""
        try:
            doc = current_core_memory(self._sources.memory_db).doc
        except Exception as exc:  # noqa: BLE001 - the goals are a courtesy.
            LOGGER.info("plan watch: core memory unreadable: %s: %s", type(exc).__name__, exc)
            return ()
        return memory_texts(doc, core_memory.GOALS_SECTION)

    def _tomorrow(self, conn: sqlite3.Connection, day: date) -> list[dict[str, Any]]:
        """Tomorrow's things with an hour, soonest first: title, start, place, private."""
        first = day_window(day + timedelta(days=1), self._zone)
        return [
            {"title": o.thing.title, "place": o.thing.place, "private": o.thing.private,
             "start": f"{datetime.fromtimestamp(o.start_ms / 1000, self._zone):%H:%M}"}
            for o in things.occurrences(conn, first[0], first[1], zone=self._zone)
            if not o.all_day and first[0] <= o.start_ms < first[1]
        ]

    # --- leave-by rings ---------------------------------------------------------------

    def _leave(self, conn: sqlite3.Connection, now: datetime, held: plan.Plan | None) -> list[str]:
        """Ring each placed thing of today at the leave time of its leg, instead of its default.

        Only for an item that is a thing of the ledger and still has the thing's own hour (an item
        that is an old copy of a moved thing is left alone), with a leave time still ahead. A
        first ring counts against :data:`jarvis.decision.plan_gate.PAIR_CAP` and is logged as
        ``plan.said`` (``leave:<id>``); a later one only follows the leg.
        """
        if held is None:
            return []
        day = now.date()
        told = plan.said(conn, day)
        room = plan_gate.PAIR_CAP - sum(1 for key in told if key.startswith(("new:", "leave:")))
        muted = "leave" in plan.muted(conn)
        ledger = things.fold(conn)
        rung: list[str] = []
        for item in held.items:
            leave_at = (item.route or {}).get("leave_at")
            one = ledger.get(plan.thing_of(item.source))
            if one is None or not item.start or not leave_at or not one.start_at:
                continue
            start = datetime.fromisoformat(one.start_at).astimezone(self._zone)
            leave = datetime.combine(day, time.fromisoformat(str(leave_at)), self._zone)
            if start.date() != day or f"{start:%H:%M}" != item.start or leave <= now:
                continue
            key = f"leave:{one.thing_id}"
            first = one.rings_from == "default"
            if first and (muted or key in told or room <= 0):
                continue
            lead = int((start - leave).total_seconds() // 60)
            if lead < 1:
                continue
            done = things.leave_ring(
                conn, one.thing_id, lead, by="plan", evidence=f"the leg leaves at {leave_at}",
                zone=self._zone, now=now,
            )
            if done == "set":
                plan.say(conn, day, key, "leave", f"{leave_at}", "", int(now.timestamp() * 1000))
                rung.append(key)
                room -= 1
        return rung

    # --- legs -------------------------------------------------------------------------

    def _route(self, conn: sqlite3.Connection, day: date, held: plan.Plan | None) -> None:
        """Compute the legs the plan lacks, once per set of places and times (ADR 0291)."""
        if held is None or self._open_leg is None:
            return
        shape = frozenset((i.item_id, i.start, i.place) for i in held.items)
        if self._routed.get(day) == shape:
            return
        self._routed[day] = shape
        if not any(i.place and i.start and i.route is None for i in held.items):
            return
        home = _home_names(places.read_kept(self._sources.memory_db))
        with self._open_leg(f"plan-{day}-routes") as plan_leg:
            if plan_leg is None:
                return
            items = route_day(plan_leg, held.items, day, home)
        if items != list(held.items):
            plan.revise(conn, day, items, "program", "legs")


def _home_names(kept: Iterable[places.Place]) -> frozenset[str]:
    """The names home goes by: ``home`` and each kept place of the home role."""
    return frozenset({"home", *(_said(p.name) for p in kept if p.role == "home")})


def build_watch(  # noqa: PLR0913 - the settings, the config, the stores, the legs and the hooks.
    settings: DayPlanSettings, *, config: Mapping[str, Any], pricing_path: Path,
    sources: DayPlanSources, open_leg: OpenLeg | None, hooks: Hooks,
) -> PlanWatch:
    """The watcher over ``sources``: its one model is ``settings.line_preset`` of ``config``."""
    ask = make_ask(
        build_analyst(
            config, settings.line_preset, pricing_path=pricing_path, account_cost=True,
            kind="day_plan", timeout_s=_CALL_TIMEOUT_S,
        ),
        sources.event_log_path,
    )
    return PlanWatch(settings=settings, sources=sources, ask=ask, open_leg=open_leg, hooks=hooks)


__all__ = [
    "Hooks", "PlanWatch", "build_watch", "leg_session", "make_route_legs", "route_day",
]
