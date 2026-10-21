"""What a turn writes and reads back around its answer: records, mail, times, places, timing."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.execution import places_api
from jarvis.execution.transit_tool import first_leave_ms
from jarvis.runtime.home import mail_body
from jarvis.shared.lang import is_filler
from jarvis.state import reactions
from jarvis.state.action_facts import action_result
from jarvis.state.attachments import AttachmentRef, Attachments, marker
from jarvis.state.event_log import emit_event, iter_events_for_turn
from jarvis.state.reminders import scheduled_by_actions
from jarvis.state.turn_facts import clarifications_asked_in_turn

if TYPE_CHECKING:

    from collections.abc import Mapping
    from pathlib import Path

    from jarvis.decision import turn_timing
    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event


LOGGER = logging.getLogger(__name__)


def record_words(store: Attachments | None, payload: Mapping[str, Any]) -> str:
    """Allen's words as memory.db keeps them; files sent with them are named (ADR 0211).

    A sentence that goes on from an earlier one keeps its own words as its row; the earlier row
    keeps the rest, and the history reads them as one message (ADR 0271).
    """
    segment = payload.get("segment")
    words = segment if isinstance(segment, str) and segment else str(payload.get("transcript", ""))
    ids = payload.get("attachments")
    if store is None or not isinstance(ids, list) or not ids:
        return words
    return f"{words}\n{marker(store.get(str(one)) for one in ids)}"


def record_attachments(
    store: Attachments | None, payload: Mapping[str, Any],
) -> list[AttachmentRef]:
    """The kept files that came with his words, in the order he sent them (ADR 0255)."""
    ids = payload.get("attachments")
    if store is None or not isinstance(ids, list):
        return []
    return [ref for one in ids if (ref := store.get(str(one))) is not None]


def record_reaction(
    runtime: JarvisRuntime, memory_db: Path, target: str, emoji: str, answer: Event,
) -> None:
    """ADR 0257: put her ``emoji`` on his message ``target``; the answer's uid keeps a retry to one.

    The reply is already out, so a reaction that cannot be written is logged, not raised.
    """
    try:
        reactions.jarvis_reacts(
            runtime.conn, memory_db, target=target, emoji=emoji,
            record_id=f"reaction:{answer.event_uid}",
        )
    except Exception:
        LOGGER.exception("drive_turn: could not write her reaction to %s", target)


def queue_picture_notes(
    runtime: JarvisRuntime, event: Event, record_id: str, answer: str,
) -> None:
    """ADR 0223: when a turn that came with files ends, queue the notes of its pictures.

    Off the turn's path and never raising into it: a failure here costs the notes only.
    """
    ids = event.payload.get("attachments")
    if runtime.picture_notes is None or runtime.memory is None:
        return
    if not isinstance(ids, list) or not ids:
        return
    words = str(event.payload.get("transcript", ""))
    try:
        runtime.picture_notes.submit(
            record_id, [str(one) for one in ids], "" if is_filler(words) else words, answer,
        )
    except Exception:
        LOGGER.exception("picture notes: could not queue the notes of turn %s", record_id)


_MAIL_GET: Final[str] = "mcp__gmail__gmail_get"
_MAIL: Final[str] = "mail"


def _read_mail_id(action: Mapping[str, Any]) -> str | None:
    """The id of the message a proposed call reads whole (``mail(read=...)``, ``gmail_get``)."""
    arguments = action.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    if action.get("tool_name") == _MAIL and arguments.get("read"):
        return str(arguments["read"])
    if action.get("tool_name") == _MAIL_GET and arguments.get("format", "full") == "full":
        return str(arguments.get("messageId"))
    return None


def fetched_mail(conn: sqlite3.Connection, turn_id: str) -> tuple[str, str] | None:
    """ADR 0063: the message this turn read whole, as a mail record.

    By ``mail(read=...)`` (ADR 0242) or ``gmail_get``. Only when the turn read exactly one
    message in full: a turn that read several is a list, answered in words. The record is
    headers, a blank line, then the body, and its id is the result's event uid.
    """
    gets = [
        event.payload
        for event in iter_events_for_turn(conn, turn_id, ("action.proposed",))
        if _read_mail_id(event.payload) is not None
    ]
    if len({_read_mail_id(get) for get in gets}) != 1:
        return None
    action_id = gets[-1].get("action_id")
    found = action_result(conn, action_id) if isinstance(action_id, str) else None
    if found is None:
        return None  # no answer yet: the call has no message to show
    _row_id, result = found
    message: Any
    try:
        output = json.loads(result.payload["tool_output"])
        if gets[-1].get("tool_name") == _MAIL:
            # `mail` has cleaned the body; the card reads gmail_get's field names.
            message = {**output, "date": output.get("at"), "threadId": output.get("thread_id")}
            body = str(output.get("body") or "")
        else:
            message = json.loads(output.get("text", "")) if isinstance(output, dict) else None
            body = mail_body(str(message.get("body") or "")) if isinstance(message, dict) else ""
    except (ValueError, KeyError, TypeError):
        return None  # a windowed (over-long) or failed answer shows no card
    if not isinstance(message, dict) or "error" in message or not message.get("id"):
        return None
    headers = [
        f"{name}: {message[key]}"
        for name, key in (
            ("From", "from"), ("To", "to"), ("Date", "date"), ("Subject", "subject"),
            ("Thread-Id", "threadId"), ("Gmail-Id", "id"),
        )
        if isinstance(message.get(key), str) and message[key]
    ]
    return result.event_uid, "\n".join(headers) + "\n\n" + body


_CALENDAR_WRITES: Final[frozenset[str]] = frozenset(
    {"mcp__microsoft__create-calendar-event", "mcp__microsoft__update-calendar-event"},
)
_TIMES_MAX: Final[int] = 5
_TIME_LABEL_CHARS: Final[int] = 40


def _event_start_ms(body: object) -> int | None:
    """Epoch ms of a Graph event body's ``start``; None if absent or its zone is not IANA's."""
    start = body.get("start") if isinstance(body, dict) else None
    if not isinstance(start, dict):
        return None
    try:
        at = datetime.fromisoformat(str(start["dateTime"]))
        if at.tzinfo is None:  # a Windows zone name ("Pacific Standard Time") has no ZoneInfo
            at = at.replace(tzinfo=ZoneInfo(str(start["timeZone"])))
        return int(at.timestamp() * 1000)
    except (KeyError, ValueError, ZoneInfoNotFoundError):
        return None


def _calendar_time(
    conn: sqlite3.Connection, action: Mapping[str, Any],
) -> tuple[int, dict[str, Any]] | None:
    """``(result row id, time entry)`` of one Outlook write that went through; else None."""
    action_id = action.get("action_id")
    found = action_result(conn, action_id) if isinstance(action_id, str) else None
    arguments = action.get("arguments")
    arguments = arguments if isinstance(arguments, dict) else {}
    body = arguments.get("body")
    at_ms = _event_start_ms(body)
    if found is None or at_ms is None:
        return None
    row_id, result_event = found
    result = result_event.payload
    if result.get("semantics") == "error" or result.get("error"):
        return None
    event_id = arguments.get("eventId")
    if not event_id:  # a created event: its id is in the server's answer
        try:
            answer = json.loads(json.loads(result.get("tool_output", "{}")).get("text", ""))
            event_id = answer.get("id")
        except (ValueError, AttributeError, TypeError):
            event_id = None
    return row_id, {
        "kind": "event", "at_ms": at_ms,
        "label": str(body.get("subject", ""))[:_TIME_LABEL_CHARS] if isinstance(body, dict) else "",
        "ref": str(event_id or action.get("action_id", "")),
    }


def turn_times(conn: sqlite3.Connection, turn_id: str) -> list[dict[str, Any]]:
    """The times this turn's own log gives for the answer, or none if the log cannot give them.

    The answer is written either way: an unreadable row costs the phone its dial, not the turn.
    """
    try:
        return _read_turn_times(conn, turn_id)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        LOGGER.warning("turn %s: the times of its answer could not be read", turn_id, exc_info=True)
        return []


def turn_places(conn: sqlite3.Connection, turn_id: str) -> list[dict[str, Any]]:
    """The places this turn's tools returned (ADR 0283); none if the log cannot give them.

    As with the times, an unreadable row costs the phone its map, not the turn its answer.
    """
    try:
        return places_api.turn_places(conn, turn_id)
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        LOGGER.warning("turn %s: its answer's places could not be read", turn_id, exc_info=True)
        return []


def _read_turn_times(conn: sqlite3.Connection, turn_id: str) -> list[dict[str, Any]]:
    """The turn's times: ``[{kind, at_ms, label, ref}]``.

    From what the turn did, never from the model's words (ADR 0215): reminders it scheduled,
    Outlook events it created or updated without error, and the first leave time of the transit
    card it showed. In log order, at most five; an Outlook time in a zone name that is not
    IANA's is left out.
    """
    found: list[tuple[int, dict[str, Any]]] = []
    actions = [
        event.payload for event in iter_events_for_turn(conn, turn_id, ("action.proposed",))
    ]
    ids = [str(one["action_id"]) for one in actions if one.get("action_id")]
    for row_id, reminder in scheduled_by_actions(conn, ids):
        found.append((row_id, {
            "kind": "reminder", "at_ms": reminder.due_at_ms,
            "label": reminder.text[:_TIME_LABEL_CHARS], "ref": reminder.reminder_id,
        }))
    found.extend(
        done for action in actions
        if action.get("tool_name") in _CALENDAR_WRITES and (done := _calendar_time(conn, action))
    )
    for row_id, card in clarifications_asked_in_turn(conn, turn_id):
        trip = card.payload.get("trip")
        if not isinstance(trip, dict):
            continue
        leave_ms = first_leave_ms(trip)
        if leave_ms is not None:
            found.append((row_id, {
                "kind": "departure", "at_ms": leave_ms,
                "label": str(trip.get("to", ""))[:_TIME_LABEL_CHARS],
                "ref": str(card.payload.get("clarification_id", "")),
            }))
    return [entry for _id, entry in sorted(found, key=lambda one: one[0])[:_TIMES_MAX]]


def emit_turn_timing(conn: sqlite3.Connection, timer: turn_timing.TurnTimer) -> None:
    """ADR 0276: write the answered turn's timing fact; a measurement never fails a turn."""
    try:
        emit_event(
            conn,
            type="turn.timing",
            payload=timer.payload(),
            correlation={"turn_id": timer.turn_id},
        )
    except Exception:
        LOGGER.exception("turn %s: its timing could not be written, skipped", timer.turn_id)
