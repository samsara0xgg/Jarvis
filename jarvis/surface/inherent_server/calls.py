"""How a failed call becomes an answer: the mapping the home, day and mail routes share."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from collections.abc import Awaitable


LOGGER = logging.getLogger(__name__)


async def home_call[T](call: Awaitable[T]) -> T:
    """ADR 0051: not connected is 404 (the home's fallback), a bad id 400, anything else 502."""
    try:
        return await call
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)[:200]) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from None
    except Exception as exc:  # noqa: BLE001 — Microsoft or the network failing is the home's 502.
        LOGGER.warning("home route failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=502, detail=str(exc)[:200]) from None
