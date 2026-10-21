"""ADR 0357: the last good answer, held for six hours, for when WeatherKit does not answer.

A web search finds pages, not a forecast, so a failed read is answered from the same source's last
good read of the same request instead. The held answer is given as it was read, with the time it
was read and why a new one could not be, and its first line starts from that time: its figures
are not about now.
"""

from __future__ import annotations

import copy
import threading
from typing import TYPE_CHECKING, Any, Final

from jarvis.runtime.weather_day import RESULT_CHARS, fit
from jarvis.runtime.weatherkit import said_at

if TYPE_CHECKING:
    from datetime import date

HELD_S: Final = 6 * 3600
"""How long a good answer is kept for a failure to give."""
MAX_HELD: Final = 32
"""Requests kept at most; the oldest go first."""
ROOM: Final = 400
"""What a held answer adds to what was read (its line and ``held``): kept out of the copy."""
LINE_CHARS: Final = 400
"""The most a first line may hold (``docs/spec.html#tools``)."""
_SECONDS_A_MINUTE: Final = 60

type Request = tuple[float, float, date, date, bool, bool, bool]
"""The place, the days, hourly, air quality and whether days were named: what makes two requests
the same."""


class Held:
    """The good answers read in the last six hours, one for each request."""

    def __init__(self) -> None:
        """Hold nothing."""
        self._lock = threading.Lock()
        self._rows: dict[Request, tuple[float, dict[str, Any]]] = {}

    def keep(self, request: Request, read_at: float, answer: dict[str, Any]) -> None:
        """Keep ``answer``, read at ``read_at``, as the last good one of ``request``.

        The copy is cut to leave :data:`ROOM` for what it is given with later.
        """
        slim = fit(copy.deepcopy(answer), RESULT_CHARS - ROOM)
        with self._lock:
            fresh = {k: v for k, v in self._rows.items() if read_at - v[0] < HELD_S}
            fresh[request] = (read_at, slim)
            for old in sorted(fresh, key=lambda k: fresh[k][0])[: max(len(fresh) - MAX_HELD, 0)]:
                del fresh[old]
            self._rows = fresh

    def last(
        self, request: Request, now: float, zone_name: str, why: str,
    ) -> dict[str, Any] | None:
        """The last good answer of ``request`` as a failure's answer; None when none is held.

        It carries ``held`` (when it was read, how old that is and why a new read failed) and its
        ``summary`` starts from that time.
        """
        with self._lock:
            found = self._rows.get(request)
        if found is None or now - found[0] >= HELD_S:
            return None
        read_at, answer = found
        when = said_at(read_at, zone_name, now)
        rest = {k: v for k, v in copy.deepcopy(answer).items() if k != "summary"}
        prefix = f"As of {when}, the last good reading (a new one failed: {why}): "
        return {
            "summary": _line(prefix, str(answer.get("summary", ""))),
            "held": {
                "read_at": when, "age_min": round((now - read_at) / _SECONDS_A_MINUTE),
                "why": why,
            },
            **rest,
        }


def _line(prefix: str, summary: str) -> str:
    """``prefix`` and ``summary`` in one line, the summary's last sentences going first."""
    room = LINE_CHARS - len(prefix)
    if len(summary) <= room:
        return prefix + summary
    cut = summary[: room - 1]
    head = cut.rsplit("; ", 1)[0] if "; " in cut else cut
    return f"{prefix}{head.rstrip(' ;,.')}."
