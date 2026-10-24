"""ADR 0367: the job hunt is one list, and ``jobs`` is her one tool for it.

It reads the applications the Jobs page shows (his edits and hidden rows applied, a hiring
platform's mail filed under its employer) and adds or changes them the way the page does. Every
time is his local time; a Pacific, Mountain or Central time a mail wrote both ways says both
(ADR 0379), and the answer says when job mail was last read. A row number is the place in the
last list or find, kept in memory; an application's id always works.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

from jarvis.execution import speakable
from jarvis.execution.tools import Receipted, Tool, ToolError
from jarvis.shared import CallerPrincipal, zone_label
from jarvis.shared.receipts import Covered, Receipt
from jarvis.state.job_ledger import APPLICATION_STATUSES
from jarvis.state.ledger import LedgerSources, same_company, submitted_count

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from datetime import tzinfo

    from jarvis.execution.tools import ToolContext

_DESCRIPTION: Final = (
    "Use for his job applications, interviews, offers and rejections, how the hunt is going, and"
    " when he says he applied somewhere or an application changed. Not for what a job mail says:"
    " use mail. Returns his applications as the Jobs page shows them, in his local time, with"
    " the count by status and when job mail was last read; add and update write that page's own"
    " rows."
)
_ACTIONS: Final = ("list", "find", "add", "update")
_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(_ACTIONS),
            "description": (
                "list (default): his applications. find: one company's application, whole."
                " add: an application he says he sent. update: change one."
            ),
        },
        "company": {
            "type": "string",
            "description": "find, add: the company, in his words.",
        },
        "role": {
            "type": "string",
            "description": "find: narrows the match to this role. add: the role, if he said it.",
        },
        "application": {
            "type": "string",
            "description": (
                "update: which one: a row number from the last list or find, an id, or a"
                " company in his words."
            ),
        },
        "status": {
            "type": "string",
            "enum": list(APPLICATION_STATUSES),
            "description": "list: only this status. update: the status he sets.",
        },
        "when": {
            "type": "string",
            "description": (
                "list: only applications he sent on this day or stretch, in his words. add,"
                " update: the day he applied, in his words; add without it is today."
            ),
        },
        "note": {
            "type": "string",
            "description": "add, update: his note on the application.",
        },
        "hidden": {
            "type": "boolean",
            "description": "update: true takes it off his list, false puts it back.",
        },
    },
    "additionalProperties": False,
}
_ARGS: Final = {
    "list": ("status", "when"),
    "find": ("company", "role"),
    "add": ("company", "role", "when", "note"),
    "update": ("application", "status", "when", "note", "hidden"),
}
_BUDGET: Final = 4000
_ROOM: Final = 600  # what the summary and the fields around the rows may take of the budget
_SUMMARY: Final = 400
_NAME_CHARS: Final = 60
_NOTE_CHARS: Final = 200
_SUBJECT_CHARS: Final = 80
_MAILS_SHOWN: Final = 5
_NAMED: Final = 3
_ROW_NUMBER: Final = re.compile(r"\d{1,4}")
_STEP_WORDS: Final = {
    "applied": "applied",
    "interview_invite": "invited to an interview",
    "interview": "interview",
    "offer": "offer",
    "rejection": "rejected",
}
_STATUS_ORDER: Final = ("offer", "interviewing", "applied", "no_reply", "rejected")
_SOURCES: Final = ("applications on the Jobs page",)
_HOW_MORE: Final = "narrow with status or when, or find a company"


def _text(args: Mapping[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _reads(args: Mapping[str, Any]) -> bool:
    """Whether a ``jobs`` call only reads: list and find do, add and update write (ADR 0319)."""
    return (_text(args, "action") or "list") in ("list", "find")


def _plain(status: str) -> str:
    return status.replace("_", " ")


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _clean(text: str) -> str:
    """One speakable line: no braces (a summary is never read as a template)."""
    return " ".join(text.replace("{", "(").replace("}", ")").split())


def _fit(core: str, notes: str) -> str:
    """The summary: ``core`` cut to leave room for ``notes``, which are always whole."""
    return _clean(f"{speakable.cut(core, _SUMMARY - len(notes) - 1)} {notes}".strip())


def _moment(text: object, zone: tzinfo) -> datetime | None:
    """A stored instant in his zone; a time with no offset is his own."""
    try:
        at = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return at.replace(tzinfo=zone) if at.tzinfo is None else at.astimezone(zone)


def _day_of(text: object, zone: tzinfo) -> date | None:
    """The local day of a stored date (2026-10-16) or instant."""
    if isinstance(text, str) and len(text) == len("2026-10-16"):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    at = _moment(text, zone)
    return None if at is None else at.date()


def _said_day(text: object, today: date, zone: tzinfo) -> str:
    day = _day_of(text, zone)
    return "" if day is None else speakable.day_name(day, today)


def _on_site(app: Mapping[str, Any]) -> str:
    """His time on the company's site over the page's window, or ``unknown`` (never 0)."""
    site = app.get("site_s")
    return "unknown" if site is None else speakable.duration(float(site))


def _interview(app: Mapping[str, Any], today: date, zone: tzinfo) -> str:
    """The next interview as he is told it: both readings when the mail's label reads two ways."""
    at = _moment(app.get("next_event_at"), zone)
    if at is None or app["status"] == "rejected":
        return ""
    later = _moment(app.get("next_event_later_at"), zone)
    said = next(
        (m["event_text"] for m in app["mails"]
         if m["event_at"] == app["next_event_at"] and m["event_text"]),
        "",
    )
    when = zone_label.when_text(at, later, zone, sentence=said, language="en")
    return f"{speakable.day_name(at.date(), today)}, {when}"


def _row(number: int, app: Mapping[str, Any], today: date, zone: tzinfo) -> dict[str, Any]:
    """One application as a list shows it: the fewest fields the next step needs."""
    row: dict[str, Any] = {
        "n": number,
        "id": app["id"],
        "company": speakable.cut(app["company"], _NAME_CHARS),
    }
    if app["role"]:
        row["role"] = speakable.cut(app["role"], _NAME_CHARS)
    row["status"] = app["status"]
    row["applied"] = _said_day(app["applied_at"], today, zone)
    row["last_activity"] = _said_day(app["last_at"], today, zone)
    if interview := _interview(app, today, zone):
        row["interview"] = interview
    row["source"] = "mail" if app["source"] == "mail" else "he added"
    row["on_site_14d"] = _on_site(app)
    return row


def _timeline(app: Mapping[str, Any], today: date, zone: tzinfo) -> list[str]:
    steps = []
    for step in app["timeline"]:
        day = _said_day(step["at"], today, zone)
        word = _STEP_WORDS.get(step["kind"], step["kind"])
        ahead = " (ahead)" if step.get("future") else ""
        steps.append(f"{day}: {word}{ahead}" if day else word)
    return steps


def _held(interview: Mapping[str, Any]) -> str:
    """How the interview is held, as the mail says: online (with its platform) or in person."""
    if interview.get("platform"):
        return f"online, {interview['platform']}"
    return {"online": "online", "onsite": "in person"}.get(str(interview.get("mode")), "")


def _whole(number: int, app: Mapping[str, Any], today: date, zone: tzinfo) -> dict[str, Any]:
    """A found application whole: its timeline, interview, note and mails (newest first)."""
    row = _row(number, app, today, zone)
    row["timeline"] = _timeline(app, today, zone)
    interview = app.get("interview") or {}
    detail = {
        key: value
        for key, value in (
            ("held", _held(interview)),
            ("where", speakable.cut(interview.get("location") or "", _NAME_CHARS)),
            ("with", list(interview.get("interviewers") or [])),
        )
        if value
    }
    if detail:
        row["interview_detail"] = detail
    if app["note"]:
        row["note"] = speakable.cut(app["note"], _NOTE_CHARS)
    mails = app["mails"]
    row["mails"] = [
        {
            "on": _said_day(m["received_at"], today, zone),
            "kind": m["kind"],
            "subject": speakable.cut(m["subject"] or "", _SUBJECT_CHARS),
        }
        for m in mails[:_MAILS_SHOWN]
    ]
    if len(mails) > _MAILS_SHOWN:
        row["more_mails"] = len(mails) - _MAILS_SHOWN
    return row


def _plain_role(role: str) -> str:
    return " ".join(role.casefold().split())


def _same_application(app: Mapping[str, Any], company: str, role: str) -> bool:
    """The same company, and the same role or no role on one side (ADR 0367)."""
    mine, other = _plain_role(app["role"]), _plain_role(role)
    return same_company(app["company"], company) and (not mine or not other or mine == other)


def _named(app: Mapping[str, Any]) -> str:
    return f"{app['company']} ({app['role']})" if app["role"] else str(app["company"])


def _counts(apps: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(
        f"{n} {_plain(status)}"
        for status in _STATUS_ORDER
        if (n := sum(1 for a in apps if a["status"] == status))
    )


def _age_note(mail: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    """When job mail was last read, or since when reading has failed (ADR 0367)."""
    read, failing = _moment(mail.get("read"), zone), _moment(mail.get("failing_since"), zone)

    def at(moment: datetime | None) -> str:
        return "" if moment is None else speakable.when(moment, now, zone)

    if read is None:
        note = f"Job mail has not been read since the start ({at(_moment(mail['started'], zone))})"
    else:
        note = f"Job mail was last read {at(read)}"
    if failing is not None:
        note += f"; reading has failed since {at(failing)}"
    return note + "."


def _site_note(view: Mapping[str, Any]) -> str:
    """Says that his time on job sites is unknown, when no TimeSink answered."""
    if all(a.get("site_s") is not None for a in view["applications"]):
        return ""
    why = "the Mac did not answer" if view.get("time_note") else "it could not be read"
    return f"His time on job sites is unknown: {why}."


def _notes(view: Mapping[str, Any], now: datetime, zone: tzinfo) -> str:
    """How fresh the rows are, and whether his time on job sites is known."""
    return " ".join(
        part for part in (_age_note(view["mail"], now, zone), _site_note(view)) if part
    )


def _size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


def _whole_rows(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that fit the result's budget, cut whole from the end."""
    shown: list[dict[str, Any]] = []
    used = _ROOM
    for row in rows:
        used += _size(row) + 2
        if used > _BUDGET:
            break
        shown.append(row)
    return shown


@dataclass(frozen=True)
class JobsSource:
    """What the runtime lends the tool: the page's list, and the page's own add and edit."""

    view: Callable[[], Mapping[str, Any]]
    """``JobMail.jobs_view``: the applications, the hidden ones and how fresh the mail is."""
    add: Callable[[Mapping[str, Any]], str]
    """``JobMail.add_application``: his own row; returns its id, a bad value is a ValueError."""
    edit: Callable[[str, Mapping[str, Any]], None]
    """``JobMail.edit_application``: his edit; an unknown id is a LookupError."""


class _Jobs:
    """The tool's handler: the list the page shows, and the page's add and edit."""

    def __init__(self, source: JobsSource, sources: LedgerSources) -> None:
        self._source = source
        self._sources = sources
        self._held: dict[int, str] = {}
        self._lock = threading.Lock()

    def run(self, args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        """Do what ``action`` says (a list when it is left out) and answer in one sentence."""
        action = self._checked(args)
        now, zone = ctx.clock.now(), ctx.clock.zone
        view = self._source.view()
        if action == "list":
            return self._list(args, view, now, zone)
        if action == "find":
            return self._find(args, view, now, zone)
        if action == "add":
            return self._add(args, view, now, zone)
        return self._update(args, view, now, zone)

    @staticmethod
    def _checked(args: Mapping[str, Any]) -> str:
        """The action asked for, once every argument is one the tool has and the action takes."""
        if unknown := sorted(set(args) - set(_SCHEMA["properties"])):
            msg = f"jobs: no argument {unknown[0]!r}; use {', '.join(_SCHEMA['properties'])}"
            raise ToolError(msg, code="invalid_arguments")
        action = _text(args, "action") or "list"
        if action not in _ACTIONS:
            msg = f"jobs: action must be one of {', '.join(_ACTIONS)}, not {action!r}"
            raise ToolError(msg, code="invalid_arguments")
        for name, value in args.items():
            given = value is not None and value != "" and value is not False
            if name != "action" and given and name not in _ARGS[action]:
                goes = " or ".join(a for a, names in _ARGS.items() if name in names)
                msg = (
                    f"jobs: {name} does not go with action={action}; it goes with {goes}."
                    " Leave it out or change action"
                )
                raise ToolError(msg, code="invalid_arguments")
        if args.get("hidden") is not None and not isinstance(args["hidden"], bool):
            msg = f"jobs: hidden is true or false, not {args['hidden']!r}"
            raise ToolError(msg, code="invalid_arguments")
        return action

    # --- reads ------------------------------------------------------------------------------

    def _list(
        self, args: Mapping[str, Any], view: Mapping[str, Any], now: datetime, zone: tzinfo,
    ) -> Receipted:
        apps, today = list(view["applications"]), now.date()
        status, words = _text(args, "status"), _text(args, "when")
        if status and status not in APPLICATION_STATUSES:
            msg = f"jobs: status is one of {', '.join(APPLICATION_STATUSES)}, not {status!r}"
            raise ToolError(msg, code="invalid_arguments")
        rows = [a for a in apps if not status or a["status"] == status]
        label = ""
        if words:
            first, last = speakable.resolve_when(words, today, back=True, tool="jobs")
            label = speakable.span(first, last, today)
            rows = [a for a in rows if (d := _day_of(a["applied_at"], zone)) and first <= d <= last]
        shown = _whole_rows([_row(n, a, today, zone) for n, a in enumerate(rows, 1)])
        with self._lock:
            self._held = {row["n"]: row["id"] for row in shown}
        what = _plural(len(rows), f"{_plain(status)} application" if status else "application")
        what += f" sent {label}" if label else ""
        what += f" (of {len(apps)})" if len(rows) != len(apps) else ""
        counts = f": {_counts(rows)}" if rows and not status else ""
        cut = f" {len(rows) - len(shown)} not shown; {_HOW_MORE}." if len(shown) < len(rows) else ""
        submitted = submitted_count(self._sources, now)
        core = f"{what}{counts}. At least {submitted} submitted in all.{cut}"
        answer = {"summary": _fit(core, _notes(view, now, zone)), "applications": shown}
        return self._covered(answer, status or label or "all", len(shown), len(rows) - len(shown))

    def _find(
        self, args: Mapping[str, Any], view: Mapping[str, Any], now: datetime, zone: tzinfo,
    ) -> Receipted:
        company, role = _text(args, "company"), _text(args, "role")
        if not company:
            msg = "jobs: find needs company, in his words; jobs() lists every application"
            raise ToolError(msg, code="invalid_arguments")
        found = [
            a for a in view["applications"]
            if same_company(a["company"], company)
            and (not role or _plain_role(role) in _plain_role(a["role"]))
        ]
        today, notes = now.date(), _notes(view, now, zone)
        answer: dict[str, Any]
        if not found:
            answer = {
                "summary": _fit(f"No application on his list matches {company}.", notes),
                "applications": [],
            }
            return self._covered(answer, company, 0, 0)
        shown = _whole_rows([_whole(n, a, today, zone) for n, a in enumerate(found, 1)])
        with self._lock:
            self._held = {row["n"]: row["id"] for row in shown}
        if len(found) == 1:
            app = found[0]
            core = f"{_named(app)}: {_plain(app['status'])}, applied {shown[0]['applied']}."
            if "interview" in shown[0]:
                core += f" Interview {shown[0]['interview']}."
        else:
            names = ", ".join(f"{r['n']} {_named(found[r['n'] - 1])}" for r in shown)
            core = f"{len(found)} applications match {company}: {names}."
            core += f" {len(found) - len(shown)} not shown." if len(shown) < len(found) else ""
        answer = {"summary": _fit(core, notes), "applications": shown}
        return self._covered(answer, company, len(shown), len(found) - len(shown))

    @staticmethod
    def _covered(answer: dict[str, Any], window: str, shown: int, more: int) -> Receipted:
        """A read with its receipt: what it looked at, how many rows it showed and left out."""
        line = f"read his job applications ({window}): {shown} shown"
        if more:
            line += f", {more} more"
        return Receipted(answer, Receipt(
            "done", reads_only=True, line=line,
            covered=Covered(_SOURCES, window, shown, more, _HOW_MORE if more else ""),
        ))

    # --- writes -----------------------------------------------------------------------------

    def _add(
        self, args: Mapping[str, Any], view: Mapping[str, Any], now: datetime, zone: tzinfo,
    ) -> Receipted:
        company, role, note = _text(args, "company"), _text(args, "role"), _text(args, "note")
        if not company:
            msg = "jobs: add needs company, in his words"
            raise ToolError(msg, code="invalid_arguments")
        today = now.date()
        applied = self._day(words, today) if (words := _text(args, "when")) else today
        apps = list(view["applications"])
        same = next((a for a in apps if _same_application(a, company, role)), None)
        if same is not None:
            with self._lock:  # the list it names is the whole one, so its row number holds
                self._held = {n: a["id"] for n, a in enumerate(apps, 1)}
            number = apps.index(same) + 1
            applied_on = _said_day(same["applied_at"], today, zone)
            answer = {
                "summary": _clean(
                    f"{_named(same)} is already on his list, row {number}:"
                    f" {_plain(same['status'])}, applied {applied_on}. Nothing added.",
                ),
                "existing": _row(number, same, today, zone),
            }
            return Receipted(answer, Receipt(
                "done", reads_only=False, object=f"job:{same['id']}",
                line=f"nothing added: {_named(same)} is already on his list",
            ))
        fields = {
            "company": company, "role": role, "applied_at": applied.isoformat(), "source": "said",
            **({"note": note} if note else {}),
        }
        try:
            new_id = self._source.add(fields)
        except ValueError as exc:
            msg = f"jobs: {exc}"
            raise ToolError(msg, code="invalid_arguments") from exc
        what = f"{company} ({role})" if role else company
        day = speakable.day_name(applied, today)
        answer = {
            "summary": _clean(f"Added {what}, applied {day}."),
            "added": {"id": new_id, "company": company, "role": role, "applied": day},
            "undo": {"action": "update", "application": new_id, "hidden": True},
        }
        return Receipted(answer, Receipt(
            "done", reads_only=False, object=f"job:{new_id}",
            line=f"added {what} to his job list, applied {day}",
        ))

    @staticmethod
    def _day(words: str, today: date) -> date:
        """The one day his words name for when he applied: not a stretch, not ahead."""
        first, last = speakable.resolve_when(words, today, back=True, tool="jobs")
        if first != last:
            msg = (
                f"jobs: {words!r} is a stretch of days, and an application is on one day; ask"
                " him which day"
            )
            raise ToolError(msg, code="invalid_arguments")
        if first > today:
            msg = f"jobs: {words!r} has not happened yet; today is {today.isoformat()}"
            raise ToolError(msg, code="invalid_arguments")
        return first

    def _update(
        self, args: Mapping[str, Any], view: Mapping[str, Any], now: datetime, zone: tzinfo,
    ) -> Receipted:
        today = now.date()
        target, app = self._target(_text(args, "application"), view)
        wants = self._wants(args, today)
        if app is None:
            was: dict[str, Any] = {"hidden": True}
        else:
            day = _day_of(app["applied_at"], zone)
            was = {
                "status": app["status"], "applied_at": day.isoformat() if day else None,
                "note": app["note"], "hidden": False,
            }
        changes = [
            {"field": field, "from": was.get(field), "to": new}
            for field, new in wants.items() if was.get(field) != new
        ]
        name = _named(target)
        if not changes:
            return Receipted(
                {"summary": _clean(f"{name} already has that; nothing changed."), "changed": []},
                Receipt(
                    "done", reads_only=False, object=f"job:{target['id']}",
                    line=f"nothing changed for {name}",
                ),
            )
        try:
            self._source.edit(target["id"], {c["field"]: c["to"] for c in changes})
        except LookupError as exc:
            msg = f"jobs: {exc}; jobs() lists his applications"
            raise ToolError(msg, code="not_found") from exc
        except ValueError as exc:
            msg = f"jobs: {exc}"
            raise ToolError(msg, code="invalid_arguments") from exc
        said = "; ".join(self._said(c, today, zone) for c in changes)
        back = {
            ("when" if c["field"] == "applied_at" else c["field"]): c["from"]
            for c in changes if c["from"] not in (None, "")
        }
        answer: dict[str, Any] = {
            "summary": _clean(f"{name}: {said}."),
            "changed": changes,
            "undo": {"action": "update", "application": target["id"], **back},
        }
        return Receipted(answer, Receipt(
            "done", reads_only=False, object=f"job:{target['id']}", line=f"{name}: {said}",
        ))

    def _wants(self, args: Mapping[str, Any], today: date) -> dict[str, Any]:
        """What an update is asked to set: each field he named, checked."""
        wants: dict[str, Any] = {}
        if status := _text(args, "status"):
            if status not in APPLICATION_STATUSES:
                msg = f"jobs: status is one of {', '.join(APPLICATION_STATUSES)}, not {status!r}"
                raise ToolError(msg, code="invalid_arguments")
            wants["status"] = status
        if words := _text(args, "when"):
            wants["applied_at"] = self._day(words, today).isoformat()
        if note := _text(args, "note"):
            wants["note"] = note
        if isinstance(args.get("hidden"), bool):
            wants["hidden"] = args["hidden"]
        if not wants:
            msg = "jobs: update needs at least one of status, when, note or hidden"
            raise ToolError(msg, code="invalid_arguments")
        return wants

    @staticmethod
    def _said(change: Mapping[str, Any], today: date, zone: tzinfo) -> str:
        """One change in words, from what to what, so another update can put it back."""
        field, before, after = change["field"], change["from"], change["to"]
        if field == "applied_at":
            was = _said_day(before, today, zone) or "no day"
            return f"applied day {was} to {_said_day(after, today, zone)}"
        if field == "hidden":
            return "taken off his list" if after else "put back on his list"
        if field == "note":
            return "note set" if not before else f"note changed from {before!r} to {after!r}"
        return f"status {_plain(str(before))} to {_plain(str(after))}"

    def _target(
        self, ref: str, view: Mapping[str, Any],
    ) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
        """The application an update is about, and its row when it is on his list."""
        apps = list(view["applications"])
        if not ref:
            msg = (
                "jobs: update needs application: a row number from the last list or find, an id,"
                " or a company in his words"
            )
            raise ToolError(msg, code="invalid_arguments")
        if _ROW_NUMBER.fullmatch(ref):
            return self._by_number(int(ref), apps)
        if app := next((a for a in apps if a["id"] == ref), None):
            return {"id": app["id"], "company": app["company"], "role": app["role"]}, app
        if gone := next((h for h in view["hidden"] if h["id"] == ref), None):
            return dict(gone), None
        same = [a for a in apps if same_company(a["company"], ref)]
        if not same:
            msg = f"jobs: nothing on his list matches {ref!r}; jobs() lists his applications"
            raise ToolError(msg, code="not_found")
        if len(same) > 1:
            named = "; ".join(
                f"{_named(a)}, {_plain(a['status'])}, id {a['id']}" for a in same[:_NAMED]
            )
            more = f" and {len(same) - _NAMED} more" if len(same) > _NAMED else ""
            msg = (
                f"jobs: {ref} matches {len(same)} applications: {named}{more}. Ask him which"
                " one, then give its id"
            )
            raise ToolError(msg, code="ambiguous", kind="needs_him")
        only = same[0]
        return {"id": only["id"], "company": only["company"], "role": only["role"]}, only

    def _by_number(
        self, number: int, apps: Sequence[Mapping[str, Any]],
    ) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
        """The application at a row number of the last list or find."""
        with self._lock:
            held = dict(self._held)
        if not held:
            msg = (
                f"jobs: row {number} means nothing now: a row number is the place in the last"
                " list or find, and none has been made since the last restart. Call jobs() or"
                " jobs(action=find, company=...) first, or give the id or the company"
            )
            raise ToolError(msg, code="no_list", kind="retry_other_args")
        if (app_id := held.get(number)) is None:
            msg = f"jobs: the last list had rows 1 to {len(held)}, not {number}; call jobs() again"
            raise ToolError(msg, code="not_found")
        app = next((a for a in apps if a["id"] == app_id), None)
        if app is None:
            msg = f"jobs: row {number} is no longer on his list; call jobs() again"
            raise ToolError(msg, code="not_found")
        return {"id": app["id"], "company": app["company"], "role": app["role"]}, app


def build_jobs_tool(source: JobsSource | None, sources: LedgerSources) -> tuple[Tool, ...]:
    """``jobs`` over the runtime's job-mail view; none while job mail is off (ADR 0185)."""
    if source is None:
        return ()
    jobs = _Jobs(source, sources)

    def run(args: Mapping[str, Any], ctx: ToolContext) -> Receipted:
        return jobs.run(args, ctx)

    return (
        Tool(
            name="jobs",
            description=_DESCRIPTION,
            input_schema=_SCHEMA,
            handler=run,
            allowed_callers=frozenset({CallerPrincipal.JARVIS_LLM}),
            risk_level="L1",
            read_only=False,
            reads=_reads,
            max_result_chars=_BUDGET,
        ),
    )


__all__ = ["JobsSource", "build_jobs_tool"]
