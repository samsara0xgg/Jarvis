"""ADR 0281: a fresh fix from his phone for ``where_am_i``, with the last report as the fallback.

:class:`Locator` is the phone reader of ``where_am_i``. It runs on a worker thread: it asks the
phone, waits a few seconds on :class:`~jarvis.state.locate_wait.LocateWaits` and answers with the
fix, or with the phone's last report (ADR 0198) and why it is not fresh.

- A phone with a live ``/phone/ws`` socket is sent a ``locate`` frame (``ask_socket``) and given
  ``socket_wait_s``. A socket that stays silent is no proof the phone is gone, since iOS suspends
  an app with its socket still open, so then it is woken by push like a phone with none.
- A phone with no socket is woken by a silent background push (``ask_push``) and given
  ``push_wait_s``. A phone that said it cannot (``denied``, ``unavailable``) is not pushed.

Whoever answers, the fix is stored as an ordinary location report before this sees it
(:meth:`~jarvis.surface.terminal_events.BrainEvents.record_phone_batch`).
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import places
from jarvis.state.event_log import open_runtime_event_log
from jarvis.state.locate_wait import Located, LocateWaits, Refused
from jarvis.state.phone_location import read_phone_here

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

SOCKET_WAIT_S: Final = 4.0
PUSH_WAIT_S: Final = 8.0


class Locator:
    """Asks his phone where he is; wire ``ask_socket`` and ``ask_push`` once the daemon is up."""

    def __init__(  # noqa: PLR0913 - the log, the meeting point, two waits and the kept places.
        self, event_log: Path, waits: LocateWaits, *, socket_wait_s: float = SOCKET_WAIT_S,
        push_wait_s: float = PUSH_WAIT_S, memory_db: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """``waits`` is the one the phone's answers are delivered to."""
        self._event_log = event_log
        self._waits = waits
        self._socket_wait_s = socket_wait_s
        self._push_wait_s = push_wait_s
        self._memory_db = memory_db
        self._clock = clock
        # ``request_id -> how many phones were asked``; each is wired by the daemon.
        self.ask_socket: Callable[[str], int] = lambda _request_id: 0
        self.ask_push: Callable[[str], int] = lambda _request_id: 0

    def __call__(self) -> dict[str, Any]:
        """The phone's reading of where he is now: a fresh fix if it answers, else its last report.

        Raises:
            NoPhoneReport: the phone gave no fix and no phone has ever reported one.
        """
        request_id = secrets.token_urlsafe(12)
        self._waits.expect(request_id)
        try:
            answer, how = self._ask(request_id)
        finally:
            self._waits.forget(request_id)
        if isinstance(answer, Located):
            return self._fresh(answer)
        with contextlib.closing(open_runtime_event_log(self._event_log)) as conn:
            return {**read_phone_here(conn), "fresh": False, "not_fresh": how}

    def _ask(self, request_id: str) -> tuple[Located | Refused | None, str]:
        """The answer to ``request_id`` (or ``None``) and, when there is none, why in words."""
        start = self._clock()
        how = "no phone could be asked"
        if self.ask_socket(request_id):
            answer = self._waits.wait(request_id, self._socket_wait_s)
            if answer is not None:
                return answer, self._why(answer)
            how = f"the phone did not answer within {self._socket_wait_s:g} s"
        if self.ask_push(request_id):
            answer = self._waits.wait(request_id, self._push_wait_s)
            if answer is not None:
                return answer, self._why(answer)
            how = f"the phone did not answer the push within {self._push_wait_s:g} s"
        LOGGER.info("locate: no fix (%s) after %.1f s", how, self._clock() - start)
        return None, how

    @staticmethod
    def _why(answer: Located | Refused) -> str:
        return "" if isinstance(answer, Located) else f"the phone said {answer.error}"

    def _fresh(self, fix: Located) -> dict[str, Any]:
        """``fix`` as the ``here`` reading, named by a kept place when it is inside one."""
        place = fix.place
        if place is None and self._memory_db is not None:
            kept = places.containing(places.read_kept(self._memory_db), fix.lat, fix.lng)
            place = None if kept is None else kept.name
        reading: dict[str, Any] = {
            "lat": fix.lat, "lng": fix.lng, "accuracy_m": fix.accuracy_m,
            "age_s": max(0, int(self._clock() * 1000) - fix.at_ms) // 1000,
            "source": "phone", "device": fix.device, "fresh": True,
        }
        if place is not None:
            reading["place"] = place
        if fix.snapshot is not None:
            reading["snapshot"] = fix.snapshot
        return reading
