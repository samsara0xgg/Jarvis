"""The daily spend limit (ADR 0185): one notch card the first time a local day's spend crosses it.

Every ``poll_s`` it sums today's ``cost.recorded`` dollars off the log's ``(type, ts)`` index and,
past ``daily_usd``, raises one job-alert row (the channel-health card's shape, so the quiet level
and the moment hold of ``served_notices`` apply, with or without job mail). It never stops or
switches off a model.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING, Final

from jarvis.decision import attention
from jarvis.shared import lang
from jarvis.state import job_ledger as ledger
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.memory_db import local_now

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

ALERT_PREFIX: Final[str] = "spend-cap-"
_SUM_TODAY: Final[str] = (
    "SELECT COALESCE(SUM(json_extract(payload_json, '$.cost_usd')), 0) FROM events "
    "WHERE type = 'cost.recorded' AND ts_epoch_ms >= ?"
)


@dataclass(frozen=True)
class SpendCapSettings:
    """The ``spend_cap:`` block of ``config/jarvis.yaml``."""

    daily_usd: float

    @classmethod
    def from_config(cls, raw: object) -> SpendCapSettings | None:
        """The settings, or None (no limit) when the block is absent or ``daily_usd`` unusable."""
        if not isinstance(raw, Mapping):
            return None
        limit = raw.get("daily_usd")
        if isinstance(limit, bool) or not isinstance(limit, int | float) or limit <= 0:
            LOGGER.warning("spend_cap.daily_usd %r is not a positive number; no limit", limit)
            return None
        return cls(daily_usd=float(limit))


class SpendCap:
    """Checks every ``poll_s``; raises the card once per local date."""

    def __init__(
        self,
        settings: SpendCapSettings,
        event_log_path: Path,
        db_path: Path,
        *,
        poll_s: float = 60,
    ) -> None:
        """Bind the limit, the log it sums and the memory.db the card goes to."""
        self._limit = settings.daily_usd
        self._event_log_path = event_log_path
        self._db = db_path
        self._poll_s = poll_s
        self._done: date | None = None  # the local date whose card exists, so no re-read

    def spent_today(self, day: date) -> float:
        """Recorded dollars from local midnight of ``day`` on (tokens priced at record time)."""
        start = datetime.combine(day, time.min).astimezone()
        with closing(open_runtime_event_log(self._event_log_path)) as conn:
            return float(conn.execute(_SUM_TODAY, (int(start.timestamp() * 1000),)).fetchone()[0])

    def check(self, now: datetime) -> bool:
        """Raise today's card when the day's spend is over the limit and none exists; True if so."""
        day = now.astimezone().date()
        if day == self._done:
            return False
        spent = self.spent_today(day)
        if spent < self._limit:
            return False
        self._done = day
        card_id = f"{ALERT_PREFIX}{day.isoformat()}"
        if ledger.has_alert(self._db, card_id):  # a restart after the card was made
            return False
        stamp = now.astimezone(UTC)
        ledger.create_alert(
            self._db, card_id, "card", lang.t("spend.cap.title"),
            lang.t("spend.cap.line", spent=f"{spent:.2f}", limit=f"{self._limit:.2f}"), stamp,
        )
        pack = attention.ContextPack(
            "spend_cap", card_id, {"kind": "spend_cap", "spent_usd": round(spent, 2)},
            {"hour": now.astimezone().hour, "weekday": now.astimezone().weekday()},
        )
        ledger.log_decision(
            self._db, source=pack.source, event_id=pack.event_id, pack_json=pack.to_json(),
            judge_id=attention.CARD_RULE_ID, judge_version="1", level="card",
            reason="fixed: the day's spend crossed its limit, one card", now=stamp,
        )
        LOGGER.warning("spend cap: %.2f USD today, over the %.2f limit", spent, self._limit)
        return True

    async def run(self) -> None:
        """One check per poll, off the loop thread; a failed check waits for the next."""
        while True:
            await asyncio.sleep(self._poll_s)
            try:
                await asyncio.to_thread(self.check, local_now())
            except Exception:
                LOGGER.exception("spend cap: check failed")


__all__ = ["SpendCap", "SpendCapSettings"]
