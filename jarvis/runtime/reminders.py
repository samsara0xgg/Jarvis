"""The reminder clock (ADR 0179): fires the reminders Allen set by voice, and serves their cards.

The state is the event log (``jarvis.state.reminders``). Once at start and then every ``TICK_S``
the daemon folds it and fires each reminder whose time has passed, so a reminder missed while the
daemon was down or the Mac asleep rings at the next tick and says how late it is. Every reminder
rings exactly once: ``reminder.fired`` is written before anything is shown, said or pushed.

A reminder ignores the quiet level, the away hold and whether the output is private (Allen asked
for it). It is a card that ``GET /inherent/notices`` serves until Allen takes it in. She says one
line when Allen is not on a call, speech is on and no conversation is live, and then the card is
silent: her voice is the alert. When she cannot speak, or the reminder is more than
``SPEAK_UNTIL`` late, the card carries the cue instead. It is never merged into a job-mail digest.
It is also pushed to his phone (ADR 0210) at any quiet level, once it is written as fired, unless
it is more than ``SPEAK_UNTIL`` late.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime import audio_output
from jarvis.runtime.departures import Departures
from jarvis.shared import lang
from jarvis.state import reminders as folded
from jarvis.state.event_log import emit_event, open_runtime_event_log

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.moment import Moment

LOGGER = logging.getLogger(__name__)

# ponytail: a poll, not a timer per reminder; the log is the only state, so a restart or a Mac
# waking up loses nothing. Revisit if the 15 s latency of a ring ever matters.
TICK_S: Final[float] = 15.0
# Later than this a reminder is a card only: the moment it was for is long gone.
SPEAK_UNTIL: Final[timedelta] = timedelta(hours=12)
# A tick is never more than ~15 s behind, so under this it is on time and says nothing of it.
_ON_TIME: Final[timedelta] = timedelta(minutes=2)
# A fired reminder's card is served until Allen takes it in, for this long.
CARD_KEEP: Final[timedelta] = timedelta(days=7)
_MINUTES_PER_HOUR: Final[int] = 60


def line(text: str, late_ms: int) -> str:
    """What is said and shown: the reminder line in the language of the text, plus how late."""
    said_in = lang.text_language(text)
    said = lang.t("reminder.line", lang=said_in, text=text)
    late = timedelta(milliseconds=late_ms)
    if late < _ON_TIME:
        return said
    minutes = round(late.total_seconds() / 60)
    if minutes < 2 * _MINUTES_PER_HOUR:
        return said + lang.t("reminder.late_minutes", lang=said_in, n=minutes)
    return said + lang.t("reminder.late_hours", lang=said_in, n=round(minutes / _MINUTES_PER_HOUR))


class Reminders:
    """The tick and what the surface reads of it; built once at boot."""

    def __init__(
        self,
        event_log: Path,
        *,
        moment: Moment | None = None,
        departures: Departures | None = None,
    ) -> None:
        """``event_log`` is the daemon's log; ``moment`` says whether Allen is on a call.

        ``departures`` is the pinned bus trip (ADR 0200): it rides this tick and its reminder is
        an ordinary one, so the notch's countdown and the ring share one clock.
        """
        self._path = event_log
        self.moment = moment
        self.departures = departures or Departures(event_log)
        self._now: Callable[[], datetime] = lambda: datetime.now(UTC)
        # Wired by the daemon: whether speaking is allowed (speech not muted, no live
        # conversation) and the one function that says a line. Until then she never speaks.
        self.may_speak: Callable[[], bool] = lambda: False
        self.say: Callable[[str], None] | None = None
        # Wired by the daemon (ADR 0210): pushes the reminder's line to his phone, at any quiet
        # level. Called after ``reminder.fired`` is written and never for a reminder more than
        # ``SPEAK_UNTIL`` late.
        self.push: Callable[[str], object] | None = None
        # Where sound would come out now; only for the notices' ``audio_private`` without job mail.
        self.output: Callable[..., dict[str, Any]] = audio_output.current_output

    @property
    def now(self) -> Callable[[], datetime]:
        """The clock; the pinned trip reads the same one."""
        return self._now

    @now.setter
    def now(self, clock: Callable[[], datetime]) -> None:
        self._now = self.departures.now = clock

    async def run(self) -> None:
        """Tick at once and then every ``TICK_S``; a failed tick is logged, never ends the loop."""
        LOGGER.info("reminders started (every %.0f s)", TICK_S)
        try:
            while True:
                try:
                    await asyncio.to_thread(self.tick)
                except Exception:
                    LOGGER.exception("reminders: tick failed; trying again next time")
                await asyncio.sleep(TICK_S)
        except asyncio.CancelledError:
            LOGGER.info("reminders cancelled")
            raise

    def tick(self) -> int:
        """Fire every pending reminder that is due; returns how many fired."""
        now_ms = int(self.now().timestamp() * 1000)
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            due = [one for one in folded.pending(conn) if one.due_at_ms <= now_ms]
            for one in due:
                late_ms = now_ms - one.due_at_ms
                speak = self._speakable(late_ms)
                # Fired before it is shown or said: a failure after this line never rings twice.
                emit_event(
                    conn,
                    type="reminder.fired",
                    payload={
                        "reminder_id": one.reminder_id,
                        "fired_at": now_ms,
                        "late_ms": late_ms,
                        "delivered": "speak" if speak else "card_sound",
                    },
                )
                LOGGER.info(
                    "reminder %s fired %d ms late (%s)",
                    one.reminder_id,
                    late_ms,
                    "spoken" if speak else "card",
                )
                if speak and self.say is not None:
                    try:
                        self.say(line(one.text, late_ms))
                    except Exception:
                        LOGGER.exception("reminders: the spoken line failed")
                if self.push is not None and timedelta(milliseconds=late_ms) <= SPEAK_UNTIL:
                    try:
                        self.push(line(one.text, late_ms))
                    except Exception:
                        LOGGER.exception("reminders: the push failed")
        self.departures.refresh()
        return len(due)

    def next_due(self) -> tuple[str, int] | None:
        """``(text, due_at_ms)`` of the reminder that rings next, or ``None`` (ADR 0210)."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            soonest = folded.pending(conn)
        return (soonest[0].text, soonest[0].due_at_ms) if soonest else None

    def _speakable(self, late_ms: int) -> bool:
        """Speak when not too late, off a call, unmuted and no live conversation, on any output."""
        if self.say is None or timedelta(milliseconds=late_ms) > SPEAK_UNTIL:
            return False
        if self.moment is not None and self.moment.hold() == "call":
            return False
        return self.may_speak()

    # --- what the surface reads ----------------------------------------------------

    def notices(self) -> list[dict[str, Any]]:
        """The fired reminders Allen has not taken in, as notices the companion already draws.

        A spoken reminder is a silent ``card``, one she could not say is ``card_sound``; the quiet
        level and a call or away hold do not apply (ADR 0179).
        """
        since = int((self.now() - CARD_KEEP).timestamp() * 1000)
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            fired = [
                one
                for one in folded.fold(conn).values()
                if one.fired_at_ms is not None and one.fired_at_ms >= since and not one.acknowledged
            ]
        return [
            {
                "id": one.reminder_id,
                "kind": "mail",
                "title": one.text,
                "line": line(one.text, one.late_ms),
                "level": "card" if one.delivered == "speak" else "card_sound",
                "text": one.text,
                "at": datetime.fromtimestamp((one.fired_at_ms or 0) / 1000, UTC).isoformat(),
                "company": "",
                "role": "",
                "event_at": one.due_at_local,
                "mail_kind": "reminder",
            }
            for one in sorted(fired, key=lambda r: r.fired_at_ms or 0)
        ]

    def acknowledge(self, notice_id: str, action: str) -> None:
        """``POST /inherent/notices/{id}``: an unknown id is a LookupError.

        ``seen`` only says the card came up, so a reminder nobody dismissed is served again after a
        companion restart; dismissed and a reaction take it in (a reminder has no level to rate).
        """
        with contextlib.closing(
            open_runtime_event_log(self._path, deadline=time.monotonic() + 1.0)
        ) as conn:
            one = folded.fold(conn).get(notice_id)
            if one is None or one.fired_at_ms is None:
                msg = f"no such notice: {notice_id}"
                raise LookupError(msg)
            if action != "seen" and not one.acknowledged:
                emit_event(conn, type="reminder.acknowledged", payload={"reminder_id": notice_id})
