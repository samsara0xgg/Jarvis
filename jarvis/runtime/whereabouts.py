"""此刻 (ADR 0217): the device a turn came from, the devices there are, where the phone put him.

The device a turn came from is not a line of its own: :meth:`Whereabouts.origin` names it for the
one line that says how the turn came in and how the answer goes out (ADR 0226).

Worked out by code on the host from what it already holds: the turn's opening row (ADR 0212), the
paired devices, the terminals connected now, the phones' conversation sockets (ADR 0209) and push
registrations (ADR 0210), and the phone's latest place report (ADR 0197). Nothing in
:meth:`Whereabouts.lines` asks a terminal, so a turn never waits on the link for it. Whether he is
at the Mac is the moment's (ADR 0161); :meth:`Whereabouts.at_mac` reads it for the card routing of
ADR 0218, off the turn.

Facts only: what she makes of them is hers. One line is a note to her, shown once per spot: the
unnamed spot he keeps going back to, when his phone puts him in it (ADR 0234).
"""

from __future__ import annotations

import contextlib
import logging
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Final

from jarvis.execution.location_tool import describe_age
from jarvis.state import device_tokens, places, push_tokens, terminal_tools
from jarvis.state.event_log import MAC_NODE, turn_origin

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from datetime import tzinfo

    from jarvis.execution.around import Surroundings
    from jarvis.runtime.locate import HerePolicy
    from jarvis.runtime.moment import Moment
    from jarvis.state.phone_location import PhoneFix
    from jarvis.surface.terminal_link import TerminalHub

LOGGER = logging.getLogger(__name__)

WHERE_PREFIX: Final = "Where you run:"
ASK_PLACE_LINE: Final = (
    "[Place] He is at a spot he has been to on {n} days in the last 4 weeks (last {date}). "
    "It has no name. If the moment suits, you may ask him once, in passing, what it is. "
    "This note is not shown again."
)


def _host() -> str:
    """This machine's name, and what the board says it is when it says (a Raspberry Pi)."""
    host = socket.gethostname()
    model = Path("/proc/device-tree/model")
    with contextlib.suppress(OSError):
        host += ", a " + model.read_text(encoding="utf-8").strip("\x00\n ")
    return host


class Whereabouts:
    """The host's one reading of "now"; every method is safe to call from any thread."""

    def __init__(  # noqa: PLR0913 - the root, hub and moment, then what the lines read.
        self, root: Path, hub: TerminalHub | None, moment: Moment | None,
        *, memory_db: Path | None = None, zone: tzinfo | None = None,
        around: Surroundings | None = None, here: HerePolicy | None = None,
    ) -> None:
        """``hub`` is the brain's terminal table (``None`` on a Mac running alone).

        ``memory_db`` and ``zone`` (the one days are cut in) let :meth:`lines` tell her of a spot
        to ask about; without them it never does. ``around`` adds the line about his surroundings
        (ADR 0284), made from what it already holds and never by a request. ``here`` (ADR 0322) says
        which device's report the place lines speak of and how old it may be; without it there are
        none.
        """
        self._root = root
        self._hub = hub
        self._moment = moment
        self._memory_db = memory_db
        self._zone = zone
        self._around = around
        self._here = here
        self._host = _host() if hub is not None else ""
        # Wired by the daemon: whether a device has its ``/phone/ws`` socket open now (ADR 0209).
        self.phone_open: Callable[[str], bool] = lambda _device: False

    def at_mac(self) -> bool:
        """Whether TimeSink says he is active at the Mac now; unknown is not (ADR 0218).

        On a brain the moment asks the terminal, so this must run off the event loop.
        """
        return self._moment is not None and self._moment.facts().get("presence") == "active"

    def lines(self, conn: sqlite3.Connection, turn_id: str | None = None) -> tuple[str, ...]:
        """This turn's lines for the state block: the devices there are and the phone's place.

        A spot he keeps going back to adds its note, once (ADR 0234).
        """
        terminals = self._terminals()
        paired = [name for name, _ in device_tokens.paired_devices(self._root)]
        kind = self._kind(terminals)
        found = [
            self._devices_line([*terminals, *(n for n in paired if n not in terminals)], kind,
                               terminals),
            self._place_line(conn, turn_id),
            self._around_line(conn, turn_id),
            self._ask_line(conn, turn_id),
        ]
        return tuple(line for line in found if line)

    def origin(self, conn: sqlite3.Connection, turn_id: str) -> str | None:
        """The device that opened ``turn_id``, as the channel line names it; ``None`` unknown.

        ``his phone (iphone)``, ``macbook, a computer``, ``this Mac`` (a Mac running alone), or
        a bare name. On a brain ``mac`` is the brain's own default row, not a device (ADR 0212).
        """
        device = turn_origin(conn, turn_id)[1]
        if device is None:
            return None
        if device == MAC_NODE:
            return None if self._hub is not None else "this Mac"
        what = self._kind(self._terminals())(device)
        if what == "his phone":
            return f"his phone ({device})"
        return f"{device}, {what}" if what else device

    def _terminals(self) -> dict[str, tuple[bool, bool]]:
        return {} if self._hub is None else {
            name: (speaks, listens) for name, speaks, listens in self._hub.roster()
        }

    def _kind(self, terminals: dict[str, tuple[bool, bool]]) -> Callable[[str], str]:
        """Name a device's kind; nothing is claimed for an unknown one.

        A computer is or was a terminal (connected, or declared tools); a phone registered push
        or has its conversation open.
        """
        computers = set(terminals) | terminal_tools.declaring(self._root)
        phones = set(push_tokens.registrations(self._root))

        def kind(name: str) -> str:
            if name in computers:
                return "a computer"
            return "his phone" if name in phones or self.phone_open(name) else ""

        return kind

    def _devices_line(
        self, names: list[str], kind: Callable[[str], str],
        terminals: dict[str, tuple[bool, bool]],
    ) -> str:
        def describe(name: str) -> str:
            if name in terminals:
                speaks, listens = terminals[name]
                does = [word for word, on in (("speaks", speaks), ("listens", listens)) if on]
                state = f"connected ({', '.join(does) or 'runs device tools only'})"
            elif self.phone_open(name):
                state = "its conversation is open"
            else:
                state = "not connected now"
            what = kind(name)
            return f"{name}, {what}, {state}" if what else f"{name}, {state}"

        where = (
            f"on the brain host {self._host}" if self._hub is not None
            else "on this " + ("Mac" if sys.platform == "darwin" else "machine")
        )
        if not names:
            return f"{WHERE_PREFIX} {where}."
        return f"{WHERE_PREFIX} {where}. His devices: {'; '.join(describe(n) for n in names)}."

    def _place_line(self, conn: sqlite3.Connection, turn_id: str | None) -> str | None:
        """Where the turn's device last put him; past Here's age limit, that it is unknown now."""
        if self._here is None:
            return None
        fix = self._here.last_report(conn, turn_id)
        if fix is None:
            return None
        age = describe_age(self._here.age_s(fix))
        if self._stale(fix):
            return (
                f"His place is unknown now: his last position report, from {fix.device}, is {age}"
                " old. where_am_i asks his phone."
            )
        if fix.place is None:
            return f"His phone's last position report, {age} ago, has no place name."
        if fix.left:
            return f"His phone last reported him leaving {fix.place}, {age} ago."
        return f"His phone last put him at {fix.place}, {age} ago."

    def _stale(self, fix: PhoneFix) -> bool:
        """Whether ``fix`` is older than Here trusts (``locate.max_age_s``)."""
        return self._here is not None and self._here.age_s(fix) > self._here.max_age_s

    def _current(self, conn: sqlite3.Connection, turn_id: str | None) -> PhoneFix | None:
        """The turn's device's report when Here still trusts it and it is not a departure."""
        if self._here is None:
            return None
        fix = self._here.last_report(conn, turn_id)
        return None if fix is None or fix.left or self._stale(fix) else fix

    def _around_line(self, conn: sqlite3.Connection, turn_id: str | None) -> str | None:
        """His street and the place he is at, from what is held for where the phone put him.

        Free: no request is made here (ADR 0284). It never fails the other lines.
        """
        if self._around is None or self._here is None:
            return None
        try:
            fix = self._current(conn, turn_id)
            if fix is None:
                return None
            now_ms = self._here.now_ms()
            return self._around.line(conn, (fix.lat, fix.lng), fix.accuracy_m, now_ms)
        except Exception:  # the other lines are worth more than this one.
            LOGGER.exception("whereabouts: the surroundings line failed, skipped")
            return None

    def _ask_line(self, conn: sqlite3.Connection, turn_id: str | None) -> str | None:
        """The note about an unnamed spot he is at now, once; it never fails the other lines."""
        if self._memory_db is None or self._zone is None or self._here is None:
            return None
        try:
            fix = self._current(conn, turn_id)
            if fix is None:
                return None
            now_ms = self._here.now_ms()
            spot = places.ask_once(self._memory_db, conn, (fix.lat, fix.lng), now_ms, self._zone)
        except Exception:  # the other lines are worth more than this one.
            LOGGER.exception("whereabouts: the unnamed-spot note failed, skipped")
            return None
        if spot is None:
            return None
        return ASK_PLACE_LINE.format(n=spot.facts.days_28, date=spot.facts.last_visit)
