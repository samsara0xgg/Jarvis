"""ADR 0243: what he and Jarvis said is one read-only tool that searches, summarizes and opens."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.daily_contract import DailyError, encoded
from jarvis.state.daily_records import rank_by_words, read_connection
from jarvis.state.memory_db import PICTURE_NOTE_SOURCE, latest_day_summaries

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo
    from pathlib import Path

_DESCRIPTION: Final = (
    "Use for what Allen and you said to each other before, not for what he did (my_day) or "
    "his mail. query: lines holding any of the words, up to 8 per page, each with who, when "
    "and an excerpt. date (a day; to= its last day, up to 14 days): the topics talked about, "
    "day by day. date or query with order, speaker or page: the lines themselves, so 'the "
    "first thing I said last month' is date+to, order=oldest, speaker=Allen. open: an id from "
    "a result, the exchange around that line."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "Words separated by spaces, in the language they were said in.",
        },
        "date": {
            "type": "string",
            "description": "A day (2026-10-01, today, yesterday); with query it limits the search.",
        },
        "to": {"type": "string", "description": "With date: the last day of a span, included."},
        "open": {
            "type": "string",
            "description": "An id from an earlier result; returns the exchange around that line.",
        },
        "order": {
            "type": "string",
            "enum": ["newest", "oldest"],
            "description": "oldest answers 'the first thing ...'; the default is newest.",
        },
        "speaker": {
            "type": "string",
            "enum": ["Allen", "Jarvis"],
            "description": "Only that side's lines.",
        },
        "page": {
            "type": "integer",
            "description": "Lines 9-16 are page 2, 17-24 page 3; omit for the first 8.",
        },
    },
    "additionalProperties": False,
}
_PAGE: Final = 8
_EXCERPT_CHARS: Final = 200
_BEFORE_MATCH_CHARS: Final = 60
_TOPIC_DAYS: Final = 14
_BUDGET: Final = 4000
_FOCUS_CHARS: Final = 1800
_NEAR_CHARS: Final = 300
_AROUND: Final = 2
_TOPIC_STEPS: Final = ((4, 150), (3, 120), (2, 90), (1, 70))
"""How many topics a day keeps and how long each may be, widest first, until the answer fits."""
_USER: Final = "allen"
_JARVIS: Final = ("jarvis", "jarvis_live")
_RECORD_ID: Final = re.compile(r"\s*\[record_id=[^\]]*\]")


def _who(source: str) -> str:
    if source == _USER:
        return "Allen"
    if source in _JARVIS:
        return "Jarvis"
    return "picture note" if source == PICTURE_NOTE_SOURCE else source


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _excerpt(text: str, words: Sequence[str]) -> str:
    """The line squeezed to one, at most 200 characters, around the first word that matches."""
    line = " ".join(text.split())
    if len(line) <= _EXCERPT_CHARS:
        return line
    lower = line.lower()
    at = min((i for w in words if (i := lower.find(w.lower())) >= 0), default=0)
    start = max(0, at - _BEFORE_MATCH_CHARS)
    shown = line[start : start + _EXCERPT_CHARS]
    return f"{'…' if start else ''}{shown}{'…' if start + _EXCERPT_CHARS < len(line) else ''}"


def _bullets(summary: str, heading: str) -> list[str]:
    """The ``- `` lines under a day summary's ``### <heading>``, without their record ids."""
    out: list[str] = []
    inside = False
    for line in summary.splitlines():
        if line.startswith("### "):
            inside = line[4:].strip() == heading
        elif inside and line.startswith("- "):
            item = _RECORD_ID.sub("", line[2:]).strip()
            if item and item.lower() != "none":
                out.append(item)
    return out


def _where(window: tuple[date, date] | None, today: date) -> str:
    if window is None:
        return ""
    first, last = window
    if first == last:
        return f" on {speakable.day_name(first, today)}"
    return f" between {speakable.day_name(first, today)} and {speakable.day_name(last, today)}"


class _Conversation:
    """The tool's handler: lines for words or a window, a window's topics, one line opened."""

    def __init__(self, store: Path, zone: tzinfo, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._zone = zone
        self._clock = clock

    def run(self, args: Mapping[str, Any], _ctx: object) -> dict[str, Any]:
        """Dispatch on the arguments; a store that cannot be read is worded for the model."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = (
                f"conversation: no argument {unknown[0]!r}; the arguments are query, date, to, "
                "open, order, speaker and page"
            )
            raise ToolError(msg, code="invalid_arguments")
        query, opened = _text(args, "query"), _text(args, "open")
        first, last = _text(args, "date"), _text(args, "to")
        order, speaker = args.get("order"), args.get("speaker")
        page = speakable.page_number(args.get("page"), "conversation", "lines 9-16")
        if problem := self._problem(query, opened, first, last, order, speaker, page):
            msg = f"conversation: {problem}"
            raise ToolError(msg, code="invalid_arguments")
        now = self._clock()
        today = now.astimezone(self._zone).date()
        window = self._window(first, last, today) if first else None
        try:
            with closing(read_connection(self._store)) as conn:
                if opened:
                    return self._open(conn, opened, now)
                if window is not None and not (query or order or speaker):
                    return self._topics(conn, window, (now, today))
                return self._lines(
                    conn, query, window, str(speaker or ""), page, (now, today),
                    newest=order != "oldest",
                )
        except DailyError as exc:
            msg = f"The conversation history cannot be read just now ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc
        except sqlite3.Error as exc:
            msg = f"The conversation history could not be read ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc

    @staticmethod
    def _problem(  # noqa: PLR0913 - the tool's arguments, each checked against the others
        query: str, opened: str, first: str, last: str, order: object, speaker: object, page: int,
    ) -> str | None:
        """Why the arguments do not fit together and what to pass instead; None when they do."""
        rules = (
            (order not in (None, "newest", "oldest"), "order is newest or oldest"),
            (speaker not in (None, "Allen", "Jarvis"), "speaker is Allen or Jarvis"),
            (
                bool(opened and (query or first or last or order or speaker or page > 1)),
                "pass open=<id> alone, or query=... / date=..., not both",
            ),
            (
                bool(last and not first),
                "to is the last day of a span that date starts; pass date=... as well",
            ),
            (
                not (opened or query or first or order or speaker),
                "pass query=... (words), date=... (a day, to= its last day), order=oldest "
                "(with speaker) or open=<id> (an id from an earlier result)",
            ),
            (
                page > 1 and not (query or order or speaker),
                "page continues a list of lines (query=..., or date with order or speaker)",
            ),
        )
        return next((message for broken, message in rules if broken), None)

    @staticmethod
    def _window(first: str, last: str, today: date) -> tuple[date, date]:
        start = speakable.parse_day(first, today, "conversation")
        end = speakable.parse_day(last, today, "conversation") if last else start
        if end < start:
            msg = f"conversation: to={last} is before date={first}; to is the span's last day"
            raise ToolError(msg, code="invalid_arguments")
        return start, end

    def _rows(
        self, conn: sqlite3.Connection, window: tuple[date, date] | None, speaker: str,
        *, newest: bool,
    ) -> list[tuple[str, str, str, str]]:
        clauses: list[str] = []
        params: list[str] = []
        if window is not None:
            begin = datetime.combine(window[0], time.min, self._zone)
            end = datetime.combine(window[1] + timedelta(days=1), time.min, self._zone)
            clauses.append("datetime(ts) >= datetime(?) AND datetime(ts) < datetime(?)")
            params += [begin.isoformat(), end.isoformat()]
        if speaker == "Allen":
            clauses.append("source = 'allen'")
        elif speaker == "Jarvis":
            clauses.append("source IN ('jarvis', 'jarvis_live')")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        direction = "DESC" if newest else "ASC"
        sql = f"SELECT id, ts, source, text FROM records {where} ORDER BY rowid {direction}"  # noqa: S608 - fixed clauses, bound values
        return [(str(a), str(b), str(c), str(d)) for a, b, c, d in conn.execute(sql, params)]

    def _lines(  # noqa: PLR0913 - what the call asked for, in the order it is applied
        self, conn: sqlite3.Connection, query: str, window: tuple[date, date] | None,
        speaker: str, page: int, clock: tuple[datetime, date], *, newest: bool,
    ) -> dict[str, Any]:
        """Page ``page`` of ``_PAGE`` lines: those holding any word, best first, else in order."""
        now, today = clock
        rows = self._rows(conn, window, speaker, newest=newest)
        words = list(dict.fromkeys(query.casefold().split()))
        absent: list[str] = []
        if words:
            # Rare words weigh more, so a digit shared by hundreds of lines cannot bury the line
            # that holds the rare words; the stable sort keeps newest (or oldest) among equals.
            scores, absent = rank_by_words([row[3] for row in rows], words)
            ranked = sorted(
                ((score, row) for score, row in zip(scores, rows, strict=True) if score),
                key=lambda pair: -pair[0],
            )
            rows = [row for _, row in ranked]
        total, begin = len(rows), (page - 1) * _PAGE
        where = _where(window, today)
        if not rows:
            summary = (
                f'No line holds "{query}"{where}. Try other words, in the language it was said '
                "in (Chinese or English), or drop date to search everything."
                if words else f"Nothing was said{where}."
            )
            return {"summary": summary, "moments": [], "more": False}
        if begin >= total:
            last = (total - 1) // _PAGE + 1
            msg = f"There is no page {page} of these {total} lines; they end at page {last}."
            return {"summary": msg, "moments": [], "more": False}
        shown = rows[begin : begin + _PAGE]
        moments = [
            {
                "id": rid,
                "when": speakable.when(datetime.fromisoformat(ts), now, self._zone),
                "who": _who(source),
                "excerpt": _excerpt(text, words),
            }
            for rid, ts, source, text in shown
        ]
        end = begin + len(shown)
        what = f'{total} line{"" if total == 1 else "s"} hold "{query}"' if words else (
            f'{total} line{"" if total == 1 else "s"} said'
        )
        side = "oldest" if not newest else "newest"
        ordering = f"best match first, {side} among equals" if len(words) > 1 else f"{side} first"
        summary = f"{what}{where}; {begin + 1}-{end} shown, {ordering}."
        if absent and len(absent) < len(words):
            summary += f" No line holds: {', '.join(absent)}."
        return {"summary": summary, "moments": moments, "more": end < total}

    def _topics(
        self, conn: sqlite3.Connection, window: tuple[date, date], clock: tuple[datetime, date],
    ) -> dict[str, Any]:
        """A window's days as topics in time order: the saved summary, else his own lines."""
        today = clock[1]
        first, last = window
        if (last - first).days + 1 > _TOPIC_DAYS:
            msg = (
                f"conversation: topics cover at most {_TOPIC_DAYS} days; for a longer span pass "
                "query=... or order=oldest / order=newest with speaker for the lines themselves"
            )
            raise ToolError(msg, code="invalid_arguments")
        by_day: dict[date, list[tuple[str, str, str, str]]] = {}
        for row in self._rows(conn, window, "", newest=False):
            local = datetime.fromisoformat(row[1]).astimezone(self._zone).date()
            by_day.setdefault(local, []).append(row)
        try:
            saved = dict(latest_day_summaries(conn, first.isoformat(), last.isoformat()))
        except sqlite3.Error:
            saved = {}
        where = _where(window, today)
        if not by_day:
            return {"summary": f"Nothing was said{where}.", "days": []}
        for items, chars in _TOPIC_STEPS:
            days = [
                self._day(day, lines, saved.get(day.isoformat()), items, chars, today)
                for day, lines in sorted(by_day.items())
            ]
            answer = self._topic_answer(days, by_day, window, today)
            if len(encoded(answer)) <= _BUDGET:
                break
        return answer

    def _day(  # noqa: PLR0913 - the day, its lines, its saved summary and the two caps
        self, day: date, lines: Sequence[tuple[str, str, str, str]], summary: str | None,
        items: int, chars: int, today: date,
    ) -> dict[str, Any]:
        topics = [speakable.cut(t, chars) for t in _bullets(summary or "", "Topics")]
        entry: dict[str, Any] = {"day": speakable.day_name(day, today), "lines": len(lines)}
        if topics:
            entry["topics"] = topics[:items]
            return entry
        # No saved summary (today, or a day too short to need one): his own lines, spread out.
        mine = [row for row in lines if row[2] == _USER] or list(lines)
        picked = [mine[(i * len(mine)) // items] for i in range(min(items, len(mine)))]
        entry["topics"] = [
            f"{speakable.clock(datetime.fromisoformat(ts).astimezone(self._zone))} "
            f"{speakable.cut(text, chars)}"
            for _, ts, _, text in picked
        ]
        entry["note"] = "no written summary of this day; these are his own lines"
        return entry

    @staticmethod
    def _topic_answer(
        days: list[dict[str, Any]], by_day: Mapping[date, Sequence[object]],
        window: tuple[date, date], today: date,
    ) -> dict[str, Any]:
        lines = sum(len(v) for v in by_day.values())
        quiet = (window[1] - window[0]).days + 1 - len(by_day)
        summary = (
            f"{len(days)} day{'' if len(days) == 1 else 's'} of conversation"
            f"{_where(window, today)}, {lines} lines in all"
            + (f"; {quiet} quiet day{'' if quiet == 1 else 's'} left out" if quiet else "")
            + "."
        )
        return {"summary": summary, "days": days}

    def _open(self, conn: sqlite3.Connection, record_id: str, now: datetime) -> dict[str, Any]:
        """The line ``record_id`` and two lines either side: whole, up to what fits 4000."""
        row = conn.execute(
            "SELECT rowid, id, ts, source, text FROM records WHERE id = ?", (record_id,),
        ).fetchone()
        if row is None:
            msg = (
                f"No line with id {record_id}; find one with conversation(query=...) or "
                "conversation(date=...) first"
            )
            raise ToolError(msg, code="not_found")
        mark = row[0]
        before = conn.execute(
            "SELECT id, ts, source, text FROM records WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
            (mark, _AROUND),
        ).fetchall()[::-1]
        after = conn.execute(
            "SELECT id, ts, source, text FROM records WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (mark, _AROUND),
        ).fetchall()

        def turn(one: Sequence[Any], limit: int) -> dict[str, Any]:
            rid, at, source, words = one
            shown = words.strip()
            if len(shown) > limit:
                shown = f"{shown[:limit].rstrip()}… ({len(shown) - limit} more characters)"
            when = speakable.when(datetime.fromisoformat(at), now, self._zone)
            return {"id": rid, "when": when, "who": _who(source), "text": shown}

        near = [turn(one, _NEAR_CHARS) for one in (*before, *after)]
        cap = _FOCUS_CHARS
        while True:
            here = {**turn(row[1:], cap), "here": True}
            answer = {
                "summary": (
                    f"{here['who']}, {here['when']}: this line with {len(before)} before and "
                    f"{len(after)} after."
                ),
                "turns": [*near[: len(before)], here, *near[len(before) :]],
            }
            if len(encoded(answer)) <= _BUDGET or cap <= _NEAR_CHARS:
                return answer
            cap = max(_NEAR_CHARS, cap - 400)


def build_conversation_tool(
    store: Path | None, zone: tzinfo, clock: Callable[[], datetime] = speakable.now,
) -> tuple[Tool, ...]:
    """``conversation`` over the memory store's records and day summaries; none without a store."""
    if store is None:
        return ()
    return (
        Tool(
            name="conversation",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Conversation(store, zone, clock).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            supersedes={
                "search_records": "conversation(query=...)",
                "recall": "conversation(date=...)",
                "read_records": "conversation(open=<id>)",
            },
        ),
    )


__all__ = ["build_conversation_tool"]
