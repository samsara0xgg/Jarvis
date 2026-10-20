"""ADR 0302: the short signals of each segment, read for the doing lane's writer.

A stretch away from his Mac has few facts: the stay, maybe a talk. The phone and the watch still
sense him, and the Mac still says whether he was at it. :func:`gather` turns what is already
stored into one line of observed signals for each segment, in this order, a part left out when
the source has nothing for the segment:

- Mac attention: the stretches TimeSink marked the Mac idle, locked or asleep, as
  ``Mac idle 12:41-13:13``. A stretch not closed by the mark that ends it is closed where
  TimeSink itself stopped or restarted, else it runs to the segment's end; where two kinds overlap
  the one that implies the others (asleep, then locked, then idle) names the time.
- messages he sent: the messages his phone or tablet logged him sending in each app, from the
  day line's ``messages`` bursts that overlap the segment (ADR 0305), as ``sent 5 messages
  (WeChat)``. A burst that runs over two segments counts in both. What he received is left out:
  many received rows are an app's notifications, not someone writing to him.
- playing: what his devices played (``media``, ADR 0305), the minutes each app covers inside
  the segment and the title heard most, as ``playing Podcasts 20m: The Daily``.
- phone motion: the minutes of the segment each activity covers, as ``phone: still 32m, walking
  6m``. Only a motion segment of medium or high confidence counts, as it does for a move
  (:data:`~jarvis.state.phone_day.MOVE_CONFIDENCES`).
- steps: the hourly totals that overlap the segment, each prorated by its overlap, as ``~18
  steps``. It is approximate: a total says how many steps an hour held, not when.
- heart rate: the lowest and highest heart rate sampled inside the segment, with the context
  the watch gave most of them, as ``heart 55-68 sedentary``.

:func:`foreground` reads, for each quarter hour of the window, what was in front of him at his
Mac: the minutes each app, site or document held and the seconds he typed in it, from TimeSink's
spans as the terminal shipped them. It is where his attention was at the Mac, which a block's
list of app names does not say.

Nothing is stored and nothing is guessed. A source that cannot be read leaves its part out,
logged. A phone picked up is not a signal: the phone reports no row for it (its battery and
location rows also come when the app wakes in the background).

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
from collections import Counter
from contextlib import closing
from datetime import datetime
from itertools import pairwise
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import imports, timesink
from jarvis.state.day_line import DaySources, shipped_conn
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.phone_day import (
    HEALTH_EVENT,
    LOOKBACK_MS,
    MEASURE_EVENT,
    MOTION_EVENT,
    MOVE_CONFIDENCES,
    health_totals,
    payloads,
    settle,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.shared import Event
    from jarvis.state.doing import Segment

LOGGER = logging.getLogger(__name__)

SIGNALS_MAX_CHARS: Final = 160
_TITLE_MAX_CHARS: Final = 30
"""The most of a played title a segment's signals hold; a longer one is cut with an ellipsis."""
"""The most characters of a segment's signals; whole parts are left off the end until they fit."""
MAX_STRETCHES: Final = 3
"""The most Mac stretches of a segment, the longest, shown in time order."""
MAX_ACTIVITIES: Final = 2
"""The most phone activities of a segment, the ones covering most of it."""

FOREGROUND_SLICE_MS: Final = 15 * 60_000
"""How long each part of the window is whose foreground is told."""
MAX_FOREGROUND_ITEMS: Final = 5
"""The most apps, sites or documents a quarter hour names, those in front longest; the rest is
summed as ``other``."""
_FOREGROUND_TITLE_MAX_CHARS: Final = 30

_MINUTE_MS: Final = 60_000
_HALF_MINUTE_MS: Final = 30_000
_TYPED_MIN_SECONDS: Final = 5
_MAC_LOOKBACK_MS: Final = 24 * 60 * _MINUTE_MS
"""How far before the first segment the Mac's state marks are read, to find a stretch that began
earlier and is still going."""
_AWAY: Final = {"idle": "idle", "lock": "locked", "sleep": "asleep"}
_RESUMES: Final = {"idle": "active", "lock": "unlock", "sleep": "wake"}
_AWAY_ORDER: Final = ("sleep", "lock", "idle")
"""Which kind names a time two kinds cover: a sleeping Mac is also locked, and so on."""
_TRACKING: Final = frozenset({"start", "stop", "tracking_pause", "tracking_resume"})
"""TimeSink's own marks: what it said about the Mac before them may no longer hold."""
_ACTIVITIES: Final = {
    "stationary": "still", "walking": "walking", "running": "running", "cycling": "cycling",
    "automotive": "automotive",
}
_NO_CONTEXT: Final = "not_set"

type _Stretch = tuple[int, int | None, str]
"""A Mac stretch: when it began, when it ended (None: not yet) and its kind, one of ``_AWAY``."""


def _clock(ms: int, zone: tzinfo) -> str:
    return f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def _overlap(start: int, end: int, other_start: int, other_end: int | None) -> int:
    """The milliseconds ``[start, end)`` shares with ``[other_start, other_end)`` (None: no end)."""
    return max(0, min(end, end if other_end is None else other_end) - max(start, other_start))


# --- the Mac ------------------------------------------------------------------------------


def _stretches(marks: Sequence[tuple[int, str]]) -> list[_Stretch]:
    """The stretches the Mac was away, from its state marks oldest first."""
    began: dict[str, int] = {}
    found: list[_Stretch] = []
    for at, kind in marks:
        if kind in _TRACKING:
            found += [(since, at, away) for away, since in began.items()]
            began.clear()
        elif kind in _AWAY:
            began.setdefault(kind, at)
        else:
            for away, resumes in _RESUMES.items():
                if kind == resumes and away in began:
                    found.append((began.pop(away), at, away))
    return [*found, *((since, None, away) for away, since in began.items())]


def _marks(shipped: sqlite3.Connection, start_ms: int, end_ms: int) -> list[tuple[int, str]]:
    """TimeSink's state marks in ``[start_ms, end_ms)`` as the shipped store holds them.

    By time, and for the same moment by TimeSink's own row number: it wrote them in that order.
    """
    marks: list[tuple[int, int, str]] = []
    for row in imports.newest(
        shipped, "timesink", start_ms=start_ms, end_ms=end_ms, kinds=("stateEvent",),
        by_prefix=True,
    ):
        at, kind = row.fields.get("at"), row.fields.get("kind")
        if at and kind:
            moment = datetime.fromisoformat(timesink.moment(str(at)))
            number = int(row.fields.get("id") or 0)
            marks.append((round(moment.timestamp() * 1000), number, str(kind)))
    return [(at, kind) for at, _number, kind in sorted(marks)]


def _mac(stretches: Sequence[_Stretch], segment: Segment, zone: tzinfo) -> str:
    """``Mac idle 12:41-12:50, locked 12:50-13:13``: the stretches inside the segment.

    Time two kinds cover goes to the one in :data:`_AWAY_ORDER` first; a piece under a minute is
    dropped; at most :data:`MAX_STRETCHES` of the longest are shown.
    """
    start, end = segment.start_ms, segment.end_ms
    inside = [
        (max(since, start), min(end if until is None else until, end), away)
        for since, until, away in stretches
        if _overlap(start, end, since, until) > 0
    ]
    pieces: list[tuple[int, int, str]] = []
    for lo, hi in pairwise(sorted({t for first, last, _ in inside for t in (first, last)})):
        named = {away for first, last, away in inside if first <= lo and hi <= last}
        label = next((away for away in _AWAY_ORDER if away in named), None)
        if label is None or hi - lo < _MINUTE_MS:
            continue
        if pieces and pieces[-1][1] == lo and pieces[-1][2] == label:
            pieces[-1] = (pieces[-1][0], hi, label)
        else:
            pieces.append((lo, hi, label))
    longest = sorted(pieces, key=lambda piece: piece[0] - piece[1])[:MAX_STRETCHES]
    shown = [
        f"{_AWAY[away]} {_clock(lo, zone)}-{_clock(hi, zone)}" for lo, hi, away in sorted(longest)
    ]
    return f"Mac {', '.join(shown)}" if shown else ""


# --- the phone and the watch --------------------------------------------------------------


def _activities(events: Sequence[Event]) -> list[tuple[int, int | None, str]]:
    """The phone's motion segments of medium or high confidence as (start, end, activity name).

    A segment ends where it says, else where the next one (of any confidence) starts, as a move
    does; the last may have no end yet.
    """
    facts = settle(list(events), "started_at_ms", "ended_at_ms")
    found: list[tuple[int, int | None, str]] = []
    for index, fact in enumerate(facts):
        end = fact.end
        if end is None and index + 1 < len(facts):
            end = facts[index + 1].start
        name = _ACTIVITIES.get(str(fact.payload["activity"]))
        if name is not None and fact.payload["confidence"] in MOVE_CONFIDENCES:
            found.append((fact.start, end, name))
    return found


def _motion(activities: Sequence[tuple[int, int | None, str]], segment: Segment) -> str:
    """``phone: still 32m, walking 6m``: the minutes each activity covers inside the segment."""
    covered: Counter[str] = Counter()
    for start, end, name in activities:
        covered[name] += _overlap(segment.start_ms, segment.end_ms, start, end)
    shown = [
        f"{name} {minutes}m"
        for name, ms in covered.most_common(MAX_ACTIVITIES)
        if (minutes := round(ms / _MINUTE_MS)) >= 1
    ]
    return f"phone: {', '.join(shown)}" if shown else ""


def _steps(totals: Sequence[tuple[int, int, float]], segment: Segment) -> str:
    """``~18 steps``: each hourly total's share of the segment, by the time they overlap."""
    steps = round(sum(
        value * _overlap(segment.start_ms, segment.end_ms, start, end) / (end - start)
        for start, end, value in totals
        if end > start
    ))
    return f"~{steps} steps" if steps >= 1 else ""


def _heart(samples: Sequence[Mapping[str, Any]], segment: Segment) -> str:
    """``heart 55-68 sedentary``: the range of the samples that began inside the segment."""
    inside = [s for s in samples if segment.start_ms <= int(s["start_ms"]) < segment.end_ms]
    if not inside:
        return ""
    values = [round(float(s["value"])) for s in inside]
    low, high = min(values), max(values)
    contexts = Counter(
        str(context) for s in inside
        if (context := (s.get("meta") or {}).get("motion_context")) not in (None, _NO_CONTEXT)
    )
    context = f" {contexts.most_common(1)[0][0]}" if contexts else ""
    return f"heart {low if low == high else f'{low}-{high}'}{context}"


def _sent(items: Sequence[Mapping[str, Any]], segment: Segment) -> str:
    """``sent 5 messages (WeChat, Messages)``: what he sent in the bursts touching the segment."""
    per_app: Counter[str] = Counter()
    for item in items:
        if item["kind"] == "messages" and item.get("sent") and _touches(item, segment):
            per_app[str(item.get("app") or "")] += int(item["sent"])
    total = sum(per_app.values())
    if not total:
        return ""
    apps = ", ".join(app for app, _count in per_app.most_common() if app)
    return f"sent {total} message{'s' if total > 1 else ''}" + (f" ({apps})" if apps else "")


def _playing(items: Sequence[Mapping[str, Any]], segment: Segment) -> str:
    """``playing Podcasts 20m: The Daily``: the app played most inside the segment."""
    per_app: Counter[str] = Counter()
    titles: dict[str, str] = {}
    for item in items:
        if item["kind"] != "media":
            continue
        app = str(item.get("app") or "")
        per_app[app] += _overlap(segment.start_ms, segment.end_ms, item["start_ms"], item["end_ms"])
        heard = item.get("titles") or []
        if heard and app not in titles:
            title = str(heard[0]["title"]).strip()
            cut = len(title) > _TITLE_MAX_CHARS
            titles[app] = title[: _TITLE_MAX_CHARS - 1].rstrip() + "…" if cut else title
    if not per_app:
        return ""
    app, ms = per_app.most_common(1)[0]
    if (minutes := round(ms / _MINUTE_MS)) < 1:
        return ""
    title = f": {titles[app]}" if app in titles else ""
    return f"playing {app} {minutes}m{title}"


def _touches(item: Mapping[str, Any], segment: Segment) -> bool:
    start, end = int(item["start_ms"]), int(item["end_ms"] or item["start_ms"])
    return start < segment.end_ms and end >= segment.start_ms


# --- the Mac's foreground ------------------------------------------------------------------


def _cut(text: object) -> str:
    line = " ".join(str(text or "").split())
    cut = len(line) > _FOREGROUND_TITLE_MAX_CHARS
    return line[: _FOREGROUND_TITLE_MAX_CHARS - 1].rstrip() + "…" if cut else line


def _held(fields: Mapping[str, Any]) -> tuple[tuple[str, str], str]:
    """What a span had in front, as (the key it is summed by, how it is named).

    A site is summed by its domain, named with the first title seen; anything else by its
    document, else its window title.
    """
    app = str(fields.get("appName") or "unknown app")
    domain = str(fields.get("domain") or "")
    if domain:
        title = _cut(fields.get("title"))
        return (app, domain), f"{app} {domain}" + (f" ({title})" if title else "")
    what = _cut(fields.get("document") or fields.get("title"))
    return (app, what), f"{app} {what}" if what and what != app else app


def _foreground_spans(
    shipped: sqlite3.Connection, start_ms: int, end_ms: int,
) -> list[tuple[int, int, tuple[str, str], str, int]]:
    """The Mac's spans touching ``[start_ms, end_ms)``: start, end, key, name, seconds typed."""
    found: list[tuple[int, int, tuple[str, str], str, int]] = []
    for row in imports.newest(
        shipped, "timesink", start_ms=start_ms - FOREGROUND_SLICE_MS, end_ms=end_ms,
        kinds=("span",), by_prefix=True,
    ):
        fields = row.fields
        if fields.get("deviceID") is not None or not fields.get("start") or not fields.get("end"):
            continue
        lo, hi = (
            round(datetime.fromisoformat(timesink.moment(str(fields[edge]))).timestamp() * 1000)
            for edge in ("start", "end")
        )
        if hi > max(lo, start_ms):
            key, name = _held(fields)
            found.append((lo, hi, key, name, int(fields.get("keySeconds") or 0)))
    return found


def _quarter(held: Mapping[tuple[str, str], list[Any]]) -> str:
    """``Claude 一起看 8m typed 40s, WeChat Weixin 6m typed 110s, other 1m``."""
    ranked = sorted(held.values(), key=lambda one: -one[1])
    shown = [one for one in ranked[:MAX_FOREGROUND_ITEMS] if one[1] >= _HALF_MINUTE_MS]
    rest = sum(one[1] for one in ranked) - sum(one[1] for one in shown)
    parts = [
        f"{name} {max(1, round(ms / _MINUTE_MS))}m"
        + (f" typed {round(typed)}s" if typed >= _TYPED_MIN_SECONDS else "")
        for name, ms, typed in shown
    ]
    if rest >= _HALF_MINUTE_MS:
        parts.append(f"other {max(1, round(rest / _MINUTE_MS))}m")
    return ", ".join(parts)


def foreground(sources: DaySources, window: tuple[int, int]) -> list[tuple[int, int, str]]:
    """What was in front of him at his Mac in each quarter hour of ``window``, oldest first.

    Each is (start, end, text): the quarter hour, clipped to the window, and the minutes each
    app, site or document held with the seconds he typed in it (a span's typing shared by the
    time it spent in each quarter). A quarter with under half a minute in front is left out.
    """
    start, end = window

    def read() -> list[tuple[int, int, tuple[str, str], str, int]]:
        conn = shipped_conn(sources)
        if conn is None:
            return []
        with closing(conn):
            return _foreground_spans(conn, start, end)

    none: list[tuple[int, int, tuple[str, str], str, int]] = []
    quarters: dict[int, dict[tuple[str, str], list[Any]]] = {}
    for lo, hi, key, name, typed in _guarded("Mac foreground", read, none):
        clipped_lo, clipped_hi = max(lo, start), min(hi, end)
        at = clipped_lo - clipped_lo % FOREGROUND_SLICE_MS
        while at < clipped_hi:
            part = min(clipped_hi, at + FOREGROUND_SLICE_MS) - max(clipped_lo, at)
            one = quarters.setdefault(at, {}).setdefault(key, [name, 0, 0.0])
            one[1] += part
            one[2] += typed * part / (hi - lo)
            at += FOREGROUND_SLICE_MS
    found = []
    for at, held in sorted(quarters.items()):
        if text := _quarter(held):
            found.append((max(at, start), min(at + FOREGROUND_SLICE_MS, end), text))
    return found


# --- the segments -------------------------------------------------------------------------


def _fitted(parts: Sequence[str]) -> str:
    """The parts that are there, joined by ``; ``, the last dropped until they fit the cap."""
    kept = [part for part in parts if part]
    while kept and len("; ".join(kept)) > SIGNALS_MAX_CHARS:
        kept.pop()
    return "; ".join(kept)


def _guarded[T](name: str, read: Callable[[], T], empty: T) -> T:
    try:
        return read()
    except Exception:
        LOGGER.exception("doing signals: the %s source could not be read", name)
        return empty


def gather(
    event_log: sqlite3.Connection, sources: DaySources, segments: Sequence[Segment],
    items: Sequence[Mapping[str, Any]] = (),
) -> dict[int, str]:
    """The signals of each segment that has any, by the segment's start.

    ``event_log`` holds the phone's rows; the Mac's marks are read from what the terminal
    shipped (``sources.shipped``); ``items`` are the day line's, which hold what his devices
    sent and played. A segment's text is at most :data:`SIGNALS_MAX_CHARS`.
    """
    if not segments:
        return {}
    first, last = segments[0].start_ms, segments[-1].end_ms
    empty: dict[str, list[Event]] = {MOTION_EVENT: [], HEALTH_EVENT: []}

    def phone() -> dict[str, list[Event]]:
        held: dict[str, list[Event]] = {MOTION_EVENT: [], HEALTH_EVENT: []}
        since = first - LOOKBACK_MS
        for event in iter_events_of_types(event_log, tuple(held), since_epoch_ms=since):
            held[event.type].append(event)
        return held

    def mac() -> list[_Stretch]:
        conn = shipped_conn(sources)
        if conn is None:
            return []
        with closing(conn):
            return _stretches(_marks(conn, first - _MAC_LOOKBACK_MS, last))

    rows = _guarded("phone", phone, empty)
    activities = _activities(rows[MOTION_EVENT])
    totals = health_totals(rows[HEALTH_EVENT], "steps")
    heart: list[dict[str, Any]] = _guarded(
        "heart rate",
        lambda: payloads(event_log, MEASURE_EVENT, "heart_rate", first, last + _MINUTE_MS),
        [],
    )
    stretches: list[_Stretch] = _guarded("Mac", mac, [])
    found: dict[int, str] = {}
    for segment in segments:
        text = _fitted([
            _mac(stretches, segment, sources.zone),
            _sent(items, segment),
            _playing(items, segment),
            _motion(activities, segment),
            _steps(totals, segment),
            _heart(heart, segment),
        ])
        if text:
            found[segment.start_ms] = text
    return found


__all__ = [
    "FOREGROUND_SLICE_MS",
    "MAX_ACTIVITIES",
    "MAX_FOREGROUND_ITEMS",
    "MAX_STRETCHES",
    "SIGNALS_MAX_CHARS",
    "foreground",
    "gather",
]
