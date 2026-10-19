"""The Mac ships what his iPhone and iPad had in the foreground, one entry per stretch (ADR 0304).

Source ``device-apps``, read from the Mac's synced Screen Time records
(:mod:`jarvis.surface.mac_screen_time`). At most every ``INTERVAL_MS`` the pass reads the events
at and after the oldest per-device cursor, pairs them per device, and ships:

- ``device_app``: one entry per closed stretch of one app, key
  ``device_app:<device id>:<bundle id>:<start_ms>:<12 hex of the fields>`` (so a send again is the
  same row and adds nothing), ``at_ms`` its start, fields ``device_id``, ``device`` ("iPhone",
  "iPad", from the model), ``bundle_id``, ``app`` and ``category`` (the store's name and primary
  genre, or ``None``), ``start_ms``, ``end_ms``, ``unpaired``;
- ``device_horizon``: when the newest event the Mac holds for a device moved, one entry with
  ``synced_ms``, key ``device_horizon:<device id>:<12 hex>``; the brain reads the newest, and what
  is after it is not yet known, not "no use".

A stretch whose "out" has not synced yet is not shipped. The device's cursor is the start of that
open stretch, else just past the newest event, so the next read starts at a boundary; an open
stretch older than a day is given up on and shipped cut at 30 minutes, unpaired. Cursors, the
horizon last shipped and the time of the last pass are ``meta`` rows, moved only after the brain
kept the batch. System overlays (Control Center, the app library, SpringBoard alerts) and stretches
under five seconds are dropped. Devices whose model is unknown, and Macs (TimeSink covers those),
are skipped.

Names: bundle ids resolve once through Apple's public lookup (China store, then US) to the store's
name and genre, cached with the misses in ``app-names.json`` beside the shipper's state; a short
table covers Apple's own apps the store does not list. A lookup that fails on the network leaves
the name unset, and then nothing is shipped that pass (the cursors stay), so no stretch is kept
without the name the store would give it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from jarvis.state import imports
from jarvis.surface.mac_screen_time import (
    ScreenTimeUnavailable,
    closed_intervals,
    pair_intervals,
    read_screen_time,
)
from jarvis.surface.mac_ship_core import SET_META, Out, Report, Sink, State, digest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)

SOURCE: Final = imports.DEVICE_SOURCE
APP_KIND: Final = imports.DEVICE_APP
HORIZON_KIND: Final = imports.DEVICE_HORIZON
NAMES_FILE: Final = "app-names.json"
"""Beside the shipper's state file."""
INTERVAL_MS: Final = 15 * 60 * 1000
OVERLAP_MS: Final = 10 * 60 * 1000
"""A read starts this far before the cursor, in case a device's records arrive out of order."""
MIN_STRETCH_MS: Final = 5 * 1000
GIVE_UP_MS: Final = 24 * 60 * 60 * 1000
LOOKUP_URL: Final = "https://itunes.apple.com/lookup"
LOOKUP_COUNTRIES: Final = ("cn", "us")
LOOKUP_TIMEOUT_S: Final = 5.0
LOOKUP_GAP_S: Final = 3.0
"""The store answers about 20 lookups a minute."""

_BUSY: Final = (403, 429)
_LAST: Final = "device-apps:last_ms"
CURSOR_PREFIX: Final = "device-apps:cursor:"
HORIZON_PREFIX: Final = "device-apps:horizon:"
_NOISE: Final = re.compile(
    r"SBSpotlightAlert|com\.apple\.(?:control-?center|springboard(?:\..+)?|PosterBoard"
    r"|ScreenshotServicesService|AuthenticationServicesUI|WebSheet|PassbookUIService"
    r"|HeadphoneProxService)",
    re.IGNORECASE,
)

APPLE_APPS: Final[dict[str, tuple[str, str | None]]] = {
    "com.apple.camera": ("相机", "Photo & Video"),
    "com.apple.mobileslideshow": ("照片", "Photo & Video"),
    "com.apple.mobilesafari": ("Safari", "Utilities"),
    "com.apple.Preferences": ("设置", "Utilities"),
    "com.apple.Maps": ("地图", "Navigation"),
    "com.apple.MobileSMS": ("信息", "Social Networking"),
    "com.apple.mobilephone": ("电话", "Utilities"),
    "com.apple.InCallService": ("电话", "Utilities"),
    "com.apple.mobiletimer": ("时钟", "Utilities"),
    "com.apple.findmy": ("查找", "Utilities"),
    "com.apple.Health": ("健康", "Health & Fitness"),
    "com.apple.Bridge": ("Watch", "Utilities"),
    "com.apple.mobilenotes": ("备忘录", "Productivity"),
    "com.apple.Music": ("音乐", "Music"),
    "com.apple.Passbook": ("钱包", "Finance"),
    "com.apple.weather": ("天气", "Weather"),
    "com.apple.mobilecal": ("日历", "Productivity"),
    "com.apple.mobilemail": ("邮件", "Productivity"),
    "com.apple.facetime": ("FaceTime", "Social Networking"),
    "com.apple.AppStore": ("App Store", None),
}
"""Apple's own apps, as he sees them: the store does not list these."""

type Lookup = Callable[[str], tuple[str | None, str | None] | None]
"""A bundle id to ``(name, genre)``, ``(None, None)`` for an app the store does not know, ``None``
when the store could not be asked."""


def itunes_lookup(bundle_id: str) -> tuple[str | None, str | None] | None:
    """The store's name and primary genre for ``bundle_id``; see :data:`Lookup`."""
    try:
        for country in LOOKUP_COUNTRIES:
            time.sleep(LOOKUP_GAP_S)
            answer = httpx.get(
                LOOKUP_URL,
                params={"bundleId": bundle_id, "country": country},
                timeout=LOOKUP_TIMEOUT_S,
            )
            if answer.status_code in _BUSY or answer.status_code >= 500:  # noqa: PLR2004
                return None
            found = (answer.json().get("results") or []) if answer.is_success else []
            if found and found[0].get("trackName"):
                return str(found[0]["trackName"]), found[0].get("primaryGenreName")
    except (httpx.TransportError, ValueError, AttributeError):
        return None
    return None, None


class AppNames:
    """Bundle id to ``(name, genre)``: the Apple table, then the cache file, then the store."""

    def __init__(self, path: Path, lookup: Lookup = itunes_lookup) -> None:
        """``path`` is the cache file; it need not exist yet."""
        self._path = path
        self._lookup = lookup
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            loaded = {}
        self._known: dict[str, list[str | None]] = loaded if isinstance(loaded, dict) else {}
        self._dirty = False
        self.incomplete = False
        """A lookup could not be answered, so some names are missing."""

    def get(self, bundle_id: str) -> tuple[str | None, str | None]:
        """The name and genre, or ``(None, None)`` when unknown or the store is unreachable."""
        if bundle_id in APPLE_APPS:
            return APPLE_APPS[bundle_id]
        if bundle_id in self._known:
            name, genre = self._known[bundle_id]
            return name, genre
        if self.incomplete:
            return None, None
        found = self._lookup(bundle_id)
        if found is None:
            self.incomplete = True  # one failure per pass, not one timeout per bundle
            return None, None
        self._known[bundle_id] = list(found)
        self._dirty = True
        return found

    def save(self) -> None:
        """Write the cache when it grew; whole or not at all."""
        if not self._dirty:
            return
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        partial = self._path.with_name(f".{self._path.name}.{os.getpid()}.part")
        partial.write_text(json.dumps(self._known, ensure_ascii=False, sort_keys=True))
        partial.replace(self._path)
        self._dirty = False


@dataclass
class Built:
    """What one read yields, before anything is sent."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    horizons: dict[str, int] = field(default_factory=dict)


def _label(device: Mapping[str, Any]) -> str | None:
    """The model's name ("iPhone", "iPad"), ``None`` for a Mac or a device with no model."""
    found = re.match(r"[A-Za-z]+", str(device.get("model") or ""))
    return None if found is None or found.group() == "Mac" else found.group()


def build(
    data: Mapping[str, Any],
    cursors: Mapping[str, int],
    horizons: Mapping[str, int],
    now_ms: int,
    names: AppNames,
) -> Built:
    """The entries for every closed stretch of ``data`` past each device's cursor, and the moves.

    ``cursors`` and ``horizons`` are what was shipped before, per device id.
    """
    built = Built()
    for device in data["devices"]:
        label = _label(device)
        events = [e for e in data["events"] if e["device"] == device["id"]]
        if label is None or not events:
            continue
        ident = str(device["id"])
        closed, tail = closed_intervals(events)
        if tail is not None and now_ms - tail[1] > GIVE_UP_MS:
            closed, tail = pair_intervals(events), None
        horizon = max(int(e["at_ms"]) for e in events)
        built.cursors[ident] = horizon + 1 if tail is None else tail[1]
        floor = cursors.get(ident, 0) - OVERLAP_MS
        for bundle, start, end, paired in closed:
            if start < floor or end - start < MIN_STRETCH_MS or _NOISE.fullmatch(bundle):
                continue
            name, genre = names.get(bundle)
            fields = {
                "device_id": ident,
                "device": label,
                "bundle_id": bundle,
                "app": name,
                "category": genre,
                "start_ms": start,
                "end_ms": end,
                "unpaired": not paired,
            }
            built.entries.append(
                {
                    "key": f"{APP_KIND}:{ident}:{bundle}:{start}:{digest(fields)}",
                    "kind": APP_KIND,
                    "zone": "UTC",
                    "at_ms": start,
                    "text": f"{label} {name or bundle}",
                    "fields": fields,
                }
            )
        if horizon > horizons.get(ident, 0):
            fields = {"device_id": ident, "device": label, "synced_ms": horizon}
            built.horizons[ident] = horizon
            built.entries.append(
                {
                    "key": f"{HORIZON_KIND}:{ident}:{digest(fields)}",
                    "kind": HORIZON_KIND,
                    "zone": "UTC",
                    "at_ms": horizon,
                    "text": f"{label} synced",
                    "fields": fields,
                }
            )
    return built


WARNED: set[str] = set()


def _held(state: State, prefix: str) -> dict[str, int]:
    rows = state.conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",))
    return {key[len(prefix) :]: int(value) for key, value in rows}


def ship(  # noqa: PLR0913 - the pass's state, sink and report, the clock and two test seams
    names_path: Path,
    state: State,
    sink: Sink,
    report: Report,
    now_ms: int,
    *,
    read: Callable[[int | None], dict[str, Any]] = read_screen_time,
    lookup: Lookup | None = None,
) -> None:
    """One pass: nothing if the last was under ``INTERVAL_MS`` ago, else read, build and send.

    Raises:
        ShipFailed: the brain did not keep the batch; the cursors stay and the next pass resends.
    """
    if now_ms - state.meta_int(_LAST) < INTERVAL_MS:
        return
    state.apply([(SET_META, (_LAST, str(now_ms)))])
    cursors, horizons = _held(state, CURSOR_PREFIX), _held(state, HORIZON_PREFIX)
    try:
        data = read(min(cursors.values()) - OVERLAP_MS if cursors else None)
        seen = {e["device"] for e in data["events"]}
        if cursors and any(_label(d) and d["id"] in seen - cursors.keys() for d in data["devices"]):
            data = read(None)  # a device the cursors have not met yet: all it has
    except ScreenTimeUnavailable as exc:
        if str(exc) not in WARNED:
            WARNED.add(str(exc))
            LOGGER.warning("mac-ship: device apps unavailable: %s", exc)
        return
    names = AppNames(names_path, itunes_lookup if lookup is None else lookup)
    built = build(data, cursors, horizons, now_ms, names)
    names.save()
    if names.incomplete:
        LOGGER.warning("mac-ship: the app store could not be asked; device apps wait")
        return
    out = Out(SOURCE, sink, state, report)
    for entry in built.entries:
        out.add(entry, [])
    out.note(
        [(SET_META, (f"{CURSOR_PREFIX}{i}", str(c))) for i, c in built.cursors.items()]
        + [(SET_META, (f"{HORIZON_PREFIX}{i}", str(h))) for i, h in built.horizons.items()],
    )
    out.flush()


def dry_run(hours: float, now_ms: int) -> tuple[Built, dict[str, Any]]:
    """What a pass over the last ``hours`` would ship, with real name lookups; sends nothing."""
    data = read_screen_time(int(now_ms - hours * 3_600_000))
    with tempfile.TemporaryDirectory(prefix="jarvis-device-apps-") as tmp:
        names = AppNames(Path(tmp) / NAMES_FILE)
        return build(data, {}, {}, now_ms, names), data


__all__ = ["AppNames", "Built", "build", "dry_run", "itunes_lookup", "ship"]
