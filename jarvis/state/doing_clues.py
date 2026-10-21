"""ADR 0302: the short dated clues of what he was doing, read for one day.

The writer of the doing lane (a model) is shown the day's facts and, with them, clues: pieces of
evidence that say what a stretch was without being a fact about where he was or what his Mac
did. :func:`gather` reads them, each tagged with one of :data:`jarvis.state.doing.CLUE_KINDS`:

- ``words``: what was said between him and her in the window, and a commitment he told her of;
- ``picture``: a picture he sent, with the note written for it (ADR 0223);
- ``mail``: a commitment made from his mail (ADR 0274) and the interview time in a job mail;
- ``calendar``: an event of his Outlook calendar (ADR 0230);
- ``plan``: an item of the day's plan (ADR 0288);
- ``payment``: a payment that moved money (ADR 0259);
- ``chat``: a conversation with Claude, or the night's letter on them (ADR 0295);
- ``code``: a Claude Code session, and the recap Claude Code wrote of it (ADR 0294);
- ``away_note``: what he wrote in TimeSink for a stretch away from his Mac (ADR 0302);
- ``screen``: what the work state said he was doing lately (ADR 0180), at each revision where
  that changed; its list of the day's activities is a recap, not dated, so it is not a clue;
- ``answer``: what he answered about a stretch of this day, and about earlier stretches within
  the stay radius of a place he stayed at (ADR 0302).

A source that cannot be read leaves its clues out, logged. Nothing is stored and nothing is
guessed: a clue is what the source holds, cut short, except what he and she said, which is whole
(ADR 0332). ``photo`` has no source yet, so none is read.

Layer rules: ``jarvis.state`` and ``jarvis.shared`` only (L2).
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import date, datetime
from functools import partial
from math import ceil
from typing import TYPE_CHECKING, Any, Final, NamedTuple

from jarvis.state import work_state
from jarvis.state.event_log import iter_events_of_types
from jarvis.state.memory_db import (
    doing_answers,
    doing_answers_near,
    open_memory_db,
    spoken_between,
)
from jarvis.state.phone_day import STAY_RADIUS_M

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.state.memory_db import DoingAnswer

LOGGER = logging.getLogger(__name__)

CLUE_TEXT_MAX_CHARS: Final = 140
"""The most characters of a clue's text but words; a longer one is cut and ends in an ellipsis."""
MAX_CLUES_PER_KIND: Final = 40
"""The most clues of any kind but words in a day; more are thinned evenly through the day."""
NEAR_ANSWERS_PER_PLACE: Final = 3
"""The newest earlier answers kept for each place he stayed at."""

_SCREEN_EVENT_TYPE: Final = work_state.EVENT_TYPE
HIS_WORDS: Final = "he: "
"""How a clue of what he said begins; hers begins with `she: `."""
_SPEAKER: Final = {"allen": HIS_WORDS, "jarvis": "she: ", "jarvis_live": "she: "}


class Clue(NamedTuple):
    """One piece of evidence: its kind (one of ``CLUE_KINDS``), when, and what it says."""

    kind: str
    at_ms: int
    text: str


def _short(text: object) -> str:
    """The text on one line, cut to :data:`CLUE_TEXT_MAX_CHARS`."""
    line = " ".join(str(text).split())
    if len(line) <= CLUE_TEXT_MAX_CHARS:
        return line
    return line[: CLUE_TEXT_MAX_CHARS - 1].rstrip() + "…"


def _clock(ms: int | None, zone: tzinfo) -> str:
    return "now" if ms is None else f"{datetime.fromtimestamp(ms / 1000, zone):%H:%M}"


def _ranged(text: str, item: Mapping[str, Any], zone: tzinfo) -> str:
    """``text`` with the clock times of an item that lasts a while."""
    start, end = int(item["start_ms"]), item["end_ms"]
    if end is None or int(end) <= start:
        return text
    return f"{text} ({_clock(start, zone)}-{_clock(int(end), zone)})"


def _event(item: Mapping[str, Any], zone: tzinfo) -> Clue:
    """A calendar event, a plan item or a commitment, by where it came from."""
    source = item.get("source")
    title = str(item.get("title") or "")
    if source == "plan":
        kind, text = "plan", title
    elif source == "mail":
        kind, text = "mail", title
    elif source == "said":
        kind, text = "words", f"he told her: {title}"
    else:
        kind, text = "calendar", title
    if item.get("place"):
        text += f" at {item['place']}"
    return Clue(kind, int(item["start_ms"]), _short(_ranged(text, item, zone)))


def _payment(item: Mapping[str, Any]) -> Clue:
    parts = (
        item.get("direction"), item.get("amount"), item.get("currency"), item.get("counterparty"),
        item.get("text"),
    )
    return Clue("payment", int(item["start_ms"]), _short(" ".join(str(p) for p in parts if p)))


def _picture(item: Mapping[str, Any], _zone: tzinfo) -> list[Clue]:
    note = item.get("note")
    return [Clue("picture", int(item["start_ms"]), _short(note) if note else "sent a picture")]


def _chat(item: Mapping[str, Any], zone: tzinfo) -> list[Clue]:
    text = f"Claude chat {item.get('text')}, {item.get('messages')} messages"
    return [Clue("chat", int(item["start_ms"]), _short(_ranged(text, item, zone)))]


def _chat_day(item: Mapping[str, Any], _zone: tzinfo) -> list[Clue]:
    text = f"Claude chats of the day: {item.get('text')}"
    return [Clue("chat", int(item["start_ms"]), _short(text))]


def _session(item: Mapping[str, Any], zone: tzinfo) -> list[Clue]:
    """A Claude Code session, and the recap Claude Code wrote of it, at the session's end."""
    start = int(item["start_ms"])
    what = f"Claude Code session in {item.get('project') or 'a project'}: {item.get('title')}"
    found = [Clue("code", start, _short(_ranged(what, item, zone)))]
    if item.get("detail"):
        found.append(Clue("code", int(item["end_ms"] or start), _short(item["detail"])))
    return found


def _away(item: Mapping[str, Any], zone: tzinfo) -> list[Clue]:
    """What he wrote in TimeSink for a stretch away from his Mac, at the stretch's start."""
    text = _ranged(f"he wrote in TimeSink: {item['title']}", item, zone)
    return [Clue("away_note", int(item["start_ms"]), _short(text))]


_CLUES: Final[dict[str, Callable[[Mapping[str, Any], tzinfo], list[Clue]]]] = {
    "picture": _picture,
    "event": lambda item, zone: [_event(item, zone)],
    "payment": lambda item, _zone: [_payment(item)],
    "claude_chat": _chat,
    "claude_day": _chat_day,
    "code": _session,
    "away_note": _away,
}
"""How each kind of day-line item that holds a clue becomes one; a fact (a stay, a work block, a
ride, a talk) holds none."""


def _from_item(item: Mapping[str, Any], zone: tzinfo) -> list[Clue]:
    """The clues one day-line item holds."""
    make = _CLUES.get(item["kind"])
    return [] if make is None else make(item, zone)


def _words(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[Clue]:
    """Each thing he or she said in the window, whole, on one line."""
    return [
        Clue("words", at, " ".join(f"{_SPEAKER[source]}{words}".split()))
        for at, source, words in spoken_between(conn, start_ms, end_ms)
    ]


def _screen(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> list[Clue]:
    """What the work state said he was doing lately, at each revision in the window changing it."""
    found: list[Clue] = []
    last: str | None = None
    for event in iter_events_of_types(conn, (_SCREEN_EVENT_TYPE,), since_epoch_ms=start_ms):
        item = event.payload.get("item")
        if event.ts_epoch_ms >= end_ms or not isinstance(item, dict):
            continue
        now = item.get("now")
        text = _short(now.get("text") or "") if isinstance(now, dict) else ""
        if text and text != last:
            found.append(Clue("screen", event.ts_epoch_ms, text))
        last = text or None
    return found


def _interviews(
    conn: sqlite3.Connection, start_ms: int, end_ms: int, zone: tzinfo,
) -> list[Clue]:
    """The time a job mail names for an interview, when it falls in the window."""
    try:
        rows = conn.execute(
            "SELECT event_at, company, role, event_text FROM job_mail "
            "WHERE event_at IS NOT NULL AND kind = 'interview' AND COALESCE(deleted, 0) = 0",
        ).fetchall()
    except sqlite3.Error:  # no job-hunt table: nothing was mailed
        return []
    found: list[Clue] = []
    for event_at, company, role, event_text in rows:
        try:
            at = datetime.fromisoformat(str(event_at))
        except ValueError:
            continue
        ms = int((at if at.tzinfo else at.replace(tzinfo=zone)).timestamp() * 1000)
        if start_ms <= ms < end_ms:
            what = f"interview mail: {company or 'a company'}, {role or 'a role'}"
            found.append(Clue("mail", ms, _short(f"{what} ({event_text or 'time given'})")))
    return found


def _answered(answer: DoingAnswer, zone: tzinfo, *, near: bool) -> Clue:
    said = answer.text or answer.choice or ""
    when = datetime.fromtimestamp(answer.start_ms / 1000, zone)
    if near:
        text = f"on {when:%m-%d} at this place he said it was: {said}"
    else:
        text = f"he said {when:%H:%M} was: {said}"
    return Clue("answer", answer.start_ms, _short(text))


def _answers(
    conn: sqlite3.Connection, day: str, items: Iterable[Mapping[str, Any]], zone: tzinfo,
) -> list[Clue]:
    """His answers about this day, and the newest about earlier stretches near each stay."""
    seen: set[str] = set()
    found: list[Clue] = []
    for answer in doing_answers(conn, day):
        seen.add(answer.id)
        found.append(_answered(answer, zone, near=False))
    places: dict[tuple[float, float], int] = {}
    for item in items:
        if item["kind"] == "stay":
            key = (round(float(item["lat"]), 4), round(float(item["lng"]), 4))
            places[key] = min(int(item["start_ms"]), places.get(key, int(item["start_ms"])))
    for at, begins in places.items():
        for answer in doing_answers_near(
            conn, at, STAY_RADIUS_M, begins, limit=NEAR_ANSWERS_PER_PLACE,
        ):
            if answer.id not in seen:
                seen.add(answer.id)
                found.append(_answered(answer, zone, near=True))
    return found


def _thinned(clues: list[Clue], limit: int) -> list[Clue]:
    """At most ``limit`` clues, every n-th through the day when there are more."""
    if len(clues) <= limit:
        return clues
    return clues[:: ceil(len(clues) / limit)]


def _guarded(name: str, read: Callable[[], list[Clue]]) -> list[Clue]:
    try:
        return read()
    except Exception:
        LOGGER.exception("doing clues: the %s source could not be read", name)
        return []


def gather(  # noqa: PLR0913 — the stores, the day, its items, its window and its zone.
    event_log: sqlite3.Connection, memory_db: Path | None, day: date,
    items: Sequence[Mapping[str, Any]], window: tuple[int, int], zone: tzinfo,
) -> list[Clue]:
    """The clues of ``day`` inside ``window`` (ms), oldest first.

    ``items`` are the day line's items, which already hold what he sent, what he has to go to,
    the plan, his payments, his Claude chats and sessions; the rest is read from ``event_log``
    (the work state's screen) and ``memory_db`` (what was said, interview mail, his answers).
    Every word he and she said is a clue, whole. Each other clue's text is cut to
    :data:`CLUE_TEXT_MAX_CHARS`, and each other kind is thinned to :data:`MAX_CLUES_PER_KIND`.
    """
    start, end = window
    found: list[Clue] = []
    for item in items:
        if start <= int(item["start_ms"]) < end:
            found += _guarded("day line", partial(_from_item, item, zone))
    found += _guarded("screen", partial(_screen, event_log, start, end))
    if memory_db is not None and memory_db.is_file():
        with closing(open_memory_db(memory_db)) as conn:
            found += _guarded("words", partial(_words, conn, start, end))
            found += _guarded("job mail", partial(_interviews, conn, start, end, zone))
            found += _guarded("answers", partial(_answers, conn, day.isoformat(), items, zone))
    by_kind: dict[str, list[Clue]] = {}
    for clue in sorted(dict.fromkeys(found), key=lambda c: (c.at_ms, c.kind, c.text)):
        by_kind.setdefault(clue.kind, []).append(clue)
    kept = [
        clue
        for kind, clues in by_kind.items()
        for clue in (clues if kind == "words" else _thinned(clues, MAX_CLUES_PER_KIND))
    ]
    return sorted(kept, key=lambda c: (c.at_ms, c.kind, c.text))


__all__ = [
    "CLUE_TEXT_MAX_CHARS",
    "HIS_WORDS",
    "MAX_CLUES_PER_KIND",
    "NEAR_ANSWERS_PER_PLACE",
    "Clue",
    "gather",
]
