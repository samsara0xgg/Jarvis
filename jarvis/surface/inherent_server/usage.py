"""The quota dashboard's routes (ADR-0018, 0048, 0065) and the day pane's quota bar.

- ``GET /inherent/usage`` and ``POST /inherent/usage/refresh`` — the read model, and one full poll
- ``POST /inherent/usage/codex/reset`` — spend one Codex limit reset (desktop credential only)
- ``POST /inherent/usage/balance`` — record a balance a provider will not report
- ``GET /inherent/usage/history?from_ms=&to_ms=`` — the readings of Claude's and Codex's quota
  windows over a span of at most 48 hours, for the day pane's quota bar
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from fastapi import FastAPI

    from jarvis.surface.inherent_server.deps import InherentDeps


_USAGE_HISTORY_MAX_SPAN_MS: Final = 48 * 60 * 60 * 1000
"""The longest span one ``GET /inherent/usage/history`` covers: a day's bar with room to spare."""
_EPOCH_MS: Final = re.compile(r"[0-9]{1,15}")
"""A span's end as a query string carries it: whole, not negative, and no wider than a log holds."""

_REQUEST_ID: Final[re.Pattern[str]] = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


def _span_bound(name: str, raw: str | None) -> int:
    """One end of a span asked for in epoch milliseconds; a 400 that names it when it is not one."""
    if raw is None or _EPOCH_MS.fullmatch(raw) is None:
        raise HTTPException(status_code=400, detail=f"{name} must be whole epoch milliseconds")
    return int(raw)


def register_usage_routes(app: FastAPI, deps: InherentDeps) -> None:  # noqa: C901 — one closed route table.
    """ADR-0018, 0048, 0065: the usage read model, its refresh, the reset and the balance."""
    if deps.usage_read is not None and deps.usage_refresh is not None:
        usage_read, usage_refresh = deps.usage_read, deps.usage_refresh

        @app.get("/inherent/usage")
        async def usage() -> dict[str, Any]:
            """ADR-0018: latest quota / spend / balance snapshot per service."""
            return usage_read()

        @app.post("/inherent/usage/refresh", status_code=200)
        async def usage_refresh_now() -> dict[str, Any]:
            """ADR-0018: poll every source now, then answer like ``GET``."""
            return await usage_refresh()

    if deps.usage_codex_reset is not None and deps.plugin_authorize is not None:
        codex_reset, desktop_authorize = deps.usage_codex_reset, deps.plugin_authorize

        @app.post("/inherent/usage/codex/reset", status_code=200)
        async def usage_codex_reset(request: Request) -> dict[str, Any]:
            """ADR 0048: spend one Codex limit reset; the request id makes a retry a no-op."""
            if not desktop_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                request_id = json.loads(await request.body()).get("request_id")
            except (ValueError, AttributeError):
                request_id = None
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise HTTPException(status_code=400, detail="request_id must be a UUID")
            try:
                return await asyncio.to_thread(codex_reset, request_id)
            except (OSError, ValueError) as exc:  # urllib errors are OSError.
                raise HTTPException(status_code=502, detail=str(exc)[:200]) from None

    if deps.usage_record_balance is not None and deps.plugin_authorize is not None:
        record_balance, balance_authorize = deps.usage_record_balance, deps.plugin_authorize

        @app.post("/inherent/usage/balance", status_code=200)
        async def usage_balance(request: Request) -> dict[str, Any]:
            """ADR 0065: record a balance; the next usage poll subtracts the spend since."""
            if not balance_authorize(request.headers.get("authorization")):
                raise HTTPException(status_code=401, detail="desktop authorization required")
            try:
                body = json.loads(await request.body())
                service, usd = body.get("service"), body.get("usd")
            except (ValueError, AttributeError):
                service = usd = None
            amount = isinstance(usd, int | float) and not isinstance(usd, bool)
            if not isinstance(service, str) or not amount:
                raise HTTPException(status_code=400, detail="needs a service and a usd amount")
            try:
                record_balance(service, float(usd))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            return {"recorded": True}


def register_usage_history_route(app: FastAPI, deps: InherentDeps) -> None:
    """``GET /inherent/usage/history``, the quota windows over a span; open as the day is."""
    if deps.usage_history is None:
        return
    usage_history = deps.usage_history

    @app.get("/inherent/usage/history")
    async def usage_history_span(
        from_ms: str | None = None, to_ms: str | None = None,
    ) -> dict[str, Any]:
        """``{services: {name: [{at_ms, status, windows}]}}``, the readings in the span.

        Both ends are required, whole UTC milliseconds, both counted. A missing or malformed one,
        a span that ends before it starts, or one over 48 hours is a 400.
        """
        start, end = _span_bound("from_ms", from_ms), _span_bound("to_ms", to_ms)
        if end < start:
            raise HTTPException(status_code=400, detail="the span ends before it starts")
        if end - start > _USAGE_HISTORY_MAX_SPAN_MS:
            raise HTTPException(status_code=400, detail="the span is longer than 48 hours")
        return usage_history(start, end)
