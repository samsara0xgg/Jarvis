"""The card when a data source stops arriving (ADR 0381): raised once, closed on recovery.

Every ``poll_s`` the daemon asks :mod:`jarvis.state.source_silence` about the last two days up to
now. A source that is silent and has no open card gets one job-alert row (the spend card's shape,
so the quiet level and the moment hold of ``served_notices`` apply); a source that is not silent
has its open card closed, so a second break makes a second card. It reads and never writes
anything but that row: a daemon with no Mac copy of TimeSink (``imports/shipped.db``) judges
nothing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Final

from jarvis.decision import attention
from jarvis.shared import lang
from jarvis.state import job_ledger as ledger
from jarvis.state import source_silence
from jarvis.state.log_invariants import LogCheckError, open_read_only

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

ALERT_PREFIX: Final[str] = "source-silence-"
WINDOW_MS: Final[int] = 2 * 24 * source_silence.HOUR_MS


class SourceSilenceWatch:
    """Judges the Mac's sources every ``poll_s``; one open card per silent source."""

    def __init__(
        self, runtime_root: Path, lifelog_dir: Path, db_path: Path, zone: tzinfo,
        *, poll_s: float = 900,
    ) -> None:
        """Bind the root holding ``imports/shipped.db``, the lifelog archive and the card store."""
        self._shipped = source_silence.shipped_path(runtime_root)
        self._lifelog = lifelog_dir
        self._db = db_path
        self._zone = zone
        self._poll_s = poll_s

    def check(self, now: datetime) -> list[str]:
        """Raise a card for each newly silent source and close the cards of the others.

        Returns the sources a card was raised for.
        """
        if not self._shipped.is_file():
            return []
        ref_ms = int(now.timestamp() * 1000)
        try:
            with closing(open_read_only(
                self._shipped, probe=source_silence.SHIPPED_PROBE, rollback_journal=True,
            )) as conn:
                judged = source_silence.check(
                    conn, self._lifelog, since_ms=ref_ms - WINDOW_MS, ref_ms=ref_ms,
                )
        except LogCheckError as exc:
            LOGGER.warning("source silence: %s", exc)
            return []
        ongoing = {one.source: one for one in judged.ongoing}
        raised = []
        for source in source_silence.SOURCES:
            key = f"{ALERT_PREFIX}{source.name}"
            silence = ongoing.get(source.name)
            if silence is None:
                ledger.close_alerts(self._db, key)
            elif not ledger.has_alert_since(self._db, key, self._outage_start(silence, judged)):
                self._raise(key, silence, now)
                raised.append(source.name)
        return raised

    @staticmethod
    def _outage_start(
        silence: source_silence.Silence, judged: source_silence.Judgement,
    ) -> datetime:
        """Where this outage began: its last arrival, else the start of the window read.

        A card made at or after it is this outage's, answered or not.
        """
        started = judged.since_ms if silence.last_arrival_ms is None else silence.last_arrival_ms
        return datetime.fromtimestamp(started / 1000, tz=UTC)

    def _raise(self, key: str, silence: source_silence.Silence, now: datetime) -> None:
        stamp = now.astimezone(UTC)
        if silence.last_arrival_ms is None:  # older than the window read: do not name a time
            line = lang.t("source.silence.line_long")
        else:
            since = datetime.fromtimestamp(silence.last_arrival_ms / 1000, tz=UTC)
            line = lang.t(
                "source.silence.line", since=since.astimezone(self._zone).strftime("%m-%d %H:%M"),
            )
        ledger.create_alert(
            self._db, key, "card",
            lang.t("source.silence.title", source=lang.t(f"source.silence.{silence.source}")),
            line, stamp,
        )
        local = now.astimezone(self._zone)
        pack = attention.ContextPack(
            "source_silence", key,
            {"kind": "source_silence", "source": silence.source,
             "eligible_minutes": silence.eligible_ms // source_silence.MINUTE_MS},
            {"hour": local.hour, "weekday": local.weekday()},
        )
        ledger.log_decision(
            self._db, source=pack.source, event_id=pack.event_id, pack_json=pack.to_json(),
            judge_id=attention.CARD_RULE_ID, judge_version="1", level="card",
            reason="fixed: a data source stopped arriving, one card until it recovers", now=stamp,
        )
        LOGGER.warning(
            "source silence: %s has not arrived for %d min of eligible time",
            silence.source, silence.eligible_ms // source_silence.MINUTE_MS,
        )

    async def run(self) -> None:
        """One check per poll, off the loop thread; a failed check waits for the next."""
        while True:
            await asyncio.sleep(self._poll_s)
            try:
                await asyncio.to_thread(self.check, datetime.now(UTC))
            except Exception:
                LOGGER.exception("source silence: check failed")


__all__ = ["SourceSilenceWatch"]
