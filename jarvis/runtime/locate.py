"""ADR 0281, 0322: where he is for a tool call, from one device under one policy.

:class:`Locator` asks one phone for a fresh fix and waits for it on the one clock.
:class:`HerePolicy` is what the runtime builds once: the Locator plus the rules that say which
device a turn reads, when its last report is too old to trust, and what a call gets when no fix
comes. A tool never reads the log's reports or asks a phone itself; it holds the
:class:`~jarvis.shared.here.Here` the policy binds to its turn.

- **The device** (ADR 0212, 0322). A turn opened by a device that reports locations (the phone, the
  iPad) reads that device's reports only. A turn from the Mac, on a Mac that has its own location
  (ADR 0194), reads the Mac. Any other turn (the Mac terminal on a brain, a turn with no opening
  row) reads the phone he carries: the one ``locate.phone`` names, else the device with the newest
  report. A report from any other device is never here.
- **The age.** A report older than ``locate.max_age_s`` is not trusted: the device is asked for a
  fresh fix (``fresh()`` asks every time). If none comes the reading is the last report, with its
  age, its device and why no fix came.

A phone with a live ``/phone/ws`` socket is sent a ``locate`` frame (``ask_socket``) and given
``socket_wait_s``. A socket that stays silent is no proof the phone is gone, since iOS suspends an
app with its socket still open, so then it is woken by push like a phone with none. A phone with no
socket is woken by a silent background push (``ask_push``) and given ``push_wait_s``. A phone that
said it cannot (``denied``, ``unavailable``) is not pushed.

Whoever answers, the fix is stored as an ordinary location report before this sees it
(:meth:`~jarvis.surface.terminal_events.BrainEvents.record_phone_batch`).
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Final

from jarvis.shared.here import Coarse, Here, HereReading, HereUnavailable
from jarvis.state import places
from jarvis.state.event_log import MAC_NODE, open_runtime_event_log, turn_origin
from jarvis.state.locate_wait import Located, LocateWaits, Refused
from jarvis.state.phone_location import NoPhoneReport, PhoneFix, latest_phone_fix

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from jarvis.shared.clock import Clock

LOGGER = logging.getLogger(__name__)

SOCKET_WAIT_S: Final = 4.0
PUSH_WAIT_S: Final = 8.0
MAX_AGE_S: Final = 900
"""How old a report may be and still be where he is (``locate.max_age_s``): 15 minutes."""
COARSE_MAX_AGE_S: Final = 86400
"""How old a report may be and still say roughly where he is (ADR 0356): a day."""
NOT_ASKED: Final = "no phone could be asked"
NO_PHONE_REPORT: Final = "no phone has reported a location yet"


class Locator:
    """Asks a phone where he is; wire ``ask_socket`` and ``ask_push`` once the daemon is up."""

    def __init__(
        self, waits: LocateWaits, clock: Clock, *, socket_wait_s: float = SOCKET_WAIT_S,
        push_wait_s: float = PUSH_WAIT_S,
    ) -> None:
        """``waits`` is the one the phone's answers are delivered to; ``clock`` times the waits."""
        self._waits = waits
        self._clock = clock
        self._socket_wait_s = socket_wait_s
        self._push_wait_s = push_wait_s
        # ``(request_id, device) -> how many phones were asked``; each is wired by the daemon.
        self.ask_socket: Callable[[str, str | None], int] = lambda _request_id, _device: 0
        self.ask_push: Callable[[str, str | None], int] = lambda _request_id, _device: 0

    def ask(self, device: str | None) -> tuple[Located | None, str]:
        """A fresh fix from ``device`` (any phone when ``None``), or ``None`` and why in words.

        Only that device is asked, and an answer from another is ignored.
        """
        request_id = secrets.token_urlsafe(12)
        self._waits.expect(request_id, device)
        try:
            answer, how = self._ask(request_id, device)
        finally:
            self._waits.forget(request_id)
        return (answer if isinstance(answer, Located) else None), how

    def _ask(self, request_id: str, device: str | None) -> tuple[Located | Refused | None, str]:
        """The answer to ``request_id`` (or ``None``) and, when there is none, why in words."""
        start = self._clock.monotonic()
        how = NOT_ASKED
        if self.ask_socket(request_id, device):
            answer = self._waits.wait(request_id, self._socket_wait_s, self._clock)
            if answer is not None:
                return answer, self._why(answer)
            how = f"the phone did not answer within {self._socket_wait_s:g} s"
        if self.ask_push(request_id, device):
            answer = self._waits.wait(request_id, self._push_wait_s, self._clock)
            if answer is not None:
                return answer, self._why(answer)
            how = f"the phone did not answer the push within {self._push_wait_s:g} s"
        LOGGER.info("locate: no fix (%s) after %.1f s", how, self._clock.monotonic() - start)
        return None, how

    @staticmethod
    def _why(answer: Located | Refused) -> str:
        return "" if isinstance(answer, Located) else f"the phone said {answer.error}"


class HerePolicy:
    """The runtime's one Here: which device, how old, when to ask (ADR 0322)."""

    def __init__(  # noqa: PLR0913 - the log and clock, the Locator and the Mac, the places and the limits.
        self, event_log: Path, clock: Clock, *, locator: Locator | None = None,
        mac: Callable[[], Mapping[str, Any]] | None = None, memory_db: Path | None = None,
        phone: str = "", max_age_s: int = MAX_AGE_S, home: tuple[float, float] | None = None,
    ) -> None:
        """``mac`` reads this Mac's own location; ``phone`` is ``locate.phone`` (empty: newest).

        ``home`` is the point :meth:`~jarvis.shared.here.Here.coarse` gives when no phone has
        reported within a day.
        """
        self._event_log = event_log
        self._clock = clock
        self._locator = locator
        self._mac = mac
        self._memory_db = memory_db
        self._phone = phone
        self.max_age_s = max_age_s
        self._home = home

    def bind(self, turn_id: str | None) -> Here:
        """The Here of ``turn_id``; it reads the log and asks the phone only when called."""
        return Here(
            lambda: self._reading(turn_id, ask_always=False),
            lambda: self._reading(turn_id, ask_always=True),
            lambda: self._coarse(turn_id),
        )

    def _coarse(self, turn_id: str | None) -> Coarse | None:
        """The turn's device's last report within a day, else home; no device is asked."""
        try:
            with contextlib.closing(open_runtime_event_log(self._event_log)) as conn:
                last = latest_phone_fix(conn, self._device(conn, turn_id, mac=False))
        except Exception as exc:  # noqa: BLE001 - a log that cannot be read is no report: home stands.
            LOGGER.warning("here: no coarse position from the log (%s)", type(exc).__name__)
            last = None
        if last is not None and self.age_s(last) <= COARSE_MAX_AGE_S:
            return Coarse(last.lat, last.lng)
        return None if self._home is None else Coarse(*self._home, home=True)

    def last_report(self, conn: sqlite3.Connection, turn_id: str | None) -> PhoneFix | None:
        """The report the state lines speak of: the turn's device, never the Mac, else the phone."""
        return latest_phone_fix(conn, self._device(conn, turn_id, mac=False))

    def now_ms(self) -> int:
        """The clock's moment, epoch ms."""
        return int(self._clock.time() * 1000)

    def age_s(self, fix: PhoneFix) -> int:
        """Whole seconds since ``fix`` puts him there (zero for a time a little ahead)."""
        return max(0, self.now_ms() - fix.at_ms) // 1000

    def _device(
        self, conn: sqlite3.Connection, turn_id: str | None, *, mac: bool = True,
    ) -> str | None:
        """The device ``turn_id`` reads: ``MAC_NODE``, a phone's name, or ``None`` (any phone)."""
        node = turn_origin(conn, turn_id or "")[1]
        if mac and node == MAC_NODE and self._mac is not None:
            return MAC_NODE
        if node not in (None, MAC_NODE) and latest_phone_fix(conn, node) is not None:
            return node
        if self._phone:
            return self._phone
        newest = latest_phone_fix(conn)
        return None if newest is None else newest.device

    def _reading(self, turn_id: str | None, *, ask_always: bool) -> HereReading:
        with contextlib.closing(open_runtime_event_log(self._event_log)) as conn:
            device = self._device(conn, turn_id)
            last = None if device == MAC_NODE else latest_phone_fix(conn, device)
        if device == MAC_NODE and self._mac is not None:
            return self._read_mac(self._mac)
        if last is not None and not ask_always and self.age_s(last) <= self.max_age_s:
            return self._report(last)
        fix, why = self._ask(device)
        if fix is not None:
            return self._fix(fix)
        if last is None:
            if self._mac is None:
                msg = "no phone has reported a location"
                raise NoPhoneReport(msg)
            return replace(self._read_mac(self._mac), why=NO_PHONE_REPORT)
        return self._report(last, why)

    @staticmethod
    def _read_mac(read: Callable[[], Mapping[str, Any]]) -> HereReading:
        """This Mac's own fix, taken now."""
        try:
            fix = read()
            return HereReading(
                float(fix["lat"]), float(fix["lng"]), float(fix["accuracy_m"]), 0, MAC_NODE,
                fresh=True, place=str(fix["place"]) if fix.get("place") else None,
            )
        except Exception as exc:  # denied, timeout, helper failure: all one short reason.
            msg = f"this Mac's location could not be read ({exc})"
            raise HereUnavailable(msg) from exc

    def _ask(self, device: str | None) -> tuple[Located | None, str]:
        if self._locator is None:
            return None, NOT_ASKED
        return self._locator.ask(device)

    def _report(self, fix: PhoneFix, why: str = "") -> HereReading:
        """A stored report as a reading; ``why`` is set when a fresh fix was asked for in vain."""
        return HereReading(
            fix.lat, fix.lng, fix.accuracy_m, self.age_s(fix), fix.device, fresh=False,
            place=fix.place, why=why,
        )

    def _fix(self, fix: Located) -> HereReading:
        """A fix the phone took because it was asked, named by a kept place if it is inside one."""
        place = fix.place
        if place is None and self._memory_db is not None:
            kept = places.containing(places.read_kept(self._memory_db), fix.lat, fix.lng)
            place = None if kept is None else kept.name
        age_s = max(0, self.now_ms() - fix.at_ms) // 1000
        return HereReading(
            fix.lat, fix.lng, fix.accuracy_m, age_s, fix.device, fresh=True,
            place=place, snapshot=fix.snapshot,
        )
