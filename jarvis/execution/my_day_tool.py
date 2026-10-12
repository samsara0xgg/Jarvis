"""ADR 0243: what he did and is doing is one read-only tool: now, a day, a span, one project."""

from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Tool, ToolError
from jarvis.shared import CallerPrincipal
from jarvis.state.daily_contract import DailyError, encoded
from jarvis.state.daily_report import summary_section
from jarvis.state.daily_store import saved_briefing
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.ledger import (
    CALL_FLOOR_S,
    TOP_FLOOR_S,
    DayFigures,
    Figures,
    LedgerSources,
    window_figures,
)
from jarvis.state.work_state import current_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from datetime import date

_DESCRIPTION: Final = (
    "Use for what Allen did or is doing: now, a day, a span of days or one project; what was "
    "said is conversation, mail is mail. No arguments: today so far and what he is doing now "
    "(the latest work analysis, with its age). date: a past day's saved report, else computer "
    "time, top projects and apps, commits, calls, places. to: a span up to 31 days, per day and "
    "in total. about: only one project or app (Jarvis, WeChat, co-op), by day and by window. "
    "Says which parts could not be read when his computer is not connected."
)
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": "A day: 2026-10-01, today or yesterday; omit for today so far.",
        },
        "to": {
            "type": "string",
            "description": "With date: the last day of a span of at most 31 days, included.",
        },
        "about": {
            "type": "string",
            "description": "One project, app or window title word, e.g. Jarvis, WeChat, co-op.",
        },
        "page": {
            "type": "integer",
            "description": "With about: 2 gives items 9-16 of the list, 3 gives 17-24.",
        },
    },
    "additionalProperties": False,
}
_SPAN_DAYS: Final = 31
_PAGE: Final = 8
_BUDGET: Final = 4000
_REPORT_CHARS: Final = 2400
_SHOWN: Final = 5
_STALE: Final = timedelta(hours=3)
_WEEK: Final = 7
_GONE: Final = {
    "terminal": "apps, projects and calls (his computer's terminal is not connected)",
    "timesink": "apps, projects and calls (TimeSink is not set up on this machine)",
}


def _ranked(totals: Mapping[str, float], n: int, floor: float, label: str = "") -> list[str]:
    """``Jarvis 3h 40m`` for the names with at least ``floor`` seconds, longest first."""
    ordered = sorted(totals.items(), key=lambda item: -item[1])
    return [
        f"{name}{label} {speakable.duration(seconds)}"
        for name, seconds in ordered if seconds >= floor
    ][:n]


def _sum(parts: Iterable[Mapping[str, float]]) -> dict[str, float]:
    total: Counter[str] = Counter()
    for part in parts:
        total.update(part)
    return dict(total)


def _fields(days: Sequence[DayFigures]) -> dict[str, Any]:
    """The lists a figure carries, each only when it has something."""
    active = sum(d.active for d in days)
    commits: Counter[str] = Counter()
    for d in days:
        commits.update(d.commits)
    found = {
        "active": speakable.duration(active) if active else "",
        "projects": _ranked(_sum(d.projects for d in days), _SHOWN, TOP_FLOOR_S),
        "apps": _ranked(_sum(d.apps for d in days), _SHOWN, TOP_FLOOR_S),
        "calls": _ranked(_sum(d.calls for d in days), _SHOWN, CALL_FLOOR_S, " call"),
        "commits": [f"{repo} {n}" for repo, n in commits.most_common(_SHOWN)],
        "places": _ranked(_sum(dict(d.places) for d in days), _SHOWN, TOP_FLOOR_S),
    }
    return {k: v for k, v in found.items() if v}


def _headline(fields: Mapping[str, Any], commits: int) -> str:
    """The figures as one spoken sentence: time, what it went to, commits, calls, places."""
    if "active" not in fields:
        parts = ["no computer activity recorded"]
    else:
        parts = [f"{fields['active']} at the computer"]
        if lead := (fields.get("projects") or fields.get("apps") or [""])[0]:
            parts.append(f"mostly {lead}")
    if commits:
        parts.append(f"{commits} commit{'' if commits == 1 else 's'}")
    if "calls" in fields:
        parts.append(", ".join(fields["calls"]))
    if "places" in fields:
        parts.append(f"at {', '.join(fields['places'][:2])}")
    return "; ".join(parts)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _right(state: Mapping[str, Any]) -> str:
    """The sentence on what he is doing now, from :meth:`_MyDay._state`."""
    if "text" not in state:
        covered = "; ".join(state["covered"])
        seen = f" It covered: {covered}." if covered else ""
        return (
            f" The latest work analysis ({state['as_of']}) names nothing he is doing right "
            f"now.{seen}"
        )
    note = f" Note: {state['stale']}." if "stale" in state else ""
    return f" Right now (as of {state['as_of']}): {state['text']}.{note}"


def _gone(missing: Sequence[str]) -> str:
    """What could not be read, as a sentence for the end of a summary."""
    if not missing:
        return ""
    text = f" Could not read: {'; '.join(_GONE[m] for m in missing)}."
    if "terminal" in missing:
        text += " Commits and places are what the brain already holds."
    return text


class _MyDay:
    """The tool's handler: today and what he is doing, a day, a span, one project."""

    def __init__(self, src: LedgerSources, clock: Callable[[], datetime]) -> None:
        self._src = src
        self._clock = clock

    def run(self, args: Mapping[str, Any], _ctx: object) -> dict[str, Any]:
        """Dispatch on the arguments; a store that cannot be read is worded for the model."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = (
                f"my_day: no argument {unknown[0]!r}; the arguments are date, to, about and page"
            )
            raise ToolError(msg, code="invalid_arguments")
        first_text, last_text = _text(args, "date"), _text(args, "to")
        about = _text(args, "about")
        page = speakable.page_number(args.get("page"), "my_day", "items 9-16")
        if last_text and not first_text:
            msg = "my_day: to is the last day of a span that date starts; pass date=... as well"
            raise ToolError(msg, code="invalid_arguments")
        if page > 1 and not about:
            msg = "my_day: page continues the list that about=... gives; pass the same about"
            raise ToolError(msg, code="invalid_arguments")
        now = self._clock()
        today = now.astimezone(self._src.zone).date()
        first = speakable.parse_day(first_text, today, "my_day") if first_text else today
        last = speakable.parse_day(last_text, today, "my_day") if last_text else first
        if last < first:
            msg = f"my_day: to={last_text} is before date={first_text}; to is the span's last day"
            raise ToolError(msg, code="invalid_arguments")
        if (last - first).days + 1 > _SPAN_DAYS:
            msg = f"my_day: a span is at most {_SPAN_DAYS} days; ask for a longer stretch in parts"
            raise ToolError(msg, code="invalid_arguments")
        try:
            if about:
                answer = self._about(first, last, about, page, now, today)
            elif first != last:
                answer = self._span(first, last, now, today)
            elif first == today:
                answer = self._today(now, today)
            else:
                answer = self._day(first, now, today)
        except (sqlite3.Error, DailyError, OSError) as exc:
            msg = f"His day cannot be read just now ({exc}); tell him so."
            raise ToolError(msg, code="unavailable") from exc
        return _fit(answer)

    def _figures(
        self, first: date, last: date, now: datetime, about: str = "",
    ) -> Figures:
        return window_figures(self._src, first, last, now, about=about)

    def _state(self, now: datetime, today: date) -> dict[str, Any] | None:
        """What the latest work-state analysis says he is doing, and how old that is.

        ``now`` is the analysis's claim about the present, made only from screen data seen
        shortly before it; without one the analysis covered the day but names no current
        activity, and says so rather than passing a past activity off as the present.
        """
        with closing(open_runtime_event_log(self._src.event_log)) as conn:
            item = current_state(conn)
        if item is None:
            return None
        claim = item.get("now")
        claim = claim if isinstance(claim, dict) and claim.get("text") else None
        try:
            stamp = str((claim or {}).get("as_of") or item["analyzed_at"])
            seen = _aware(datetime.fromisoformat(stamp))
        except (KeyError, ValueError):
            return None
        local = seen.astimezone(self._src.zone)
        at = (
            speakable.clock(local) if local.date() == today
            else speakable.when(seen, now, self._src.zone)
        )
        found: dict[str, Any] = {"as_of": f"{speakable.age(seen, now)}, at {at}"}
        if claim is None:
            found["covered"] = [
                speakable.cut(str(one["text"]), 160)
                for one in (item.get("activities") or ())[:3] if one.get("text")
            ]
            return found
        found["text"] = speakable.cut(str(claim["text"]), 240).rstrip(".")
        found["basis"] = claim.get("basis", "inferred")
        if now - seen > _STALE:
            found["stale"] = "this analysis is old and may not describe the present"
        return found

    def _today(self, now: datetime, today: date) -> dict[str, Any]:
        figures = self._figures(today, today, now)
        day = figures.days[0]
        fields = _fields(figures.days)
        clock = speakable.clock(now.astimezone(self._src.zone))
        stamp = f"{today:%a} {clock}"
        head = _headline(fields, sum(day.commits.values()))
        state = self._state(now, today)
        if day.bounds:
            started = day.bounds[0]
            fields["since"] = speakable.clock(started) + (
                " the evening before" if started.date() < today else ""
            )
        if state is None:
            right = " No work-state analysis has been made yet, so what he is doing now is unknown."
        else:
            right = _right(state)
        answer: dict[str, Any] = {
            "summary": f"Today so far ({stamp}): {head}.{right}{_gone(figures.missing)}",
            **fields,
        }
        if state is not None:
            answer["now"] = state
        return _with_missing(answer, figures)

    def _day(self, day: date, now: datetime, today: date) -> dict[str, Any]:
        label = speakable.day_name(day, today)
        with closing(open_runtime_event_log(self._src.event_log)) as conn:
            report = saved_briefing(conn, day.isoformat())
        if report is not None:
            content = str(report.get("content") or "")
            text = summary_section(content) or content
            summary = speakable.cut(text, _REPORT_CHARS)
            return {
                "summary": f"{label}, from his saved daily report: {summary}",
                "source": "saved report",
                "note": "about=<project or app> breaks one thing down; the whole report is on "
                "the Dashboard's brief page",
            }
        figures = self._figures(day, day, now)
        fields = _fields(figures.days)
        start = figures.days[0].bounds
        if start:
            fields["started"] = speakable.clock(start[0]) + (
                " the evening before" if start[0].date() < day else ""
            )
            fields["stopped"] = speakable.clock(start[1]) + (
                " (after midnight)" if start[1].date() > day else ""
            )
        head = _headline(fields, sum(figures.days[0].commits.values()))
        answer = {
            "summary": f"{label}, no saved report yet: {head}.{_gone(figures.missing)}",
            **fields,
        }
        return _with_missing(answer, figures)

    def _span(self, first: date, last: date, now: datetime, today: date) -> dict[str, Any]:
        figures = self._figures(first, last, now)
        days = figures.days
        fields = _fields(days)
        worked = sum(1 for d in days if d.active)
        total = sum(d.active for d in days)
        label = speakable.span(first, last, today)
        head = _headline(fields, sum(sum(d.commits.values()) for d in days))
        if worked:
            head += (
                f"; {worked} of {len(days)} days with activity, "
                f"averaging {speakable.duration(total / worked)} on those"
            )
        answer: dict[str, Any] = {
            "summary": f"{label}, {len(days)} days: {head}.{_gone(figures.missing)}",
            **fields,
            "days": self._rows(days, today),
        }
        if len(days) > _WEEK:
            answer["weeks"] = [
                self._week(days[i : i + _WEEK], today) for i in range(0, len(days), _WEEK)
            ]
        return _with_missing(answer, figures)

    def _rows(self, days: Sequence[DayFigures], today: date) -> list[str]:
        """One row per day with something in it; a run of empty days is one row."""
        rows: list[str] = []
        quiet: list[DayFigures] = []

        def flush() -> None:
            if len(quiet) > 1:
                where = speakable.span(quiet[0].day, quiet[-1].day, today)
                rows.append(f"{where}: no computer activity ({len(quiet)} days)")
            elif quiet:
                rows.append(self._row(quiet[0], today))
            quiet.clear()

        for d in days:
            if d.active or d.commits:
                flush()
                rows.append(self._row(d, today))
            else:
                quiet.append(d)
        flush()
        return rows

    @staticmethod
    def _row(d: DayFigures, today: date) -> str:
        commits = sum(d.commits.values())
        parts = [speakable.duration(d.active)] if d.active else []
        parts += _ranked(d.projects, 1, TOP_FLOOR_S)
        if commits:
            parts.append(f"{commits} commit{'' if commits == 1 else 's'}")
        return f"{speakable.day_name(d.day, today)}: {', '.join(parts) or 'no computer activity'}"

    @staticmethod
    def _week(block: Sequence[DayFigures], today: date) -> str:
        a, b = block[0].day, block[-1].day
        name = f"{a:%b} {a.day}-{b.day}"
        if a.month != b.month:
            name = f"{a:%b} {a.day} - {b:%b} {b.day}"
        total = sum(d.active for d in block)
        so_far = " so far" if b >= today else ""
        figure = (
            f"{speakable.duration(total)}, {speakable.duration(total / len(block))} a day"
            if total else "no computer activity"
        )
        return f"{name} ({len(block)} days{so_far}): {figure}"

    def _about(  # noqa: PLR0913 - the window, the filter, the page and the two clocks
        self, first: date, last: date, about: str, page: int, now: datetime, today: date,
    ) -> dict[str, Any]:
        figures = self._figures(first, last, now, about)
        days = figures.days
        fields = _fields(days)
        label = speakable.span(first, last, today)
        commits = sum(sum(d.commits.values()) for d in days)
        if not fields:
            return self._nothing(about, (first, last), label, now, figures)
        head = _headline(fields, commits)
        answer: dict[str, Any] = {
            "summary": f'"{about}", {label}: {head}.{_gone(figures.missing)}', **fields,
        }
        if first != last:
            answer["days"] = [
                self._row(d, today) for d in days if d.active or d.commits
            ]
        inside = [f"{name} {speakable.duration(seconds)}" for name, seconds in figures.inside]
        shown = inside[(page - 1) * _PAGE : page * _PAGE]
        if page > 1 and not shown:
            last_page = max(1, (len(inside) - 1) // _PAGE + 1)
            msg = f'There is no page {page} of "{about}"; its list ends at page {last_page}.'
            return {"summary": msg, "inside": [], "more": False}
        if shown:
            answer["inside"] = shown
            answer["more"] = page * _PAGE < len(inside)
        return _with_missing(answer, figures)

    def _nothing(
        self, about: str, window: tuple[date, date], label: str, now: datetime, figures: Figures,
    ) -> dict[str, Any]:
        """No time, commit or call matched: say so and what the time went to, to retry with."""
        seen = _fields(self._figures(*window, now).days)
        names = [*(seen.get("projects") or []), *(seen.get("apps") or [])]
        hint = f" The time went to: {', '.join(names)}; try one of those." if names else ""
        return _with_missing({
            "summary": f'Nothing on "{about}" {label}.{hint}{_gone(figures.missing)}',
        }, figures)


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _with_missing(answer: dict[str, Any], figures: Figures) -> dict[str, Any]:
    if figures.missing:
        answer["missing"] = [_GONE[m] for m in figures.missing]
    return answer


def _fit(answer: dict[str, Any]) -> dict[str, Any]:
    """The answer within 4000 characters: the longest list loses its last rows, and says so."""
    dropped = 0
    while len(encoded(answer)) > _BUDGET:
        lists = [k for k, v in answer.items() if isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        answer[max(lists, key=lambda k: len(encoded(answer[k])))].pop()
        dropped += 1
    if dropped:
        answer["cut"] = f"{dropped} rows left out to fit; ask for a shorter span or use about"
    return answer


def build_my_day_tool(
    sources: LedgerSources, clock: Callable[[], datetime] = speakable.now,
) -> tuple[Tool, ...]:
    """``my_day`` over the ledger's sources (memory.db, the Event Log, TimeSink or its terminal)."""
    return (
        Tool(
            name="my_day",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=_MyDay(sources, clock).run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L0",
            read_only=True,
            supersedes={
                "get_briefing": "my_day(date=...)",
                "query_activity": "my_day(date=..., about=...)",
                "read_activity": "my_day(date=..., about=...)",
                "refresh_work_state": "my_day()",
            },
        ),
    )


__all__ = ["build_my_day_tool"]
