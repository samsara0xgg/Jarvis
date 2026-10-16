"""L2 day experience (ADR 0276): one local day of how it went, folded from the event log.

:func:`fold_day` is a pure function over the rows of one local date; :func:`compute_day` reads
them (and the proactive cards' reactions in memory.db) and folds. Nothing here is a judgement
by a model: every number is a count or a delta of timestamps, and every signal that decided a
count is named in the result so a reader can say why a turn was not right the first time.

A *use* is a real turn: spoken (``utterance.received``) or typed (``surface.user_intent``),
never a card tap, an ask-card answer, a plugin resume or a Live delegation, and never a turn
whose answer was cancelled as ``superseded`` (a later turn took its place, ADR 0268, 0271).
Latency, redo signals and labels are per use.

``plan_settled`` and ``calendar_missed`` are ``None``: a planning feature will own them.

Layer rules: stdlib + L2 siblings; no wiring.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import job_ledger

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

VERSION: Final[int] = 1
# A line within this long of the one it corrects marks that one as not right the first time.
CORRECTION_WINDOW_MS: Final[int] = 120_000
# A reminder that rings later than this missed its moment.
LATE_REMINDER_MS: Final[int] = 300_000
# Events of a turn opened late in the day may land this long after midnight.
_TAIL_MS: Final[int] = 3_600_000
_RIDE_LOOKBACK_MS: Final[int] = 24 * 3_600_000
_P90: Final[float] = 0.9

SIGNALS: Final[tuple[str, ...]] = (
    "guard_repeat", "guard_unbacked", "turn_failed", "response_failed", "user_stop", "barge_in",
    "speech_dropped", "clarified", "correction",
)
"""What makes a use not right the first time, in the order the result lists them."""

# surface.user_intent channels that are not a sentence he typed to her.
_NOT_TYPED: Final[frozenset[str]] = frozenset({"card", "clarify", "plugin_resume", "gpt_live"})
_INTERRUPTS: Final[frozenset[str]] = frozenset({"barge_in", "user_stop"})
_OPENING: Final[tuple[str, ...]] = ("utterance.received", "surface.user_intent")
_TURN_TYPES: Final[tuple[str, ...]] = (
    *_OPENING, "response.cancelled", "response.completed", "response.failed", "turn.failed",
    "turn.guard_fired", "surface.speech_dropped", "clarification.requested",
    "surface.clarified", "surface.dismissed", "surface.playback_started",
    "surface.playback_interrupted", "surface.response_chunk", "route.oneshot_read",
    "reminder.fired", "reminder.acknowledged", "ride.candidate_answered",
)
_RIDE_TYPES: Final[tuple[str, ...]] = (
    "ride.started", "ride.alight_set", "ride.warned", "ride.ended",
)
# card_feedback reactions that say what he did with the card; the rest rate it.
_CARD_OUTCOMES: Final[tuple[tuple[str, str], ...]] = (
    ("acted", "followed"), ("dismissed", "dismissed"), ("ignored", "ignored"),
)


class Row(NamedTuple):
    """One event as the fold reads it: ``node`` is the device that wrote it (``ingestion_node``)."""

    type: str
    ts_ms: int
    payload: Mapping[str, Any]
    node: str


def day_bounds_ms(day: date) -> tuple[int, int]:
    """The local midnight that opens ``day`` and the one that closes it, in epoch milliseconds."""
    start = datetime.combine(day, time()).astimezone()
    end = datetime.combine(day + timedelta(days=1), time()).astimezone()
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


_SELECT_ROWS: Final[str] = (
    "SELECT type, ts_epoch_ms, payload_json, ingestion_node FROM events "
    "WHERE type IN (SELECT value FROM json_each(?)) AND ts_epoch_ms >= ? AND ts_epoch_ms < ? "
    "ORDER BY id"
)


def read_rows(conn: sqlite3.Connection, day: date) -> list[Row]:
    """The rows :func:`fold_day` reads for ``day``, from the event log, in append order."""
    start, end = day_bounds_ms(day)
    rows = conn.execute(_SELECT_ROWS, (json.dumps(_TURN_TYPES), start, end + _TAIL_MS)).fetchall()
    rows += conn.execute(
        _SELECT_ROWS, (json.dumps(_RIDE_TYPES), start - _RIDE_LOOKBACK_MS, end),
    ).fetchall()
    return [Row(str(t), int(ts), json.loads(payload), str(node)) for t, ts, payload, node in rows]


def first_day(conn: sqlite3.Connection) -> date | None:
    """The local date of the first turn on the log, or None when there is none."""
    found = conn.execute(
        "SELECT MIN(ts_epoch_ms) FROM events WHERE type IN (SELECT value FROM json_each(?))",
        (json.dumps(_OPENING),),
    ).fetchone()
    if found is None or found[0] is None:
        return None
    return datetime.fromtimestamp(found[0] / 1000).astimezone().date()


@dataclass
class _Turn:
    ts_ms: int
    node: str
    spoken: bool
    spoke_ago_ms: int | None
    signals: set[str] = field(default_factory=set)
    answered: bool = False
    playback_ms: int | None = None
    chunk_ms: int | None = None
    verdict: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class _Ride:
    alight: bool = False
    warned: bool = False
    ended_ms: int | None = None
    reason: str | None = None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _opened(row: Row) -> tuple[str, _Turn] | None:
    """The turn a row opens, or None when it opens no real turn."""
    payload = row.payload
    turn_id, words = _text(payload.get("turn_id")), payload.get("transcript")
    if turn_id is None or not isinstance(words, str) or not words.strip():
        return None
    if row.type == "utterance.received":
        ago = payload.get("spoke_ago_ms")
        known = isinstance(ago, int) and not isinstance(ago, bool) and ago >= 0
        return turn_id, _Turn(row.ts_ms, row.node, spoken=True, spoke_ago_ms=ago if known else None)
    if payload.get("confirmation_decision") or payload.get("channel") in _NOT_TYPED:
        return None
    return turn_id, _Turn(row.ts_ms, row.node, spoken=False, spoke_ago_ms=None)


class _Day:
    """The accumulators of one fold; :meth:`feed` routes a row to the handler of its type."""

    def __init__(self, day: date) -> None:
        self.start, self.end = day_bounds_ms(day)
        self.turns: dict[str, _Turn] = {}
        self.superseded: dict[str, str | None] = {}
        self.asked: dict[str, str] = {}
        self.rides: dict[str, _Ride] = {}
        self.counts: Counter[str] = Counter()
        self.handlers: dict[str, Callable[[Row], None]] = {
            "response.cancelled": self._cancelled,
            "response.completed": self._completed,
            "response.failed": lambda row: self._signal(row, "response_failed"),
            "turn.failed": lambda row: self._signal(row, "turn_failed"),
            "turn.guard_fired": self._guard,
            "surface.speech_dropped": lambda row: self._signal(row, "speech_dropped"),
            "clarification.requested": self._asked,
            "surface.clarified": self._clarified,
            "surface.dismissed": self._dismissed,
            "surface.playback_started": self._started,
            "surface.playback_interrupted": self._interrupted,
            "surface.response_chunk": self._chunk,
            "route.oneshot_read": self._read,
            "reminder.fired": self._fired,
            "reminder.acknowledged": lambda row: self._count(row, "followed"),
            "ride.candidate_answered": self._candidate,
            "ride.started": self._ride_started,
            "ride.alight_set": self._ride_alight,
            "ride.warned": self._ride_warned,
            "ride.ended": self._ride_ended,
        }

    def in_day(self, row: Row) -> bool:
        return self.start <= row.ts_ms < self.end

    def feed(self, row: Row) -> None:
        handler = self.handlers.get(row.type)
        if handler is not None:
            handler(row)

    def _turn(self, row: Row) -> _Turn | None:
        return self.turns.get(_text(row.payload.get("turn_id")) or "")

    def _count(self, row: Row, name: str) -> None:
        if self.in_day(row):
            self.counts[name] += 1

    def _signal(self, row: Row, name: str) -> None:
        if (turn := self._turn(row)) is not None:
            turn.signals.add(name)

    def _cancelled(self, row: Row) -> None:
        reason = row.payload.get("reason")
        if reason == "superseded":
            turn_id = _text(row.payload.get("turn_id"))
            if turn_id is not None:
                self.superseded[turn_id] = _text(row.payload.get("interrupted_by_turn_id"))
        elif reason in _INTERRUPTS:
            self._signal(row, str(reason))

    def _completed(self, row: Row) -> None:
        if (turn := self._turn(row)) is not None:
            turn.answered = True

    def _guard(self, row: Row) -> None:
        guard = row.payload.get("guard")
        if guard == "unbacked":
            self._count(row, "claimed_not_done")
        if guard in ("repeat", "unbacked"):
            self._signal(row, f"guard_{guard}")

    def _asked(self, row: Row) -> None:
        clarification, turn_id = _text(row.payload.get("clarification_id")), _text(
            row.payload.get("turn_id"),
        )
        if clarification is not None and turn_id is not None:
            self.asked[clarification] = turn_id

    def _clarified(self, row: Row) -> None:
        asker = self.asked.get(_text(row.payload.get("clarification_id")) or "")
        if (turn := self.turns.get(asker or "")) is not None:
            turn.signals.add("clarified")

    def _dismissed(self, row: Row) -> None:
        self._count(row, "dismissed")

    def _started(self, row: Row) -> None:
        self._count(row, "playbacks")
        if (turn := self._turn(row)) is not None and turn.playback_ms is None:
            turn.playback_ms = row.ts_ms

    def _interrupted(self, row: Row) -> None:
        if row.payload.get("reason") in _INTERRUPTS:
            self._count(row, "interrupted")

    def _chunk(self, row: Row) -> None:
        if (turn := self._turn(row)) is not None and turn.chunk_ms is None:
            turn.chunk_ms = row.ts_ms

    def _read(self, row: Row) -> None:
        if (turn := self._turn(row)) is not None:
            turn.verdict = row.payload

    def _fired(self, row: Row) -> None:
        late = row.payload.get("late_ms")
        if (
            isinstance(late, int) and late > LATE_REMINDER_MS
        ) or not row.payload.get("delivered"):
            self._count(row, "reminders_missed")

    def _candidate(self, row: Row) -> None:
        self._count(row, "followed" if row.payload.get("answer") == "yes" else "dismissed")

    def _ride(self, row: Row) -> _Ride | None:
        ride_id = _text(row.payload.get("ride_id"))
        return None if ride_id is None else self.rides.setdefault(ride_id, _Ride())

    def _ride_started(self, row: Row) -> None:
        if (ride := self._ride(row)) is not None and row.payload.get("alight_stop"):
            ride.alight = True

    def _ride_alight(self, row: Row) -> None:
        if (ride := self._ride(row)) is not None:
            ride.alight = True

    def _ride_warned(self, row: Row) -> None:
        if (ride := self._ride(row)) is not None:
            ride.warned = True

    def _ride_ended(self, row: Row) -> None:
        if (ride := self._ride(row)) is not None:
            ride.ended_ms, ride.reason = row.ts_ms, str(row.payload.get("reason"))


def _mark_corrections(day: _Day) -> None:
    """Mark the use a following ``correction`` line corrected, within the window.

    A corrected turn that a later one took over (superseded) is not a use itself, so the mark
    goes to the turn that took its place: the use he had to correct.
    """
    for turn_id, line in day.turns.items():
        earlier = day.turns.get(str(line.verdict.get("prev_turn_id")))
        if (
            line.verdict.get("relation") != "correction"
            or earlier is None
            or not 0 <= line.ts_ms - earlier.ts_ms <= CORRECTION_WINDOW_MS
        ):
            continue
        target, seen = str(line.verdict["prev_turn_id"]), {turn_id}
        while target in day.superseded and target not in seen:
            seen.add(target)
            target = day.superseded[target] or ""
        if target in day.turns and target not in day.superseded:
            day.turns[target].signals.add("correction")


def _spread(values: list[int]) -> dict[str, Any]:
    """Count, median and nearest-rank 90th percentile, in the unit of ``values``."""
    if not values:
        return {"n": 0, "median": None, "p90": None}
    ordered = sorted(values)
    rank = max(0, math.ceil(_P90 * len(ordered)) - 1)
    return {"n": len(ordered), "median": round(statistics.median(ordered)), "p90": ordered[rank]}


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _latencies(uses: Iterable[_Turn]) -> tuple[list[int], list[int]]:
    """Voice (end of his speech to the first sound) and typed (sent to the first text), in ms."""
    voice: list[int] = []
    typed: list[int] = []
    for turn in uses:
        if turn.spoken and turn.spoke_ago_ms is not None and turn.playback_ms is not None:
            voice.append(turn.playback_ms - (turn.ts_ms - turn.spoke_ago_ms))
        elif not turn.spoken and turn.chunk_ms is not None:
            typed.append(turn.chunk_ms - turn.ts_ms)
    return [v for v in voice if v >= 0], [v for v in typed if v >= 0]


def _proactive(day: _Day, reactions: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Followed, dismissed and ignored: the events counted by the fold, plus each card once.

    A card with several reactions counts under the first of acted, dismissed, ignored; a card
    only rated (right, a level) is neither.
    """
    seen: dict[str, set[str]] = {}
    for card, reaction in reactions:
        seen.setdefault(card, set()).add(reaction)
    counts = Counter({name: day.counts[name] for name in ("followed", "dismissed")})
    for kinds in seen.values():
        for reaction, name in _CARD_OUTCOMES:
            if reaction in kinds:
                counts[name] += 1
                break
    return {name: counts[name] for name in ("followed", "dismissed", "ignored")}


def fold_day(
    rows: Iterable[Row], day: date, reactions: Iterable[tuple[str, str]] = (),
) -> dict[str, Any]:
    """The metrics of local date ``day``.

    ``reactions`` are ``(card id, reaction)`` of the day's proactive cards (``notice_feedback``).
    """
    rows = list(rows)
    fold = _Day(day)
    for row in rows:
        if row.type in _OPENING and fold.in_day(row) and (opened := _opened(row)) is not None:
            fold.turns.setdefault(*opened)
    for row in rows:
        fold.feed(row)
    _mark_corrections(fold)
    uses = {tid: t for tid, t in fold.turns.items() if tid not in fold.superseded}
    voice, typed = _latencies(uses.values())
    by_device: dict[str, Counter[str]] = {}
    for turn in uses.values():
        by_device.setdefault(turn.node, Counter())["spoken" if turn.spoken else "typed"] += 1
    spoken = sum(1 for t in uses.values() if t.spoken)
    clean = sum(1 for t in uses.values() if not t.signals)
    signalled = Counter(name for t in uses.values() for name in t.signals)
    started = fold.counts["playbacks"]
    return {
        "version": VERSION,
        "date": day.isoformat(),
        "uses": {
            "total": len(uses),
            "spoken": spoken,
            "typed": len(uses) - spoken,
            "by_device": {
                node: {"spoken": by_device[node]["spoken"], "typed": by_device[node]["typed"]}
                for node in sorted(by_device)
            },
        },
        "right_first_time": {
            "turns": len(uses),
            "clean": clean,
            "share": _ratio(clean, len(uses)),
            "signals": {name: signalled[name] for name in SIGNALS},
        },
        "latency_voice_ms": _spread(voice),
        "latency_typed_ms": _spread(typed),
        "interrupted_ratio": {
            "started": started,
            "interrupted": fold.counts["interrupted"],
            "ratio": _ratio(fold.counts["interrupted"], started),
        },
        "answered_unaddressed": sum(
            1 for t in uses.values() if t.answered and t.verdict.get("relation") == "unrelated"
        ),
        "claimed_not_done": fold.counts["claimed_not_done"],
        "proactive": _proactive(fold, reactions),
        "hard_missed": {
            "reminders": fold.counts["reminders_missed"],
            "rides_missed_alight": sum(
                1
                for ride in fold.rides.values()
                if ride.alight and not ride.warned and ride.reason == "passed_stop"
                and ride.ended_ms is not None and fold.start <= ride.ended_ms < fold.end
            ),
        },
        "labels": [
            {
                "turn_id": tid,
                "tool_group": turn.verdict.get("tool_group"),
                "intent": turn.verdict.get("intent"),
                "failed": bool(turn.signals),
            }
            for tid, turn in sorted(uses.items(), key=lambda item: item[1].ts_ms)
            if turn.spoken
        ],
        "plan_settled": None,
        "calendar_missed": None,
    }


def compute_day(events: sqlite3.Connection, memory_db: Path, day: date) -> dict[str, Any]:
    """Read ``day`` from the event log and the cards' reactions from memory.db, then fold."""
    start, end = day_bounds_ms(day)
    return fold_day(
        read_rows(events, day),
        day,
        job_ledger.card_reactions(
            memory_db,
            datetime.fromtimestamp(start / 1000).astimezone(),
            datetime.fromtimestamp(end / 1000).astimezone(),
        ),
    )


def label_of(entry: Mapping[str, Any]) -> str:
    """What a labelled voice turn is counted under: its tool group, else its instant intent."""
    for key in ("tool_group", "intent"):
        value = entry.get(key)
        if isinstance(value, str) and value != "none":
            return value
    return "chat" if entry.get("tool_group") or entry.get("intent") else "unlabelled"


def weekly(days: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Most used and most failed label over ``days`` (the last seven rows), from their labels.

    Most failed is the label with the most turns that were not right the first time, the higher
    share breaking a tie; None when nothing failed.
    """
    used: Counter[str] = Counter()
    failed: Counter[str] = Counter()
    for metrics in days:
        for entry in metrics.get("labels") or ():
            label = label_of(entry)
            used[label] += 1
            failed[label] += bool(entry.get("failed"))
    if not used:
        return {"days": len(days), "turns": 0, "most_used": None, "most_failed": None}
    top = max(used, key=lambda name: (used[name], name))
    worst = max(
        (name for name in failed if failed[name]),
        key=lambda name: (failed[name], failed[name] / used[name], name),
        default=None,
    )
    return {
        "days": len(days),
        "turns": sum(used.values()),
        "most_used": {"label": top, "turns": used[top]},
        "most_failed": None
        if worst is None
        else {"label": worst, "failed": failed[worst], "turns": used[worst]},
    }


__all__ = [
    "CORRECTION_WINDOW_MS",
    "LATE_REMINDER_MS",
    "SIGNALS",
    "VERSION",
    "Row",
    "compute_day",
    "day_bounds_ms",
    "first_day",
    "fold_day",
    "label_of",
    "read_rows",
    "weekly",
]
