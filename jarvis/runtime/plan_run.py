"""The plan watcher at run time (ADR 0291): notice, ask, tell.

Off unless ``day_plan.watch`` is true. Once a minute, in a thread, for a date that has a plan (or a
failed draft), the watcher reads the facts the log holds (:func:`jarvis.state.plan_facts.facts_at`),
asks :func:`jarvis.decision.plan_watch.notice` what changed, writes each new notice as
``plan.noticed`` and then:

- ``woke`` opens the day (ADR 0288): her opening is written into the conversation as her record
  ``plan-open-<date>`` and ``plan.opened`` is logged. It is never judged. The opening starts a
  conversation she leads until the plan is settled: when her last words there wait unanswered
  (10, 15, 20, then 30 minutes), she follows up, at most six times a day (``unsettled:<n>``).
- ``outing_soon`` first reads the weather and the leg again, so she is asked with both.
- every other notice goes to the ``decide_preset`` model; its answer is logged as ``plan.decided``.
  ``adjust`` also revises the plan by her: it may mark tasks done, move or drop them.
- a ``progress`` notice goes in two steps. First a done-check: the model reads the tasks and the
  evidence since the last check (screen, his words, commits) and says which are done and how many
  minutes a task under way still needs; the program applies that as an edit by her and logs it as
  ``plan.decided`` under ``<key>:done``. Then the budget is worked out again on the updated plan:
  if the tasks fit the time left she stays silent (no second call); if not, the normal decision
  is asked with the fresh budget, and obeys the quiet level and the caps.

What she says (``tell``, ``adjust``) is always written into the conversation as her record. It is
spoken on the Mac and pushed to the phone under the limits the ADR fixes in code: outing notices
obey speech mute, a live conversation and a call, as reminders do; every other notice obeys the
quiet level and the caps. A private item is named by its time only on speakers and in a push.
The first item's start settles a plan nobody settled and nobody opened: an opened plan is
settled only by him.

Legs: :func:`make_route_legs` is the ``route_legs`` the 04:00 draft takes, and the watcher computes
the legs of items that lack one (a draft whose legs failed, an item he or she moved) when the
plan's places and times change.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision.day_plan import (
    Progress,
    build_decide_prompt,
    build_follow_up_prompt,
    build_opening_prompt,
    build_progress_prompt,
    parse_decision,
    parse_opening,
    parse_progress,
    unfit_notice,
)
from jarvis.decision.plan_watch import Notice, notice
from jarvis.execution.tools import ToolContext
from jarvis.runtime.day_plan import DayPlanSettings, DayPlanSources, make_ask
from jarvis.runtime.work_state import build_analyst
from jarvis.shared import lang
from jarvis.state import places, plan, plan_budget, things
from jarvis.state.day_line import day_window
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import append_record, message_record, messages_since
from jarvis.state.plan_facts import DayFacts, evidence, facts_at

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
    from contextlib import AbstractContextManager
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.execution.tools import RuntimePathsLike
    from jarvis.runtime.day_plan import Ask, RouteLegs
    from jarvis.shared.clock import Clock

LOGGER = logging.getLogger(__name__)

TICK_S: Final[float] = 60.0
OUTING: Final[frozenset[str]] = frozenset({"outing_soon", "left_late"})
"""Notices that obey only speech mute, a live conversation and a call (like a reminder)."""
DONE: Final[str] = ":done"
"""The suffix of a progress notice's key for its done-check in ``plan.decided``."""
FOLLOW_UP: Final[str] = "unsettled:"
"""The key prefix of her follow-ups on an opened day that is not settled."""
FOLLOW_UP_AFTER_MIN: Final[tuple[int, ...]] = (10, 15, 20, 30, 30, 30)
"""Minutes her last words wait unanswered before each follow-up; their number caps them."""
OPEN_ATTEMPTS: Final[int] = 3
"""How often a day's opening is tried when the model fails, before the day goes without."""
MAX_OUTINGS: Final[int] = 6
"""The outings, soonest first, whose own weather the opening is given."""
HOUR_MS: Final[int] = 3_600_000
_CALL_TIMEOUT_S: Final[float] = 120.0
OPEN_PRESET: Final[str] = "sonnet"
"""The conversation preset for her opening: no other has been wired for it (ADR 0288)."""
_HELD: Final[frozenset[str]] = frozenset({"no-pop", "dnd"})
_PUSH_TITLE: Final[str] = "Jarvis"
_PUSH_THREAD: Final[str] = "talk"
"""A push with this thread opens the conversation on the phone."""

type PlanLeg = Callable[..., dict[str, Any]]
type OpenLeg = Callable[[str], AbstractContextManager[PlanLeg | None]]
"""``open_leg(action_id)`` gives a ``plan_leg`` bound to its own event-log connection, or None."""
type LegFactory = Callable[[ToolContext], PlanLeg | None]


def _record_time(ts: str, zone: tzinfo) -> datetime:
    """A record's ``ts`` as an aware time (a naive one is local)."""
    moment = datetime.fromisoformat(ts)
    return moment if moment.tzinfo else moment.replace(tzinfo=zone)


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


def _home_names(kept: Iterable[places.Place]) -> frozenset[str]:
    """The names home goes by: ``home`` and each kept place of the home role."""
    return frozenset({"home", *(_said(p.name) for p in kept if p.role == "home")})


def _leg_end(answer: Mapping[str, Any]) -> tuple[float, float] | None:
    """Where a ``plan_leg`` answer says the route ends, if it does."""
    at: Mapping[str, Any] = answer.get("end_at") or {}
    try:
        return float(at["lat"]), float(at["lng"])
    except (KeyError, TypeError, ValueError):
        return None


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
    """What the daemon wires in for telling him; until then nothing is said or pushed."""

    quiet: Callable[[], str] = lambda: "off"
    may_speak: Callable[[], bool] = lambda: False
    """Speech not muted, no live conversation and not on a call."""
    private_output: Callable[[], bool] = lambda: False
    at_mac: Callable[[], bool] = lambda: False
    say: Callable[[str], None] | None = None
    push: Callable[..., object] | None = None
    """``Push.send``: blocking, so it is only called from the watcher's thread."""


@dataclass
class _Run:
    """One tick's reads: the day, the connection, and the plan as it stands."""

    conn: sqlite3.Connection
    day: date
    now: datetime
    held: plan.Plan | None
    facts: DayFacts
    handled: list[str] = field(default_factory=list)
    last_check_ms: int | None = None
    """When the day's latest progress check looked back to (its evidence ended), if any."""


class PlanWatch:
    """The watcher; :meth:`tick` is one minute's work, :meth:`run` the loop."""

    def __init__(  # noqa: PLR0913 - the settings, the stores, two models, legs, hooks, the clock.
        self,
        *,
        settings: DayPlanSettings,
        sources: DayPlanSources,
        ask_decide: Ask | None,
        ask_open: Ask | None,
        open_leg: OpenLeg | None = None,
        hooks: Hooks | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind the models (None: that call is skipped and said so), the legs and the hooks."""
        self._settings = settings
        self._sources = sources
        self._ask_decide = ask_decide
        self._ask_open = ask_open
        self._open_leg = open_leg
        self._hooks = hooks or Hooks()
        self._zone = sources.zone
        self._clock = clock or (lambda: datetime.now(sources.zone))
        self._open_failures: dict[date, int] = {}
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
        """Do one minute's work at ``now``; the keys of the notices it handled."""
        now = (now or self._clock()).astimezone(self._zone)
        day = now.date()
        with closing(open_runtime_event_log(self._sources.event_log_path)) as conn:
            held = plan.for_day(conn, day)
            if held is None and plan.failed(conn, day) is None:
                return []
            facts = facts_at(
                conn, day, int(now.timestamp() * 1000), self._zone,
                places.read_kept(self._sources.memory_db),
            )
            run = _Run(conn, day, now, held, facts)
            seen = plan.notices(conn, day)
            run.last_check_ms = max((
                int(n.facts["evidence"]["until_ms"]) for n in seen.values()
                if n.kind == "progress" and "evidence" in n.facts
            ), default=None)
            found = notice(
                held or plan.Plan(day, ()), facts, now, seen.keys(),
                day_ends=self._settings.day_ends,
            )
            if held is None:
                found = [n for n in found if n.kind == "woke"]  # nothing else to compare with
            for one in found:
                self._take(run, one)
            for step in (self._open_day, self._follow_up, self._route, self._settle):
                try:  # one step failing must not hold back the others
                    step(run)
                except Exception:
                    LOGGER.exception("plan watch: %s failed", step.__name__)
            return run.handled

    def _take(self, run: _Run, one: Notice) -> None:
        """Write the notice, then act on it. An action that fails is logged, never retried."""
        try:
            if one.kind == "outing_soon":
                one = self._refresh(run, one)
            elif one.kind == "progress" and run.facts.wake is not None:
                span = (run.last_check_ms or run.facts.wake.at_ms, int(run.now.timestamp() * 1000))
                one = replace(one, facts={**one.facts, "evidence": evidence(
                    run.conn, *span, self._zone)})
            plan.noticed(run.conn, run.day, one.key, one.kind, one.item_id, one.facts)
            run.handled.append(one.key)
            if one.kind != "woke":  # the opening is its own step, whenever it is due
                self._judge(run, one)
        except Exception:
            LOGGER.exception("plan watch: notice %s failed", one.key)

    # --- the opening ------------------------------------------------------------------

    def _open_day(self, run: _Run) -> None:
        """Open the day once he is up, if nobody has: retried a few times when the model fails."""
        wake = run.facts.wake
        if wake is None or run.now.time() >= self._settings.day_ends:
            return
        record_id = f"plan-open-{run.day}"
        if (run.held is not None and run.held.opened) or message_record(
            self._sources.memory_db, record_id,
        ):
            return
        if self._ask_open is None or self._open_failures.get(run.day, 0) >= OPEN_ATTEMPTS:
            return
        try:
            held = run.held or plan.Plan(run.day, ())
            system, user = build_opening_prompt(
                held, unconfirmed=self._unconfirmed(run), weather=self._weather(run.day)[0],
                now=run.now, zone=self._zone, draft_failed=run.held is None,
                outings=self._outings(run),
            )
            message = parse_opening(self._ask_open(system, user))
        except Exception as exc:  # noqa: BLE001 - a model that fails is tried again next minute.
            self._open_failures[run.day] = self._open_failures.get(run.day, 0) + 1
            LOGGER.warning("plan watch: opening failed: %s: %s", type(exc).__name__, exc)
            return
        append_record(self._sources.memory_db, record_id=record_id, source="jarvis", text=message)
        if run.held is not None:
            plan.opened(run.conn, run.day, record_id, wake.kind, wake.at_ms)
            run.held = plan.for_day(run.conn, run.day)
        run.handled.append("open")
        self._deliver(message, push=self._settings.morning_push)

    def _follow_up(self, run: _Run) -> None:
        """She opened the day and it is not settled: her last words unanswered, she picks it up."""
        held = run.held
        if held is None or not held.opened or held.status == "settled" or self._ask_open is None:
            return
        if run.now.time() >= self._settings.day_ends:
            return
        asked = sum(1 for key in plan.notices(run.conn, run.day) if key.startswith(FOLLOW_UP))
        if asked >= len(FOLLOW_UP_AFTER_MIN):
            return
        talk = messages_since(self._sources.memory_db, held.opened_record_id or "")
        if not talk or talk[-1][2] != "jarvis":
            return  # his words are the last: her answer is the conversation's, not the watcher's
        waited = run.now - _record_time(talk[-1][1], self._zone)
        if waited < timedelta(minutes=FOLLOW_UP_AFTER_MIN[asked]):
            return
        key = f"{FOLLOW_UP}{asked + 1}"
        plan.noticed(run.conn, run.day, key, "unsettled", None, {
            "minutes": int(waited.total_seconds() // 60), "asked": asked,
        })
        run.handled.append(key)
        conversation = [
            ("him" if source == "allen" else "you", _record_time(ts, self._zone), text)
            for _, ts, source, text in talk
        ]
        system, user = build_follow_up_prompt(
            held, conversation, run.facts, run.now, asked=asked, zone=self._zone,
        )
        try:
            message = parse_opening(self._ask_open(system, user))
        except Exception as exc:  # noqa: BLE001 - a failed follow-up waits for the next one.
            LOGGER.warning("plan watch: follow-up failed: %s: %s", type(exc).__name__, exc)
            plan.decided(run.conn, run.day, key, "silent", "", f"failed: {type(exc).__name__}",
                         OPEN_PRESET)
            return
        plan.decided(run.conn, run.day, key, "tell", message, "unanswered", OPEN_PRESET)
        append_record(
            self._sources.memory_db, record_id=f"plan-{run.day}-{key}", source="jarvis",
            text=message,
        )
        self._deliver(message)

    def _deliver(self, text: str, *, push: bool = True) -> None:
        """Her own words (the opening, a follow-up): said where he is at the Mac, else pushed.

        ``no-pop`` and ``dnd`` keep them in the conversation only.
        """
        level = self._hooks.quiet()
        if level in _HELD:
            return
        if level == "off" and self._hooks.at_mac() and self._hooks.say and self._hooks.may_speak():
            self._call("say", self._hooks.say, text)
        if push:
            self._push(text, sound=level == "off", urgent=False)

    def _unconfirmed(self, run: _Run) -> list[dict[str, Any]]:
        """Open things he has not confirmed that fall in the horizon, and the ones with no time."""
        first = day_window(run.day, self._zone)[0]
        last = day_window(run.day + timedelta(days=self._settings.horizon_days), self._zone)[0]
        found: list[dict[str, Any]] = [
            {"date": datetime.fromtimestamp(o.start_ms / 1000, self._zone).date().isoformat(),
             "title": o.thing.title, "place": o.thing.place, "private": o.thing.private}
            for o in things.occurrences(run.conn, first, last, zone=self._zone)
            if not o.thing.confirmed
        ]
        for one in things.open_things(run.conn):
            if one.confirmed or one.start_at or one.on_day:
                continue
            due = things.due_ms(one, self._zone) if one.due else None
            if due is not None and due >= last:
                continue
            found.append({
                "date": None if due is None
                else datetime.fromtimestamp(due / 1000, self._zone).date().isoformat(),
                "title": one.title, "place": one.place, "private": one.private,
            })
        return found

    # --- a notice ---------------------------------------------------------------------

    def _weather(self, day: date) -> tuple[dict[str, Any] | None, list[str]]:
        """``(forecast of day, notes)``; a missing or failing source is None and a note."""
        read = self._sources.weather
        if read is not None:
            try:
                found = read(day)
            except Exception as exc:  # noqa: BLE001 - the forecast is a courtesy.
                LOGGER.info("plan watch: weather unreadable: %s: %s", type(exc).__name__, exc)
            else:
                if found is not None:
                    return found, []
        return None, ["weather unreadable"]

    def _outing(
        self, held: plan.Plan, item: plan.PlanItem, kept: Sequence[places.Place],
        leg_end: tuple[float, float] | None = None,
    ) -> dict[str, Any] | None:
        """The weather of ``item``'s own hours, leave time to end, at its place (ADR 0355).

        The place's position is the kept place's, else where its leg ends; with neither the hours
        are home's and the answer says so. None when there is no reader or it fails.
        """
        read = self._sources.outing_weather
        span = plan_budget.span_of(held, item, self._zone)
        if read is None or span is None or not item.place:
            return None
        leave = plan_budget.at(held, (item.route or {}).get("leave_at"), self._zone)
        point = places.position_of(kept, item.place) or leg_end
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

    def _outings(self, run: _Run) -> list[dict[str, Any]]:
        """Each timed outing away from home, soonest first, with the weather of its own hours."""
        held = run.held
        if held is None or self._sources.outing_weather is None:
            return []
        kept = places.read_kept(self._sources.memory_db)
        home = _home_names(kept)
        timed = sorted(
            ((span[0], item) for item in held.items
             if item.place and not item.done and _said(item.place) not in home
             and (span := plan_budget.span_of(held, item, self._zone))),
            key=lambda row: row[0],
        )
        found = ((item, self._outing(held, item, kept)) for _, item in timed[:MAX_OUTINGS])
        return [
            {"item_id": item.item_id, "place": item.place, "weather": weather}
            for item, weather in found if weather is not None
        ]

    def _refresh(self, run: _Run, one: Notice) -> Notice:
        """``outing_soon`` with the leg and the outing's own weather read again; None on failure."""
        leg_now, leg_end, notes = None, None, []
        item = next((i for i in (run.held.items if run.held else ()) if i.item_id == one.item_id),
                    None)
        if item is not None and self._open_leg is not None and item.start:
            origin = str(one.facts.get("from") or "home")
            try:
                with self._open_leg(f"plan-{run.day}-{one.key}") as plan_leg:
                    if plan_leg is not None:
                        answer = plan_leg(
                            origin, item.place, arrive_by=f"{run.day.isoformat()} {item.start}",
                        )
                        leg_now, leg_end = _route(origin, answer), _leg_end(answer)
            except Exception as exc:  # noqa: BLE001 - she is asked with the old leg alone.
                LOGGER.info("plan watch: leg unreadable: %s: %s", type(exc).__name__, exc)
        if leg_now is None:
            notes.append("leg unreadable")
        weather = None
        if item is not None and run.held is not None:
            kept = places.read_kept(self._sources.memory_db)
            weather = self._outing(run.held, item, kept, leg_end)
        if weather is None:
            notes.append("weather unreadable")
        facts = {**one.facts, "weather": weather, "leg_now": leg_now}
        if notes:
            facts["notes"] = notes
        return replace(one, facts=facts)

    def _judge(self, run: _Run, one: Notice) -> None:
        """Ask the model what to do about ``one`` and do it."""
        if one.kind == "progress":
            progressed = self._check_progress(run, one)
            if progressed is None:
                return
            one = progressed
        held = run.held
        if held is None:
            return
        prior = [d for d in plan.decisions(run.conn, run.day) if not d.key.endswith(DONE)]
        capped = one.kind not in OUTING and self._over_cap(prior, run.now)
        preset = self._settings.decide_preset
        try:
            if self._ask_decide is None:
                msg = f"no model: llm.presets.{preset} is missing or malformed"
                raise RuntimeError(msg)  # noqa: TRY301 - one failure path for every cause
            system, user = build_decide_prompt(
                held, one, run.facts, run.now, prior, zone=self._zone,
            )
            decision = parse_decision(self._ask_decide(system, user), held)
        except Exception as exc:  # noqa: BLE001 - silence is the safe answer to a model failure.
            LOGGER.warning("plan watch: %s not judged: %s: %s", one.key, type(exc).__name__, exc)
            plan.decided(run.conn, run.day, one.key, "silent", "", f"{type(exc).__name__}", preset)
            return
        act, say, why = decision.act, decision.say, decision.why or "; ".join(decision.notes)
        if act == "adjust":
            items, notes = plan.apply_changes(held.items, list(decision.changes))
            if notes:  # the plan moved on since the model read it
                act, say, why = "silent", "", "; ".join(notes)
            else:
                plan.revise(run.conn, run.day, items, "her", why or say)
                run.held = plan.for_day(run.conn, run.day)
        plan.decided(run.conn, run.day, one.key, act, say, why, preset)
        if act != "silent":
            self._tell(run, one, say, capped=capped)

    def _check_progress(self, run: _Run, one: Notice) -> Notice | None:
        """The done-check of a ``progress`` notice; the notice to decide on, None if the day fits.

        The model marks what the evidence shows done (and the minutes a task under way still
        needs); the budget is then worked out again. Tasks that fit: silence, no second call.
        """
        held = run.held
        if held is None:
            return None
        preset = self._settings.decide_preset
        key = f"{one.key}{DONE}"
        marked = Progress()
        try:
            if self._ask_decide is None:
                msg = f"no model: llm.presets.{preset} is missing or malformed"
                raise RuntimeError(msg)  # noqa: TRY301 - one failure path for every cause
            system, user = build_progress_prompt(held, one, run.now, zone=self._zone)
            marked = parse_progress(self._ask_decide(system, user), held)
        except Exception as exc:  # noqa: BLE001 - no done-check leaves the plan as it was.
            LOGGER.warning("plan watch: %s not checked: %s: %s", one.key, type(exc).__name__, exc)
            plan.decided(run.conn, run.day, key, "silent", "", f"{type(exc).__name__}", preset)
        else:
            items, notes = plan.apply_changes(held.items, marked.changes())
            why = "; ".join(filter(None, [marked.why, *marked.notes, *notes]))
            changed = items != list(held.items)
            if changed:
                plan.revise(run.conn, run.day, items, "her", marked.why or "progress")
                run.held = plan.for_day(run.conn, run.day)
            plan.decided(run.conn, run.day, key, "adjust" if changed else "silent", "", why, preset)
            marked = marked if changed else Progress()
        if run.held is None:
            return None
        fresh = unfit_notice(run.held, one, marked, run.now, self._settings.day_ends)
        if fresh is None:
            plan.decided(run.conn, run.day, one.key, "silent", "", "fits", preset)
        return fresh

    def _over_cap(self, prior: Sequence[plan.Decided], now: datetime) -> bool:
        """Whether the told notices already fill the day's or the hour's allowance."""
        told = [d for d in prior if d.act != "silent" and d.key.partition(":")[0] not in OUTING]
        cut = int(now.timestamp() * 1000) - HOUR_MS
        return (
            len(told) >= self._settings.told_per_day
            or sum(1 for d in told if d.at_ms >= cut) >= self._settings.told_per_hour
        )

    # --- telling him ------------------------------------------------------------------

    def _tell(self, run: _Run, one: Notice, say: str, *, capped: bool) -> None:
        """Write her words into the conversation, then speak and push them as allowed."""
        append_record(
            self._sources.memory_db, record_id=f"plan-{run.day}-{one.key}", source="jarvis",
            text=say,
        )
        outing = one.kind in OUTING
        level = self._hooks.quiet()
        if capped or (not outing and level in _HELD):
            return
        spoken = pushed = say
        if one.facts.get("private") is True:
            pushed = self._private_line(run, one, say)
            spoken = say if self._hooks.private_output() else pushed
        # out loud only where he is: away from the Mac the phone gets it, not an empty room
        at_mac = self._hooks.at_mac()
        if (outing or level == "off") and at_mac and self._hooks.say and self._hooks.may_speak():
            self._call("say", self._hooks.say, spoken)
        if outing or level in ("off", "quiet"):
            self._push(pushed, sound=outing or level == "off", urgent=outing)

    def _private_line(self, run: _Run, one: Notice, say: str) -> str:
        """The item's time alone: a private item's words stay off speakers and lock screens."""
        item = next((i for i in (run.held.items if run.held else ()) if i.item_id == one.item_id),
                    None)
        at = (item and (item.start or item.window_from)) or one.facts.get("start")
        said_in = lang.text_language(say)
        if at:
            return lang.t("plan.private", lang=said_in, time=at)
        return lang.t("plan.private_untimed", lang=said_in)

    def _push(self, text: str, *, sound: bool, urgent: bool) -> None:
        """Push to the phone when he is away from the Mac; a tap opens the conversation."""
        push = self._hooks.push
        if push is None or self._hooks.at_mac():
            return
        self._call(
            "push", lambda body: push(
                _PUSH_TITLE, body, thread=_PUSH_THREAD, time_sensitive=urgent, sound=sound,
            ), text,
        )

    @staticmethod
    def _call(what: str, deliver: Callable[[str], object], text: str) -> None:
        try:
            deliver(text)
        except Exception:
            LOGGER.exception("plan watch: the %s failed", what)

    # --- legs and settling ------------------------------------------------------------

    def _route(self, run: _Run) -> None:
        """Compute the legs the plan lacks, once per set of places and times (ADR 0291)."""
        held = run.held
        if held is None or self._open_leg is None:
            return
        shape = frozenset((i.item_id, i.start, i.place) for i in held.items)
        if self._routed.get(run.day) == shape:
            return
        self._routed[run.day] = shape
        if not any(i.place and i.start and i.route is None for i in held.items):
            return
        home = _home_names(places.read_kept(self._sources.memory_db))
        with self._open_leg(f"plan-{run.day}-routes") as plan_leg:
            if plan_leg is None:
                return
            items = route_day(plan_leg, held.items, run.day, home)
        if items != list(held.items):
            plan.revise(run.conn, run.day, items, "program", "legs")
            run.held = plan.for_day(run.conn, run.day)

    def _settle(self, run: _Run) -> None:
        """The day getting under way settles a plan nobody settled and nobody opened.

        Under way: the first timed item's start, or the first progress check, which is how a
        day of tasks alone gets under way. An opened plan is hers to settle with him.
        """
        held = run.held
        if held is None or held.status == "settled" or held.opened:
            return
        starts = [i.start or i.window_from for i in held.items]
        first = min((s for s in starts if s), default=None)
        checked = any(key.startswith("progress:") for key in plan.notices(run.conn, run.day))
        if checked or (first is not None and f"{run.now:%H:%M}" >= first):
            plan.settle(run.conn, run.day, "first_item")
            run.handled.append("settle")


def build_watch(  # noqa: PLR0913 - the settings, the config, the stores, the legs and the hooks.
    settings: DayPlanSettings, *, config: Mapping[str, Any], pricing_path: Path,
    sources: DayPlanSources, open_leg: OpenLeg | None, hooks: Hooks,
) -> PlanWatch:
    """The watcher over ``sources``: the decider is ``settings.decide_preset`` of ``config``."""
    def ask(preset: str) -> Ask | None:
        return make_ask(
            build_analyst(
                config, preset, pricing_path=pricing_path, account_cost=True, kind="day_plan",
                timeout_s=_CALL_TIMEOUT_S,
            ),
            sources.event_log_path,
        )

    return PlanWatch(
        settings=settings, sources=sources, ask_decide=ask(settings.decide_preset),
        ask_open=ask(OPEN_PRESET), open_leg=open_leg, hooks=hooks,
    )


__all__ = [
    "Hooks", "PlanWatch", "build_watch", "leg_session", "make_route_legs", "route_day",
]
