"""The Mac ships what his iPhone and iPad had in the foreground, one entry per stretch (ADR 0304).

Source ``device-apps``, read from the Mac's synced Screen Time records
(:mod:`jarvis.surface.mac_screen_time`). At most every ``INTERVAL_MS`` the pass reads the events
at and after the oldest per-device cursor, pairs them per device, and ships:

- ``device_app``: one entry per closed stretch of one app, key
  ``device_app:<device id>:<bundle id>:<start_ms>:<12 hex of the fields>`` (so a send again is the
  same row and adds nothing), ``at_ms`` its start, fields ``device_id``, ``device`` ("iPhone",
  "iPad", from the model), ``bundle_id``, ``start_ms``, ``end_ms``, ``unpaired``;
- ``device_horizon``: when the newest event the Mac holds for a device moved, one entry with
  ``synced_ms``, key ``device_horizon:<device id>:<12 hex>``; the brain reads the newest, and what
  is after it is not yet known, not "no use";
- ``app_name``: what a bundle id is called, key ``app_name:<bundle id>:<12 hex>``, fields
  ``bundle_id``, ``app`` and ``category`` (the store's name and primary genre), sent once the
  bundle is resolved and again only if the resolved value changes.

A stretch whose "out" has not synced yet is not shipped. The device's cursor is the start of that
open stretch, else just past the newest event, so the next read starts at a boundary; an open
stretch older than a day is given up on and shipped cut at 30 minutes, unpaired. Cursors, the
horizon and the name last shipped and the time of the last pass are ``meta`` rows, moved only after
the brain kept the batch. System overlays (Control Center, the app library, SpringBoard alerts) and
stretches under five seconds are dropped. Devices whose model is unknown, and Macs (TimeSink covers
those), are skipped.

Names never hold the stretches back. A bundle resolves through a short table of Apple's own apps,
else Apple's public lookup (China store, then US), at most ``LOOKUPS_PER_PASS`` a pass, the bundles
of this pass first. The answers are cached in ``app-names.json`` beside the shipper's state as
``[name, genre, checked_ms]``: a miss is asked again after ``MISS_RETRY_MS``, and a bundle the
store could not be asked about (network, 403, 429, 5xx, or no lookups left this pass) is
``checked_ms`` 0, asked again next pass. Until a name arrives the day line shows the bundle id.
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
    from collections.abc import Callable, Iterable, Mapping

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
LOOKUPS_PER_PASS: Final = 20
MISS_RETRY_MS: Final = 7 * 24 * 60 * 60 * 1000

_BUSY: Final = (403, 429)
_LAST: Final = "device-apps:last_ms"
CURSOR_PREFIX: Final = "device-apps:cursor:"
HORIZON_PREFIX: Final = "device-apps:horizon:"
NAME_PREFIX: Final = "device-apps:name:"
NAME_KIND: Final = imports.DEVICE_NAME
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
    """Bundle id to ``(name, genre)``: the Apple table, the cache file, then the store."""

    def __init__(
        self,
        path: Path,
        now_ms: int,
        lookup: Lookup = itunes_lookup,
        budget: int = LOOKUPS_PER_PASS,
    ) -> None:
        """``path`` is the cache file (it need not exist yet); ``budget`` caps the lookups."""
        self._path = path
        self._now = now_ms
        self._lookup = lookup
        self._budget = budget
        try:
            loaded = json.loads(path.read_text())
        except (OSError, ValueError):
            loaded = {}
        self._known: dict[str, list[Any]] = loaded if isinstance(loaded, dict) else {}
        self._dirty = False

    def _due(self, bundle_id: str) -> bool:
        found = self._known.get(bundle_id)
        if found is None or not found[2]:
            return True
        return found[0] is None and self._now - found[2] >= MISS_RETRY_MS

    def resolve(self, wanted: Iterable[str]) -> dict[str, tuple[str, str | None]]:
        """Every name known now, for ``wanted`` and for what the cache already holds.

        Looks up what is due, ``wanted`` first, until the budget is spent or the store cannot be
        asked; the rest stays due for the next pass.
        """
        wanted = list(dict.fromkeys(wanted))
        order = [b for b in wanted if b not in APPLE_APPS]
        order += [b for b in self._known if b not in order]
        asking = True
        for bundle in order:
            if not self._due(bundle):
                continue
            found = self._lookup(bundle) if asking and self._budget > 0 else None
            self._budget -= asking
            asking = asking and found is not None
            self._known[bundle] = [*(found or (None, None)), self._now if found else 0]
            self._dirty = True
        named = {b: (str(v[0]), v[1]) for b, v in self._known.items() if v[0] is not None}
        return {**named, **{b: APPLE_APPS[b] for b in wanted if b in APPLE_APPS}}

    def save(self) -> None:
        """Write the cache when it changed; whole or not at all."""
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
            fields = {
                "device_id": ident,
                "device": label,
                "bundle_id": bundle,
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
                    "text": f"{label} {bundle}",
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


def _held(state: State, prefix: str) -> dict[str, str]:
    rows = state.conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"{prefix}%",))
    return {key[len(prefix) :]: value for key, value in rows}


def _name_entries(
    names: Mapping[str, tuple[str, str | None]],
    sent: Mapping[str, str],
    now_ms: int,
) -> list[tuple[dict[str, Any], str]]:
    """``(entry, digest)`` for each name that was never sent or has changed since."""
    out = []
    for bundle, (name, genre) in sorted(names.items()):
        fields = {"bundle_id": bundle, "app": name, "category": genre}
        if sent.get(bundle) == digest(fields):
            continue
        entry = {
            "key": f"{NAME_KIND}:{bundle}:{digest(fields)}",
            "kind": NAME_KIND,
            "zone": "UTC",
            "at_ms": now_ms,
            "text": name,
            "fields": fields,
        }
        out.append((entry, digest(fields)))
    return out


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
    cursors = {i: int(v) for i, v in _held(state, CURSOR_PREFIX).items()}
    horizons = {i: int(v) for i, v in _held(state, HORIZON_PREFIX).items()}
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
    built = build(data, cursors, horizons, now_ms)
    names = AppNames(names_path, now_ms, itunes_lookup if lookup is None else lookup)
    known = names.resolve(e["fields"]["bundle_id"] for e in built.entries if e["kind"] == APP_KIND)
    names.save()
    out = Out(SOURCE, sink, state, report)
    for entry in built.entries:
        out.add(entry, [])
    for entry, kept in _name_entries(known, _held(state, NAME_PREFIX), now_ms):
        out.add(entry, [(SET_META, (NAME_PREFIX + entry["fields"]["bundle_id"], kept))])
    out.note(
        [(SET_META, (f"{CURSOR_PREFIX}{i}", str(c))) for i, c in built.cursors.items()]
        + [(SET_META, (f"{HORIZON_PREFIX}{i}", str(h))) for i, h in built.horizons.items()],
    )
    out.flush()


def dry_run(
    hours: float, now_ms: int
) -> tuple[Built, dict[str, Any], dict[str, tuple[str, str | None]]]:
    """What a pass over the last ``hours`` would ship, and the names of its apps, from real lookups.

    Nothing is sent and nothing is kept; the lookup budget is lifted.
    """
    data = read_screen_time(int(now_ms - hours * 3_600_000))
    built = build(data, {}, {}, now_ms)
    with tempfile.TemporaryDirectory(prefix="jarvis-device-apps-") as tmp:
        names = AppNames(Path(tmp) / NAMES_FILE, now_ms, budget=10**6)
        known = names.resolve(
            e["fields"]["bundle_id"] for e in built.entries if e["kind"] == APP_KIND
        )
    return built, data, known


__all__ = ["AppNames", "Built", "build", "dry_run", "itunes_lookup", "ship"]
