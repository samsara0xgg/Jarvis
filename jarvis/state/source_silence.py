"""L2 — has a data source stopped arriving while it should be producing? (ADR 0381).

Three sources reach the brain from the Mac and two of them failed once without a word: the
microphone's recording pieces (after a sleep the Mac terminal logged ``output_unavailable`` and
kept no piece) and TimeSink's screen text (every OCR run failed for 30 hours). TimeSink's spans
are the canary for TimeSink itself. Nothing marks such a gap but the missing rows, so this module
reads the rows.

A source is judged by three fixed things, borrowed from how a commercial tracker judges its own:

- **A precondition.** Time counts against a source only while it should be producing: the Mac is
  awake for the microphone; awake, unlocked, not idle, not paused and not hands-off, with TimeSink
  running, for TimeSink. A closed lid is not a silence. And only time the brain has heard from the
  Mac counts: a sleeping Mac ships its sleep mark after it wakes, so eligible time ends at the
  newest TimeSink row the copy holds.
- **The time since the last success**, counted in that eligible time alone, so a night asleep adds
  nothing. A source is silent once it passes its allowed gap (:data:`SOURCES`).
- **Debounce.** The allowed gap is two windows of the source's own cadence (:data:`SOURCES`): one
  empty window is a pause, two are a break.

Everything here is a pure function over rows already read except the readers, which only open
files and databases read-only. The nightly check (:mod:`jarvis.state.log_invariants`) reports
every silence inside its window; the daemon's watcher (``jarvis.runtime.source_silence``) asks the
same question about now and raises one card.

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

HOUR_MS: Final = 3_600_000
MINUTE_MS: Final = 60_000
SHIPPED_PARTS: Final = ("imports", "shipped.db")
"""Under the runtime root: where the Mac's TimeSink copy lands (ADR 0294)."""
LIFELOG_DIRNAME: Final = "lifelog"
"""Under the runtime root unless ``lifelog.dir`` names another place (ADR 0238)."""

SPANS: Final = "timesink.spans"
OCR: Final = "timesink.ocr"
MIC: Final = "mac.mic"

# TimeSink's state events, as the Mac copy holds them (kind ``stateEvent``). The kinds come in
# pairs of opposites; a group is bad from its first member until its second.
_RUN, _LOCK, _SLEEP, _IDLE, _TRACKING, _CAPTURE, _HANDS = range(7)
_GROUPS: Final = 7
_BAD_AT: Final[dict[str, int]] = {
    "stop": _RUN, "lock": _LOCK, "sleep": _SLEEP, "idle": _IDLE,
    "tracking_pause": _TRACKING, "pause": _CAPTURE, "screen_denied": _CAPTURE,
    "hands_off": _HANDS,
}
_GOOD_AT: Final[dict[str, tuple[int, ...]]] = {
    "unlock": (_LOCK,), "wake": (_SLEEP,), "active": (_IDLE,),
    "tracking_resume": (_TRACKING,), "resume": (_CAPTURE,), "hands_on": (_HANDS,),
}
_RESETS: Final = frozenset({"start", "stop"})
"""TimeSink's own start and stop: what it said about the Mac before them no longer holds."""
_ENDS_HANDS_OFF: Final = frozenset({"idle", "lock", "sleep"})
"""A hands-off stretch ends where he goes idle, locks the screen or the Mac sleeps."""
_SPAN_GROUPS: Final = frozenset({_RUN, _LOCK, _SLEEP, _IDLE, _TRACKING, _HANDS})
_OCR_GROUPS: Final = frozenset({_RUN, _LOCK, _SLEEP, _IDLE, _TRACKING, _CAPTURE})
_AWAKE_GROUPS: Final = frozenset({_RUN, _SLEEP})
"""TimeSink running and the Mac awake: all the microphone can be said to need."""

_STATE_LOOKBACK_MS: Final = 7 * 24 * HOUR_MS
"""How far before the window the state events are kept, to know the state at its start."""
RETIRED_AFTER_MS: Final = 14 * 24 * HOUR_MS
"""A source with no arrival at all for this long was switched off on purpose, not broken."""

# Spans, capture health and the heard-from probe walk the time index from the window (`INDEXED BY`:
# the planner would otherwise take the (source, key) index and walk every TimeSink row ever
# shipped, 100,000 spans). The state marks are few and read by key, as the doing lane reads them:
# `+at_ms` keeps the planner on that range.
_SQL_STATES: Final = (
    "SELECT at_ms, json_extract(fields, '$.kind') FROM import_entries "
    "WHERE source = 'timesink' AND key >= 'stateEvent:' AND key < 'stateEvent;' "
    "AND +at_ms >= ? AND +at_ms <= ? ORDER BY at_ms"
)
_SQL_SPANS: Final = (
    "SELECT DISTINCT at_ms, json_extract(fields, '$.end') "
    "FROM import_entries INDEXED BY import_entries_at "
    "WHERE source = 'timesink' AND kind = 'span' AND at_ms >= ? AND at_ms <= ? ORDER BY at_ms"
)
# A row of the 10-minute health table is shipped again as it grows (a revision per version), all
# at the row's own start: the best of its versions is what it did.
_SQL_OCR: Final = (
    "SELECT at_ms, MAX(json_extract(fields, '$.ocrRuns') - json_extract(fields, '$.ocrFailed')), "
    "MAX(json_extract(fields, '$.ocrFailed')) FROM import_entries INDEXED BY import_entries_at "
    "WHERE source = 'timesink' AND kind = 'captureHealth' AND at_ms >= ? AND at_ms <= ? "
    "GROUP BY at_ms ORDER BY at_ms"
)
_SQL_ALIVE: Final = (
    "SELECT 1 FROM import_entries INDEXED BY import_entries_at "
    "WHERE source = 'timesink' AND kind = ? "
    "AND at_ms >= ? AND at_ms <= ? LIMIT 1"
)
# The newest moment a row of TimeSink's own activity carries: the Mac ships what it has seen, so
# its newest row is the last the brain heard from it (a configuration row says nothing of that).
_SQL_HEARD: Final = (
    "SELECT MAX(at_ms) FROM import_entries INDEXED BY import_entries_at "
    "WHERE source = 'timesink' AND kind IN ('span', 'captureHealth', 'stateEvent') "
    "AND at_ms <= ?"
)
ALL_QUERIES: Final[tuple[tuple[str, str], ...]] = (
    ("silence_heard", _SQL_HEARD),
    ("silence_alive", _SQL_ALIVE),
    ("silence_states", _SQL_STATES),
    ("silence_spans", _SQL_SPANS),
    ("silence_ocr", _SQL_OCR),
)
"""The statements run on the Mac copy, for the ``EXPLAIN QUERY PLAN`` test."""

_RETIRED: Final = "no arrival for 14 days: switched off on purpose, not judged"
SHIPPED_PROBE: Final = "SELECT 1 FROM import_entries LIMIT 1"
"""Fails unless the file opened is the Mac copy."""
_PIECE: Final = re.compile(r"(\d{8}T\d{6}\.\d{3})Z_(\d+)ms_")
_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Source:
    """One row of the table: where arrivals are read, what must hold, how long a gap is fine."""

    name: str
    arrivals: str
    precondition: str
    allowed_gap_ms: int


SOURCES: Final[tuple[Source, ...]] = (
    Source(
        SPANS,
        "a TimeSink span starts, or a shipped version of one ends (kind span of the Mac copy)",
        "TimeSink running, Mac awake and unlocked, not idle, not hands-off, tracking not paused",
        2 * HOUR_MS,
    ),
    Source(
        OCR,
        "a 10-minute capture row with at least one OCR run that did not fail",
        "TimeSink running, Mac awake and unlocked, not idle, tracking and capture not paused",
        HOUR_MS,
    ),
    Source(
        MIC,
        "a recording piece of any device in the lifelog archive, at its end",
        "TimeSink running and the Mac awake (the lock and idle do not stop the microphone)",
        2 * HOUR_MS,
    ),
)
_GAP: Final = {one.name: one.allowed_gap_ms for one in SOURCES}


@dataclass(frozen=True)
class Evidence:
    """The rows a judgement needs, already read.

    ``spans`` are the instants a span started or a shipped version of one ended. ``ocr`` is
    ``(start, runs that did not fail, runs that failed)`` per 10-minute row. ``until_ms`` is the
    newest moment the brain has heard from the Mac, where the judgement stops. A source
    that is not judged, and why, is in ``skipped``: its rows could not be read, or it has not
    arrived at all for :data:`RETIRED_AFTER_MS`.
    """

    states: tuple[tuple[int, str], ...] = ()
    spans: tuple[int, ...] = ()
    ocr: tuple[tuple[int, int, int], ...] = ()
    pieces: tuple[tuple[int, int], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    until_ms: int | None = None


@dataclass(frozen=True)
class Silence:
    """One stretch where a source passed its allowed gap.

    ``last_arrival_ms`` is the last arrival before it (``None`` when none lies inside the window),
    ``tripped_ms`` the instant the allowed gap ran out, ``ended_ms`` the arrival that ended it
    (``None`` while it goes on), ``eligible_ms`` the eligible time without an arrival, and
    ``failed`` how many OCR runs failed in it (OCR only).
    """

    source: str
    last_arrival_ms: int | None
    tripped_ms: int
    ended_ms: int | None
    eligible_ms: int
    failed: int = 0

    @property
    def ongoing(self) -> bool:
        """Whether no arrival has ended it yet."""
        return self.ended_ms is None

    def to_dict(self) -> dict[str, object]:
        """Names, times and a gap; the JSON shape."""
        return {
            "source": self.source,
            "last_arrival": None if self.last_arrival_ms is None else iso(self.last_arrival_ms),
            "tripped": iso(self.tripped_ms),
            "ended": None if self.ended_ms is None else iso(self.ended_ms),
            "eligible_minutes": self.eligible_ms // MINUTE_MS,
            "failed_runs": self.failed,
        }


@dataclass(frozen=True)
class Judgement:
    """Every source's silences over a window, and why a source was not judged."""

    since_ms: int
    ref_ms: int
    silences: tuple[Silence, ...]
    skipped: tuple[tuple[str, str], ...]

    @property
    def ongoing(self) -> tuple[Silence, ...]:
        """The silences that have not ended."""
        return tuple(one for one in self.silences if one.ongoing)


def iso(epoch_ms: int) -> str:
    """UTC, second precision, as the check log writes it."""
    moment = datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def shipped_path(runtime_root: Path) -> Path:
    """Where the Mac copy sits under a runtime root."""
    return runtime_root.joinpath(*SHIPPED_PARTS)


# --- Reading ------------------------------------------------------------------------------


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _utc_ms(text: object) -> int | None:
    """The UTC millisecond of a TimeSink ``DATETIME`` text like ``2026-10-19 05:01:47.282``."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return int(moment.replace(tzinfo=moment.tzinfo or UTC).timestamp() * 1000)


def read_evidence(
    shipped: sqlite3.Connection, lifelog_dir: Path, *, since_ms: int, ref_ms: int,
) -> Evidence:
    """The rows of ``[since_ms, ref_ms]``, read-only, up to where the brain last heard the Mac.

    ``shipped`` is the Mac copy (``imports/shipped.db``) opened read-only; the recording pieces
    are read from the archive's file names alone and never opened. A source the rows cannot
    judge is named in ``skipped`` with the reason.

    Raises:
        sqlite3.Error: ``shipped`` is not the Mac copy.
    """
    (heard,) = shipped.execute(_SQL_HEARD, (ref_ms,)).fetchone()
    if not isinstance(heard, int):
        why = "the Mac copy holds no TimeSink rows yet"
        return Evidence(skipped=tuple((one.name, why) for one in SOURCES), until_ms=ref_ms)
    until = min(ref_ms, heard)
    states = tuple(
        (_count(at), kind)
        for at, kind in shipped.execute(_SQL_STATES, (since_ms - _STATE_LOOKBACK_MS, until))
        if isinstance(kind, str)
    )
    span_rows = shipped.execute(_SQL_SPANS, (since_ms, until)).fetchall()
    started = {_count(at) for at, _end in span_rows}
    ended = {
        moment for _at, text in span_rows
        if (moment := _utc_ms(text)) is not None and moment <= until
    }
    ocr = tuple(
        (_count(at), max(_count(ok), 0), _count(failed))
        for at, ok, failed in shipped.execute(_SQL_OCR, (since_ms, until))
    )
    recent = ref_ms - RETIRED_AFTER_MS
    skipped: dict[str, str] = {}
    for name, kind in ((SPANS, "span"), (OCR, "captureHealth")):
        if shipped.execute(_SQL_ALIVE, (kind, min(recent, since_ms), until)).fetchone() is None:
            skipped[name] = _RETIRED
    pieces = _pieces(lifelog_dir, min(recent, since_ms), until)
    if pieces is None:
        skipped[MIC] = f"no {LIFELOG_DIRNAME}/audio archive to read the recording pieces from"
    elif not pieces:
        skipped[MIC] = _RETIRED
    kept = tuple(piece for piece in pieces or () if since_ms <= piece[1])
    return Evidence(
        states, tuple(sorted(started | ended)), ocr, kept, tuple(sorted(skipped.items())), until,
    )


def _pieces(lifelog_dir: Path, since_ms: int, ref_ms: int) -> tuple[tuple[int, int], ...] | None:
    """``(start, end)`` of each kept audio piece that ends in the window, from the file names."""
    audio = lifelog_dir / "audio"
    if not audio.is_dir():
        return None
    first = datetime.fromtimestamp((since_ms - 24 * HOUR_MS) / 1000, tz=UTC).strftime("%Y-%m-%d")
    found: list[tuple[int, int]] = []
    try:
        for device in audio.iterdir():
            for day in device.iterdir():
                if not _DAY.fullmatch(day.name) or day.name < first:
                    continue
                for path in day.iterdir():
                    piece = _PIECE.match(path.name)
                    if piece is None:
                        continue
                    started = datetime.strptime(piece[1], "%Y%m%dT%H%M%S.%f").replace(tzinfo=UTC)
                    start = round(started.timestamp() * 1000)
                    end = start + int(piece[2])
                    if since_ms <= end <= ref_ms:
                        found.append((start, end))
    except OSError:
        return None
    return tuple(sorted(found))


# --- Judging ------------------------------------------------------------------------------


def _marked(bad: list[bool], kind: str) -> None:
    """Apply one state mark to the groups' badness, in place."""
    if kind in _RESETS:
        bad[:] = [False] * _GROUPS
    if kind in _BAD_AT:
        bad[_BAD_AT[kind]] = True
    if kind in _ENDS_HANDS_OFF:
        bad[_HANDS] = False
    for group in _GOOD_AT.get(kind, ()):
        bad[group] = False


def eligible_intervals(
    states: tuple[tuple[int, str], ...], groups: frozenset[int], since_ms: int, ref_ms: int,
) -> list[tuple[int, int]]:
    """The stretches of ``[since_ms, ref_ms]`` where every state group in ``groups`` is good.

    Every group starts good and a state event turns one bad or good again; the events before
    ``since_ms`` give the state at its start. TimeSink's own start and stop clear the marks it
    made before them. Kinds this module does not know are ignored.
    """
    bad = [False] * _GROUPS
    out: list[tuple[int, int]] = []
    open_at: int | None = since_ms
    for at, kind in states:
        if at > ref_ms:
            break
        _marked(bad, kind)
        good = not any(bad[group] for group in groups)
        if at <= since_ms:
            open_at = since_ms if good else None
        elif open_at is not None and not good:
            out.append((open_at, at))
            open_at = None
        elif open_at is None and good:
            open_at = at
    if open_at is not None and ref_ms > open_at:
        out.append((open_at, ref_ms))
    return out


def _eligible_between(intervals: list[tuple[int, int]], low: int, high: int) -> int:
    return sum(max(0, min(b, high) - max(a, low)) for a, b in intervals)


def _trip_instant(intervals: list[tuple[int, int]], low: int, high: int, gap_ms: int) -> int:
    """The instant the eligible time in ``[low, high]`` reaches ``gap_ms``."""
    left = gap_ms
    for a, b in intervals:
        start, end = max(a, low), min(b, high)
        if end <= start:
            continue
        if end - start >= left:
            return start + left
        left -= end - start
    return high


def silences(  # noqa: PLR0913 - the source's arrivals and gate, and the window it is judged in
    name: str, arrivals: list[int], eligible: list[tuple[int, int]], *,
    since_ms: int, ref_ms: int, gap_ms: int, failures: list[tuple[int, int]] | None = None,
) -> list[Silence]:
    """Each stretch where ``gap_ms`` or more of eligible time passed with no arrival.

    The count starts at ``since_ms`` and again at every arrival. ``failures`` are ``(instant,
    how many)`` of failed attempts, counted into the silence they fall in.
    """
    marks = [since_ms, *sorted(a for a in arrivals if since_ms < a <= ref_ms), ref_ms]
    found: list[Silence] = []
    for index in range(len(marks) - 1):
        low, high = marks[index], marks[index + 1]
        spent = _eligible_between(eligible, low, high)
        if spent < gap_ms:
            continue
        found.append(Silence(
            source=name,
            last_arrival_ms=low if index > 0 else None,
            tripped_ms=_trip_instant(eligible, low, high, gap_ms),
            ended_ms=high if index + 2 < len(marks) else None,
            eligible_ms=spent,
            failed=sum(n for at, n in failures or () if low < at <= high),
        ))
    return found


def judge(evidence: Evidence, *, since_ms: int, ref_ms: int) -> Judgement:
    """Judge every source in :data:`SOURCES` that ``evidence`` does not skip.

    The window ends where the brain last heard from the Mac (``evidence.until_ms``) if that is
    before ``ref_ms``: the Mac's own marks of a sleep arrive after it wakes.
    """
    ref_ms = ref_ms if evidence.until_ms is None else min(ref_ms, evidence.until_ms)
    left_out = {name for name, _why in evidence.skipped}
    spans_gate = eligible_intervals(evidence.states, _SPAN_GROUPS, since_ms, ref_ms)
    ocr_gate = eligible_intervals(evidence.states, _OCR_GROUPS, since_ms, ref_ms)
    awake = eligible_intervals(evidence.states, _AWAKE_GROUPS, since_ms, ref_ms)
    found: list[Silence] = []
    if SPANS not in left_out:
        found += silences(
            SPANS, list(evidence.spans), spans_gate, gap_ms=_GAP[SPANS],
            since_ms=since_ms, ref_ms=ref_ms,
        )
    if OCR not in left_out:
        found += silences(
            OCR, [at for at, ok, _failed in evidence.ocr if ok > 0], ocr_gate, gap_ms=_GAP[OCR],
            failures=[(at, failed) for at, _ok, failed in evidence.ocr if failed],
            since_ms=since_ms, ref_ms=ref_ms,
        )
    if MIC not in left_out:
        found += silences(
            MIC, [end for _start, end in evidence.pieces], awake, gap_ms=_GAP[MIC],
            since_ms=since_ms, ref_ms=ref_ms,
        )
    return Judgement(since_ms, ref_ms, tuple(found), evidence.skipped)


def check(
    shipped: sqlite3.Connection, lifelog_dir: Path, *, since_ms: int, ref_ms: int,
) -> Judgement:
    """Read the rows of the window and judge them.

    Raises:
        sqlite3.Error: ``shipped`` is not the Mac copy.
    """
    evidence = read_evidence(shipped, lifelog_dir, since_ms=since_ms, ref_ms=ref_ms)
    return judge(evidence, since_ms=since_ms, ref_ms=ref_ms)


__all__ = [
    "ALL_QUERIES",
    "MIC",
    "OCR",
    "RETIRED_AFTER_MS",
    "SHIPPED_PROBE",
    "SOURCES",
    "SPANS",
    "Evidence",
    "Judgement",
    "Silence",
    "Source",
    "check",
    "eligible_intervals",
    "iso",
    "judge",
    "read_evidence",
    "shipped_path",
    "silences",
]
