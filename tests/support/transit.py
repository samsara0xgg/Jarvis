"""A fake routes server and the buses, fixes and places the transit tool is tested with."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from jarvis.execution import transit_tool

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest

# Built by hand from Google's documented computeRoutes TRANSIT response (not a recording):
# Saanich to downtown Victoria on 2026-10-07, four routes, the fourth to be cut.
HANDBUILT = json.loads(
    (
        Path(__file__).parents[1]
        / "fixtures"
        / "google_routes"
        / "saanich-to-downtown-handbuilt.json"
    ).read_text()
)


FAKE_KEY = "test-key-not-real"


PLACES = {"home": "48.48,-123.38", "school": "University of Victoria, Victoria, BC"}


def serve_routes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: Exception | None = None,
    answer: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] = HANDBUILT,
    modes: dict[str, Any] | None = None,
) -> list[Any]:
    """Serve ``answer`` (or what it makes of the request body) to TRANSIT.

    DRIVE and WALK get ``modes[mode]`` (none: no routes).
    """
    seen: list[Any] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        if not request.full_url.startswith("https://routes.googleapis.com/"):
            # `where_am_i` also asks Google what is around him (ADR 0284); not this stub's job.
            msg = "not served here"
            raise urllib.error.URLError(msg)
        seen.append((request, timeout))
        sent = payload(request)
        travel = sent["travelMode"]
        if travel != "TRANSIT":
            reply = (modes or {}).get(travel, {})
            if isinstance(reply, Exception):
                raise reply
            return io.BytesIO(json.dumps(reply).encode())
        if fail is not None:
            raise fail
        return io.BytesIO(json.dumps(answer(sent) if callable(answer) else answer).encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    now = datetime(2026, 10, 6, 17, 0, tzinfo=ZoneInfo("America/Vancouver"))
    monkeypatch.setattr(transit_tool, "_now", lambda: now)
    return seen


def payload(request: urllib.request.Request) -> Any:  # noqa: ANN401 - the JSON the stub was sent
    """The JSON body a stubbed request carried."""
    assert isinstance(request.data, bytes)
    return json.loads(request.data)


def sent_request(seen: list[Any], travel: str = "TRANSIT") -> tuple[urllib.request.Request, float]:
    """The one request made for ``travel``, with its timeout."""
    (found,) = (s for s in seen if json.loads(s[0].data)["travelMode"] == travel)
    return found  # type: ignore[no-any-return]


FIX = {"lat": 48.46, "lng": -123.31, "accuracy_m": 40.4, "place": "Ring Rd, Saanich"}


def fix_with(**changes: object) -> Callable[[], dict[str, Any]]:
    return lambda: {**FIX, **changes}


def bus_route(
    line: str,
    departs: str,
    *,
    walk: int = 4,
    ride: int = 20,
    at: tuple[float, float] = (48.46, -123.3),
) -> dict[str, Any]:
    """One route: a walk, one bus ``line`` leaving at local ``departs`` (HH:MM, 2026-10-06)."""

    def utc(hhmm: str) -> str:
        hour, minute = map(int, hhmm.split(":"))
        total = hour * 60 + minute + 7 * 60  # PDT is UTC-7
        return f"2026-10-{6 + total // 1440:02d}T{total % 1440 // 60:02d}:{total % 60:02d}:00Z"

    end = f"{int(departs[:2]) * 60 + int(departs[3:]) + ride}"
    arrives = f"{int(end) // 60:02d}:{int(end) % 60:02d}"
    return {
        "legs": [
            {
                "steps": [
                    {"staticDuration": f"{walk * 60}s", "travelMode": "WALK"},
                    {
                        "staticDuration": f"{ride * 60}s",
                        "travelMode": "TRANSIT",
                        "transitDetails": {
                            "stopDetails": {
                                "departureStop": {
                                    "name": f"Stop of {line}",
                                    "location": {"latLng": {"latitude": at[0], "longitude": at[1]}},
                                },
                                "departureTime": utc(departs),
                                "arrivalStop": {"name": "UVic Exchange"},
                                "arrivalTime": utc(arrives),
                            },
                            "headsign": "UVic",
                            "transitLine": {"nameShort": line},
                        },
                    },
                ]
            }
        ]
    }


def local_ms(hour: int, minute: int) -> int:
    at = datetime(2026, 10, 6, hour, minute, tzinfo=ZoneInfo("America/Vancouver"))
    return int(at.timestamp() * 1000)


def routes_with(*buses: dict[str, Any]) -> dict[str, Any]:
    return {"routes": list(buses)}
