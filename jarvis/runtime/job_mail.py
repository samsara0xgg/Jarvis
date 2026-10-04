"""The job-mail poller (ADR 0155): reads Allen's mail in the background and keeps the job ledger.

Every ``poll_s`` it lists recent mail through the shared ``gmail`` MCP client, asks Jev about the
letters it has not seen (header first, then the body of what is left), writes the typed facts to
the ledger and raises an alert by the rule: a receipt is ledger only, a rejection a card, other
job mail a card with sound, an interview or offer a card and one fixed spoken line. The only
Gmail tools used are ``gmail_search`` and ``gmail_get``: nothing is ever modified, labelled,
trashed or sent from here. What a client shows of the alerts is held by the quiet level.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import job_mail as triage
from jarvis.decision.surrogate_route import KEY_ENV
from jarvis.execution.tools import ToolError
from jarvis.runtime import audio_output
from jarvis.runtime.home import MAIL_SERVER, _gmail, _letter, mail_body
from jarvis.shared import lang
from jarvis.state import job_ledger as ledger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.decision.attention import Judge
    from jarvis.decision.surrogate_route import SurrogateRoute
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.plugin_connections import PluginConnections

LOGGER = logging.getLogger(__name__)

# The only Gmail tools this module may call; a mail is read, never changed.
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset({"gmail_search", "gmail_get"})
_QUERY: Final[str] = "newer_than:{days}d -in:sent -in:drafts"
_SEARCH_LIMIT: Final[int] = 100
_IGNORED_AFTER: Final[timedelta] = timedelta(minutes=30)
_REACTIONS: Final[frozenset[str]] = frozenset({"right", "dismissed"})
_ALERT_LEVELS: Final[frozenset[str]] = frozenset({"card", "card_sound", "speak"})
# The channel is called down after this many failed cycles in a row, or this long without a good
# one, and says so at most once per ``_HEALTH_EVERY``.
_HEALTH_FAILURES: Final[int] = 3
_HEALTH_AFTER: Final[timedelta] = timedelta(hours=2)
_HEALTH_EVERY: Final[timedelta] = timedelta(hours=12)
_LEVEL_PREFIX: Final[str] = "level:"


@dataclass(frozen=True)
class JobMailSettings:
    """The ``job_mail:`` block of ``config/jarvis.yaml`` once validated."""

    poll_s: float
    backfill_days: int
    max_messages_per_cycle: int
    header_skip_at: float
    body_min: float
    max_body_chars: int
    max_calls_per_day: int
    speak: bool
    speak_gap_s: float


def gmail_read(servers: McpServers, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """One Gmail read; any tool outside ``READ_ONLY_TOOLS`` is refused before it is sent."""
    if tool not in READ_ONLY_TOOLS:
        msg = f"job mail may only read Gmail, not call {tool!r}"
        raise ValueError(msg)
    return _gmail(servers, tool, args)


class JobMail:
    """The poller and what the surface reads of it; built once at boot."""

    def __init__(
        self,
        settings: JobMailSettings,
        route: SurrogateRoute,
        connections: PluginConnections,
        db_path: Path,
        judge: Judge,
    ) -> None:
        """``route`` carries Jev's model and timeout; ``db_path`` is memory.db.

        ``judge`` decides how loudly each typed letter reaches Allen (ADR 0155).
        """
        self._settings = settings
        self._judge = judge
        self._connections = connections
        self._db = db_path
        self._jev = triage.JobMailJev(
            route,
            header_skip_at=settings.header_skip_at,
            body_min=settings.body_min,
            max_body_chars=settings.max_body_chars,
            max_calls_per_day=settings.max_calls_per_day,
        )
        self._route = route
        self._last_spoke: float | None = None
        self._warned: set[str] = set()
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._failures = 0
        self._last_ok = self.now()
        # Wired by the daemon: the quiet level now, whether speaking is allowed (speech not
        # muted, no live conversation) and the one fixed line. Until then she never speaks.
        self.quiet: Callable[[], str] = lambda: "dnd"
        self.may_speak: Callable[[], bool] = lambda: False
        self.say: Callable[[], None] | None = None
        # Where sound would come out now (``fresh=True`` skips the two-second cache); unprompted
        # sound happens only on private output, so tests and the daemon may swap this.
        self.output: Callable[..., dict[str, Any]] = audio_output.current_output

    # --- the poller ------------------------------------------------------------------

    async def run(self) -> None:
        """Poll every ``poll_s``, first at once; a failed cycle is logged, never ends the loop."""
        LOGGER.info("job mail started (every %.0f s)", self._settings.poll_s)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.poll_once)
                except Exception:
                    LOGGER.exception("job mail: cycle failed; trying again next time")
                await asyncio.sleep(self._settings.poll_s)
        except asyncio.CancelledError:
            LOGGER.info("job mail cancelled")
            raise

    def poll_once(self) -> int:
        """One cycle, on the calling thread; returns how many letters it settled."""
        now = self.now()
        ledger.expire_shown(self._db, now, _IGNORED_AFTER)
        if not os.environ.get(KEY_ENV, "").strip():
            self._warn_once("no_key", "no OPENROUTER_API_KEY; nothing is read")
            return 0
        try:
            servers = self._connections.client_for(MAIL_SERVER)
        except ToolError as exc:
            self._warn_once("no_gmail", "the gmail server is not connected; nothing is read")
            self._failed(exc)
            return 0
        query = {
            "query": _QUERY.format(days=self._settings.backfill_days),
            "maxResults": _SEARCH_LIMIT,
        }
        try:
            hits = gmail_read(servers, "gmail_search", query).get("messages") or []
        except Exception as exc:
            self._failed(exc)
            raise
        self._failures, self._last_ok = 0, now  # a recovery is silent
        seen = ledger.seen_ids(self._db)
        fresh = [str(h["id"]) for h in hits if str(h["id"]) not in seen]
        fresh = fresh[: min(self._settings.max_messages_per_cycle, self._jev.room())]
        if not fresh:
            return 0
        return self._settle(servers, fresh, now)

    def _settle(self, servers: McpServers, ids: list[str], now: datetime) -> int:
        verdicts: dict[str, str] = {}
        chances: dict[str, float] = {}  # Jev's probability that a letter is job mail
        heads = self._heads(servers, ids, verdicts)
        skips = self._jev.skips(heads)
        letters: list[tuple[triage.Head, str]] = []
        for head in heads:
            skip = skips[head.message_id]
            if skip is None:
                verdicts[head.message_id] = "error"
                continue
            chances[head.message_id] = skip.p_job
            if skip.skipped:
                verdicts[head.message_id] = "not_job"
            else:
                letters.append((head, self._body(servers, head, verdicts)))
        letters = [one for one in letters if one[0].message_id not in verdicts]
        letters = letters[: self._jev.room()]  # the rest stay unseen and are asked tomorrow
        typed = self._jev.types(letters)
        for head, _body in letters:
            found = typed[head.message_id]
            if found is None:
                verdicts[head.message_id] = "error"
                continue
            chances[head.message_id] = found.p_job
            if found.kind == "not_job":
                verdicts[head.message_id] = "not_job"
                continue
            ledger.upsert_mail(self._db, triage.as_row(head, found), now)
            verdicts[head.message_id] = "job"
            # Seen before it is delivered: a failure after this line never alerts twice.
            ledger.record_seen(self._db, head.message_id, "job", now, p_job=found.p_job)
            self._deliver(head, found, now)
        by_id = {head.message_id: head for head in heads}
        for message_id, verdict in verdicts.items():
            if verdict == "job":
                continue
            chance = chances.get(message_id)
            seen = by_id.get(message_id)
            audit = None
            if verdict == "not_job" and seen is not None and (chance or 0) >= ledger.AUDIT_MIN:
                audit = {
                    "received_at": seen.received_at,
                    "name": seen.name,
                    "domain": seen.domain,
                    "subject": seen.subject,
                }
            ledger.record_seen(self._db, message_id, verdict, now, p_job=chance, audit=audit)
        counts = {v: list(verdicts.values()).count(v) for v in ("job", "not_job", "error")}
        LOGGER.info(
            "job mail: %d letters settled: %d job, %d not job, %d errors ($%.6f spent so far)",
            len(verdicts),
            counts["job"],
            counts["not_job"],
            counts["error"],
            self._jev.spent_usd,
        )
        return len(verdicts)

    @staticmethod
    def _heads(servers: McpServers, ids: list[str], verdicts: dict[str, str]) -> list[triage.Head]:
        """The header of each letter; one that cannot be read is an error and the rest go on."""
        heads = []
        for message_id in ids:
            args = {"messageId": message_id, "format": "metadata"}
            try:
                heads.append(_head(gmail_read(servers, "gmail_get", args)))
            except Exception:  # noqa: BLE001 - one unreadable letter must not stop the others
                LOGGER.warning("job mail: cannot read the header of %s", message_id, exc_info=True)
                verdicts[message_id] = "error"
        return heads

    def _body(self, servers: McpServers, head: triage.Head, verdicts: dict[str, str]) -> str:
        """The plain-text start of a letter's body; a letter that cannot be read is an error."""
        try:
            message = gmail_read(
                servers, "gmail_get", {"messageId": head.message_id, "format": "full"}
            )
        except Exception:  # noqa: BLE001 - as for the header
            LOGGER.warning("job mail: cannot read the body of %s", head.message_id, exc_info=True)
            verdicts[head.message_id] = "error"
            return ""
        return mail_body(str(message.get("body") or ""))[: self._settings.max_body_chars]

    def _deliver(self, head: triage.Head, typed: triage.Typed, now: datetime) -> None:
        """Build the pack, ask the judge, log the decision and act on its level."""
        local = now.astimezone()
        quiet = self.quiet()
        pack = triage.pack_for(
            head,
            typed,
            now,
            {
                "hour": local.hour,
                "weekday": local.weekday(),
                "quiet": quiet,
                "speech_ok": self.may_speak(),
            },
        )
        judgement = self._judge(pack)
        ledger.log_decision(
            self._db,
            source=pack.source,
            event_id=pack.event_id,
            pack_json=pack.to_json(),
            judge_id=judgement.judge_id,
            judge_version=judgement.judge_version,
            level=judgement.level,
            reason=judgement.reason,
            now=now,
        )
        if judgement.level not in _ALERT_LEVELS:
            ledger.note_delivery(self._db, head.message_id, "ledger_only", now)
            return
        level = judgement.level
        silenced = False
        if level in ledger.SOUNDING:
            # Unprompted sound only on private output: on speakers it is a silent card.
            device = self.output()
            ledger.note_audio(self._db, head.message_id, device)
            LOGGER.info(
                "job mail: %s would sound (%s); output %r is %s",
                head.message_id,
                level,
                device["name"],
                "private" if device["private"] else "not private, so a silent card",
            )
            if not device["private"]:
                level, silenced = "card", True
        title, line = triage.alert_text(typed.kind, typed.facts)
        alert_id = ledger.create_alert(self._db, head.message_id, level, title, line, now)
        if quiet != "off":
            ledger.note_delivery(self._db, head.message_id, "held", now)
        if silenced:
            ledger.note_delivery(self._db, head.message_id, "silenced", now)
        if level != "speak":
            return
        say = self.say
        if say is None or not self._speak_now(quiet):
            ledger.note_delivery(self._db, head.message_id, "suppressed", now)
            return
        try:
            say()
        except Exception:
            LOGGER.exception("job mail: the spoken line failed")
            ledger.note_delivery(self._db, head.message_id, "suppressed", now)
            return
        self._last_spoke = time.monotonic()
        ledger.mark_spoken(self._db, alert_id)
        ledger.note_delivery(self._db, head.message_id, "spoken", now)

    def _speak_now(self, quiet: str) -> bool:
        """Speak only at quiet ``off``, unmuted, outside a live conversation and not too soon.

        The output must also be private right now: a fresh look, not the one from decision time.
        """
        gap = self._settings.speak_gap_s
        rested = self._last_spoke is None or time.monotonic() - self._last_spoke >= gap
        if not (self._settings.speak and quiet == "off" and self.may_speak() and rested):
            return False
        device = self.output(fresh=True)
        if not device["private"]:
            LOGGER.warning("job mail: not spoken, output %r is not private", device["name"])
        return bool(device["private"])

    def _failed(self, exc: BaseException) -> None:
        """Count a failed cycle; raise the one health alert when the channel is down."""
        now = self.now()
        self._failures += 1
        if self._failures < _HEALTH_FAILURES and now - self._last_ok < _HEALTH_AFTER:
            return
        last = ledger.last_health_alert(self._db)
        if last is not None and now - last < _HEALTH_EVERY:
            return
        # The reason is one of three fixed lines: the error's own text is never shown.
        text = str(exc).lower()
        reason = (
            "job.health.auth"
            if any(k in text for k in ("auth", "401", "403", "sign in"))
            else "job.health.timeout"
            if "time" in text
            else "job.health.down"
        )
        ledger.create_alert(
            self._db,
            ledger.HEALTH_ID,
            "card_sound",
            lang.t("job.health.title"),
            lang.t(reason),
            now,
        )
        LOGGER.warning("job mail: the mail channel is down (%s)", reason)

    def _warn_once(self, kind: str, what: str) -> None:
        if kind not in self._warned:
            self._warned.add(kind)
            LOGGER.warning("job mail: %s (logged once)", what)

    # --- what the surface reads ----------------------------------------------------

    def notices(self) -> dict[str, Any]:
        """``GET /inherent/notices``: the alerts a client may show at the quiet level now.

        ``audio_private`` says whether sound may play now; while it is false every alert is served
        as a silent card, so a disconnect between two polls cannot leak a cue.
        """
        private = bool(self.output()["private"])
        shown = ledger.alerts_for_client(self._db, self.quiet(), self.now())
        if not private:
            for notice in shown:
                for one in (notice, *notice.get("items", [])):
                    if one["level"] in ledger.SOUNDING:
                        one["level"] = "card"
        return {"notices": shown, "audio_private": private}

    def act(self, notice_id: str, action: str, reaction: str | None) -> None:
        """``POST /inherent/notices/{id}``: ``seen``, or feedback (``dismissed`` is feedback too).

        An unknown id is a LookupError (404), a reaction that is not one of ``right``,
        ``dismissed`` and ``level:<name>`` a ValueError (400). A digest id stands for its alerts.
        """
        if action == "dismissed":
            reaction = "dismissed"
        if action != "seen" and not _known_reaction(reaction):
            msg = f"not a reaction: {reaction!r}"
            raise ValueError(msg)
        now = self.now()
        alerts = [ledger.get_alert(self._db, one) for one in ledger.alert_ids(notice_id)]
        if not alerts or any(one is None for one in alerts):
            msg = f"no such notice: {notice_id}"
            raise LookupError(msg)
        for alert in alerts:
            if alert is None:
                continue
            if action == "seen":
                if alert["state"] == "pending":
                    ledger.mark_alert(self._db, alert["id"], "shown", now)
                continue
            level = ledger.shown_level(alert["level"], self.quiet())
            ledger.add_feedback(self._db, alert["id"], level, str(reaction), now)
            ledger.mark_alert(self._db, alert["id"], "done", now)

    def ledger(self) -> dict[str, Any]:
        """``GET /inherent/jobs``: the ledger by company, and the mail held back that came close."""
        return {
            "ledger": ledger.list_ledger(self._db, self.now()),
            "skipped": ledger.list_skipped(self._db),
        }

    def delete(self, message_id: str) -> None:
        """``POST /inherent/jobs/{id}/delete``: hide a mail from the ledger; unknown is a 404."""
        if not ledger.delete_mail(self._db, message_id):
            msg = f"no such mail: {message_id}"
            raise LookupError(msg)


def _known_reaction(reaction: str | None) -> bool:
    if reaction is None:
        return False
    if reaction.startswith(_LEVEL_PREFIX):
        return reaction[len(_LEVEL_PREFIX) :] in lang.JOB_LEVEL_NAMES
    return reaction in _REACTIONS


def _head(message: Mapping[str, Any]) -> triage.Head:
    """A ``gmail_get`` metadata answer as a header: the domain is kept, never the address."""
    one = _letter(message)
    _name, address = parseaddr(str(message.get("from") or ""))
    return triage.Head(
        message_id=one["id"],
        thread_id=one["thread_id"],
        received_at=one["received"] or datetime.now(UTC).isoformat(),
        name="" if one["from"] == address else one["from"],
        domain=address.rpartition("@")[2].lower(),
        subject=one["subject"],
    )
