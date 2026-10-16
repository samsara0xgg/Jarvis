"""The commitment poller (ADR 0274): reads his mail for things he has to go to.

Every ``poll_s`` it lists the mail since ``backfill_since`` newest first, skips what it has read
(``commitment_mail`` in memory.db), and for each new letter: sets aside what the job ledger types
as an interview or offer (ADR 0186 reminds those), reads the kept body or fetches it once, drops
what has no sign of a date or an hour, and asks the ``commitments.preset`` model about the rest.
Its actions become ``commitment.*`` events. A mail-made commitment starts unconfirmed unless its
sender's rule is ``always``; a sender whose rule is ``never`` is not read. The only Gmail tools
used are ``gmail_search`` and ``gmail_get`` (the guard of :func:`job_mail.gmail_read`).

After each cycle one pass arms the reminders of the next seven days.
# ponytail: that pass is the interim nudge, replaced by the day plan's own nudges (design step 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
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
from jarvis.runtime.interview_reminders import local_zone
from jarvis.runtime.job_mail import gmail_read
from jarvis.shared import lang
from jarvis.state import commitment_mail as seen
from jarvis.state import commitments
from jarvis.state import job_ledger as ledger
from jarvis.state import reminders as reminder_state
from jarvis.state.event_log import emit_event, open_runtime_event_log

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
_AHEAD: Final[timedelta] = timedelta(days=7)
_ACTION_ID: Final[str] = "commitment-reminders"
# A reminder this module armed has this id prefix; no other reminder does.
_RID: Final[str] = commitments.REMINDER_PREFIX
# A mail this young that the job ledger has not seen yet is left for the next cycle: job mail may
# be about to call it an interview, and that has its own reminders.
_JOB_FIRST: Final[timedelta] = timedelta(minutes=30)
_EVENING: Final[clock] = clock(20, 0)
_PLACE_SHORT: Final[int] = 40
_LOG_WAIT_S: Final[float] = 5.0
_FIELDS: Final[tuple[str, ...]] = (
    "title", "start_at", "end_at", "weekly_until", "skip_dates", "place", "link", "private",
    "evidence",
)


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
    """The poller and the interim reminder pass; built once at boot."""

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
                try:
                    await asyncio.to_thread(self.reconcile)
                except Exception:
                    LOGGER.exception("commitments: the reminder pass failed")
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
        live = {c.commitment_id: c for c in commitments.live(conn)}
        shown = triage.related(
            [
                triage.Known(c.commitment_id, c.title, c.start_at, c.weekly_until, c.sender_domain)
                for c in live.values()
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
        applied = self._apply(conn, actions, live, domain, message_id, always=rule == "always")
        return ("noted" if applied else "none"), json.dumps({"user": user}), said

    @staticmethod
    def _full_body(servers: McpServers, message_id: str) -> str:
        """One ``gmail_get`` in full format as plain text, cut to what the model is given."""
        message = gmail_read(servers, "gmail_get", {"messageId": message_id, "format": "full"})
        return mail_body(str(message.get("body") or ""))[: triage.BODY_CHARS]

    @staticmethod
    def _apply(  # noqa: PLR0913 - the actions and what they act on
        conn: sqlite3.Connection,
        actions: list[triage.Action],
        live: Mapping[str, commitments.Commitment],
        domain: str,
        message_id: str,
        *,
        always: bool,
    ) -> int:
        """Write the actions as events; returns how many changed anything."""
        applied = 0
        for action in actions:
            fields = action.fields
            if action.action == "new":
                first = datetime.fromisoformat(fields["start_at"])
                if any(
                    c.sender_domain == domain and datetime.fromisoformat(c.start_at) == first
                    for c in live.values()
                ):
                    continue  # a reminder mail for one already held, said as new
                record = commitments.Commitment(
                    commitment_id=commitments.new_id(),
                    title=fields["title"],
                    start_at=fields["start_at"],
                    source="mail",
                    confirmed=always,
                    private=fields.get("private", False),
                    end_at=fields.get("end_at", ""),
                    weekly_until=fields.get("weekly_until", ""),
                    skip_dates=tuple(fields.get("skip_dates", ())),
                    all_day=fields.get("all_day", False),
                    place=fields.get("place", ""),
                    link=fields.get("link", ""),
                    source_ref=message_id,
                    sender_domain=domain,
                    evidence=fields.get("evidence", ""),
                )
                commitments.note(conn, record)
            elif action.action == "update":
                old = live[action.commitment_id]
                changed = dict(fields)
                if "skip_dates" in changed:
                    changed["skip_dates"] = tuple(changed["skip_dates"])
                if "start_at" in changed and "end_at" not in changed and old.end_at:
                    start = datetime.fromisoformat(changed["start_at"])
                    changed["end_at"] = (start + timedelta(minutes=old.minutes)).isoformat(
                        timespec="seconds",
                    )
                changed["private"] = old.private or bool(changed.get("private"))
                record = dataclasses.replace(old, **changed)
                if record.payload() == old.payload():
                    continue
                commitments.note(conn, record)
            else:
                commitments.drop(conn, action.commitment_id, "cancelled")
            applied += 1
        return applied

    # --- the interim reminders -----------------------------------------------------------

    def reconcile(self) -> None:
        """Make the pending reminders match the next seven days; running it twice changes nothing.

        One reminder, ``remind_before_min`` before each occurrence still ahead (an all-day one at
        20:00 the evening before). One that no occurrence wants any more (dropped, changed,
        skipped) is cancelled.
        """
        now = self.now()
        zone = ZoneInfo(self.zone)
        wanted: dict[str, tuple[datetime, str]] = {}
        with contextlib.closing(self._log()) as conn:
            for occ in commitments.occurrences(
                conn, int(now.timestamp() * 1000), int((now + _AHEAD).timestamp() * 1000),
                zone=zone,
            ):
                start = datetime.fromtimestamp(occ.start_ms / 1000, zone)
                if start > now:  # one that has begun is not reminded of
                    tail = occ.commitment.commitment_id.removeprefix("commitment-")[:12]
                    due = (
                        datetime.combine(start.date() - timedelta(days=1), _EVENING, zone)
                        if occ.commitment.all_day
                        else start - timedelta(minutes=self._settings.remind_before_min)
                    )
                    line = self._line(occ.commitment, start)
                    wanted[f"{_RID}{tail}-{occ.start_ms}"] = (due, line)
            known = reminder_state.fold(conn)
            for rid, held in known.items():
                if rid.startswith(_RID) and held.pending and rid not in wanted:
                    emit_event(
                        conn, type="reminder.cancelled", actor="jarvis_runtime",
                        payload={"reminder_id": rid, "action_id": _ACTION_ID},
                    )
            for rid, (due, text) in wanted.items():
                one = known.get(rid)
                if due <= now and one is None:
                    continue  # that moment has passed: skipped, not made late
                if one is None or one.cancelled or (one.pending and one.text != text):
                    emit_event(
                        conn, type="reminder.scheduled", actor="jarvis_runtime",
                        payload={
                            "reminder_id": rid,
                            "due_at_epoch_ms": int(due.timestamp() * 1000),
                            "due_at_local": due.isoformat(timespec="seconds"),
                            "text": text,
                            "action_id": _ACTION_ID,
                        },
                    )

    @staticmethod
    def _line(c: commitments.Commitment, start: datetime) -> str:
        """The reminder's words: a private commitment is only its time (an all-day one, nothing)."""
        at = f"{start:%H:%M}"
        place = f" · {c.place}" if c.place and len(c.place) <= _PLACE_SHORT else ""
        if c.all_day:
            if c.private:
                return lang.t("commitment.rem.private_day")
            return lang.t("commitment.rem.day", title=c.title, place=place)
        if c.private:
            return lang.t("commitment.rem.private", time=at)
        return lang.t("commitment.rem.line", time=at, title=c.title, place=place)

    # --- plumbing ------------------------------------------------------------------------

    def _warn_once(self, kind: str, what: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            LOGGER.warning("commitments: %s", what)

    def _log(self) -> sqlite3.Connection:
        return open_runtime_event_log(self._event_log, deadline=time.monotonic() + _LOG_WAIT_S)
