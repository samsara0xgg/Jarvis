"""ADR 0305: what his iPhone and iPad sync to the Mac, folded into the day line's items.

The rows are the ``device-records`` fields the Mac terminal shipped (:mod:`jarvis.state.imports`
``RECORD_*``); the functions here are the folds, each on the rows it needs, and
:mod:`jarvis.state.day_line` reads the store, adds ``lane`` and ``state`` and cuts to the window.
An item here is ``{id, kind, start_ms, end_ms, ...}`` with ``end_ms`` ``None`` while it goes on.

- ``media``: one device playing one app. A ``now_playing`` record in state 1 (playing) starts a
  stretch that runs to the next record, no longer than the track (its ``duration_s``; a track of
  unknown length counts for at most 30 minutes). Stretches of one app whose gap is at most five
  minutes are one block; a block with less than two minutes of playing is dropped. Fields
  ``device``, ``app`` and ``bundle``, ``active_s`` (seconds playing), ``titles`` (the five with the
  most playing, each ``{title, artist}``) and ``route`` (where the sound went: the earbuds' name
  when the output is a Bluetooth address a ``bluetooth`` row names, else "Bluetooth", else the
  output's own name).
- ``earbuds``: a Bluetooth device connected, from the record that says so to the next that says it
  is not. Fields ``device``, ``device_name``, ``battery_connect`` and ``battery_disconnect`` (the
  three levels in the order the stream gives them). A connection with no disconnect yet goes on
  for twelve hours after it began; past that, or when another connect comes later than that, it is
  cut to a point, for a device that stopped reporting does not stay connected.
- ``messages``: his messages in one conversation of one app, in bursts whose gaps are at most
  thirty minutes. Fields ``device``, ``app``, ``bundle``, ``conversation`` (what Siri calls it,
  else the other person's name, else their handle), ``is_group``, ``sent`` and ``received``.
- ``phone_call``: a call Siri remembers, from its start for the seconds it lasted. Fields
  ``device``, ``app``, ``bundle``, ``direction`` ("sent" or "received"), ``person`` and
  ``duration_s``. It is not ``call`` (that one is TimeSink's, a call on the Mac).

Layer rules: stdlib only (L2).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

_MINUTE_MS: Final = 60_000
MEDIA_MERGE_GAP_MS: Final = 5 * _MINUTE_MS
MEDIA_MIN_S: Final = 2 * 60
MEDIA_UNKNOWN_TRACK_MS: Final = 30 * _MINUTE_MS
MEDIA_TOP: Final = 5
EARBUDS_OPEN_MS: Final = 12 * 60 * _MINUTE_MS
MESSAGE_MERGE_GAP_MS: Final = 30 * _MINUTE_MS
_BLUETOOTH_OUT: Final = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})-t\w+")
_WHO_ROLES: Final = (
    "recipients",
    "recipientHandles",
    "contacts",
    "contactHandles",
    "sender",
    "senderHandle",
)
_NAMED: Final = 3
"""Names shown for a conversation that has none of its own."""


def route_name(route: str | None, earbuds: Mapping[str, str]) -> str | None:
    """Where the sound went: ``earbuds`` maps an upper-case Bluetooth address to its name."""
    if not route:
        return None
    found = _BLUETOOTH_OUT.fullmatch(route)
    if found is None:
        return route
    return earbuds.get(found.group(1).upper()) or "Bluetooth"


def bluetooth_names(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Each Bluetooth address (upper case) to the newest name its rows gave."""
    names: dict[str, str] = {}
    for row in sorted(rows, key=lambda f: f["at_ms"]):
        if row.get("name"):
            names[str(row["address"]).upper()] = str(row["name"])
    return names


# --- media -----------------------------------------------------------------------------------


def _stretches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One device's now-playing records as stretches of playing, oldest first."""
    at_times: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for one in records:
        at_times[int(one["at_ms"])].append(one)
    times = sorted(at_times)
    out: list[dict[str, Any]] = []
    for index, at in enumerate(times):
        playing = [r for r in at_times[at] if r.get("state") == 1]
        if not playing:
            continue
        # records of one instant have no order: the one that says what plays stands for them
        one = max(playing, key=lambda r: bool(r.get("title")))
        following = times[index + 1] if index + 1 < len(times) else None
        known = int(one["duration_s"]) * 1000 if one.get("duration_s") else None
        if following is None:
            end = at + (known or 0)
        else:
            end = min(following, at + (known or MEDIA_UNKNOWN_TRACK_MS))
        out.append({**one, "start": at, "end": end})
    return out


def media_items(
    rows: Iterable[Mapping[str, Any]],
    names: Mapping[str, str],
    earbuds: Mapping[str, str],
) -> list[dict[str, Any]]:
    """The ``media`` blocks of ``now_playing`` rows; ``names`` maps a bundle id to its app."""
    by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_device[str(row["device_id"])].append(dict(row))
    items: list[dict[str, Any]] = []
    for records in by_device.values():
        block: list[dict[str, Any]] = []
        for stretch in _stretches(records):
            if block and (
                stretch.get("bundle_id") != block[-1].get("bundle_id")
                or stretch["start"] - max(s["end"] for s in block) > MEDIA_MERGE_GAP_MS
            ):
                items.extend(_media_block(block, names, earbuds))
                block = []
            block.append(stretch)
        items.extend(_media_block(block, names, earbuds))
    return items


def _media_block(
    block: list[dict[str, Any]], names: Mapping[str, str], earbuds: Mapping[str, str]
) -> list[dict[str, Any]]:
    if not block:
        return []
    seconds = sum(s["end"] - s["start"] for s in block) / 1000
    if seconds < MEDIA_MIN_S:
        return []
    per_title: dict[tuple[str, str | None], float] = defaultdict(float)
    per_route: dict[str | None, float] = defaultdict(float)
    for s in block:
        long = (s["end"] - s["start"]) / 1000
        if s.get("title"):
            per_title[(s["title"], s.get("artist"))] += long
        per_route[route_name(s.get("route"), earbuds)] += long
    start, end = block[0]["start"], max(s["end"] for s in block)
    bundle = block[0].get("bundle_id") or ""
    device = block[0]["device"]
    top = sorted(per_title, key=lambda t: -per_title[t])[:MEDIA_TOP]  # ties: first heard first
    return [
        {
            "id": f"media-{device}-{start}",
            "kind": "media",
            "start_ms": start,
            "end_ms": end,
            "device": device,
            "app": names.get(bundle) or bundle,
            "bundle": bundle,
            "active_s": round(seconds),
            "titles": [{"title": title, "artist": artist} for title, artist in top],
            "route": max(per_route, key=lambda r: per_route[r]),
        }
    ]


# --- earbuds ---------------------------------------------------------------------------------


def earbuds_items(rows: Iterable[Mapping[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    """The ``earbuds`` spans of ``bluetooth`` rows, one per connect to the next disconnect."""
    by_device: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        by_device[(str(row["device_id"]), str(row["address"]))].append(row)
    items: list[dict[str, Any]] = []
    for records in by_device.values():
        began: Mapping[str, Any] | None = None
        for one in sorted(records, key=lambda f: f["at_ms"]):
            if one.get("connected"):
                if began is not None and one["at_ms"] - began["at_ms"] > EARBUDS_OPEN_MS:
                    items.append(_span(began, None))  # its disconnect was never heard
                    began = None
                began = began or one
            elif began is not None:
                items.append(_span(began, one))
                began = None
        if began is not None:
            recent = now_ms - began["at_ms"] < EARBUDS_OPEN_MS
            items.append(_span(began, None, going=recent))
    return items


def _span(
    began: Mapping[str, Any], ended: Mapping[str, Any] | None, *, going: bool = False
) -> dict[str, Any]:
    start = int(began["at_ms"])
    return {
        "id": f"earbuds-{began['device']}-{start}",
        "kind": "earbuds",
        "start_ms": start,
        "end_ms": int(ended["at_ms"]) if ended is not None else (None if going else start),
        "device": began["device"],
        "device_name": began.get("name") or (ended or {}).get("name") or began["address"],
        "battery_connect": list(began.get("battery") or []),
        "battery_disconnect": list(ended.get("battery") or []) if ended is not None else None,
    }


# --- messages and calls ----------------------------------------------------------------------


def _who(row: Mapping[str, Any]) -> str | None:
    """The other person of a record: the names of the first role that has any, else a handle."""
    people = row.get("people") or []
    for key in ("name", "handle"):
        for role in _WHO_ROLES:
            found = list(dict.fromkeys(p[key] for p in people if p["role"] == role and p.get(key)))
            if found:
                return ", ".join(found[:_NAMED])
    return None


def message_items(
    rows: Iterable[Mapping[str, Any]], names: Mapping[str, str]
) -> list[dict[str, Any]]:
    """The ``messages`` bursts of ``message`` rows, per conversation of one app on one device."""
    chats: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        label = row.get("chat_name") or _who(row) or ""
        chat = row.get("conversation_id") or label
        chats[(str(row["device_id"]), row.get("bundle_id") or "", chat)].append(row)
    items: list[dict[str, Any]] = []
    for key, records in chats.items():
        records.sort(key=lambda f: f["at_ms"])
        burst: list[Mapping[str, Any]] = []
        for one in records:
            if burst and one["at_ms"] - burst[-1]["at_ms"] > MESSAGE_MERGE_GAP_MS:
                items.append(_burst(key, burst, names))
                burst = []
            burst.append(one)
        items.append(_burst(key, burst, names))
    return items


def _burst(
    key: tuple[str, str, str], burst: list[Mapping[str, Any]], names: Mapping[str, str]
) -> dict[str, Any]:
    start, end = int(burst[0]["at_ms"]), int(burst[-1]["at_ms"])
    bundle = key[1]
    label = next((c for one in reversed(burst) if (c := one.get("chat_name") or _who(one))), None)
    tag = hashlib.sha256("\x00".join(key).encode()).hexdigest()[:8]
    return {
        "id": f"messages-{tag}-{start}",
        "kind": "messages",
        "start_ms": start,
        "end_ms": end,
        "device": burst[0]["device"],
        "app": names.get(bundle) or bundle,
        "bundle": bundle,
        "conversation": label,
        "is_group": any(one.get("is_group") for one in burst),
        "sent": sum(1 for one in burst if one.get("direction") == "sent"),
        "received": sum(1 for one in burst if one.get("direction") == "received"),
    }


def call_items(rows: Iterable[Mapping[str, Any]], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """The ``phone_call`` items of ``call`` rows."""
    items: list[dict[str, Any]] = []
    for row in rows:
        start = int(row["at_ms"])
        seconds = int(row.get("duration_s") or 0)
        bundle = row.get("bundle_id") or ""
        items.append(
            {
                "id": f"phone_call-{row['device']}-{start}-{str(row.get('uuid', ''))[:8]}",
                "kind": "phone_call",
                "start_ms": start,
                "end_ms": start + seconds * 1000,
                "device": row["device"],
                "app": names.get(bundle) or bundle,
                "bundle": bundle,
                "direction": row.get("direction"),
                "person": _who(row),
                "duration_s": seconds,
            }
        )
    return items
