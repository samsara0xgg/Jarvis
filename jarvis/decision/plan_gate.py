"""Which of her unprompted lines about the day are due, as the program sees it (ADR 0385). Pure.

:func:`due` compares what the log holds (:mod:`jarvis.state.plan_facts`), what she has already
said today and what he switched off, and names each line that is due now: the morning line, a new
hard thing, the evening line. Whether a line is due is a fact about the log; only the words of the
morning and the evening line are a model's (:mod:`jarvis.decision.day_plan`). Nothing here asks
her whether to speak, and nothing chases an answer: a line is due once, and a line nobody answers
is dropped. The thresholds are the code's, not settings.

A line is keyed by its kind and its thing (``morning``, ``new:thing-1``, ``evening``), so each is
said once a day. The leave-by ring is not a line of this gate: it is a ring on the thing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import tzinfo

    from jarvis.state.plan import Plan
    from jarvis.state.plan_facts import DayFacts, Here, NotedThing, Stay

EVENING_FROM: Final[time] = time(17, 0)
"""The morning line is only before this."""
HOME_FROM: Final[time] = time(20, 0)
"""Getting home counts as the end of the day only from this."""
HOME_STAYED: Final[timedelta] = timedelta(minutes=10)
"""A stay at home this long, after one elsewhere, ends the day."""
MAC_FROM: Final[time] = time(21, 0)
"""The Mac locked or asleep ends the day only from this; an earlier one is dinner."""
MAC_STOPPED: Final[timedelta] = timedelta(minutes=20)
"""The Mac locked or asleep this long ends the day."""
WIND_DOWN: Final[timedelta] = timedelta(hours=1)
"""The evening line is due this long before ``day_ends`` whatever else was seen."""
PAIR_CAP: Final[int] = 2
"""New-hard-thing and leave-by lines together, at most, in a day."""
_MIN_NAME: Final[int] = 4
"""A place name shorter than this must match whole, never as part of another."""
_PAIR_KEYS: Final[tuple[str, ...]] = ("new:", "leave:")
_TOLD: Final[tuple[str, ...]] = ("mail", "mirror")
"""The writers of a thing whose news is told: his own words are never told back to him."""


@dataclass(frozen=True)
class Line:
    """One line that is due. ``facts`` is JSON-able: what the line is about, as the log has it."""

    key: str
    kind: str
    facts: dict[str, Any]


def _clock(ms: int, zone: tzinfo | None) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _said(name: str) -> str:
    return " ".join(name.casefold().split())


def _same(place: str, where: Here | Stay) -> bool:
    """True when ``place`` (a kept name or free text) is where ``where`` is."""
    wanted = _said(place)
    if not wanted:
        return False
    for name in (where.kept, where.kept_role, where.place):
        held = _said(name)
        if held and (
            held == wanted
            or (min(len(held), len(wanted)) >= _MIN_NAME and (held in wanted or wanted in held))
        ):
            return True
    return False


def _morning(plan: Plan | None, facts: DayFacts, now: datetime) -> list[Line]:
    if plan is None or facts.wake is None or now.time() >= EVENING_FROM:
        return []
    return [Line("morning", "morning", {
        "signal": facts.wake.kind, "at": _clock(facts.wake.at_ms, now.tzinfo),
    })]


def _new(
    facts: DayFacts, now: datetime, said: Collection[str], room: int, after_ms: int | None,
) -> list[Line]:
    """A thing with an hour that mail or the calendar mirror added or moved after ``after_ms``."""
    if after_ms is None:
        return []
    found = [
        Line(f"new:{noted.thing_id}", "new", {
            "title": noted.title, "start": noted.start,
            "day": "today" if noted.day == now.date() else "tomorrow",
            "place": noted.place, "private": noted.private,
        })
        for noted in sorted(facts.things, key=lambda n: n.noted_at_ms)
        if (
            noted.start and noted.by in _TOLD and noted.noted_at_ms > after_ms
            and f"new:{noted.thing_id}" not in said and not _behind(noted, now)
        )
    ]
    return found[:room]


def _behind(noted: NotedThing, now: datetime) -> bool:
    """True when the thing's hour today has already gone by: there is nothing to get ready for."""
    return noted.day == now.date() and noted.start < f"{now:%H:%M}"


def _at_home(stay: Stay, home: Collection[str]) -> bool:
    return stay.kept_role == "home" or any(_same(name, stay) for name in home)


def _home_again(facts: DayFacts, now: datetime, home: Collection[str]) -> int | None:
    """When he got home from somewhere else, late enough in the evening and long enough ago."""
    from_ms = _ms(datetime.combine(now.date(), HOME_FROM, tzinfo=now.tzinfo))
    for before, stay in pairwise(facts.stays):
        end = _ms(now) if stay.end_ms is None else stay.end_ms
        if (
            stay.start_ms >= from_ms and _at_home(stay, home) and not _at_home(before, home)
            and end - stay.start_ms >= int(HOME_STAYED.total_seconds() * 1000)
        ):
            return stay.start_ms
    return None


def _mac_stopped(facts: DayFacts, now: datetime) -> int | None:
    """When the Mac was locked or asleep from late in the evening for long enough to end the day."""
    from_ms = _ms(datetime.combine(now.date(), MAC_FROM, tzinfo=now.tzinfo))
    for away in facts.mac_away:
        end = _ms(now) if away.end_ms is None else away.end_ms
        if (
            away.kind in {"lock", "sleep"} and away.start_ms >= from_ms
            and end - away.start_ms >= int(MAC_STOPPED.total_seconds() * 1000)
        ):
            return away.start_ms
    return None


def _evening(
    facts: DayFacts, now: datetime, home: Collection[str], day_ends: time,
) -> list[Line]:
    zone = now.tzinfo
    if (at := _home_again(facts, now, home)) is not None:
        signal = "home"
    elif (at := _mac_stopped(facts, now)) is not None:
        signal = "mac"
    elif now >= (late := datetime.combine(now.date(), day_ends, tzinfo=zone) - WIND_DOWN):
        signal, at = "late", _ms(late)
    else:
        return []
    return [Line("evening", "evening", {"signal": signal, "at": _clock(at, zone)})]


def due(  # noqa: PLR0913 - the plan, the facts, the moment, what was said and muted, three knobs.
    plan: Plan | None, facts: DayFacts, now: datetime, said: Collection[str],
    muted: Collection[str], *, day_ends: time, home: Collection[str], first_said_ms: int | None,
) -> list[Line]:
    """The lines of ``plan``'s day that are due at ``now`` (aware, in his zone).

    ``said`` holds the keys said today, ``muted`` the kinds he switched off, ``home`` the names
    his home goes by (casefolded), ``day_ends`` when the day's work stops, ``first_said_ms`` when
    the morning line went out (None: not yet). ``plan`` is None when there is no plan and no
    failed draft; a failed draft is an empty plan.

    - ``morning``: the first sign he is up, before :data:`EVENING_FROM`, with a plan (or its
      failure) to speak of.
    - ``new:<id>``: a thing with an hour today or tomorrow that mail or the calendar mirror added
      or moved after the morning line, whose hour is not behind; with no morning line yet the
      morning will hold it, and with the morning switched off the first sign of waking stands in.
      At most :data:`PAIR_CAP` of these and leave-by lines in a day.
    - ``evening``: the first of: he got home from somewhere else from :data:`HOME_FROM` and has
      stayed :data:`HOME_STAYED`; the Mac locked or asleep (not idle) :data:`MAC_STOPPED` from
      :data:`MAC_FROM`; :data:`WIND_DOWN` before ``day_ends``.
    """
    lines: list[Line] = []
    if "morning" not in said:
        lines += _morning(plan, facts, now)
    after = first_said_ms
    if after is None and "morning" in muted and facts.wake is not None:
        after = facts.wake.at_ms
    room = max(0, PAIR_CAP - sum(1 for key in said if key.startswith(_PAIR_KEYS)))
    lines += _new(facts, now, said, room, after)
    if "evening" not in said:
        lines += _evening(facts, now, home, day_ends)
    return [line for line in lines if line.kind not in muted]
