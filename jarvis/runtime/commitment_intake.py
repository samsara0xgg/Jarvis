"""The commitment poller (ADR 0274): reads his mail for things he has to go to.

Every ``poll_s`` it lists the mail since ``backfill_since`` newest first, skips what it has read
(``commitment_mail`` in memory.db), and for each new letter: sets aside what the job ledger types
as an interview or offer (ADR 0186 keeps those), reads the kept body or fetches it once, drops
what has no sign of a date or an hour, and asks the ``commitments.preset`` model about the rest.
The model is shown the open mail-made ``go`` things the ledger holds (ADR 0299) and its actions
are written to that ledger: a new one is a ``go`` thing made by ``mail``, a changed time a move, a
changed word a change, a called-off one a cancel. A mail-made thing starts unconfirmed unless its
sender's rule is ``always``; a sender whose rule is ``never`` is not read. The only Gmail tools
used are ``gmail_search`` and ``gmail_get`` (the guard of :func:`job_mail.gmail_read`). The rings
are the ledger's own (``things.sync_rings``); this module arms nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.decision import commitment_mail as triage
from jarvis.execution.tools import ToolError
from jarvis.runtime.home import MAIL_SERVER, _letter, mail_body
from jarvis.runtime.interview_reminders import REF_PREFIX, local_zone
from jarvis.runtime.job_mail import gmail_read
from jarvis.state import commitment_mail as seen
from jarvis.state import commitments, things
from jarvis.state import job_ledger as ledger
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.import_parsers import is_daily_subject

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)

_QUERY: Final[str] = "after:{day:%Y/%m/%d} -in:sent -in:drafts"
_SEARCH_LIMIT: Final[int] = 100
# At most this many result pages are read in one cycle.
# ponytail: paging stops at the first page with nothing unread, so a mail that errored and sits
# behind a whole page of read ones is not retried. Upgrade: keep the errored ids and fetch them
# directly.
_MAX_PAGES: Final[int] = 30
# While unread mail is left over the cap, the next cycle follows this soon.
_CATCH_UP_S: Final[float] = 5.0
# A mail this young that the job ledger has not seen yet is left for the next cycle: job mail may
# be about to call it an interview, and that has its own reminders.
_JOB_FIRST: Final[timedelta] = timedelta(minutes=30)
_LOG_WAIT_S: Final[float] = 5.0


@dataclass(frozen=True)
class CommitmentSettings:
    """The ``commitments:`` block of ``config/jarvis.yaml`` once validated."""

    poll_s: float
    backfill_since: date
    max_mails_per_cycle: int
    preset: str
    remind_before_min: int


class _ModelFailed(Exception):  # noqa: N818 - the model did not answer; the cycle stops
    """The model call raised; the mail is recorded as an error and the cycle ends."""


class CommitmentIntake:
    """The poller; built once at boot."""

    def __init__(
        self,
        settings: CommitmentSettings,
        connections: PluginConnections,
        db_path: Path,
        event_log: Path,
        ask: Callable[[str, str], str] | None,
    ) -> None:
        """``db_path`` is memory.db, ``event_log`` the log; ``ask(system, user)`` is the model.

        With no ``ask`` (the preset is missing or malformed) nothing is read.
        """
        self._settings, self._db, self._event_log = settings, db_path, event_log
        self._connections, self._ask = connections, ask
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self.zone = local_zone()
        self._warned: set[str] = set()

    async def run(self) -> None:
        """Poll every ``poll_s``, first at once; a failed cycle is logged, never ends the loop."""
        LOGGER.info("commitments started (every %.0f s)", self._settings.poll_s)
        try:
            while True:
                done = 0
                try:
                    done = await asyncio.to_thread(self.poll_once)
                except Exception:
                    LOGGER.exception("commitments: cycle failed; trying again next time")
                capped = done >= self._settings.max_mails_per_cycle
                await asyncio.sleep(_CATCH_UP_S if capped else self._settings.poll_s)
        except asyncio.CancelledError:
            LOGGER.info("commitments cancelled")
            raise

    # --- reading the mail ----------------------------------------------------------------

    def poll_once(self) -> int:
        """One cycle on the calling thread; returns how many letters it recorded."""
        if self._ask is None:
            self._warn_once("no_model", "the preset is not usable; nothing is read")
            return 0
        try:
            servers = self._connections.client_for(MAIL_SERVER)
        except ToolError:
            self._warn_once("no_gmail", "the gmail server is not connected; nothing is read")
            return 0
        done = 0
        with contextlib.closing(self._log()) as conn:
            for message_id in self._unseen(servers):
                now = self.now()
                try:
                    read = self._read(servers, conn, message_id, now)
                except _ModelFailed:
                    LOGGER.warning("commitments: the model did not answer", exc_info=True)
                    seen.record(self._db, message_id, "error", now)
                    break  # an outage costs one mail one try, not the whole backlog
                except Exception:  # noqa: BLE001 - one unreadable letter must not stop the others
                    LOGGER.warning("commitments: cannot read %s", message_id, exc_info=True)
                    seen.record(self._db, message_id, "error", now)
                else:
                    if read is None:
                        continue  # left unread for a later cycle
                    verdict, given, said = read
                    seen.record(
                        self._db, message_id, verdict, now, input_json=given, output_json=said,
                    )
                done += 1
        return done

    def _unseen(self, servers: McpServers) -> list[str]:
        """The oldest ``max_mails_per_cycle`` ids not read yet, oldest first.

        Mail is read in the order it came, so a reminder or a cancellation is read after the mail it
        is about. The listing is newest first and paged until a page holds nothing unread.
        """
        query = _QUERY.format(day=self._settings.backfill_since)
        known = seen.seen_ids(self._db)
        want = self._settings.max_mails_per_cycle
        fresh: list[str] = []
        page = ""
        for _ in range(_MAX_PAGES):
            args: dict[str, Any] = {"query": query, "maxResults": _SEARCH_LIMIT}
            if page:
                args["pageToken"] = page
            reply = gmail_read(servers, "gmail_search", args)
            listed = [str(h["id"]) for h in reply.get("messages") or []]
            unread = [one for one in listed if one not in known]
            fresh += unread
            page = str(reply.get("nextPageToken") or "")
            if not unread or not page:
                break
        # a mail arriving mid-listing shifts a page, so a repeated id is dropped
        return list(dict.fromkeys(fresh))[::-1][:want]

    def _read(
        self, servers: McpServers, conn: sqlite3.Connection, message_id: str, now: datetime,
    ) -> tuple[str, str, str] | None:
        """One letter to ``(verdict, what the model was given, what it said)``; None to wait."""
        args = {"messageId": message_id, "format": "metadata"}
        head = _letter(gmail_read(servers, "gmail_get", args))
        domain = head["address"].rpartition("@")[2].lower()
        if is_daily_subject(head["subject"]):
            return "filtered", "", ""  # his own Claude daily letter (ADR 0295), not mail to read
        if ledger.is_interview_mail(self._db, message_id):
            return "interview", "", ""  # ADR 0186 has its reminders
        sent = datetime.fromisoformat(head["received"]) if head["received"] else None
        young = sent is not None and now - sent < _JOB_FIRST
        if young and not ledger.has_seen_row(self._db, message_id):
            return None  # job mail has not looked at it yet
        rule = commitments.sender_rules(conn).get(domain)
        if rule == "never":
            return "filtered", "", ""
        body = ledger.body_excerpt(self._db, message_id) or self._full_body(servers, message_id)
        if not triage.may_hold_time(head["subject"], body):
            return "filtered", "", ""
        received = head["received"] or now.isoformat()
        mail = triage.Mail(head["from"], domain, head["subject"], received, body)
        zone = ZoneInfo(self.zone)
        held = {
            one.thing_id.removeprefix(things.ID_PREFIX): one
            for one in things.open_things(conn)
            if one.kind == "go" and one.source == "mail"
            and not one.source_ref.startswith(REF_PREFIX)  # an interview is ADR 0186's
        }
        shown = triage.related(
            [
                triage.Known(key, one.title, _start_at(one, zone), one.weekly_until, one.sender)
                for key, one in held.items()
            ],
            domain,
            datetime.fromisoformat(received),
        )
        today = now.astimezone(zone).date()
        system, user = triage.build_prompt(mail, shown, zone=self.zone, today=today)
        assert self._ask is not None  # noqa: S101 - poll_once returned early without it
        try:
            said = self._ask(system, user)
        except Exception as exc:
            raise _ModelFailed from exc
        actions = triage.parse_actions(
            said, {k.commitment_id for k in shown}, zone=zone, today=today,
        )
        applied = self._apply(conn, actions, held, mail, message_id, always=rule == "always")
        return ("noted" if applied else "none"), json.dumps({"user": user}), said

    @staticmethod
    def _full_body(servers: McpServers, message_id: str) -> str:
        """One ``gmail_get`` in full format as plain text, cut to what the model is given."""
        message = gmail_read(servers, "gmail_get", {"messageId": message_id, "format": "full"})
        return mail_body(str(message.get("body") or ""))[: triage.BODY_CHARS]

    # --- writing the ledger --------------------------------------------------------------

    def _apply(  # noqa: PLR0913 - the actions and what they act on
        self,
        conn: sqlite3.Connection,
        actions: list[triage.Action],
        held: Mapping[str, things.Thing],
        mail: triage.Mail,
        message_id: str,
        *,
        always: bool,
    ) -> int:
        """Write the actions to the ledger; returns how many changed anything.

        ``held`` is the open mail-made ``go`` things by the hex the model answers with.
        """
        zone, now = ZoneInfo(self.zone), self.now()
        applied = 0
        for action in actions:
            fields = action.fields
            write: dict[str, Any] = {
                "by": "mail", "evidence": fields.get("evidence") or mail.subject,
                "ref": message_id, "zone": zone, "now": now,
            }
            if action.action == "new":
                first = datetime.fromisoformat(fields["start_at"])
                if any(
                    one.sender == mail.sender_domain and _start(one, zone) == first
                    for one in held.values()
                ):
                    continue  # a reminder mail for one already held, said as new
                _, made = things.add(conn, self._new(fields, mail, message_id, always), **write)
                applied += made
            elif action.action == "update":
                applied += self._update(conn, held[action.commitment_id], fields, zone, write)
            else:
                things.close(
                    conn, held[action.commitment_id].thing_id, "cancelled",
                    why="called off in a mail", **write,
                )
                applied += 1
        return applied

    def _new(
        self,
        fields: Mapping[str, Any],
        mail: triage.Mail,
        message_id: str,
        always: bool,  # noqa: FBT001 - the sender's rule
    ) -> things.Thing:
        """The ``go`` thing a ``new`` action says; a whole day is its ``on_day``."""
        whole = bool(fields.get("all_day"))
        start_at = "" if whole else fields["start_at"]
        on_day = datetime.fromisoformat(fields["start_at"]).date().isoformat() if whole else ""
        return things.Thing(
            things.new_id(), fields["title"], "go", start_at=start_at,
            end_at=fields.get("end_at", ""), on_day=on_day,
            weekly_until=fields.get("weekly_until", ""),
            skip_dates=tuple(fields.get("skip_dates", ())),
            place=fields.get("place", ""), link=fields.get("link", ""),
            rings=things.default_rings(
                "go", start_at=start_at, on_day=on_day,
                before_min=self._settings.remind_before_min,
            ),
            private=fields.get("private", False), confirmed=always, source="mail",
            source_ref=message_id, sender=mail.sender_domain,
        )

    def _update(
        self,
        conn: sqlite3.Connection,
        old: things.Thing,
        fields: Mapping[str, Any],
        zone: ZoneInfo,
        write: Mapping[str, Any],
    ) -> int:
        """Move and change a held thing to what an ``update`` says; 0 when it says what it has."""
        moved = _moved(old, fields)
        changed: dict[str, Any] = {
            name: fields[name] for name in ("title", "place", "link", "weekly_until")
            if name in fields and fields[name] != getattr(old, name)
        }
        if "skip_dates" in fields and tuple(fields["skip_dates"]) != old.skip_dates:
            changed["skip_dates"] = list(fields["skip_dates"])
        if fields.get("private") and not old.private:  # a mail can make it private, never public
            changed["private"] = True
        if moved and bool(moved["on_day"]) != bool(old.on_day):  # a day became an hour, or back
            changed["rings"] = list(things.default_rings(
                "go", start_at=moved["start_at"], on_day=moved["on_day"],
                before_min=self._settings.remind_before_min,
            ))
        # A last date before the first day reads back only once the series has moved.
        change_first = (
            not moved or "weekly_until" not in changed
            or changed["weekly_until"] >= _first_day(old, zone)
        )
        if changed and change_first:
            things.change(conn, old.thing_id, changed, **write)
        if moved:
            things.move(
                conn, old.thing_id, start_at=moved["start_at"], end_at=moved["end_at"],
                on_day=moved["on_day"], **write,
            )
        if changed and not change_first:
            things.change(conn, old.thing_id, changed, **write)
        return int(bool(moved or changed))

    # --- plumbing ------------------------------------------------------------------------

    def _warn_once(self, kind: str, what: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            LOGGER.warning("commitments: %s", what)

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)


def _start(one: things.Thing, zone: ZoneInfo) -> datetime | None:
    """When a ``go`` thing starts: its ``start_at``, or the local midnight of its ``on_day``."""
    if one.start_at:
        return datetime.fromisoformat(one.start_at)
    if one.on_day:
        return datetime.combine(date.fromisoformat(one.on_day), clock(), zone)
    return None


def _start_at(one: things.Thing, zone: ZoneInfo) -> str:
    """:func:`_start` as the model is shown it; empty for a thing with no start."""
    start = _start(one, zone)
    return "" if start is None else start.isoformat(timespec="seconds")


def _first_day(one: things.Thing, zone: ZoneInfo) -> str:
    """The date a ``go`` thing starts on, in his zone."""
    return one.on_day or datetime.fromisoformat(one.start_at).astimezone(zone).date().isoformat()


def _moved(old: things.Thing, fields: Mapping[str, Any]) -> dict[str, str] | None:
    """The time an ``update`` gives, as ``things.move`` takes it; None when it is the time held.

    A new start with no new end keeps the length the held one had.
    """
    if "start_at" not in fields and "end_at" not in fields:
        return None
    if fields.get("all_day", bool(old.on_day)):
        moved = {
            "start_at": "", "end_at": "",
            "on_day": fields["start_at"][:10] if "start_at" in fields else old.on_day,
        }
    else:
        start = fields.get("start_at") or old.start_at
        end = fields.get("end_at") or ("" if "start_at" in fields else old.end_at)
        if "start_at" in fields and not end and old.end_at and old.start_at:
            span = datetime.fromisoformat(old.end_at) - datetime.fromisoformat(old.start_at)
            end = (datetime.fromisoformat(start) + span).isoformat(timespec="seconds")
        moved = {"start_at": start, "end_at": end, "on_day": ""}
    if not (moved["start_at"] or moved["on_day"]):
        return None
    if _when(**moved) == _when(old.start_at, old.end_at, old.on_day):
        return None
    return moved


def _when(
    start_at: str, end_at: str, on_day: str,
) -> tuple[datetime | None, datetime | None, str]:
    """A time as instants, so two spellings of one moment are the same."""
    return (
        datetime.fromisoformat(start_at) if start_at else None,
        datetime.fromisoformat(end_at) if end_at else None,
        on_day,
    )
