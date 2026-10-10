"""The pinned bus trip: what the notch is served and the live refresh behind it (ADR 0200).

The state is the event log (``jarvis.state.departures``). While the pin's bus has not left, the
reminder tick calls :meth:`Departures.refresh`, which every ``REFRESH_S`` asks BC Transit's live
feed (``jarvis.runtime.bus_live``) when that bus really leaves. If it moved, ``departure.updated``
is written and the pin's reminder moves with it. A bus the feed does not show, or a feed that does
not answer, leaves the times as they were and marks the served pin ``stale``; a failed refresh
never drops the pin.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from zoneinfo import ZoneInfo

from jarvis.execution.tools import ToolError
from jarvis.execution.transit_tool import TransitOffers, pin_offered, pin_trip
from jarvis.shared import lang
from jarvis.state import departures as folded
from jarvis.state import reminders
from jarvis.state.event_log import open_runtime_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.bus_live import BusLive

LOGGER = logging.getLogger(__name__)

# The feed itself moves about every 30 s. The reminder tick is 15 s, so this is every second tick;
# the margin keeps a tick that comes a little early from skipping a whole round.
REFRESH_S: Final[float] = 30.0
_EARLY_S: Final[float] = 5.0
_ZONE: Final = ZoneInfo("America/Vancouver")
_MS_PER_MIN: Final[int] = 60_000


def _clock(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, _ZONE).strftime("%H:%M")


def _shifted(one: folded.Departure, departs_ms: int, shift_ms: int) -> str:
    """The pin's ``arrive_at`` moved by ``shift_ms``, on the day of the new departure."""
    arrive = datetime.combine(
        datetime.fromtimestamp(departs_ms / 1000, _ZONE).date(),
        datetime.strptime(one.arrive_at, "%H:%M").time(),  # noqa: DTZ007 - a clock only.
        tzinfo=_ZONE,
    ) + timedelta(milliseconds=shift_ms)
    return f"{arrive:%H:%M}"


class Departures:
    """The pin's served view, its unpin and its live refresh; built once at boot."""

    def __init__(
        self, event_log: Path, live: BusLive | None = None, offers: TransitOffers | None = None,
    ) -> None:
        """``live`` is BC Transit's feed; without it the pin keeps Google's times.

        ``offers`` is what ``transit`` left for the notch's card (ADR 0203).
        """
        self._path = event_log
        self.live = live
        self.offers = offers if offers is not None else TransitOffers()
        self._undo: folded.Departure | None = None
        self._delay_min = 0
        self._checked_ms: int | None = None
        self.now: Callable[[], datetime] = lambda: datetime.now(UTC)
        self._pin: str | None = None
        self._last = 0.0
        self._trip: str | None = None
        self._stale = False

    # --- what the surface reads -----------------------------------------------------

    def view(self) -> dict[str, Any] | None:
        """The pin while its bus has not left, as ``GET /inherent/notices`` serves it."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.active(conn, int(self.now().timestamp() * 1000))
        if one is None:
            return None
        return {
            "id": one.pin_id,
            "route": one.route,
            "board_stop": one.board_stop,
            "leave_at_ms": one.leave_at_ms,
            "departs_at_ms": one.departs_at_ms,
            "leave_at": _clock(one.leave_at_ms),
            "departs": _clock(one.departs_at_ms),
            "arrive_at": one.arrive_at,
            "to": one.to,
            "stale": self._stale and one.pin_id == self._pin,
            "delay_min": self._delay_min if one.pin_id == self._pin else 0,
            "checked_at_ms": self._checked_ms if one.pin_id == self._pin else None,
        }

    def offer(self) -> dict[str, Any] | None:
        """The latest ``transit`` answer's options while the card may show them."""
        return self.offers.view()

    def unpin(self, pin_id: str) -> None:
        """``POST /inherent/notices/{id}``: take the pin off and cancel its reminder."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.current(conn)
            if one is None or one.pin_id != pin_id:
                return
            self._undo = one
            ringing = reminders.fold(conn).get(one.reminder_id)
            if ringing is not None and ringing.pending:
                reminders.cancel(conn, one.reminder_id, action_id=pin_id)
            folded.unpin(conn, pin_id)

    # --- what the notch's cards ask for (ADR 0203) --------------------------------------

    def act(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /inherent/departure``: one of the cards' three actions."""
        match body["action"]:
            case "pin":
                return self.pin_offer(body["offer_id"], body["index"])
            case "next":
                return self.next_bus()
            case _:
                return self.undo(body["pin_id"])

    def pin_offer(self, offer_id: str, index: int) -> dict[str, Any]:
        """Pin one option of the offer, as the tool would; LookupError when it cannot be."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            pin_offered(conn, self.offers, offer_id, index)
        return {"departure": self.view(), "reason": None}

    def next_bus(self) -> dict[str, Any]:
        """Move the pin to the next later bus of its route at its stop, or say there is none."""
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.active(conn, int(self.now().timestamp() * 1000))
            if one is None:
                msg = "nothing is pinned"
                raise LookupError(msg)
            found = self._later(one)
            reason: str | None = "no_later"
            if found is not None:
                reason = self._repin(conn, one, *found)
        return {"departure": self.view(), "reason": reason}

    def undo(self, pin_id: str) -> dict[str, Any]:
        """Pin again the trip just unpinned, unless its time to leave has passed."""
        one = self._undo
        if one is None or one.pin_id != pin_id:
            msg = "nothing to bring back"
            raise LookupError(msg)
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            reason = self._repin(conn, one, one.leave_at_ms, one.departs_at_ms, one.arrive_at)
        if reason is None:
            self._undo = None
        return {"departure": self.view(), "reason": reason}

    @staticmethod
    def _repin(
        conn: sqlite3.Connection,
        one: folded.Departure,
        leave_ms: int,
        departs_ms: int,
        arrive_at: str,
    ) -> str | None:
        """Pin ``one``'s trip at these times through the tool's path; None, or why it cannot be."""
        lat, lng = one.stop_lat, one.stop_lng
        stop = None if lat is None or lng is None else (lat, lng)
        try:
            pin_trip(
                conn,
                route=one.route,
                board_stop=one.board_stop,
                leave=datetime.fromtimestamp(leave_ms / 1000, _ZONE),
                departs=datetime.fromtimestamp(departs_ms / 1000, _ZONE),
                arrive_at=arrive_at,
                to=one.to,
                stop=stop,
                action_id=one.pin_id,
            )
        except ToolError:
            return "past"
        return None

    def _later(self, one: folded.Departure) -> tuple[int, int, str] | None:
        """``(leave ms, departs ms, arrive_at)`` of the next bus: last answer or feed."""
        after = datetime.fromtimestamp(one.departs_at_ms / 1000, _ZONE)
        if later := self.offers.later(one.first_route, one.board_stop, after):
            return int(later[0].timestamp() * 1000), int(later[1].timestamp() * 1000), later[2]
        if self.live is None or one.stop_lat is None or one.stop_lng is None:
            return None
        try:
            found = self.live.next_departure(
                one.first_route, (one.stop_lat, one.stop_lng), one.departs_at_ms,
                self._trip if one.pin_id == self._pin else None,
            )
        except Exception:  # noqa: BLE001 - network, zip or feed trouble all mean no answer.
            LOGGER.warning("departures: BC Transit live data unavailable", exc_info=True)
            return None
        if found is None:
            return None
        departs = round(found[1] / _MS_PER_MIN) * _MS_PER_MIN
        shift = departs - one.departs_at_ms
        return one.leave_at_ms + shift, departs, _shifted(one, departs, shift)

    # --- the live refresh -----------------------------------------------------------

    def refresh(self) -> None:
        """One refresh if the pin is due for one; a failure is logged and never raised."""
        try:
            self._refresh()
        except Exception:
            LOGGER.exception("departures: refresh failed; the pin keeps its times")
            self._stale = True

    def _refresh(self) -> None:
        now = self.now()
        with contextlib.closing(open_runtime_event_log(self._path)) as conn:
            one = folded.active(conn, int(now.timestamp() * 1000))
            if one is None:
                self._pin = None
                return
            if one.pin_id != self._pin:
                # A new pin starts with Google's times, fresh from the trip lookup.
                self._pin, self._last = one.pin_id, now.timestamp()
                self._trip, self._stale = None, False
                self._delay_min, self._checked_ms = 0, None
                return
            if now.timestamp() - self._last < REFRESH_S - _EARLY_S:
                return
            self._last = now.timestamp()
            found = None
            if self.live is not None and one.stop_lat is not None and one.stop_lng is not None:
                try:
                    found = self.live.departure(
                        one.first_route,
                        (one.stop_lat, one.stop_lng),
                        one.departs_at_ms,
                        self._trip,
                    )
                except Exception:  # noqa: BLE001 - network, zip or feed trouble all mean stale.
                    LOGGER.warning("departures: BC Transit live data unavailable", exc_info=True)
            self._stale = found is None
            if found is None:
                return
            self._trip, self._checked_ms = found[0], int(now.timestamp() * 1000)
            # The minute it will leave; seconds would move the pin on every wobble of the feed.
            departs = round(found[1] / _MS_PER_MIN) * _MS_PER_MIN
            shift = departs - one.departs_at_ms
            if not shift:
                return
            self._delay_min += shift // _MS_PER_MIN
            reminder_id = one.reminder_id
            leave = one.leave_at_ms + shift
            ringing = reminders.fold(conn).get(reminder_id)
            if ringing is not None and ringing.pending:
                reminders.cancel(conn, reminder_id, action_id=one.pin_id)
                reminder_id = reminders.schedule(
                    conn,
                    due=datetime.fromtimestamp(leave / 1000, _ZONE),
                    text=lang.t(
                        "departure.go", route=one.first_route, departs=_clock(departs),
                        stop=one.board_stop,
                    ),
                    action_id=one.pin_id,
                )
            folded.update(
                conn,
                one.pin_id,
                reminder_id=reminder_id,
                leave_at_ms=leave,
                departs_at_ms=departs,
                arrive_at=_shifted(one, departs, shift),
            )
            LOGGER.info("departure %s moved by %d min", one.pin_id, shift // _MS_PER_MIN)
