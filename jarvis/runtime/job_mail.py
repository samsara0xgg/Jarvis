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
import dataclasses
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any, Final

from jarvis.decision import attention
from jarvis.decision import job_mail as triage
from jarvis.decision.surrogate_route import KEY_ENV
from jarvis.execution.tools import ToolError
from jarvis.runtime import audio_output
from jarvis.runtime.home import MAIL_SERVER, _gmail, _letter, mail_body
from jarvis.shared import lang
from jarvis.state import device_reads, job_time
from jarvis.state import job_ledger as ledger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.decision.attention import Judge
    from jarvis.decision.surrogate_route import SurrogateRoute
    from jarvis.execution.mcp_tools import McpServers
    from jarvis.runtime.moment import Moment
    from jarvis.runtime.plugin_connections import PluginConnections
    from jarvis.shared.device_link import DeviceLink

LOGGER = logging.getLogger(__name__)

# The only Gmail tools this module may call; a mail is read, never changed.
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset({"gmail_search", "gmail_get"})
_QUERY: Final[str] = "newer_than:{days}d -in:sent -in:drafts"
# ``backfill_since`` replaces the window with a fixed date (ADR 0177).
_QUERY_SINCE: Final[str] = "after:{day:%Y/%m/%d} -in:sent -in:drafts"
_SEARCH_LIMIT: Final[int] = 100
# At most this many result pages are read in one cycle.
# ponytail: every poll re-lists the pages of mail already seen, newest first, before it reaches an
# unseen id; once the backfill is done that is up to this many cheap searches each cycle. Upgrade:
# keep the oldest received date scanned and search ``after:`` it instead of the fixed date.
_MAX_PAGES: Final[int] = 30
_IGNORED_AFTER: Final[timedelta] = timedelta(minutes=30)
_REACTIONS: Final[frozenset[str]] = frozenset({"right", "dismissed"})
_ALERT_LEVELS: Final[frozenset[str]] = frozenset({"card", "card_sound", "speak"})
# The channel is called down after this many failed cycles in a row, or this long without a good
# one, and says so at most once per ``_HEALTH_EVERY``.
_HEALTH_FAILURES: Final[int] = 3
_HEALTH_AFTER: Final[timedelta] = timedelta(hours=2)
_HEALTH_EVERY: Final[timedelta] = timedelta(hours=12)
_LEVEL_PREFIX: Final[str] = "level:"
LINKEDIN_ALERTS: Final[tuple[str, ...]] = ("ledger_only", "card_sound")
# The judge name logged for a header held back by a local rule rather than by Jev.
_RULE_JUDGE: Final[str] = "local-rule/social-v1"
# The same, for a header whose sender domain is in ``exclude_domains`` (ADR 0171).
_EXCLUDE_JUDGE: Final[str] = "local-rule/exclude-v1"
# How much of each scanned mail's plain-text body the local decision snapshot keeps (ADR 0162).
SNAPSHOT_BODY_CHARS: Final[int] = 3000
# The one-off re-read at start (ADR 0184): which mail, how many per start, who is named as judge.
_REREAD_KINDS: Final[tuple[str, ...]] = ("interview", "offer")
_REREAD_CAP: Final[int] = 20
_REREAD_JUDGE: Final[str] = "local-reread/body-v1"


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
    # LinkedIn job-alert digests: ``ledger_only`` (no card, no sound) or ``card_sound``.
    linkedin_alerts: str
    # Sender domains kept out of job mail: held back before Jev is asked, rows hidden (ADR 0171).
    exclude_domains: tuple[str, ...]
    # A fixed first day of mail to read, instead of the last ``backfill_days`` (ADR 0177).
    backfill_since: date | None = None


def gmail_read(servers: McpServers, tool: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """One Gmail read; any tool outside ``READ_ONLY_TOOLS`` is refused before it is sent."""
    if tool not in READ_ONLY_TOOLS:
        msg = f"job mail may only read Gmail, not call {tool!r}"
        raise ValueError(msg)
    return _gmail(servers, tool, args)


class JobMail:
    """The poller and what the surface reads of it; built once at boot."""

    def __init__(  # noqa: PLR0913 - the poller's collaborators
        self,
        settings: JobMailSettings,
        route: SurrogateRoute,
        connections: PluginConnections,
        db_path: Path,
        judge: Judge,
        *,
        moment: Moment | None = None,
        timesink_path: Path | None = None,
        device: DeviceLink | None = None,
    ) -> None:
        """``route`` carries Jev's model and timeout; ``db_path`` is memory.db.

        ``judge`` decides how loudly each typed letter reaches Allen (ADR 0155). ``moment``
        holds alerts while Allen is in a call or away and is stored with each decision;
        ``timesink_path`` is where the ledger's time column is read (ADR 0161), or, on a brain,
        ``device`` is the link to the terminal whose TimeSink it is (ADR 0170).
        """
        self.moment = moment
        self.timesink_path = timesink_path
        self.device = device
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
            await asyncio.to_thread(self.repair)
            await asyncio.to_thread(self.reread)
            while True:
                try:
                    await asyncio.to_thread(self.poll_once)
                except Exception:
                    LOGGER.exception("job mail: cycle failed; trying again next time")
                await asyncio.sleep(self._settings.poll_s)
        except asyncio.CancelledError:
            LOGGER.info("job mail cancelled")
            raise

    def repair(self) -> int:
        """Run the repair pass over the ledger; a failure is logged and never stops the poller."""
        try:
            settings = self._settings
            changed = repair(self._db, settings.linkedin_alerts, settings.exclude_domains)
        except Exception:
            LOGGER.exception("job mail: the ledger repair failed; the poller goes on")
            return 0
        if changed:
            LOGGER.info("job mail: repaired %d ledger rows", changed)
        return changed

    def reread(self) -> int:
        """Read again the body of interview and offer mail settled before it was kept (ADR 0184).

        At most ``_REREAD_CAP`` mails per start, newest first, by ``gmail_get`` alone; a failure is
        logged and skipped, never stops the poller. Each body start is kept as a ``reread`` row,
        and a mail with no event time gets the one its body names. Returns how many were kept.
        """
        try:
            rows = ledger.bodyless_mails(self._db, _REREAD_KINDS, _REREAD_CAP)
            if not rows:
                return 0
            servers = self._connections.client_for(MAIL_SERVER)
        except Exception:
            LOGGER.exception("job mail: the re-read of old bodies could not start")
            return 0
        kept, now = 0, self.now()
        for row in rows:
            message_id = row["message_id"]
            text = self._read_body(servers, message_id)
            if text is None:
                continue
            try:
                ledger.record_decision(
                    self._db,
                    message_id,
                    "reread",
                    "job",
                    now,
                    head={k: row[k] or "" for k in ("received_at", "subject")}
                    | {"name": row["sender_name"] or "", "domain": row["sender_domain"] or ""},
                    judge=_REREAD_JUDGE,
                    body_excerpt=text[:SNAPSHOT_BODY_CHARS],
                    body_status="read",
                )
                if not row["event_at"]:
                    self._fill_event(row, text)
            except Exception:
                LOGGER.exception("job mail: cannot keep the re-read body of %s", message_id)
                continue
            kept += 1
        if kept:
            LOGGER.info("job mail: read again the bodies of %d old mails", kept)
        return kept

    def _fill_event(self, row: Mapping[str, Any], body: str) -> None:
        """Give a ledger row with no event time the one its body names, if any."""
        try:
            received = datetime.fromisoformat(row["received_at"])
        except (TypeError, ValueError):
            received = self.now()
        sentence, at = triage.event_of(body, received, dated=True)
        if at or sentence:
            ledger.set_event(self._db, row["message_id"], at, sentence or row["event_text"])

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
        try:
            fresh = self._unseen(servers)
        except Exception as exc:
            self._failed(exc)
            raise
        self._failures, self._last_ok = 0, now
        ledger.resolve_health(self._db)  # a recovery is silent and clears the old down card
        if not fresh:
            return 0
        return self._settle(servers, fresh, now)

    def _unseen(self, servers: McpServers) -> list[str]:
        """The newest ids not triaged yet, paged until a cycle's worth is found or the pages end."""
        settings = self._settings
        since = settings.backfill_since
        query = (
            _QUERY.format(days=settings.backfill_days)
            if since is None
            else _QUERY_SINCE.format(day=since)
        )
        seen = ledger.seen_ids(self._db)
        want = min(settings.max_messages_per_cycle, self._jev.room())
        fresh: list[str] = []
        page = ""
        for _ in range(_MAX_PAGES):
            args: dict[str, Any] = {"query": query, "maxResults": _SEARCH_LIMIT}
            if page:
                args["pageToken"] = page
            reply = gmail_read(servers, "gmail_search", args)
            fresh += [
                str(h["id"]) for h in reply.get("messages") or [] if str(h["id"]) not in seen
            ]
            page = str(reply.get("nextPageToken") or "")
            if len(fresh) >= want or not page:
                break
        return list(dict.fromkeys(fresh))[:want]  # a mail arriving mid-listing shifts a page

    def _settle(self, servers: McpServers, ids: list[str], now: datetime) -> int:
        verdicts: dict[str, str] = {}
        chances: dict[str, float] = {}  # Jev's probability that a letter is job mail
        heads = self._heads(servers, ids, verdicts)
        bodies = self._bodies(servers, heads)  # one full read of every letter, kept locally
        snap = _Snapper(self, now, bodies)

        for message_id in verdicts:  # a header that could not be read: only the failure is known
            snap(message_id, "header", "error")
        left = self._keep_out(heads, verdicts, snap)  # what Jev may be asked about
        skips = self._jev.skips(left)
        letters: list[tuple[triage.Head, str]] = []
        for head in left:
            skip = skips[head.message_id]
            if skip is None:
                verdicts[head.message_id] = "error"
                snap(head.message_id, "header", "error", head)
                continue
            chances[head.message_id] = skip.p_job
            verdict = "not_job" if skip.skipped else "pass"
            snap(head.message_id, "header", verdict, head, skip.probabilities)
            social = not skip.skipped and triage.is_social(head.domain, head.subject)
            if social:  # LinkedIn social news is not job mail whatever Jev said: held back unread
                snap(head.message_id, "rule", "not_job", head, judge=_RULE_JUDGE)
            if skip.skipped or social:
                verdicts[head.message_id] = "not_job"
                continue
            body = bodies.get(head.message_id)
            if body is None:  # the body could not be read
                verdicts[head.message_id] = "error"
                snap(head.message_id, "body", "error", head)
            else:
                letters.append((head, body[: self._settings.max_body_chars]))
        letters = letters[: self._jev.room()]  # the rest stay unseen and are asked tomorrow
        typed = self._jev.types(letters)
        for head, _body in letters:
            found = typed[head.message_id]
            if found is None:
                verdicts[head.message_id] = "error"
                snap(head.message_id, "body", "error", head)
                continue
            chances[head.message_id] = found.p_job
            if found.kind == "not_job":
                verdicts[head.message_id] = "not_job"
                snap(head.message_id, "body", "not_job", head, found.probabilities)
                continue
            snap(head.message_id, "body", "job", head, found.probabilities)
            ledger.upsert_mail(self._db, triage.as_row(head, found), now)
            verdicts[head.message_id] = "job"
            # Seen before it is delivered: a failure after this line never alerts twice.
            ledger.record_seen(self._db, head.message_id, "job", now, p_job=found.p_job)
            self._deliver(head, found, now)
        self._record_rest(verdicts, chances, heads, now)
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

    def _keep_out(
        self, heads: list[triage.Head], verdicts: dict[str, str], snap: _Snapper
    ) -> list[triage.Head]:
        """Hold back the heads of ``exclude_domains`` by a local rule; return the others (ADR 0171).

        No Jev call is spent on them; their bodies are already read and kept like any scan's.
        LinkedIn's "your application was sent to X" mail is the exception (ADR 0177).
        """
        left = []
        for head in heads:
            if triage.is_excluded(
                head.domain, self._settings.exclude_domains
            ) and not triage.is_application_sent(head.domain, head.subject):
                snap(head.message_id, "rule", "not_job", head, judge=_EXCLUDE_JUDGE)
                verdicts[head.message_id] = "not_job"
            else:
                left.append(head)
        return left

    def _record_rest(
        self,
        verdicts: Mapping[str, str],
        chances: Mapping[str, float],
        heads: list[triage.Head],
        now: datetime,
    ) -> None:
        """Mark every letter that was not a job as seen: held back (with its header) or an error."""
        by_id = {head.message_id: head for head in heads}
        for message_id, verdict in verdicts.items():
            if verdict == "job":
                continue
            seen = by_id.get(message_id)
            audit = _audit(seen) if verdict == "not_job" and seen is not None else None
            ledger.record_seen(
                self._db, message_id, verdict, now, p_job=chances.get(message_id), audit=audit
            )

    def _snap(  # noqa: PLR0913 - the row's fields
        self,
        message_id: str,
        stage: str,
        verdict: str,
        now: datetime,
        head: triage.Head | None = None,
        probabilities: dict[str, float] | None = None,
        body: str | None = None,
        judge: str | None = None,
        body_status: str = "",
    ) -> None:
        """Keep what Jev saw and answered at a stage (ADR 0157), for any verdict.

        ``judge`` names a local rule instead of Jev for a ``rule`` stage (ADR 0158). The row also
        keeps the sender address and the body start (ADR 0162); ``body_status`` says whether the
        body was ``read``, ``unavailable`` (the read failed) or ``not_read`` (none was tried).
        """
        ledger.record_decision(
            self._db,
            message_id,
            stage,
            verdict,
            now,
            head=None if head is None else _snapshot_head(head),
            probabilities=probabilities,
            judge=judge or self._jev.judge_id(stage),
            body_excerpt=None if body is None else body[:SNAPSHOT_BODY_CHARS],
            body_status=body_status or "not_read",
            moment=self._situation(),
        )

    def _pack_situation(self, now: datetime, quiet: str) -> dict[str, Any]:
        """What the judge sees besides the mail: the hour, the quiet level and the moment."""
        local = now.astimezone()
        situation: dict[str, Any] = {
            "hour": local.hour,
            "weekday": local.weekday(),
            "quiet": quiet,
            "speech_ok": self.may_speak(),
            "linkedin_alerts": self._settings.linkedin_alerts,
        }
        if (moment := self._situation()) is not None:
            situation["moment"] = moment
        return situation

    def _situation(self) -> dict[str, Any] | None:
        """The situation and its 现况 doc now, to store with a decision; None while it is off."""
        return None if self.moment is None else self.moment.snapshot()

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

    def _bodies(self, servers: McpServers, heads: list[triage.Head]) -> dict[str, str | None]:
        """The plain-text body of every letter, read once; ``None`` where the read failed."""
        return {head.message_id: self._read_body(servers, head.message_id) for head in heads}

    @staticmethod
    def _read_body(servers: McpServers, message_id: str) -> str | None:
        """One ``gmail_get`` in full format as plain text; ``None`` if the letter cannot be read."""
        try:
            message = gmail_read(servers, "gmail_get", {"messageId": message_id, "format": "full"})
        except Exception:  # noqa: BLE001 - one unreadable letter must not stop the others
            LOGGER.warning("job mail: cannot read the body of %s", message_id, exc_info=True)
            return None
        return mail_body(str(message.get("body") or ""))

    def _deliver(self, head: triage.Head, typed: triage.Typed, now: datetime) -> None:
        """Build the pack, ask the judge, log the decision and act on its level."""
        quiet = self.quiet()
        pack = triage.pack_for(head, typed, now, self._pack_situation(now, quiet))
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
        level = self._unless_burst(judgement.level, head.message_id, now)
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
        if reason := self._moment_hold():
            ledger.note_delivery(self._db, head.message_id, f"held_{reason}", now)
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

    def _unless_burst(self, level: str, message_id: str, now: datetime) -> str:
        """A line that would be the third alert within the burst window is a card with sound.

        The burst is one summary card (ADR 0158) and a summary never speaks.
        """
        if level != "speak" or ledger.recent_alerts(self._db, now) < ledger.BURST_SIZE - 1:
            return level
        ledger.note_delivery(self._db, message_id, "suppressed", now)
        return "card_sound"

    def _speak_now(self, quiet: str) -> bool:
        """Speak only at quiet ``off``, unmuted, outside a live conversation and not too soon.

        The output must also be private right now: a fresh look, not the one from decision time.
        """
        if (reason := self._moment_hold()) is not None:
            LOGGER.info("job mail: not spoken, Allen is %s", reason)
            return False
        gap = self._settings.speak_gap_s
        rested = self._last_spoke is None or time.monotonic() - self._last_spoke >= gap
        if not (self._settings.speak and quiet == "off" and self.may_speak() and rested):
            return False
        device = self.output(fresh=True)
        if not device["private"]:
            LOGGER.warning("job mail: not spoken, output %r is not private", device["name"])
        return bool(device["private"])

    def _moment_hold(self) -> str | None:
        """Why alerts wait for Allen now (a call, or he is away), or None (ADR 0161)."""
        return None if self.moment is None else self.moment.hold()

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
        # ADR 0160: the health card keeps a snapshot too, so his answer is logged like any other.
        local = now.astimezone()
        pack = attention.ContextPack(
            "job_health",
            ledger.HEALTH_ID,
            {"kind": "health", "reason": reason, "failures": self._failures},
            {"hour": local.hour, "weekday": local.weekday(), "quiet": self.quiet()},
        )
        ledger.log_decision(
            self._db,
            source=pack.source,
            event_id=pack.event_id,
            pack_json=pack.to_json(),
            judge_id=attention.CARD_RULE_ID,
            judge_version="1",
            level="card_sound",
            reason="fixed: a mail channel health alert is a card with sound",
            now=now,
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
        # ``hold`` (ADR 0163) tells the client to hold its own cards: ``call``, ``away`` or None.
        hold = None if self.moment is None else self.moment.client_hold()
        if hold is not None:
            # In a call or away: every alert stays pending, so none is shown or marked shown, and
            # when it ends they come back as one summary (several waited) or one card.
            return {"notices": [], "audio_private": private, "hold": hold}
        shown = ledger.alerts_for_client(self._db, self.quiet(), self.now())
        if not private:
            for notice in shown:
                for one in (notice, *notice.get("items", [])):
                    if one["level"] in ledger.SOUNDING:
                        one["level"] = "card"
        return {"notices": shown, "audio_private": private, "hold": None}

    def act(self, notice_id: str, action: str, reaction: str | None) -> None:
        """``POST /inherent/notices/{id}``: ``seen``, or feedback (``dismissed`` is feedback too).

        An unknown id is a LookupError (404), a reaction that is not one of ``right``,
        ``dismissed`` and ``level:<name>`` a ValueError (400). A digest id stands for its alerts:
        seen, dismissed or a reaction applies to each, logged once per alert with the summary's
        level as the level shown (ADR 0159).
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
        quiet = self.quiet()
        # A summary is one card: it was as loud as its loudest alert (and never spoke).
        loud = any(
            ledger.shown_level(one["level"], quiet) in ledger.SOUNDING for one in alerts if one
        )
        for alert in alerts:
            if alert is None:
                continue
            if action == "seen":
                if alert["state"] == "pending":
                    ledger.mark_alert(self._db, alert["id"], "shown", now)
                continue
            level = ledger.shown_level(alert["level"], quiet)
            if notice_id.startswith("digest-"):
                level = "card_sound" if loud else "card"
            ledger.add_feedback(self._db, alert["id"], level, str(reaction), now)
            ledger.mark_alert(self._db, alert["id"], "done", now)

    def ledger(self) -> dict[str, Any]:
        """``GET /inherent/jobs``: the ledger by company, the applications, held-back mail, rules.

        ``rules`` are the standing alert rules in force, for the ledger page to show (ADR 0158).
        """
        now = self.now()
        groups = ledger.list_ledger(self._db, now)
        known = job_time.companies(ledger.company_sites(self._db))
        note = None
        try:
            found = job_time.job_time_on(self.timesink_path, self.device, known, now)
        except device_reads.DeviceUnavailable as exc:
            found, note = None, str(exc)  # the time column is not empty: the device is not there
        return {
            "ledger": [{**g, **job_time.spent_view(found, g["company"])} for g in groups],
            "applications": ledger.list_applications(
                self._db, now, triage.is_ats_company, triage.mail_details
            ),
            "job_site_other_s": 0 if found is None else round(found["other_s"]),
            **({} if note is None else {"time_note": note}),
            "skipped": ledger.list_skipped(self._db),
            "rules": [
                {"id": "linkedin_alerts", "value": self._settings.linkedin_alerts},
                *(
                    [{"id": "exclude_domains", "value": ", ".join(self._settings.exclude_domains)}]
                    if self._settings.exclude_domains
                    else []
                ),
            ],
        }

    def add_application(self, fields: Mapping[str, Any]) -> str:
        """``POST /inherent/jobs/applications``: Allen's own row; returns its id (400 if bad)."""
        return ledger.add_application(self._db, self.now(), **fields)

    def edit_application(self, app_id: str, fields: Mapping[str, Any]) -> None:
        """``POST /inherent/jobs/applications/{id}``: his edit; an unknown id is a LookupError."""
        ledger.edit_application(self._db, self.now(), app_id, fields, triage.is_ats_company)

    def flag(self, message_id: str, reaction: str) -> None:
        """``POST /inherent/jobs/{id}/flag``: Allen says a held-back mail was job mail after all.

        The mail is read again (``gmail_get`` only), typed with the header skip ignored, and
        delivered as any job mail is (the judge and the quiet level still apply); a typing of
        ``not_job`` becomes ``job_other``, so his flag wins. Flagging twice does nothing. A reaction
        other than ``should_alert`` is a ValueError (400); a mail that cannot be read or typed
        raises, and the flag is not recorded.
        """
        if reaction != "should_alert":
            msg = f"not a flag: {reaction!r}"
            raise ValueError(msg)
        if ledger.is_flagged(self._db, message_id):
            return
        now = self.now()
        servers = self._connections.client_for(MAIL_SERVER)
        args = {"messageId": message_id, "format": "metadata"}
        head = _head(gmail_read(servers, "gmail_get", args))
        text = self._read_body(servers, message_id)
        body = (text or "")[: self._settings.max_body_chars]
        found = None if text is None else self._jev.types([(head, body)])[head.message_id]
        if found is None:
            msg = f"cannot read or type {message_id}"
            raise RuntimeError(msg)
        # His flag wins over Jev's "not job" and over the local rules that kept it off the cards.
        kind = "job_other" if found.kind in ("not_job", triage.ACCOUNT_KIND) else found.kind
        found = dataclasses.replace(found, kind=kind, alert_digest=False)
        _Snapper(self, now, {message_id: text})(
            message_id, "body", "job", head, found.probabilities
        )
        ledger.upsert_mail(self._db, triage.as_row(head, found), now)
        ledger.record_seen(self._db, head.message_id, "job", now, p_job=found.p_job)
        self._deliver(head, found, now)
        ledger.add_flag(self._db, head.message_id, now)

    def delete(self, message_id: str) -> None:
        """``POST /inherent/jobs/{id}/delete``: hide a mail from the ledger; unknown is a 404."""
        if not ledger.delete_mail(self._db, message_id):
            msg = f"no such mail: {message_id}"
            raise LookupError(msg)


def repair(db: Path, linkedin_alerts: str, exclude_domains: tuple[str, ...] = ()) -> int:
    """Recompute what the current rules read from each ledger row's stored header (ADR 0158).

    Idempotent and offline: only the stored sender name, domain and subject and the body start
    kept in the decision snapshots (ADR 0162) are used, never Gmail; returns how many rows
    changed. A stored role that is a generic word, or that the subject holds but the rules no
    longer read from it, is cleared; a role the subject does not hold was read from the body and
    stays. LinkedIn social mail is hidden and shown as held back, an account notice becomes kind
    ``other``, and the pending alerts of a mail that is now ledger only, ``other`` or hidden end
    as done. A row whose domain is in ``exclude_domains`` is hidden the same way (ADR 0171), except
    a LinkedIn "your application was sent to X" row (ADR 0177). A mail Allen flagged is never
    touched by the routing rules. An interview or offer row's event time is read again from its
    kept body and replaced when the body names one.
    """
    changed = 0
    for row in ledger.mail_rows(db):
        message_id, subject = row["message_id"], row["subject"] or ""
        name, domain = row["sender_name"] or "", row["sender_domain"] or ""
        fixes: dict[str, str | int] = {}
        body = ledger.body_excerpt(db, message_id)  # kept locally since ADR 0162; '' before it
        company = triage.company_of(name, domain, subject, body)
        role = triage.role_of(subject, body, company)
        old = (row["role"] or "").casefold()
        if not role and old not in subject.casefold() and old not in triage.GENERIC_ROLES:
            role = triage.clean_role(row["role"] or "", company)
        fixes.update(
            {
                field: value
                for field, value in (("company", company), ("role", role))
                if value != (row[field] or "")
            }
        )
        if row["kind"] in _REREAD_KINDS and body and _fix_event(db, row, body):
            changed += 1
        if not ledger.is_flagged(db, message_id):
            quiet = False  # whether its pending alerts must go
            kept_out = triage.is_excluded(
                domain, exclude_domains
            ) and not triage.is_application_sent(domain, subject)
            if not row["deleted"] and (kept_out or triage.is_social(domain, subject)):
                _hold_back(db, row, _EXCLUDE_JUDGE if kept_out else _RULE_JUDGE)
                fixes["deleted"], quiet = 1, True
            elif row["kind"] == "job_other" and triage.is_account_notice(subject):
                fixes["kind"], quiet = triage.ACCOUNT_KIND, True
            elif (
                row["kind"] == "job_other"
                and linkedin_alerts == "ledger_only"
                and triage.is_alert_digest(name, domain, subject)
            ):
                quiet = True
            if quiet and ledger.drop_alerts(db, message_id):
                changed += 1
        if fixes:
            ledger.update_mail(db, message_id, fixes)
            changed += 1
    return changed


def _fix_event(db: Path, row: Mapping[str, Any], body: str) -> bool:
    """Replace an interview or offer row's event time with the one its kept body names, if any."""
    try:
        received = datetime.fromisoformat(row["received_at"])
    except (TypeError, ValueError):
        return False
    sentence, at = triage.event_of(body, received, dated=True)
    if not at or (at, sentence) == (row["event_at"], row["event_text"]):
        return False
    ledger.set_event(db, row["message_id"], at, sentence)
    return True


def _hold_back(db: Path, row: Mapping[str, Any], judge: str) -> None:
    """Show a ledger row in the held-back list: a ``not_job`` verdict and the rule's decision."""
    now = datetime.now(UTC)
    head = {
        "received_at": row["received_at"] or "",
        "name": row["sender_name"] or "",
        "domain": row["sender_domain"] or "",
        "subject": row["subject"] or "",
    }
    ledger.record_seen(db, row["message_id"], "not_job", now, audit=head)
    ledger.record_decision(
        db, row["message_id"], "rule", "not_job", now, head=head, judge=judge
    )


class _Snapper:
    """Writes the decision snapshots of one cycle; each row carries what was read of its mail."""

    def __init__(self, job: JobMail, now: datetime, bodies: Mapping[str, str | None]) -> None:
        """``bodies`` maps a message id to its body, or ``None`` where the read failed."""
        self._job, self._now, self._bodies = job, now, bodies

    def __call__(  # noqa: PLR0913 - the row's fields
        self,
        message_id: str,
        stage: str,
        verdict: str,
        head: triage.Head | None = None,
        probabilities: dict[str, float] | None = None,
        judge: str | None = None,
    ) -> None:
        """A mail never read at all (``message_id`` not in ``bodies``) is ``not_read``."""
        text = self._bodies.get(message_id)
        status = "read" if text is not None else "unavailable" if message_id in self._bodies else ""
        self._job._snap(  # noqa: SLF001 - the one writer of this module's snapshots
            message_id, stage, verdict, self._now, head, probabilities, text, judge, status
        )


def _snapshot_head(head: triage.Head) -> dict[str, str]:
    """The header facts of a decision snapshot: ``_audit`` and the sender address (ADR 0162)."""
    return {**_audit(head), "address": head.address}


def _audit(head: triage.Head) -> dict[str, str]:
    """The header facts kept for the audit list: never an address."""
    return {
        "received_at": head.received_at,
        "name": head.name,
        "domain": head.domain,
        "subject": head.subject,
    }


def _known_reaction(reaction: str | None) -> bool:
    if reaction is None:
        return False
    if reaction.startswith(_LEVEL_PREFIX):
        return reaction[len(_LEVEL_PREFIX) :] in lang.JOB_LEVEL_NAMES
    return reaction in _REACTIONS


def _head(message: Mapping[str, Any]) -> triage.Head:
    """A ``gmail_get`` metadata answer as a header; the address goes to the snapshot alone."""
    one = _letter(message)
    _name, address = parseaddr(str(message.get("from") or ""))
    return triage.Head(
        message_id=one["id"],
        thread_id=one["thread_id"],
        received_at=one["received"] or datetime.now(UTC).isoformat(),
        name="" if one["from"] == address else one["from"],
        domain=address.rpartition("@")[2].lower(),
        subject=one["subject"],
        address=address,
    )
