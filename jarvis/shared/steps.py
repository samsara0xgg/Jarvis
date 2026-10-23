"""ADR 0231: the steps of her turn, as the phone shows them, folded from the action rows.

A step is one tool call, seen as a fixed label from the language table (``step.<kind>``) plus, for
a few tools, a short detail taken from the call's arguments. No model writes either. The fold is
pure: the live ``step`` frames of ``/phone/ws`` and the summary under her answer in
``GET /inherent/conversation`` read the same rows through it, so every surface shows the same
thing, and the summary survives a restart because it is read from the log, not from memory.

Layer rules: stdlib and ``jarvis.shared`` only.
"""

from __future__ import annotations

import contextlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Literal
from urllib.parse import urlsplit

from jarvis.shared import lang
from jarvis.shared.receipts import Receipt, derive

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jarvis.shared import Event

READ_TOOL: Final = re.compile(r"search|list|get|fetch|read|query|view|find", re.IGNORECASE)
"""An MCP tool whose name reads (rather than writes); ADR 0115 and the kinds below share it."""
ENDS_ACTION: Final = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
ENDS_TURN: Final = ("turn.ended", "turn.failed", "response.cancelled")
EVENT_TYPES: Final[tuple[str, ...]] = (
    "action.proposed",
    "action.running",
    *ENDS_ACTION,
    *ENDS_TURN,
)
"""Every row type :meth:`StepFold.feed` reads."""

DETAIL_CHARS: Final = 24
"""A detail is cut to this many characters, with an ellipsis."""
_SEP: Final = " · "

_KIND: Final[dict[str, str]] = {
    "web_search": "web",
    "web_fetch": "page",
    "screen_look": "screen",
    "screen_capture": "screen",
    "look_at_picture": "picture",
    "bus": "transit",
    # The six tools `bus` replaced (ADR 0246): turns already in the log still name them.
    "transit": "transit",
    "pin_departure": "ride",
    "watch_for_boarding": "ride",
    "start_ride": "ride",
    "set_ride_stop": "ride",
    "end_ride": "ride",
    "weather": "weather",
    "where_am_i": "place",
    "places": "places",
    "save_place": "remember",
    "forget_place": "remember",
    "mark_highlight": "remember",
    "set_reminder": "reminder",
    "list_reminders": "reminder",
    "cancel_reminder": "reminder",
    "create_memo": "memo",
    "list_memos": "memo",
    "remember": "remember",
    "keep": "remember",
    "kept": "records",
    "search_records": "records",
    "conversation": "records",
    "my_day": "day",
    "my_schedule": "calendar",
    "schedule": "calendar",
    "search_notes": "records",
    "read_file": "file",
    "open_path": "file",
    "open_url": "url",
    "mail": "mail",
    "refresh_work_state": "work_state",
    "daily_work_report": "report",
    "job_ledger": "jobs",
    "record_application": "jobs",
    "show_on_dashboard": "dashboard",
}
_RIDE_ARGUMENTS: Final = frozenset({"pin", "ride"})
_HIDDEN: Final = frozenset(
    {"tool_search", "get_current_time", "ask_user", "dismiss_card"},
)
"""Plumbing and cards: a call to one of these is not a step he would name."""


def _by_arguments(tool: str, arguments: object) -> str | None:
    """The kind a call's own arguments decide, or None when the tool alone does."""
    given = _arguments(arguments)
    if tool == "bus" and _RIDE_ARGUMENTS & given.keys():
        return "ride"
    if tool == "schedule" and given.get("action") not in (None, "", "read"):
        return "calendar_edit"
    if tool == "mail" and given.get("action") == "archive":
        return "mail_archive"
    if tool == "mail" and given.get("action") == "draft":
        return "mail_write"
    if tool == "mail" and given.get("action") == "send":
        return "mail_send"
    return None


def kind(tool: str, arguments: object = None) -> str:
    """The language-table kind of a tool: ``step.<kind>`` names it; ``generic`` is no kind.

    ``bus`` is a ride when the call pins, rides or sets a stop, else a transit look (ADR 0246).
    ``schedule`` changes the calendar when its action is not a read (ADR 0299). ``mail`` archives,
    writes a letter or sends one when its action says so (ADR 0362, 0366), else it checks his mail.
    """
    if decided := _by_arguments(tool, arguments):
        return decided
    if known := _KIND.get(tool):
        return known
    parts = tool.split("__")
    if len(parts) != 3 or parts[0] != "mcp":  # noqa: PLR2004
        return "generic"
    server, name = parts[1], parts[2]
    reads = READ_TOOL.search(name) is not None
    if server == "microsoft":
        return "todo" if "todo" in name else "calendar" if reads else "calendar_edit"
    if server == "gmail":
        return "mail" if reads else "mail_write"
    return {"notion": "notion", "hue": "lights"}.get(server, "generic")


def _cut(text: str) -> str:
    """One line of at most :data:`DETAIL_CHARS` characters; control characters become spaces."""
    line = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
    return line if len(line) <= DETAIL_CHARS else line[: DETAIL_CHARS - 1] + "…"


def _text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return value if isinstance(value, str) else ""


def _place(arguments: dict[str, Any]) -> str:
    where = (_text(arguments, "destination") or _text(arguments, "to")).strip()
    if not where:
        return ""
    key = f"step.place.{where.lower()}"
    named = where.lower() in {"here", "home", "school"}
    return lang.t("step.detail.to", place=lang.t(key) if named else _cut(where))


def _host(arguments: dict[str, Any]) -> str:
    host = urlsplit(_text(arguments, "url").strip()).hostname or ""
    return _cut(host.removeprefix("www."))


def _days(arguments: dict[str, Any]) -> str:
    try:
        first = datetime.fromisoformat(_text(arguments, "startDateTime")).date()
        last = datetime.fromisoformat(_text(arguments, "endDateTime")).date()
    except ValueError:
        return ""
    day = f"{first.month}/{first.day}"
    return day if last <= first else f"{day}-{last.month}/{last.day}"


_DETAIL: Final = {
    "web": lambda a: _cut(_text(a, "query")),
    "page": _host,
    "transit": _place,
    "calendar": _days,
}
"""The kinds that carry a detail, and how it is read. Anything else (a file path, a mail, a
reminder's words, a place he is) is never put in a label."""


def _arguments(raw: object) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def label(tool: str, arguments: object = None) -> str:
    """The step's label: its table name, then `` · `` and the detail when the kind has one."""
    which = kind(tool, arguments)
    if which == "generic":
        return lang.t("tool_status.generic")
    name = lang.t(f"step.{which}")
    detail = _DETAIL[which](_arguments(arguments)) if which in _DETAIL else ""
    return f"{name}{_SEP}{detail}" if detail else name


def line(tool: str, arguments: object = None, *, language: lang.Language | None = None) -> str:
    """The step's table name alone, in ``language``: what is said when a slow call starts."""
    which = kind(tool, arguments)
    return lang.t("tool_status.generic" if which == "generic" else f"step.{which}", lang=language)


def phrase(tool: str, arguments: object = None) -> str:
    """The step in the past tense, for the line under her answer."""
    return lang.t(f"step.{kind(tool, arguments)}.done")


def _waiting(tool: str, arguments: object, status: str) -> str:
    """A call that did not finish its job, in its own words: sent unconfirmed, or awaiting a pick.

    The kind's name, then what is the case (ADR 0319, ADR 0231): never the past-tense phrase,
    which would say it was done.
    """
    which = kind(tool, arguments)
    name = lang.t("tool_status.generic") if which == "generic" else lang.t(f"step.{which}")
    return f"{name}{_SEP}{lang.t(f'step.{status}')}"


@dataclass(frozen=True)
class Step:
    """One tool call of a turn, as it stands after the last row folded in."""

    turn_id: str
    n: int
    tool: str
    label: str
    phrase: str
    kind: str
    state: Literal["running", "done", "failed"]
    started_ms: int
    ended_ms: int | None
    receipt: Receipt | None = None
    """ADR 0319: the call's receipt, once it ended; ``None`` while it runs."""
    action_id: str = ""
    """The call's action id: the receipt's id (ADR 0320) is worked out from it."""

    def frame(self) -> dict[str, Any]:
        """The ``step`` frame of ``/phone/ws``."""
        return {
            "type": "step",
            "turn_id": self.turn_id,
            "step": self.n,
            "tool": self.tool,
            "label": self.label,
            "state": self.state,
            "started_ms": self.started_ms,
            "ended_ms": self.ended_ms,
        }


def _turn(event: Event) -> str:
    return str(event.payload.get("turn_id") or (event.correlation or {}).get("turn_id") or "")


class StepFold:
    """The steps of the turns fed to it, from their action and turn-end rows in log order.

    A step starts at ``action.running`` (the tool really runs; a call still waiting on his
    confirmation has none) and ends at its terminal row: ``failed`` for a failed, timed-out or
    cancelled action and for a result that carries an error, ``done`` otherwise. A turn that ends
    with a step still open closes it ``failed``. A call to a tool in :data:`_HIDDEN` is no step.
    Rows of actions it never saw proposed are ignored, so a reader can feed only the turns it
    wants by feeding their ``action.proposed`` rows.
    """

    _TURNS_KEPT = 1024

    def __init__(self) -> None:
        """Start with no action known."""
        self._proposed: dict[str, tuple[str, str, object]] = {}
        self._open: dict[str, Step] = {}
        self._count: dict[str, int] = {}

    def feed(self, event: Event) -> list[Step]:
        """Fold one row; the steps it started or ended."""
        payload = event.payload
        action_id = str(payload.get("action_id") or "")
        if event.type == "action.proposed":
            tool = str(payload.get("tool_name") or "")
            if tool not in _HIDDEN:
                self._proposed[action_id] = (_turn(event), tool, payload.get("arguments"))
            return []
        if event.type == "action.running":
            if (proposal := self._proposed.get(action_id)) is None:
                return []
            turn, tool, arguments = proposal
            if len(self._count) >= self._TURNS_KEPT:
                self._count.clear()
            n = self._count[turn] = self._count.get(turn, 0) + 1
            step = Step(
                turn,
                n,
                tool,
                label(tool, arguments),
                phrase(tool, arguments),
                kind(tool, arguments),
                "running",
                event.ts_epoch_ms,
                None,
                action_id=action_id,
            )
            self._open[action_id] = step
            return [step]
        if event.type in ENDS_ACTION:
            proposal = self._proposed.pop(action_id, None)
            if (was := self._open.pop(action_id, None)) is None:
                return []
            arguments = proposal[2] if proposal is not None else None
            return [self._end(was, arguments, _receipt(was.tool, arguments, event), event)]
        turn = _turn(event)
        ended = [
            self._end(s, args := self._proposed.get(a, ("", "", None))[2], _receipt(
                s.tool, args, None,
            ), event)
            for a, s in self._open.items()
            if s.turn_id == turn
        ]
        self._open = {a: s for a, s in self._open.items() if s.turn_id != turn}
        self._proposed = {a: p for a, p in self._proposed.items() if p[0] != turn}
        return ended

    @staticmethod
    def _end(was: Step, arguments: object, receipt: Receipt, event: Event) -> Step:
        """``was`` as it stands once its call ended; a call still waiting says so in its label."""
        said = (
            _waiting(was.tool, arguments, receipt.status)
            if receipt.status in ("pending", "needs_choice")
            else None
        )
        return replace(
            was,
            label=said or was.label,
            phrase=said or was.phrase,
            state="failed" if receipt.status == "failed" else "done",
            ended_ms=event.ts_epoch_ms,
            receipt=receipt,
        )


def _receipt(tool: str, arguments: object, row: Event | None) -> Receipt:
    """The receipt of a call that ended with ``row`` (``None``: its turn ended first; ADR 0319).

    A result row carries the call's receipt. An old row has none, and a call that ended without
    a result (refused, failed, timed out, cancelled, or cut off by its turn) never had one:
    those are derived, failed with ``dont_retry`` where the call did not finish, and a result
    row's own error code decides its kind.
    """
    payload = row.payload if row is not None else {}
    if (raw := payload.get("receipt")) is not None:
        with contextlib.suppress(ValueError):
            return Receipt.from_json(raw)
    given = _arguments(arguments)
    if row is None or row.type != "action.result_observed":
        return derive(tool, given, reads=False, code="no_result", failure="dont_retry")
    if payload.get("semantics") == "error" or payload.get("error"):
        return derive(tool, given, reads=False, code=str(payload.get("error") or "tool_error"))
    return derive(tool, given, reads=False)


def summarize(steps: Sequence[Step]) -> dict[str, Any]:
    """What a surface shows under an answer.

    ``steps`` (``label``, ``ms``, and ``failed`` only when it did), ``steps_ms`` (first start to
    last end) and ``steps_summary``, the past-tense phrases of the distinct kinds in order. Steps
    that never ended are left out; ``{}`` if none ended.
    """
    ended = [s for s in steps if s.ended_ms is not None]
    if not ended:
        return {}
    phrases = list(dict.fromkeys(s.phrase for s in ended))
    last = max(s.ended_ms or 0 for s in ended)
    return {
        "steps": [
            {
                "label": s.label,
                "ms": (s.ended_ms or 0) - s.started_ms,
                **({"failed": True} if s.state == "failed" else {}),
            }
            for s in ended
        ],
        "steps_ms": last - min(s.started_ms for s in ended),
        "steps_summary": lang.t("step.join").join(phrases),
    }
