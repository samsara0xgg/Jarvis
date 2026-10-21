"""Where he is: the locator, Here, the bus feed and the places' geocoding."""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from jarvis.execution.geocode import Unplaced, geocode
from jarvis.runtime.locate import Locator
from jarvis.state import places as place_state
from jarvis.surface.mac_location import read_mac_location

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime.config import (
        LocateSettings,
    )
    from jarvis.shared.clock import Clock
    from jarvis.state.locate_wait import LocateWaits


LOGGER = logging.getLogger(__name__)


def _locator(settings: LocateSettings, waits: LocateWaits, clock: Clock) -> Locator:
    """``locate`` — how long Here waits for his phone's fresh fix (ADR 0281)."""
    return Locator(
        waits, clock, socket_wait_s=settings.socket_wait_s, push_wait_s=settings.push_wait_s,
    )


def _here_location(role: str) -> Callable[[], dict[str, Any]] | None:
    """This Mac's location for `bus` and `where_am_i`, read on demand (ADR 0194).

    None in the `brain` role, which has no Mac to read, and off macOS. Every role also gets the
    phone's last report (`_phone_here`, ADR 0198); which one answers is decided per turn
    (ADR 0212), and a brain's only reading is the phone's.
    """
    if role == "brain" or sys.platform != "darwin":
        return None
    return lambda: asdict(read_mac_location())


def _locate_places(db_path: Path, api_key: str) -> None:
    """ADR 0237: give each place kept as an address the point Google gives it, once.

    A place is asked about until a geocoder change touches it; one that fails stays an address
    and is asked again at the next boot. The log names the place, never its address or the key.
    """
    for one in place_state.unlocated(db_path):
        if one.address is None:
            continue
        try:
            lat, lng = geocode(one.address, api_key)
            written = place_state.locate(db_path, one.id, lat=lat, lng=lng)
        except (Unplaced, LookupError, ValueError, sqlite3.Error) as exc:
            LOGGER.warning("place %r not located: %s", one.name, exc)
            continue
        if written.change_id is not None:
            LOGGER.info("place %r located from its address", one.name)


def _start_locating_places(db_path: Path, api_key: str) -> None:
    """Run :func:`_locate_places` in the background when there is a key and a place to locate."""
    if api_key and place_state.unlocated(db_path):
        threading.Thread(
            target=_locate_places, args=(db_path, api_key), name="jarvis-place-locate", daemon=True,
        ).start()
