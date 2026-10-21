"""ADR 0283: what the Google map calls cost, counted per request, and the monthly stop.

Every paid request (Places and Routes) is priced from :data:`PRICE_USD` and recorded as one
``maps.spend_recorded`` row per SKU, so the month's spend is a sum over the event log and survives
a restart. (Not ``cost.recorded``: only L3 emits that, canary ``cost_recorded_l3_only``.) Near
:data:`STOP_AT` of the cap a paid call is refused with a plain reason she can say.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from jarvis.execution.tools import ToolError, _turn_of
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from jarvis.execution.tools import ToolContext

LOGGER = logging.getLogger(__name__)

DEFAULT_MONTHLY_USD: Final = 10.0
STOP_AT: Final = 0.9
"""Paid calls stop once this share of the month's cap is spent."""

# USD per request, from Google Maps Platform's price list as remembered at the March 2025 pricing
# change (per 1000 requests, first tier, free monthly allowance ignored, so the count runs high).
# No network from here to re-read the page: where a SKU was not certain the higher figure is used.
#   Places API (New): Text Search and Nearby Search at the Enterprise tier (rating, price level,
#   opening hours and phone are Enterprise fields) 35; Place Details with reviews (Enterprise +
#   Atmosphere) 25; Place Details with name and address only (Pro) 17.
#   Geocoding API (reverse, ADR 0284): 5 per thousand requests.
#   Routes: TRANSIT counted at Pro, 10, because it was not certain whether transit is billed at
#   Essentials; DRIVE and WALK with the default routing preference are Essentials, 5.
PRICE_USD: Final[dict[str, float]] = {
    "places.text_search.enterprise": 0.035,
    "places.nearby_search.enterprise": 0.035,
    "places.details.enterprise_atmosphere": 0.025,
    "places.details.pro": 0.017,
    "geocoding.reverse": 0.005,
    "routes.transit.pro": 0.010,
    "routes.basic.essentials": 0.005,
}
_BILLING_ZONE: Final = ZoneInfo("America/Los_Angeles")  # Google bills by Pacific time.
_MONTH_SQL: Final = (
    "SELECT COALESCE(SUM(json_extract(payload_json, '$.cost_usd')), 0) FROM events "
    "WHERE type = 'maps.spend_recorded' AND ts_epoch_ms >= ?"
)


def _month_start_ms(now: datetime) -> int:
    local = now.astimezone(_BILLING_ZONE)
    return int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def month_spent(conn: sqlite3.Connection, now: datetime) -> float:
    """USD recorded for map calls since the first of the month ``now`` falls in, Pacific time."""
    return float(conn.execute(_MONTH_SQL, (_month_start_ms(now),)).fetchone()[0])


def guard(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> None:
    """Refuse a paid map call once the month's spend is near the cap; a cap of 0 or less is off.

    Raises ``ToolError`` (code ``spend_cap``) whose message she can pass on as it is.
    """
    if cap_usd <= 0:
        return
    spent = month_spent(conn, now)
    if spent >= STOP_AT * cap_usd:
        msg = (
            f"Map lookups are paused: this month they have cost about ${spent:.2f} of the"
            f" ${cap_usd:.2f} monthly limit. Tell him, and that they start again next month or"
            " when he raises the limit (maps.monthly_usd). Do not guess places or times."
        )
        raise ToolError(msg, code="spend_cap")


def record(ctx: ToolContext, skus: Iterable[str], now: datetime | None = None) -> None:
    """Record the requests made, one ``maps.spend_recorded`` row per SKU; a failed write is logged.

    ``now`` stamps the rows (tests); the default is the context clock's moment.
    """
    turn = _turn_of(ctx.action_id)
    stamp = int((now or ctx.clock.now()).timestamp() * 1000)
    for sku, count in Counter(skus).items():
        try:
            emit_event(
                ctx.conn,
                type="maps.spend_recorded",
                payload={
                    "sku": sku,
                    "requests": count,
                    "cost_usd": round(PRICE_USD[sku] * count, 6),
                    "action_id": ctx.action_id,
                },
                correlation={"turn_id": turn} if turn else None,
                ts_epoch_ms=stamp,
            )
        except sqlite3.Error:
            LOGGER.exception("maps spend: could not record %d x %s", count, sku)


__all__ = ["DEFAULT_MONTHLY_USD", "PRICE_USD", "STOP_AT", "guard", "month_spent", "record"]
