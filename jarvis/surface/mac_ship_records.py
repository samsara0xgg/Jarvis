"""The Mac ships what else his iPhone and iPad sync to it: playing, earbuds, who he wrote to (0305).

Source ``device-records``, read with the same helper as ``device-apps``
(:mod:`jarvis.surface.mac_screen_time`, ``streams``). At most every ``INTERVAL_MS`` the pass reads
the records at and after the oldest per-stream cursor and ships one entry per record, key
``<kind>:<device id>:<time or uuid>-<8 hex>:<12 hex of the fields>``. The eight hex digits tell
apart records that share a millisecond or a uuid (many do), so a row is one record and a send
again is the same row. Kinds, fields in ``fields`` beside ``device_id``, ``device`` ("iPhone",
"iPad", from the model) and ``at_ms``; a field with nothing in it is left out:

- ``now_playing`` (Media.NowPlaying, about four weeks): ``state`` (0 to 4; 1 playing, 2 paused,
  3 stopped, 4 interrupted, as MRPlaybackState has it), ``title``, ``artist``, ``duration_s``,
  ``text`` (the album, a post's text or a lyric line, kept whole up to ``TEXT_CAP`` bytes, then
  ``text_truncated``), ``media_type``, ``route`` (the output's name) and ``route_codes``,
  ``bundle_id``;
- ``bluetooth`` (Device.Wireless.Bluetooth, about four weeks): ``address``, ``name``,
  ``product_id``, ``vendor_id``, ``connected``, ``battery`` (the three levels in field order),
  ``extra`` (the fields that never change); the time is the segment's own, the payload has none;
- ``message`` and ``call`` (Siri.Remembers.MessageHistory and CallHistory, about a year): who and
  when and in which app, no words (none are kept anywhere): ``uuid``, ``bundle_id``, ``intent``,
  ``domain``, ``origin``, ``direction`` ("sent", "received"), ``conversation_id``, ``is_group``,
  ``chat_name`` (what Siri calls the conversation: a group's name, a channel, the other person),
  ``people`` (each ``{role, name, contact_id}`` or ``{role, name, handle, handle_type, label,
  country}``), ``message_ids``, and for a call ``duration_s``;
- ``intent`` (InteractionHistory and AudioHistory): ``stream`` ("interaction" or "audio"),
  ``uuid``, ``intent``, ``domain``, ``bundle_id``, ``origin``, ``direction``, ``conversation_id``
  and ``items``, every role entry the record held as ``{role, value, type, name, ...}``.

Three more kinds come from this Mac's own databases (ADR 0309), read by the same helper under
``device_id`` ``mac``, ``device`` "Mac". They are stored, not interpreted, and their key carries
the record's own id (an asset uuid, a message guid, a visit's time and its id in the file) instead
of a few digits of the fields, so a record that changes later (a photo edited, a message read) is a
newer revision of the same row:

- ``photo`` (Photos.sqlite): one asset, metadata only: ``uuid``, ``kind`` ("photo", "video"),
  ``subtype``, the flags ``screenshot``, ``live``, ``burst`` beside the codes they come from,
  ``added_ms``, ``modified_ms``, ``trashed_ms``, ``latitude`` and ``longitude`` when it has a
  position, size, ``duration_s``, ``favorite``, ``hidden``, ``trashed``, ``original_filename``,
  the camera and exposure fields, ``moment_title`` and the reverse-geocoded ``place``;
- ``chat_message`` (Messages/chat.db): one message of iMessage, SMS or RCS: ``guid``, ``service``,
  ``handle``, ``from_me``, ``read_ms``, ``delivered_ms``, the chat's ``chat_identifier``,
  ``chat_display_name`` and ``participants``, ``text`` (kept whole up to ``TEXT_CAP`` bytes, then
  ``text_truncated``; ``text_source`` says whether it was the ``text`` column or decoded from
  ``attributedBody``), the tapback and reply targets, and ``attachments`` (name, type, size);
- ``safari_visit`` (Safari/History.db): one visit: ``url`` (cut at ``TEXT_CAP`` like the text),
  ``title``, ``visit_id``, ``origin`` (0 this Mac, 1 synced from another device, inferred),
  ``redirect_source``, ``redirect_destination``, ``load_successful``.

Cursors (``device-records:cursor:<stream>:<device id>``, the newest time shipped) and each row's
memory move only after the brain kept the batch. A read starts ``OVERLAP_MS`` before the oldest
cursor, never more than ``LOOKBACK_MS`` back: a record that reaches the Mac later than that is
not seen again. The first read, and the first read after a device or stream is met, takes all
the Mac holds, a year of Siri included. The three databases have their own window
(``LOCAL_OVERLAP_MS``, ``LOCAL_LOOKBACK_MS``) over the newest time a row was made, changed or
read, and are read whole once a day (``FULL_EVERY_MS``), because their rows arrive late (iCloud
brings old messages and photos down) and change after the fact; a row the brain holds is never
sent twice. The apps' names are the ``app_name`` rows of ``device-apps``
(:mod:`mac_ship_devices`): bundles first met here are resolved and sent there.

``python -m jarvis.surface.mac_ship_records dry-run`` prints what a pass would ship, masked, and
with ``--fold`` ships it to a temporary store and counts what today's day line makes of it.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import imports
from jarvis.surface import mac_ship_devices as devices
from jarvis.surface.mac_screen_time import ScreenTimeUnavailable, read_screen_time
from jarvis.surface.mac_ship_core import (
    BATCH_BYTES,
    BATCH_LINES,
    SET_META,
    SET_ROW,
    Out,
    Report,
    Sink,
    State,
    decide,
    digest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

LOGGER = logging.getLogger(__name__)

SOURCE: Final = imports.RECORDS_SOURCE
LOCAL_STREAMS: Final = ("photos", "messages", "safari")
"""This Mac's own databases (ADR 0309), as the helper names them."""
STREAMS: Final = "now_playing,bluetooth,interactions," + ",".join(LOCAL_STREAMS)
INTERVAL_MS: Final = devices.INTERVAL_MS
OVERLAP_MS: Final = 12 * 60 * 60 * 1000
LOOKBACK_MS: Final = 3 * 24 * 60 * 60 * 1000
LOCAL_OVERLAP_MS: Final = 3 * 24 * 60 * 60 * 1000
"""A message is read, delivered or edited, a photo changed, within days of when it was made."""
LOCAL_LOOKBACK_MS: Final = 14 * 24 * 60 * 60 * 1000
FULL_EVERY_MS: Final = 24 * 60 * 60 * 1000
"""The databases are read whole this often: what arrives or changes after the window is caught."""
TEXT_CAP: Final = 4096
"""Bytes of a record's ``text`` kept; a lyric line or a post is at most a few thousand."""
CURSOR_PREFIX: Final = "device-records:cursor:"
ROW_TABLE: Final = "device-records"
_LAST: Final = "device-records:last_ms"
_FULL: Final = "device-records:full_ms"
_MAC: Final = ("mac", "Mac")
_DIRECTION: Final = {1: "sent", 2: "received"}
_PEOPLE: Final = frozenset(
    {"sender", "senderHandle", "recipients", "recipientHandles", "contacts", "contactHandles"}
)
_MESSAGE_KINDS: Final = {"message": imports.RECORD_MESSAGE, "call": imports.RECORD_CALL}
_ENTRY_KEYS: Final = ("name", "handle_type", "label", "country", "title", "artist", "media_type")
_SHOWN: Final = 3
_SAMPLE_KINDS: Final = (
    imports.RECORD_NOW_PLAYING,
    imports.RECORD_BLUETOOTH,
    imports.RECORD_MESSAGE,
    imports.RECORD_CALL,
    imports.RECORD_INTENT,
)


@dataclass
class Built:
    """What one read yields, before anything is sent."""

    entries: list[dict[str, Any]] = field(default_factory=list)
    cursors: dict[str, int] = field(default_factory=dict)
    """``<stream>:<device id>`` to the newest record time (for the Mac's own databases, the newest
    time a record was made, changed or read)."""


def clip(text: str, limit: int = TEXT_CAP) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-8 bytes (on a character), and whether it was cut."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode(errors="ignore"), True


def _kept(**values: Any) -> dict[str, Any]:  # noqa: ANN401 - JSON values
    """The values that hold something: not None, an empty text or an empty list."""
    return {k: v for k, v in values.items() if v not in (None, "", [])}


def _entry(  # noqa: PLR0913 - the kind, device, identity, fields and words of one record
    kind: str,
    who: tuple[str, str],
    discriminator: object,
    rest: dict[str, Any],
    text: str,
    *,
    own_id: bool = False,
) -> dict[str, Any]:
    """The entry of one record: ``who`` is the device id and label, ``rest`` holds ``at_ms``.

    The key says which record it is, then what it held: a few digits of the fields too, so records
    that share a moment stay rows of their own; with ``own_id`` (the discriminator is the record's
    own id) it does not, and a changed record is a newer revision of the same row.
    """
    fields = {"device_id": who[0], "device": who[1], **rest}
    full = digest(fields)
    return {
        "key": f"{kind}:{who[0]}:{discriminator}{'' if own_id else '-' + full[:8]}:{full}",
        "kind": kind,
        "zone": "UTC",
        "at_ms": rest["at_ms"],
        "text": " ".join(text.split())[:200],
        "fields": fields,
    }


def _now_playing(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    words, cut = clip(str(rec.get("text") or ""))
    rest = _kept(
        at_ms=int(rec["at_ms"]),
        state=int(rec["state"]),
        title=rec.get("title"),
        artist=rec.get("artist"),
        duration_s=rec.get("duration_s"),
        text=words,
        text_truncated=cut or None,
        media_type=rec.get("media_type"),
        route=rec.get("route"),
        route_codes=rec.get("route_codes"),
        bundle_id=rec.get("bundle_id"),
    )
    what = " - ".join(str(rec[k]) for k in ("title", "artist") if rec.get(k))
    return _entry(imports.RECORD_NOW_PLAYING, who, rest["at_ms"], rest, what)


def _bluetooth(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    address = str(rec["address"])
    rest = _kept(
        at_ms=int(rec["at_ms"]),
        address=address,
        name=rec.get("name"),
        product_id=rec.get("product_id"),
        vendor_id=rec.get("vendor_id"),
        connected=bool(rec.get("connected")),
        battery=rec.get("battery"),
        extra=rec.get("extra") or None,
    )
    what = f"{rec.get('name') or address} {'connected' if rest['connected'] else 'disconnected'}"
    return _entry(
        imports.RECORD_BLUETOOTH, who, f"{address.replace(':', '')}-{rest['at_ms']}", rest, what
    )


def _person(one: Mapping[str, Any]) -> dict[str, Any]:
    if one.get("type") == "PersonHandle":
        return _kept(
            role=one["role"],
            name=one.get("name"),
            handle=one.get("value"),
            handle_type=one.get("handle_type"),
            label=one.get("label"),
            country=one.get("country"),
        )
    return _kept(role=one["role"], name=one.get("name"), contact_id=one.get("value"))


def _item(one: Mapping[str, Any]) -> dict[str, Any]:
    return _kept(
        role=one["role"],
        value=one.get("value"),
        type=one.get("type"),
        identifier=one.get("identifier"),
        **{k: one.get(k) for k in _ENTRY_KEYS},
    )


def _interaction(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    source = str(rec["source"])
    entries = [one for one in rec.get("entries") or [] if isinstance(one, dict) and "role" in one]
    rest: dict[str, Any] = _kept(
        at_ms=int(rec["at_ms"]),
        uuid=str(rec["uuid"]),
        bundle_id=rec.get("bundle_id"),
        intent=rec.get("intent"),
        domain=rec.get("domain"),
        origin=rec.get("origin"),
        direction=_DIRECTION.get(rec.get("direction") or 0),
        conversation_id=rec.get("conversation_id"),
        is_group=rec.get("is_group"),
    )
    if source in _MESSAGE_KINDS:
        chat = next((e for e in entries if e["role"] == "speakableGroupName"), {})
        people = [_person(e) for e in entries if e["role"] in _PEOPLE]
        rest.update(
            _kept(
                chat_name=chat.get("name") or chat.get("value"),
                people=people,
                message_ids=[
                    e.get("identifier") or e.get("value")
                    for e in entries
                    if e["role"] == "sentMessages"
                ],
                duration_s=rec.get("duration_s"),
            )
        )
        kind = _MESSAGE_KINDS[source]
        what = f"{rest.get('bundle_id', '')} {rest.get('direction', '')}"
    else:
        rest["stream"] = source
        rest.update(_kept(items=[_item(e) for e in entries]))
        kind = imports.RECORD_INTENT
        what = f"{rest.get('intent', '')} {rest.get('bundle_id', '')}"
    return _entry(kind, who, rest["uuid"], rest, what)


def _photo(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    rest = _kept(**{**rec, "at_ms": int(rec["at_ms"]), "seen_ms": None})
    what = " ".join(str(rec[k]) for k in ("kind", "original_filename") if rec.get(k))
    return _entry(imports.RECORD_PHOTO, who, rec["uuid"], rest, what, own_id=True)


def _chat_message(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    words, cut = clip(str(rec.get("text") or ""))
    rest = _kept(
        **{
            **rec,
            "at_ms": int(rec["at_ms"]),
            "seen_ms": None,
            "text": words,
            "text_truncated": cut or None,
        }
    )
    return _entry(imports.RECORD_CHAT_MESSAGE, who, rec["guid"], rest, words, own_id=True)


def _safari_visit(who: tuple[str, str], rec: Mapping[str, Any]) -> dict[str, Any]:
    url, cut = clip(str(rec["url"]))
    title, title_cut = clip(str(rec.get("title") or ""))
    rest = _kept(
        **{
            **rec,
            "at_ms": int(rec["at_ms"]),
            "seen_ms": None,
            "url": url,
            "url_truncated": cut or None,
            "title": title,
            "title_truncated": title_cut or None,
        }
    )
    # Two visits can share a millisecond and an address (a redirect and its target), so the time
    # alone does not name a visit; the file's own id does.
    which = f"{rest['at_ms']}-{int(rec['visit_id'])}"
    return _entry(imports.RECORD_SAFARI_VISIT, who, which, rest, title or url, own_id=True)


_MAKERS: Final[dict[str, Callable[[tuple[str, str], Mapping[str, Any]], dict[str, Any]]]] = {
    "now_playing": _now_playing,
    "bluetooth": _bluetooth,
    "interactions": _interaction,
}
_LOCAL_MAKERS: Final[dict[str, Callable[[tuple[str, str], Mapping[str, Any]], dict[str, Any]]]] = {
    "photos": _photo,
    "messages": _chat_message,
    "safari": _safari_visit,
}


def build(data: Mapping[str, Any]) -> Built:
    """An entry for every record in ``data["streams"]``, and the cursors.

    Those of the iPhone and iPad, and of this Mac's own databases. A record that lacks what its
    kind needs (a time, an address, a uuid, a guid, a url) is left out.
    """
    built = Built()
    labels = {d["id"]: lab for d in data["devices"] if (lab := devices.device_label(d))}
    streams = data.get("streams") or {}
    for stream, make in {**_MAKERS, **_LOCAL_MAKERS}.items():
        for rec in streams.get(stream) or []:
            label = _MAC[1] if stream in _LOCAL_MAKERS else labels.get(rec.get("device"))
            if label is None:
                continue
            device = _MAC[0] if stream in _LOCAL_MAKERS else str(rec["device"])
            try:
                entry = make((device, label), rec)
                seen = int(rec.get("seen_ms", entry["at_ms"]))
            except (KeyError, TypeError, ValueError):
                continue
            built.entries.append(entry)
            cursor = f"{stream}:{device}"
            built.cursors[cursor] = max(built.cursors.get(cursor, 0), seen)
    return built


WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in WARNED:
        WARNED.add(message)
        LOGGER.warning("mac-ship: %s", message)


Read = Callable[[int | None, str | None, int | None], dict[str, Any]]
"""``(since_ms, streams, local_since_ms)`` to the helper's answer; ``None`` is everything."""


def _read(
    read: Read, cursors: Mapping[str, int], now_ms: int, last_full_ms: int
) -> tuple[dict[str, Any], Built, bool]:
    """The helper's answer, its entries, and whether the three databases were read whole."""
    mine = [at for name, at in cursors.items() if name.split(":", 1)[0] in LOCAL_STREAMS]
    theirs = [at for name, at in cursors.items() if name.split(":", 1)[0] not in LOCAL_STREAMS]
    since = local_since = None
    if theirs:
        since = max(min(theirs) - OVERLAP_MS, now_ms - LOOKBACK_MS)
    if mine and now_ms - last_full_ms < FULL_EVERY_MS:
        local_since = max(min(mine) - LOCAL_OVERLAP_MS, now_ms - LOCAL_LOOKBACK_MS)
    data = read(since, STREAMS, local_since)
    built = build(data)
    if cursors and built.cursors.keys() - cursors.keys():  # a device or stream not met yet
        local_since = None
        data = read(None, STREAMS, None)
        built = build(data)
    return data, built, local_since is None


def ship(  # noqa: PLR0913 - the pass's state, sink and report, the clock and two test seams
    names_path: Path,
    state: State,
    sink: Sink,
    report: Report,
    now_ms: int,
    *,
    read: Read = read_screen_time,
    lookup: devices.Lookup | None = None,
) -> None:
    """One pass: nothing if the last was under ``INTERVAL_MS`` ago, else read, build and send.

    Raises:
        ShipFailed: the brain did not keep the batch; the cursors stay and the next pass resends.
    """
    if now_ms - state.meta_int(_LAST) < INTERVAL_MS:
        return
    state.apply([(SET_META, (_LAST, str(now_ms)))])
    cursors = {k: int(v) for k, v in devices.held(state, CURSOR_PREFIX).items()}
    try:
        data, built, whole = _read(read, cursors, now_ms, state.meta_int(_FULL))
    except ScreenTimeUnavailable as exc:
        _warn_once(f"device records unavailable: {exc}")
        return
    for stream, why in sorted((data.get("unavailable") or {}).items()):
        _warn_once(f"device records: {stream} unavailable: {why}")
    out = Out(SOURCE, sink, state, report)
    fresh: list[dict[str, Any]] = []
    for entry in built.entries:
        ident, sha = entry["key"][: -imports.REVISION_SUFFIX], entry["key"][-12:]
        again, memory = decide(
            state.row(ROW_TABLE, ident), sha, now_ms, settled=True, interval_ms=0
        )
        if again:
            out.add(entry, [(SET_ROW, (ROW_TABLE, ident, *memory))])
            fresh.append(entry)
    out.note(
        [
            (SET_META, (CURSOR_PREFIX + name, str(at + 1)))
            for name, at in built.cursors.items()
            if at + 1 > cursors.get(name, 0)
        ]
        + ([(SET_META, (_FULL, str(now_ms)))] if whole else [])
    )
    out.flush()
    names = devices.AppNames(
        names_path, now_ms, devices.itunes_lookup if lookup is None else lookup
    )
    known = names.resolve(e["fields"]["bundle_id"] for e in fresh if "bundle_id" in e["fields"])
    names.save()
    sent = devices.held(state, devices.NAME_PREFIX)
    named = Out(devices.SOURCE, sink, state, report)
    for entry, kept in devices.name_entries(known, sent, now_ms):
        named.add(entry, [(SET_META, (devices.NAME_PREFIX + entry["fields"]["bundle_id"], kept))])
    named.flush()


# --- the dry run -----------------------------------------------------------------------------

_NAME_FIELDS: Final = frozenset(
    {"name", "chat_name", "title", "artist", "text", "label", "device_name", "value"}
)
_ID_FIELDS: Final = frozenset(
    {"handle", "conversation_id", "address", "contact_id", "uuid", "message_ids", "identifier"}
)


def _mask(value: Any, key: str = "") -> Any:  # noqa: ANN401 - JSON values
    """A copy of a row value that hides who and what: names to a first character, ids to a tail."""
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v, key) for v in value]
    if isinstance(value, str) and key in _ID_FIELDS | {"device_id"}:
        return "…" + value[-2:]
    if key == "route":
        return re.sub(r"^[0-9A-Fa-f:]{17}", "…", value)
    if isinstance(value, str) and key in _NAME_FIELDS and value:
        return value[0] + "…"
    return value


class _Counting:
    """A sink that keeps nothing: it counts the bodies a pass sends, and their sizes."""

    def __init__(self) -> None:
        self.bodies: list[tuple[str, int]] = []

    def send_entries(self, source: str, body: bytes) -> None:
        self.bodies.append((source, len(body)))

    def send_file(self, source: str, relpath: str, data: bytes) -> str:  # pragma: no cover
        raise AssertionError((source, relpath, len(data)))


class _Local(_Counting):
    """A sink that also keeps what is sent in a temporary brain store."""

    def __init__(self, directory: Path) -> None:
        super().__init__()
        from jarvis.state.shipments import Shipments  # noqa: PLC0415 - only the dry run needs it

        self._shipments = Shipments(directory, min_free_bytes=0)

    def send_entries(self, source: str, body: bytes) -> None:
        super().send_entries(source, body)
        self._shipments.receive_entries(source, body)


def _fold_today(directory: Path, now_ms: int) -> str:
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from jarvis.state.day_line import DaySources, day_window, fold_day  # noqa: PLC0415
    from jarvis.state.event_log import open_event_log  # noqa: PLC0415

    zone = datetime.now().astimezone().tzinfo or ZoneInfo("UTC")
    start, end = day_window(datetime.fromtimestamp(now_ms / 1000, zone).date(), zone)
    with closing(open_event_log(directory.parent / "events.db")) as conn:
        line = fold_day(conn, start, end, now_ms, DaySources(zone, shipped=directory))
    counts = Counter(i["kind"] for i in line.items)
    kinds = ("media", "earbuds", "messages", "phone_call", "screen")
    shown = ", ".join(f"{k} {counts.get(k, 0)}" for k in kinds)
    return f"today's day line: {shown}; missing {line.missing}"


_LOCAL_KINDS: Final = (
    imports.RECORD_PHOTO,
    imports.RECORD_CHAT_MESSAGE,
    imports.RECORD_SAFARI_VISIT,
)


def _shape(value: Any) -> str:  # noqa: ANN401 - JSON values
    """What a value is, never what it says: its type and, for text, its length."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, str):
        return f"str{len(value)}"
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{_shape(v)}" for k, v in sorted(value.items())) + "}"
    if isinstance(value, list):
        return f"list{len(value)}"
    return type(value).__name__


def _local_report(entries: list[dict[str, Any]]) -> list[str]:
    """Per kind of this Mac's own databases: rows, time range, fields and, for messages, text."""
    lines: list[str] = []
    for kind in _LOCAL_KINDS:
        rows = [e for e in entries if e["kind"] == kind]
        if not rows:
            continue
        first, last = min(e["at_ms"] for e in rows), max(e["at_ms"] for e in rows)
        span = [f"{datetime.fromtimestamp(ms / 1000, UTC):%Y-%m-%d}" for ms in (first, last)]
        names = Counter(name for e in rows for name in e["fields"])
        lines.append(f"{kind}: {len(rows)} rows, {span[0]} to {span[1]} (UTC)")
        lines.append("  fields: " + ", ".join(f"{k} {n}" for k, n in sorted(names.items())))
        if kind == imports.RECORD_CHAT_MESSAGE:
            sources = Counter(e["fields"].get("text_source", "none") for e in rows)
            lines.append(f"  text from: {dict(sorted(sources.items()))}")
            cut = sum(1 for e in rows if e["fields"].get("text_truncated"))
            lines.append(f"  text cut at {TEXT_CAP} bytes: {cut}")
        if kind == imports.RECORD_SAFARI_VISIT:
            origins = Counter(e["fields"].get("origin") for e in rows)
            lines.append(f"  origin: {dict(sorted(origins.items()))}")
        middle = rows[len(rows) // 2]["fields"]
        shape = ", ".join(f"{k}={_shape(v)}" for k, v in sorted(middle.items()))
        lines.append(f"  one row's shape: {shape}")
    return lines


def dry_run(hours: float | None, now_ms: int, *, fold: bool) -> int:
    """Print counts and masked samples of what a pass over the last ``hours`` would ship."""
    since = None if hours is None else int(now_ms - hours * 3_600_000)
    try:
        data = read_screen_time(since, STREAMS, since)
    except ScreenTimeUnavailable as exc:
        sys.stderr.write(f"device records unavailable: {exc}\n")
        return 1
    for stream, why in sorted((data.get("unavailable") or {}).items()):
        sys.stdout.write(f"{stream} unavailable: {why}\n")
    built = build(data)
    sys.stdout.write(f"{len(built.entries)} entries\n")
    counts = Counter((e["fields"]["device"], e["kind"]) for e in built.entries)
    sys.stdout.writelines(f"  {d} {k}: {n}\n" for (d, k), n in sorted(counts.items()))
    keys = Counter(e["key"][: -imports.REVISION_SUFFIX] for e in built.entries)
    sys.stdout.write(
        f"  distinct rows: {len(keys)} (duplicates held back: {len(built.entries) - len(keys)})\n"
    )
    for kind in _SAMPLE_KINDS:
        rows = [e for e in built.entries if e["kind"] == kind]
        for entry in rows[:: max(1, len(rows) // _SHOWN)][:_SHOWN]:
            sys.stdout.write(f"{kind}: {json.dumps(_mask(entry['fields']), ensure_ascii=False)}\n")
    sys.stdout.writelines(line + "\n" for line in _local_report(built.entries))
    with tempfile.TemporaryDirectory(prefix="jarvis-device-records-") as tmp:
        directory = Path(tmp) / imports.IMPORTS_DIRNAME
        state = State(Path(tmp) / "state.db")
        report = Report()
        sink = _Local(directory) if fold else _Counting()
        ship(
            Path(tmp) / devices.NAMES_FILE,
            state,
            sink,
            report,
            now_ms,
            read=lambda _since, _streams, _local: data,
            lookup=lambda _bundle: None,
        )
        sizes = [size for _, size in sink.bodies]
        sys.stdout.write(
            f"a first pass would send {sum(report.entries.values())} entries in {len(sizes)} "
            f"batches, {sum(sizes)} bytes (largest {max(sizes, default=0)}; a batch is closed at "
            f"{BATCH_BYTES} bytes or {BATCH_LINES} lines, the brain takes 16 MiB)\n"
        )
        if fold:
            sys.stdout.write(f"shipped to a temporary store: {dict(report.entries)}\n")
            sys.stdout.write(_fold_today(directory, now_ms) + "\n")
        state.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    """``dry-run [--hours N | --all] [--fold]``."""
    parser = argparse.ArgumentParser(prog="python -m jarvis.surface.mac_ship_records")
    sub = parser.add_subparsers(dest="cmd", required=True)
    dry = sub.add_parser("dry-run", help="what a pass would ship, masked; nothing is sent")
    dry.add_argument("--hours", type=float, default=72.0)
    dry.add_argument("--all", action="store_true", help="everything the Mac holds")
    dry.add_argument("--fold", action="store_true", help="also ship to a temp store and fold today")
    args = parser.parse_args(argv)
    return dry_run(None if args.all else args.hours, int(time.time() * 1000), fold=args.fold)


__all__ = ["Built", "build", "clip", "dry_run", "ship"]


if __name__ == "__main__":
    sys.exit(main())
