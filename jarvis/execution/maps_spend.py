"""ADR 0283, 0347: what the Google map calls cost, counted per request, and the monthly stop.

Every paid request (Places and Routes) is recorded as one ``maps.spend_recorded`` row per SKU. The
month's spend is what Google bills: per SKU, the requests past its free monthly usage at its price,
summed over the event log, so it survives a restart. (Not ``cost.recorded``: only L3 emits that,
canary ``cost_recorded_l3_only``.) Near :data:`STOP_AT` of the cap a paid call is refused with a
plain reason she can say.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
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

FREE_ESSENTIALS: Final = 10_000
FREE_PRO: Final = 5_000
FREE_ENTERPRISE: Final = 1_000
"""Google's free monthly usage per SKU, by the SKU's tier (since March 2025)."""


@dataclass(frozen=True)
class Sku:
    """What one billable request of a SKU costs, and how many a month Google does not bill."""

    price_usd: float
    free: int


# Google Maps Platform's price list as remembered at the March 2025 pricing change, first tier. No
# network from here to re-read the page: where a SKU was not certain the higher price and the
# smaller free usage are used, so the count runs high rather than low.
#   Places API (New): Text Search and Nearby Search at the Enterprise tier (rating, price level,
#   opening hours and phone are Enterprise fields) 35 per thousand; Text Search with only name,
#   address, point and viewport (Pro) 32; Place Details with reviews (Enterprise + Atmosphere)
#   25; Place Details with name and address only (Pro) 17.
#   Geocoding API (reverse, ADR 0284): 5 per thousand requests, Essentials.
#   Routes: TRANSIT counted at Pro, 10, because it was not certain whether transit is billed at
#   Essentials; DRIVE and WALK with the default routing preference and one route matrix element
#   are Essentials, 5.
SKUS: Final[dict[str, Sku]] = {
    "places.text_search.enterprise": Sku(0.035, FREE_ENTERPRISE),
    "places.text_search.pro": Sku(0.032, FREE_PRO),
    "places.nearby_search.enterprise": Sku(0.035, FREE_ENTERPRISE),
    "places.details.enterprise_atmosphere": Sku(0.025, FREE_ENTERPRISE),
    "places.details.pro": Sku(0.017, FREE_PRO),
    "geocoding.reverse": Sku(0.005, FREE_ESSENTIALS),
    "routes.transit.pro": Sku(0.010, FREE_PRO),
    "routes.basic.essentials": Sku(0.005, FREE_ESSENTIALS),
    "routes.matrix.essentials": Sku(0.005, FREE_ESSENTIALS),
}
_BILLING_ZONE: Final = ZoneInfo("America/Los_Angeles")  # Google bills by Pacific time.
_MONTH_SQL: Final = (
    "SELECT json_extract(payload_json, '$.sku'),"
    " COALESCE(SUM(json_extract(payload_json, '$.requests')), 0) FROM events "
    "WHERE type = 'maps.spend_recorded' AND ts_epoch_ms >= ? GROUP BY 1"
)


def _month_start_ms(now: datetime) -> int:
    local = now.astimezone(_BILLING_ZONE)
    return int(local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def month_spent(conn: sqlite3.Connection, now: datetime) -> float:
    """USD Google bills for map calls since the first of the month ``now`` falls in, Pacific time.

    Per SKU, the requests past its free monthly usage at its price (ADR 0347); a SKU the table no
    longer knows is not priced.
    """
    total = 0.0
    for sku, requests in conn.execute(_MONTH_SQL, (_month_start_ms(now),)):
        if (known := SKUS.get(str(sku))) is not None:
            total += max(0, int(requests) - known.free) * known.price_usd
    return total


def guard(conn: sqlite3.Connection, cap_usd: float, now: datetime) -> None:
    """Refuse a paid map call once the month's billed spend is near the cap; 0 or less is off.

    Raises ``ToolError`` (code ``spend_cap``) whose message she can pass on as it is.
    """
    if cap_usd <= 0:
        return
    spent = month_spent(conn, now)
    if spent >= STOP_AT * cap_usd:
        msg = (
            f"Map lookups are paused: this month Google has billed about ${spent:.2f} of the"
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
                    "cost_usd": round(SKUS[sku].price_usd * count, 6),
                    "action_id": ctx.action_id,
                },
                correlation={"turn_id": turn} if turn else None,
                ts_epoch_ms=stamp,
            )
        except sqlite3.Error:
            LOGGER.exception("maps spend: could not record %d x %s", count, sku)


__all__ = ["DEFAULT_MONTHLY_USD", "SKUS", "STOP_AT", "Sku", "guard", "month_spent", "record"]
