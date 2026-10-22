"""One thing in the ledger for each interview time job mail finds (ADR 0186, 0299).

One pass (:meth:`InterviewReminders.reconcile`) compares the applications' next interview times
with the ``go`` things the ledger holds for them (``source="mail"``, ``source_ref`` ``job:<id>``,
written ``by="mail"``) and makes the two agree: a new time is a new thing, a changed time a move,
an interview that is gone (rejected, hidden, no time) a cancel. The thing carries the two rings of
ADR 0186, the evening before at ``evening_at`` and ``before_min`` before the start; the ledger arms
them (``things.sync_rings``). A pass is idempotent, so a restart or a repeat changes nothing.

No Outlook event is written any more: the interview is in the ledger and rings, and its going into
Outlook waits for the general write-back (ADR 0299). The first pass after the ledger also settles
what ADR 0186 armed before it, kept in the ledger's ``job_interview_reminder`` table: its two
reminders are cancelled (the thing's rings stand in for them), the Outlook event it wrote is put on
the thing's ``outlook_id`` so the mirror does not read Jarvis's own event back in, and a time
Allen had cancelled stays cancelled.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as time_of_day
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import job_mail as triage
from jarvis.shared import lang, zone_label
from jarvis.shared.clock import system_zone, zone_key
from jarvis.state import job_ledger as ledger
from jarvis.state import reminders as reminder_state
from jarvis.state import things
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from jarvis.shared.clock import Clock

LOGGER = logging.getLogger(__name__)

REF_PREFIX: Final[str] = "job:"
"""A thing made for an interview has ``source_ref`` ``job:<application id>``."""
_ACTION_ID: Final[str] = "job-interview-reminders"
_LOG_WAIT_S: Final[float] = 5.0


@dataclass(frozen=True)
class InterviewSettings:
    """The ``job_mail.interview_reminders`` block of ``config/jarvis.yaml`` once validated."""

    evening_at: time_of_day
    before_min: int


def local_zone() -> str:
    """The machine's IANA zone name (``TZ``, else the ``/etc/localtime`` link), or ``UTC``."""
    return zone_key(system_zone()) or "UTC"


def _at(text: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)).astimezone()
    except ValueError:
        return None


def _span(one: things.Thing) -> tuple[datetime | None, datetime | None]:
    """When a thing starts and ends, as instants, so two spellings of one moment are the same."""
    return _at(one.start_at) if one.start_at else None, _at(one.end_at) if one.end_at else None


def _wrote_note(note: str) -> bool:
    """Whether a note is one this pass wrote (``job.thing.note``, in either language)."""
    wordings = (text.partition("{when}") for text in lang.TEXT["job.thing.note"].values())
    return any(note.startswith(head) and note.endswith(tail) for head, _, tail in wordings)


def _he_closed(one: things.Thing) -> bool:
    """Whether the thing is closed and the last word on it was not the mail's."""
    return one.state != "open" and one.history[-1].by != "mail"


def _he_cancelled(one: things.Thing) -> bool:
    """Whether Allen (or her) cancelled the interview, rather than the mail taking it back."""
    return one.state == "cancelled" and one.history[-1].by != "mail"


class InterviewReminders:
    """The reconciler and the user's cancel; built once beside the job-mail poller."""

    def __init__(
        self, settings: InterviewSettings, db: Path, event_log: Path, clock: Clock,
    ) -> None:
        """``db`` is memory.db (the job ledger), ``event_log`` the log the things are written to.

        ``clock`` is the runtime's: its zone is his, where an interview's time of day is read.
        """
        self._settings, self._db, self._event_log = settings, db, event_log
        self._lock = threading.Lock()
        self._clock = clock

    def now(self) -> datetime:
        """The wall time, aware, in UTC."""
        return self._clock.now().astimezone(UTC)

    # --- the pass ------------------------------------------------------------------------

    def reconcile(self, applications: Sequence[Mapping[str, Any]]) -> None:
        """Make the ledger's interview things say the applications' times; each fails on its own."""
        now = self.now()
        wanted: dict[str, tuple[Mapping[str, Any], datetime]] = {}
        for app in applications:
            at = _at(app["next_event_at"]) if app["next_event_at"] else None
            if at is not None and at > now and app["status"] != "rejected":
                wanted[app["id"]] = (app, at)
        with self._lock, contextlib.closing(self._log()) as conn:
            held = self._held(conn)
            for app, at in wanted.values():
                self._guarded(self._arm, conn, held, app, at, now)
            for app_id, one in list(held.items()):
                if app_id not in wanted and one.state == "open":
                    self._guarded(self._disarm, conn, one, now)
            for app_id, row in ledger.interview_rows(self._db).items():
                self._guarded(self._carry_over, conn, held, app_id, row, now)

    def cancel(self, app_id: str) -> None:
        """Allen's undo: cancel the thing and its rings; that time is not made again.

        A new time makes a new thing. ``LookupError`` when no interview is armed for the
        application.
        """
        with self._lock, contextlib.closing(self._log()) as conn:
            one = self._held(conn).get(app_id)
            if one is None or (one.state != "open" and not _he_cancelled(one)):
                msg = f"no reminders for application {app_id}"
                raise LookupError(msg)
            if one.state == "open":
                things.close(
                    conn, one.thing_id, "cancelled", why="cancelled on the Jobs card", by="him",
                    evidence=one.title, ref=app_id, zone=self._clock.zone, now=self.now(),
                )

    def views(self) -> dict[str, dict[str, Any]]:
        """What the Jobs card says is armed, by application id: the time, which rings, the undo.

        An interview ahead (or one Allen cancelled) has an entry; ``outlook`` says an Outlook
        event Jarvis wrote earlier is still on the thing.
        """
        now = self.now()
        with contextlib.closing(self._log()) as conn:
            held = self._held(conn)
            rings = [
                rid for rid, one in reminder_state.fold(conn).items()
                if rid.startswith(things.RING_PREFIX) and not one.cancelled
            ]
        found: dict[str, dict[str, Any]] = {}
        for app_id, one in held.items():
            at = _at(one.start_at)
            if not (_he_cancelled(one) or (one.state == "open" and at is not None and at > now)):
                continue
            prefix = f"{things.RING_PREFIX}{one.thing_id.removeprefix(things.ID_PREFIX)[:12]}-"
            lit = {rid.rpartition("-")[2] for rid in rings if rid.startswith(prefix)}
            found[app_id] = {
                "at": one.start_at,
                "evening": "0" in lit,
                "before": "1" in lit,
                "evening_at": self._settings.evening_at.strftime("%H:%M"),
                "before_min": self._settings.before_min,
                "outlook": bool(one.outlook_id),
                "cancelled": one.state == "cancelled",
            }
        return found

    # --- one application -----------------------------------------------------------------

    @staticmethod
    def _guarded(step: Callable[..., None], *args: Any) -> None:  # noqa: ANN401 - the step's own
        try:
            step(*args)
        except Exception:
            LOGGER.exception("job mail: interview things failed for one application")

    @staticmethod
    def _held(conn: sqlite3.Connection) -> dict[str, things.Thing]:
        """The newest thing made for each application's interview, whatever became of it."""
        return {
            one.source_ref.removeprefix(REF_PREFIX): one
            for one in things.fold(conn).values()
            if one.source == "mail" and one.source_ref.startswith(REF_PREFIX)
        }

    def _arm(
        self,
        conn: sqlite3.Connection,
        held: dict[str, things.Thing],
        app: Mapping[str, Any],
        at: datetime,
        now: datetime,
    ) -> None:
        """Make the application's thing say the interview: add it, move it, or change its words."""
        want, evidence, ref = self._thing(app, at)
        write: dict[str, Any] = {
            "by": "mail", "evidence": evidence, "ref": ref, "zone": self._clock.zone, "now": now,
        }
        one = held.get(app["id"])
        if one is not None and one.state != "open":
            if _he_closed(one) and _span(one)[0] == _span(want)[0]:
                return  # he cancelled or finished it for this time
            one = None
        if one is None:
            held[app["id"]], _ = things.add(conn, want, **write)
            return
        if _span(one) != _span(want):
            one = things.move(
                conn, one.thing_id, start_at=want.start_at, end_at=want.end_at, **write,
            )
        fields = {
            name: getattr(want, name) for name in ("title", "place", "link", "rings")
            if getattr(want, name) != getattr(one, name)
        }
        if want.note != one.note and (not one.note or _wrote_note(one.note)):
            fields["note"] = want.note  # a note of his own is never replaced
        if fields:
            one = things.change(conn, one.thing_id, fields, **write)
        held[app["id"]] = one

    def _disarm(self, conn: sqlite3.Connection, one: things.Thing, now: datetime) -> None:
        """The application is rejected, hidden or has no time: cancel the thing made for it.

        A time already past was an interview that happened: nothing is undone.
        """
        at = _at(one.start_at)
        if at is None or at <= now:
            return
        things.close(
            conn, one.thing_id, "cancelled", why="the interview is no longer on", by="mail",
            evidence=one.title, ref=one.source_ref, zone=self._clock.zone, now=now,
        )

    def _carry_over(
        self,
        conn: sqlite3.Connection,
        held: dict[str, things.Thing],
        app_id: str,
        row: dict[str, Any],
        now: datetime,
    ) -> None:
        """Settle what ADR 0186 armed for the application before the ledger, then forget it."""
        known = reminder_state.fold(conn)
        for key in ("evening_id", "before_id"):
            old = known.get(row.get(key) or "")
            if old is not None and old.pending:
                reminder_state.cancel(conn, old.reminder_id, action_id=_ACTION_ID)
        one = held.get(app_id)
        if one is not None and one.state == "open":
            write: dict[str, Any] = {
                "evidence": one.title, "ref": app_id, "zone": self._clock.zone, "now": now,
            }
            if row["outlook_id"] and one.outlook_id != row["outlook_id"]:
                one = things.change(
                    conn, one.thing_id, {"outlook_id": row["outlook_id"]}, by="mail", **write,
                )
            if row["cancelled"] and _at(row["event_at"]) == _at(one.start_at):
                things.close(
                    conn, one.thing_id, "cancelled", why="cancelled on the Jobs card", by="him",
                    **write,
                )
        ledger.drop_interview(self._db, app_id)

    # --- the thing -----------------------------------------------------------------------

    def _thing(self, app: Mapping[str, Any], at: datetime) -> tuple[things.Thing, str, str]:
        """The thing an application's next interview is, with the evidence and mail it came from.

        Its title is the interview and who it is with; its place is how it is held (the platform
        online, the address on site); its end is the invitation's range when it names one. It
        starts at ``at``, the earlier reading, and its note names both when the mail's zone label
        reads two ways (ADR 0379).
        """
        interview = app.get("interview") or {}
        mail = next((m for m in app["mails"] if _at(m["event_at"]) == at), None)
        said = (mail["event_text"] or "") if mail else ""
        minutes = triage.event_minutes(said)
        local = at.astimezone(self._clock.zone)
        later = _at(app.get("next_event_later_at"))
        note = (
            lang.t(
                "job.thing.note",
                when=zone_label.when_text(local, later, self._clock.zone, sentence=said),
            )
            if later is not None
            else ""
        )
        title = (
            lang.t("job.thing.title_role", company=app["company"], role=app["role"])
            if app["role"]
            else lang.t("job.thing.title", company=app["company"])
        )[: things.TITLE_CHARS]
        place = (
            interview.get("platform") if interview.get("mode") == "online"
            else interview.get("location") if interview.get("mode") == "onsite" else None
        )
        want = things.Thing(
            things.new_id(), title, "go",
            start_at=local.isoformat(timespec="seconds"),
            end_at=(
                (local + timedelta(minutes=minutes)).isoformat(timespec="seconds")
                if minutes else ""
            ),
            place=place or "", link=interview.get("join_url") or "", note=note,
            rings=(
                f"-1d@{self._settings.evening_at:%H:%M}", f"-{self._settings.before_min}m",
            ),
            source="mail", source_ref=f"{REF_PREFIX}{app['id']}",
        )
        if mail is None:
            return want, title, ""
        return want, mail["event_text"] or mail["subject"] or title, mail["message_id"]

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)
