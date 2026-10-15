"""ADR 0259: what he handed over from other apps is one read-only tool that searches and opens."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state import imports, meaning
from jarvis.state.daily_contract import DailyError, encoded
from jarvis.state.daily_records import rank_by_words, read_connection
from jarvis.state.import_parsers import money
from jarvis.state.imports import mask_secrets

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import tzinfo
    from pathlib import Path

    from jarvis.state.embedding import Embedder

LOGGER = logging.getLogger(__name__)

_DESCRIPTION: Final = (
    "Use for what he handed over from other apps: his payments (WeChat Pay, Alipay, Bank of "
    "China) and his past conversations with Claude. Not for what he said to you (conversation) "
    "or what he did on his devices (my_day). query finds the closest entries by meaning and by "
    "words, in either language; date and to limit the span; source limits to one app. Returns up "
    "to 8 entries per page, each with its id, time (in the same time zone as the rest of your "
    "context), source and text, and for payments the "
    "count and the totals spent and received over every match, not just the page. open: an id "
    "from a result; for a Claude message, the messages around it."
)
_SOURCES: Final = ("claude", "wechat_pay", "alipay", "boc")
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Words separated by spaces, either language."},
        "source": {"type": "string", "enum": list(_SOURCES), "description": "Only that app."},
        "date": {
            "type": "string",
            "description": "A day (2026-10-01, today, yesterday); with to, the span's first day.",
        },
        "to": {"type": "string", "description": "With date: the last day of a span, included."},
        "page": {
            "type": "integer",
            "description": "Entries 9-16 are page 2, 17-24 page 3; omit for the first 8.",
        },
        "open": {"type": "string", "description": "An id from an earlier result."},
    },
    "additionalProperties": False,
}
_PAGE: Final = 8
_EXCERPT_CHARS: Final = 200
_BUDGET: Final = 4000
_FOCUS_CHARS: Final = 1800
_NEAR_CHARS: Final = 300
_AROUND: Final = 2
_MEANING_LIMIT: Final = 20
_MEANING_MARGIN: Final = 0.06
"""As in ``conversation``: the closest entries by meaning merged with the word hits."""


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def rank(rows: list[imports.Entry], words: list[str]) -> tuple[list[imports.Entry], list[str]]:
    """The entries holding any of ``words``, best first (newest among equals), and the absent words.

    The order by words; ``_Archive._find`` merges the closest entries by meaning into it.
    """
    scores, absent = rank_by_words([row.text for row in rows], words)
    best = sorted(((s, row) for s, row in zip(scores, rows, strict=True) if s), key=lambda p: -p[0])
    return [row for _, row in best], absent


def _totals(rows: list[imports.Entry]) -> dict[str, Any] | None:
    """Count and sums over every payment among ``rows``; only money that moved is summed."""
    pays = [row for row in rows if row.kind == "payment"]
    if not pays:
        return None
    moved = [row for row in pays if row.fields.get("counts")]
    spent = sum(row.fields["amount_fen"] for row in moved if row.fields["direction"] == "out")
    got = sum(row.fields["amount_fen"] for row in moved if row.fields["direction"] == "in")
    return {
        "payments": len(pays),
        "spent": money(spent),
        "received": money(got),
        "currency": "CNY",
        "note": (
            f"{len(pays) - len(moved)} refunded, closed or internal; not in the totals"
            if len(moved) < len(pays) else "all counted"
        ),
    }


def _masked_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The entry's fields with any secret-looking string masked (a second guard)."""
    masked: dict[str, Any] = json.loads(mask_secrets(json.dumps(fields, ensure_ascii=False)))
    return masked


class _Archive:
    """The tool's handler: entries for words, a source or a span; one entry opened."""

    def __init__(
        self, store: Path, zone: tzinfo, clock: Callable[[], datetime],
        embedder: Embedder | None,
    ) -> None:
        self._store = store
        self._zone = zone
        self._clock = clock
        self._embedder = embedder

    def run(self, args: Mapping[str, Any], _ctx: object) -> dict[str, Any]:
        """Dispatch on the arguments; a store that cannot be read is worded for the model."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = (
                f"archive: no argument {unknown[0]!r}; the arguments are query, source, date, "
                "to, page and open"
            )
            raise ToolError(msg, code="invalid_arguments")
        query, opened, source = _text(args, "query"), _text(args, "open"), _text(args, "source")
        first, last = _text(args, "date"), _text(args, "to")
        page = speakable.page_number(args.get("page"), "archive", "entries 9-16")
        if problem := self._problem(query, opened, source, first, last, page):
            msg = f"archive: {problem}"
            raise ToolError(msg, code="invalid_arguments")
        now = self._clock()
        today = now.astimezone(self._zone).date()
        window = self._window(first, last, today) if first else None
        try:
            with closing(read_connection(self._store)) as conn:
                if opened:
                    return self._open(conn, opened, now)
                return self._find(conn, query, source, window, page, (now, today))
        except DailyError as exc:
            msg = f"The archive cannot be read just now ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc
        except sqlite3.Error as exc:
            msg = f"The archive could not be read ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc

    @staticmethod
    def _problem(  # noqa: PLR0913 - the tool's arguments, each checked against the others
        query: str, opened: str, source: str, first: str, last: str, page: int,
    ) -> str | None:
        rules = (
            (source not in ("", *_SOURCES), f"source is one of {', '.join(_SOURCES)}"),
            (
                bool(opened and (query or source or first or last or page > 1)),
                "pass open=<id> alone, or query / source / date, not both",
            ),
            (bool(last and not first), "to is the last day of a span that date starts"),
            (
                not (opened or query or source or first),
                "pass query=... (words), source=..., date=... or open=<id> (an id from a result)",
            ),
        )
        return next((message for broken, message in rules if broken), None)

    @staticmethod
    def _window(first: str, last: str, today: date) -> tuple[date, date]:
        start = speakable.parse_day(first, today, "archive")
        end = speakable.parse_day(last, today, "archive") if last else start
        if end < start:
            msg = f"archive: to={last} is before date={first}; to is the span's last day"
            raise ToolError(msg, code="invalid_arguments")
        return start, end

    def _find(  # noqa: PLR0913 - what the call asked for, in the order it is applied
        self, conn: sqlite3.Connection, query: str, source: str,
        window: tuple[date, date] | None, page: int, clock: tuple[datetime, date],
    ) -> dict[str, Any]:
        now = clock[0]
        start = end = None
        if window is not None:
            start = int(datetime.combine(window[0], time.min, self._zone).timestamp() * 1000)
            stop = datetime.combine(window[1] + timedelta(days=1), time.min, self._zone)
            end = int(stop.timestamp() * 1000)
        rows = imports.entries(conn, source=source or None, start_ms=start, end_ms=end)
        words = list(dict.fromkeys(query.casefold().split()))
        absent: list[str] = []
        counted, near = rows, False
        if words:
            everything = rows
            rows, absent = rank(rows, words)
            counted = rows
            if close := self._by_meaning(conn, query, {row.id for row in everything}):
                # Merged, not chosen between: an exact hit stays first. Totals stay on the word
                # hits when there are any, since a near miss by meaning must not change a sum.
                by_id = {row.id: row for row in everything}
                rows = [by_id[eid] for eid in meaning.fuse([row.id for row in rows], close)]
                counted, near = (counted, False) if counted else (rows, True)
        total, begin = len(rows), (page - 1) * _PAGE
        what = f' for "{query}"' if words else ""
        if not rows:
            summary = f"No entry{what} was handed over for this."
            return {"summary": summary, "entries": [], "more": False}
        if begin >= total:
            last = (total - 1) // _PAGE + 1
            msg = f"There is no page {page} of these {total} entries; they end at page {last}."
            return {"summary": msg, "entries": [], "more": False}
        shown = rows[begin : begin + _PAGE]
        answer: dict[str, Any] = {
            "summary": (
                f"{total} entr{'y' if total == 1 else 'ies'}{what}; "
                f"{begin + 1}-{begin + len(shown)} shown, best match first."
                if words else
                f"{total} entr{'y' if total == 1 else 'ies'}; "
                f"{begin + 1}-{begin + len(shown)} shown, newest first."
            ),
            "entries": [
                {
                    "id": row.id,
                    "when": speakable.when(self._at(row), now, self._zone),
                    "source": row.source,
                    "text": speakable.cut(" ".join(mask_secrets(row.text).split()), _EXCERPT_CHARS),
                }
                for row in shown
            ],
            "more": begin + len(shown) < total,
        }
        if absent and len(absent) < len(words):
            answer["summary"] += f" No entry holds: {', '.join(absent)}."
        if (totals := _totals(counted)) is not None:
            if near:
                totals["note"] += (
                    "; no entry holds these words, so these are the closest by meaning"
                )
            answer["totals"] = totals
        return answer

    @staticmethod
    def _at(row: imports.Entry) -> datetime:
        return datetime.fromtimestamp(row.at_ms / 1000, tz=UTC)

    def _by_meaning(self, conn: sqlite3.Connection, query: str, ids: set[str]) -> list[str]:
        """Ids among ``ids`` closest to ``query`` by meaning; none when the model is absent."""
        if self._embedder is None or not self._embedder.available:
            return []
        try:
            return meaning.nearest(
                conn, self._store, self._embedder, query, meaning.IMPORTS,
                among=ids, limit=_MEANING_LIMIT, margin=_MEANING_MARGIN,
            )
        except Exception:  # a search never fails a turn for want of the model
            LOGGER.exception("archive: search by meaning failed; words only")
            return []

    def _open(self, conn: sqlite3.Connection, entry_id: str, now: datetime) -> dict[str, Any]:
        """One entry whole; a Claude message with two messages either side, up to what fits."""
        row = imports.entry(conn, entry_id)
        if row is None:
            msg = f"No entry with id {entry_id}; find one with archive(query=...) first"
            raise ToolError(msg, code="not_found")

        def turn(one: imports.Entry, limit: int) -> dict[str, Any]:
            shown = mask_secrets(one.text).strip()
            if len(shown) > limit:
                shown = f"{shown[:limit].rstrip()}… ({len(shown) - limit} more characters)"
            return {
                "id": one.id,
                "when": speakable.when(self._at(one), now, self._zone),
                "who": one.fields.get("sender", one.kind),
                "text": shown,
            }

        if row.kind != "message":
            return {
                "summary": f"{row.source} {row.kind}, "
                f"{speakable.when(self._at(row), now, self._zone)}.",
                "entry": {**turn(row, _FOCUS_CHARS), "fields": _masked_fields(row.fields)},
            }
        before, after = imports.around(conn, row, _AROUND)
        near = [turn(one, _NEAR_CHARS) for one in (*before, *after)]
        cap = _FOCUS_CHARS
        while True:
            here = {**turn(row, cap), "here": True}
            answer = {
                "summary": (
                    f"{row.fields.get('conversation_name') or 'Claude conversation'}: this message "
                    f"with {len(before)} before and {len(after)} after."
                ),
                "turns": [*near[: len(before)], here, *near[len(before) :]],
            }
            if len(encoded(answer)) <= _BUDGET or cap <= _NEAR_CHARS:
                return answer
            cap = max(_NEAR_CHARS, cap - 400)


def build_archive_tool(
    store: Path | None, zone: tzinfo, clock: Callable[[], datetime] = speakable.now,
    *, embedder: Embedder | None = None,
) -> tuple[Tool, ...]:
    """``archive`` over the memory store's import entries; none without a store."""
    if store is None:
        return ()
    return (
        Tool(
            name="archive",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_Archive(store, zone, clock, embedder).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
        ),
    )


__all__ = ["build_archive_tool"]
