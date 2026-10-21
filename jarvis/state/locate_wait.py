"""ADR 0281: the requests for a fresh fix that are waiting for his phone's answer.

Here (ADR 0322) asks his phone for a fix and waits on a worker thread; the answer comes back on
the loop thread, over ``/phone/ws`` (a ``located`` frame) or in a ``phone.location_observed``
event carrying the request's id. This is the meeting point of the two: a request is
:meth:`~LocateWaits.expect`-ed, :meth:`~LocateWaits.deliver`-ed an answer or a refusal, and
:meth:`~LocateWaits.wait`-ed for. Nothing is kept past the wait.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, TypeGuard

if TYPE_CHECKING:
    from jarvis.shared.clock import Clock

REQUEST_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")
"""What a request id looks like on the wire (``locate`` frame, push ``locate`` key, report)."""
LOCATE_ERRORS: Final = ("denied", "unavailable")
"""Why a phone may answer a ``locate`` without a fix."""


def valid_request_id(value: object) -> TypeGuard[str]:
    """Whether ``value`` is the shape of a request id."""
    return isinstance(value, str) and REQUEST_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class Located:
    """A fix his phone took because it was asked."""

    lat: float
    lng: float
    accuracy_m: float
    at_ms: int
    device: str
    place: str | None = None
    snapshot: str | None = None


@dataclass(frozen=True)
class Refused:
    """The phone answered that it cannot take a fix (``denied`` or ``unavailable``)."""

    error: str
    device: str = ""


class LocateWaits:
    """Open requests by id; safe to call from any thread."""

    def __init__(self) -> None:
        """Start with no request open."""
        self._lock = threading.Lock()
        self._open: dict[str, tuple[threading.Event, list[Located | Refused], str | None]] = {}

    def expect(self, request_id: str, device: str | None = None) -> None:
        """Open ``request_id``; an answer for an id that is not open is ignored.

        With ``device``, only that device's answer counts (ADR 0322).
        """
        with self._lock:
            self._open[request_id] = (threading.Event(), [], device)

    def deliver(self, request_id: str, answer: Located | Refused) -> None:
        """Hand ``answer`` to whoever waits on ``request_id``; the first answer counts."""
        with self._lock:
            slot = self._open.get(request_id)
            if slot is None or slot[1] or (slot[2] is not None and answer.device != slot[2]):
                return
            slot[1].append(answer)
        slot[0].set()

    def wait(self, request_id: str, timeout_s: float, clock: Clock) -> Located | Refused | None:
        """The answer to ``request_id`` within ``timeout_s`` seconds of ``clock``, else ``None``."""
        with self._lock:
            slot = self._open.get(request_id)
        if slot is None:
            return None
        clock.wait(slot[0], max(0.0, timeout_s))
        with self._lock:
            return slot[1][0] if slot[1] else None

    def forget(self, request_id: str) -> None:
        """Close ``request_id``; an answer that comes later is ignored."""
        with self._lock:
            self._open.pop(request_id, None)
