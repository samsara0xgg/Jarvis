"""Reminders and an Outlook event for each interview time job mail finds (ADR 0186).

One pass (:meth:`InterviewReminders.reconcile`) compares the applications' next interview times
with what was armed for them, kept in the ledger's ``job_interview_reminder`` table, and makes the
two agree: two reminders (the evening before and ``before_min`` before the start) written as the
``reminder.scheduled`` / ``reminder.cancelled`` events of ADR 0179, and one event on Allen's
Outlook calendar. A pass is idempotent, so a restart or a repeat changes nothing; an Outlook
failure is logged and retried by the next pass and never undoes a reminder.

The only Outlook tools called are the three that write one event, and only an event id this module
itself created and stored is ever updated or deleted.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jarvis.decision import job_mail as triage
from jarvis.shared import lang
from jarvis.state import job_ledger as ledger
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import emit_event, open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)

OUTLOOK_SERVER: Final[str] = "microsoft"
# The only Outlook tools this module may call: one event is written, changed or removed.
CREATE_TOOL: Final[str] = "create-calendar-event"
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {CREATE_TOOL, "update-calendar-event", "delete-calendar-event"}
)
_ACTION_ID: Final[str] = "job-interview-reminders"
_DEFAULT_MINUTES: Final[int] = 60
_LOG_WAIT_S: Final[float] = 5.0


@dataclass(frozen=True)
class InterviewSettings:
    """The ``job_mail.interview_reminders`` block of ``config/jarvis.yaml`` once validated."""

    evening_at: clock
    before_min: int
    outlook: bool


def outlook_write(
    servers: McpServers, tool: str, args: Mapping[str, Any], owned: Collection[str]
) -> dict[str, Any]:
    """One Outlook write; a tool outside ``WRITE_TOOLS`` or a foreign event id is refused unsent."""
    if tool not in WRITE_TOOLS:
        msg = f"interview reminders may only write one Outlook event, not call {tool!r}"
        raise ValueError(msg)
    if tool != CREATE_TOOL and args.get("eventId") not in owned:
        msg = f"{tool}: not an event Jarvis created: {args.get('eventId')!r}"
        raise ValueError(msg)
    return servers.call(OUTLOOK_SERVER, tool, args)


def local_zone() -> str:
    """The machine's IANA zone name (``TZ``, else the ``/etc/localtime`` link), or ``UTC``."""
    name = os.environ.get("TZ") or os.path.realpath("/etc/localtime").partition("zoneinfo/")[2]
    try:
        return ZoneInfo(name).key if name else "UTC"
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return "UTC"


def _at(text: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)).astimezone()
    except ValueError:
        return None


class InterviewReminders:
    """The reconciler and the user's cancel; built once beside the job-mail poller."""

    def __init__(
        self,
        settings: InterviewSettings,
        db: Path,
        event_log: Path,
        connections: PluginConnections,
    ) -> None:
        """``db`` is memory.db (the ledger), ``event_log`` the log reminders are written to."""
        self._settings, self._db, self._event_log = settings, db, event_log
        self._connections = connections
        self._lock = threading.Lock()
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.zone = local_zone()

    # --- the pass ------------------------------------------------------------------------

    def reconcile(self, applications: Sequence[Mapping[str, Any]]) -> None:
        """Arm what is missing and disarm what is gone; each application fails on its own."""
        now = self.now()
        wanted: dict[str, tuple[Mapping[str, Any], datetime]] = {}
        for app in applications:
            at = _at(app["next_event_at"]) if app["next_event_at"] else None
            if at is not None and at > now and app["status"] != "rejected":
                wanted[app["id"]] = (app, at)
        with self._lock, contextlib.closing(self._log()) as conn:
            known = reminder_state.fold(conn)
            for app_id, row in ledger.interview_rows(self._db).items():
                if app_id not in wanted:
                    self._guarded(self._disarm, conn, known, app_id, row, now)
            for app, at in wanted.values():
                self._guarded(self._arm, conn, known, app, at, now)

    def cancel(self, app_id: str) -> None:
        """Allen's undo: cancel both reminders and delete the event; that time is not re-armed."""
        with self._lock, contextlib.closing(self._log()) as conn:
            row = ledger.interview_rows(self._db).get(app_id)
            if row is None:
                msg = f"no reminders for application {app_id}"
                raise LookupError(msg)
            self._cancel_reminders(conn, reminder_state.fold(conn), row)
            row["cancelled"] = 1
            self._delete_event(row)
            ledger.put_interview(self._db, app_id, row)

    def view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """What the Jobs card says is armed: the time, which reminders, the Outlook event."""
        return {
            "at": row["event_at"],
            "evening": bool(row["evening_id"]),
            "before": bool(row["before_id"]),
            "evening_at": self._settings.evening_at.strftime("%H:%M"),
            "before_min": self._settings.before_min,
            "outlook": bool(row["outlook_id"]),
            "cancelled": bool(row["cancelled"]),
        }

    # --- one application -----------------------------------------------------------------

    @staticmethod
    def _guarded(step: Callable[..., None], *args: Any) -> None:  # noqa: ANN401 - the step's own
        try:
            step(*args)
        except Exception:
            LOGGER.exception("job mail: interview reminders failed for one application")

    def _arm(
        self,
        conn: sqlite3.Connection,
        known: dict[str, reminder_state.Reminder],
        app: Mapping[str, Any],
        at: datetime,
        now: datetime,
    ) -> None:
        old = ledger.interview_rows(self._db).get(app["id"])
        row: dict[str, Any] = old or {"cancelled": 0}
        if old is not None and _at(old["event_at"]) != at:  # a new time: the old reminders go
            self._cancel_reminders(conn, known, old)
            row |= {"evening_id": None, "before_id": None, "cancelled": 0}
        row["event_at"] = app["next_event_at"]
        if row["cancelled"]:
            self._delete_event(row)  # a delete that failed when he cancelled is tried again
            ledger.put_interview(self._db, app["id"], row)
            return
        ledger.put_interview(self._db, app["id"], row)
        slots = {
            "evening_id": (self._evening(at), "job.rem.evening"),
            "before_id": (at - timedelta(minutes=self._settings.before_min), "job.rem.before"),
        }
        for key, (due, line) in slots.items():
            if row.get(key) is None:
                if due <= now:  # that moment has passed: it is skipped, not made late
                    continue
                row[key] = reminder_state.ID_PREFIX + uuid.uuid4().hex[:8]
                ledger.put_interview(self._db, app["id"], row)  # the id is kept before it is used
            if row[key] not in known and due > now:
                self._schedule(conn, known, row[key], due, self._line(line, app, at))
        if self._settings.outlook:
            self._sync_event(app, at, row)
        ledger.put_interview(self._db, app["id"], row)

    def _disarm(
        self,
        conn: sqlite3.Connection,
        known: dict[str, reminder_state.Reminder],
        app_id: str,
        row: dict[str, Any],
        now: datetime,
    ) -> None:
        """The application is rejected, hidden or has no time: undo what was armed for it.

        A time already past was an interview that happened: nothing is undone and the Outlook
        event stays in his calendar.
        """
        at = _at(row["event_at"])
        if at is None or at <= now:
            ledger.drop_interview(self._db, app_id)
            return
        self._cancel_reminders(conn, known, row)
        self._delete_event(row)
        if row["outlook_id"]:
            ledger.put_interview(self._db, app_id, row)  # the delete failed: tried again next pass
        else:
            ledger.drop_interview(self._db, app_id)

    # --- reminders -----------------------------------------------------------------------

    def _evening(self, at: datetime) -> datetime:
        """20:00 (``evening_at``) on the day before the interview, in Allen's zone."""
        zone = ZoneInfo(self.zone)
        day = at.astimezone(zone).date() - timedelta(days=1)
        return datetime.combine(day, self._settings.evening_at, tzinfo=zone)

    def _schedule(
        self,
        conn: sqlite3.Connection,
        known: dict[str, reminder_state.Reminder],
        reminder_id: str,
        due: datetime,
        text: str,
    ) -> None:
        """Write one ``reminder.scheduled`` event, the same payload ``set_reminder`` writes."""
        local = due.astimezone(ZoneInfo(self.zone))
        epoch_ms = int(due.timestamp() * 1000)
        emit_event(
            conn,
            type="reminder.scheduled",
            actor="jarvis_runtime",  # no model turn wrote it
            payload={
                "reminder_id": reminder_id,
                "due_at_epoch_ms": epoch_ms,
                "due_at_local": local.isoformat(timespec="seconds"),
                "text": text,
                "action_id": _ACTION_ID,
            },
        )
        known[reminder_id] = reminder_state.Reminder(
            reminder_id, epoch_ms, local.isoformat(timespec="seconds"), text
        )

    @staticmethod
    def _cancel_reminders(
        conn: sqlite3.Connection,
        known: dict[str, reminder_state.Reminder],
        row: Mapping[str, Any],
    ) -> None:
        """Write ``reminder.cancelled`` for each of the row's reminders still waiting to ring."""
        for key in ("evening_id", "before_id"):
            one = known.get(row.get(key) or "")
            if one is not None and one.pending:
                emit_event(
                    conn,
                    type="reminder.cancelled",
                    actor="jarvis_runtime",
                    payload={"reminder_id": one.reminder_id, "action_id": _ACTION_ID},
                )
                one.cancelled = True

    def _line(self, key: str, app: Mapping[str, Any], at: datetime) -> str:
        """What the reminder says: the company, how it is held and, for the second, the link."""
        interview = app.get("interview") or {}
        if interview.get("mode") == "online":
            platform = interview.get("platform") or ""
            where = lang.t("job.rem.online", platform=f" {platform}" if platform else "")
        elif interview.get("mode") == "onsite":
            place = interview.get("location") or ""
            where = lang.t("job.rem.onsite", place=f" {place}" if place else "")
        else:
            where = ""
        if key == "job.rem.before" and interview.get("join_url"):
            platform = interview.get("platform")
            where = lang.t("job.rem.link", platform=f"{platform} " if platform else "")
        return lang.t(
            key,
            company=app["company"],
            time=lang.spoken_time(at.astimezone(ZoneInfo(self.zone))),
            n=self._settings.before_min,
            where=where,
        )

    # --- the Outlook event ---------------------------------------------------------------

    def _event(self, app: Mapping[str, Any], at: datetime) -> dict[str, Any]:
        """The event's fields: subject, start and end in the zone, body, a reminder before."""
        zone = ZoneInfo(self.zone)
        text = next(
            (m["event_text"] for m in app["mails"] if _at(m["event_at"]) == at and m["event_text"]),
            "",
        )
        end = at + timedelta(minutes=triage.event_minutes(text) or _DEFAULT_MINUTES)
        interview = app.get("interview") or {}
        subject = (
            lang.t("job.cal.subject_role", company=app["company"], role=app["role"])
            if app["role"]
            else lang.t("job.cal.subject", company=app["company"])
        )
        lines = [
            lang.t(key, **{field: interview[name]})
            for key, field, name in (
                ("job.cal.platform", "platform", "platform"),
                ("job.cal.link", "url", "join_url"),
                ("job.cal.place", "place", "location"),
            )
            if interview.get(name)
        ]
        event: dict[str, Any] = {
            "subject": subject,
            "start": {
                "dateTime": at.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds"),
                "timeZone": self.zone,
            },
            "end": {
                "dateTime": end.astimezone(zone).replace(tzinfo=None).isoformat(timespec="seconds"),
                "timeZone": self.zone,
            },
            "body": {"contentType": "text", "content": "\n".join([*lines, lang.t("job.cal.note")])},
            "isReminderOn": True,
            "reminderMinutesBeforeStart": self._settings.before_min,
            "isOnlineMeeting": False,
        }
        if interview.get("location"):
            event["location"] = {"displayName": interview["location"]}
        return event

    def _sync_event(self, app: Mapping[str, Any], at: datetime, row: dict[str, Any]) -> None:
        """Create the Outlook event, or update it when what it shows changed; failure is logged."""
        event = self._event(app, at)
        sig = hashlib.sha1(json.dumps(event, sort_keys=True).encode(), usedforsecurity=False)
        sig_text = sig.hexdigest()[:16]
        if row.get("outlook_id") and row.get("outlook_sig") == sig_text:
            return
        try:
            servers = self._connections.client_for(OUTLOOK_SERVER)
            owned = self._owned(row)
            if not row.get("outlook_id"):
                reply = outlook_write(servers, CREATE_TOOL, {"body": event}, owned)
                row["outlook_id"] = _event_id(reply)
            else:
                outlook_write(
                    servers,
                    "update-calendar-event",
                    {"eventId": row["outlook_id"], "body": event},
                    owned,
                )
            row["outlook_sig"] = sig_text
        except Exception:  # noqa: BLE001 - any failure is retried by the next pass
            LOGGER.warning(
                "job mail: the Outlook event could not be written; tried again next pass",
                exc_info=True,
            )

    def _delete_event(self, row: dict[str, Any]) -> None:
        """Delete the row's Outlook event; an event already gone is done, a failure is retried."""
        if not row.get("outlook_id"):
            return
        try:
            servers = self._connections.client_for(OUTLOOK_SERVER)
            outlook_write(
                servers, "delete-calendar-event", {"eventId": row["outlook_id"]}, self._owned(row)
            )
        except Exception as exc:  # noqa: BLE001 - any failure is retried by the next pass
            if not any(word in str(exc).lower() for word in ("404", "not found", "notfound")):
                LOGGER.warning(
                    "job mail: the Outlook event could not be deleted; tried again next pass",
                    exc_info=True,
                )
                return
        row["outlook_id"] = row["outlook_sig"] = None

    def _owned(self, row: Mapping[str, Any]) -> set[str]:
        """The event ids this module created and still holds: the only ones it may change."""
        stored = {r["outlook_id"] for r in ledger.interview_rows(self._db).values()}
        return {one for one in (*stored, row.get("outlook_id")) if one}

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)


def _event_id(reply: Mapping[str, Any]) -> str:
    """The id of a created event, from the server's JSON text block or structured answer."""
    body = json.loads(reply["text"]) if "text" in reply else reply
    if not isinstance(body, dict) or not body.get("id"):
        msg = "create-calendar-event answered with no event id"
        raise ValueError(msg)
    return str(body["id"])
