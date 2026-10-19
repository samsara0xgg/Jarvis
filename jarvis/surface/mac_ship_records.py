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

Cursors (``device-records:cursor:<stream>:<device id>``, the newest time shipped) and each row's
memory move only after the brain kept the batch. A read starts ``OVERLAP_MS`` before the oldest
cursor, never more than ``LOOKBACK_MS`` back: a record that reaches the Mac later than that is
not seen again. The first read, and the first read after a device or stream is met, takes all
the Mac holds, a year of Siri included. The apps' names are the ``app_name`` rows of
``device-apps`` (:mod:`mac_ship_devices`): bundles first met here are resolved and sent there.

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
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jarvis.state import imports
from jarvis.surface import mac_ship_devices as devices
from jarvis.surface.mac_screen_time import ScreenTimeUnavailable, read_screen_time
from jarvis.surface.mac_ship_core import SET_META, SET_ROW, Out, Report, Sink, State, decide, digest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

LOGGER = logging.getLogger(__name__)

SOURCE: Final = imports.RECORDS_SOURCE
STREAMS: Final = "now_playing,bluetooth,interactions"
INTERVAL_MS: Final = devices.INTERVAL_MS
OVERLAP_MS: Final = 12 * 60 * 60 * 1000
LOOKBACK_MS: Final = 3 * 24 * 60 * 60 * 1000
TEXT_CAP: Final = 4096
"""Bytes of a record's ``text`` kept; a lyric line or a post is at most a few thousand."""
CURSOR_PREFIX: Final = "device-records:cursor:"
ROW_TABLE: Final = "device-records"
_LAST: Final = "device-records:last_ms"
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
    """``<stream>:<device id>`` to the newest record time."""


def clip(text: str, limit: int = TEXT_CAP) -> tuple[str, bool]:
    """``text`` cut to at most ``limit`` UTF-8 bytes (on a character), and whether it was cut."""
    raw = text.encode()
    if len(raw) <= limit:
        return text, False
    return raw[:limit].decode(errors="ignore"), True


def _kept(**values: Any) -> dict[str, Any]:  # noqa: ANN401 - JSON values
    """The values that hold something: not None, an empty text or an empty list."""
    return {k: v for k, v in values.items() if v not in (None, "", [])}


def _entry(
    kind: str, who: tuple[str, str], discriminator: object, rest: dict[str, Any], text: str
) -> dict[str, Any]:
    """The entry of one record: ``who`` is the device id and label, ``rest`` holds ``at_ms``."""
    fields = {"device_id": who[0], "device": who[1], **rest}
    full = digest(fields)
    return {
        "key": f"{kind}:{who[0]}:{discriminator}-{full[:8]}:{full}",
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


_MAKERS: Final[dict[str, Callable[[tuple[str, str], Mapping[str, Any]], dict[str, Any]]]] = {
    "now_playing": _now_playing,
    "bluetooth": _bluetooth,
    "interactions": _interaction,
}


def build(data: Mapping[str, Any]) -> Built:
    """An entry for every record of the iPhone and iPad in ``data["streams"]``, and the cursors.

    A record that lacks what its kind needs (a time, an address, a uuid) is left out.
    """
    built = Built()
    labels = {d["id"]: lab for d in data["devices"] if (lab := devices.device_label(d))}
    streams = data.get("streams") or {}
    for stream, make in _MAKERS.items():
        for rec in streams.get(stream) or []:
            label = labels.get(rec.get("device"))
            if label is None:
                continue
            try:
                entry = make((str(rec["device"]), label), rec)
            except (KeyError, TypeError, ValueError):
                continue
            built.entries.append(entry)
            cursor = f"{stream}:{rec['device']}"
            built.cursors[cursor] = max(built.cursors.get(cursor, 0), entry["at_ms"])
    return built


WARNED: set[str] = set()


def _read(
    read: Callable[[int | None, str | None], dict[str, Any]],
    cursors: Mapping[str, int],
    now_ms: int,
) -> tuple[dict[str, Any], Built]:
    since = None
    if cursors:
        since = max(min(cursors.values()) - OVERLAP_MS, now_ms - LOOKBACK_MS)
    data = read(since, STREAMS)
    built = build(data)
    if cursors and built.cursors.keys() - cursors.keys():  # a device or stream not met yet
        data = read(None, STREAMS)
        built = build(data)
    return data, built


def ship(  # noqa: PLR0913 - the pass's state, sink and report, the clock and two test seams
    names_path: Path,
    state: State,
    sink: Sink,
    report: Report,
    now_ms: int,
    *,
    read: Callable[[int | None, str | None], dict[str, Any]] = read_screen_time,
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
        _, built = _read(read, cursors, now_ms)
    except ScreenTimeUnavailable as exc:
        if str(exc) not in WARNED:
            WARNED.add(str(exc))
            LOGGER.warning("mac-ship: device records unavailable: %s", exc)
        return
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


class _Local:
    """A sink that keeps what is sent in a temporary brain store."""

    def __init__(self, directory: Path) -> None:
        from jarvis.state.shipments import Shipments  # noqa: PLC0415 - only the dry run needs it

        self._shipments = Shipments(directory, min_free_bytes=0)

    def send_entries(self, source: str, body: bytes) -> None:
        self._shipments.receive_entries(source, body)

    def send_file(self, source: str, relpath: str, data: bytes) -> str:  # pragma: no cover
        raise AssertionError((source, relpath, len(data)))


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


def dry_run(hours: float | None, now_ms: int, *, fold: bool) -> int:
    """Print counts and masked samples of what a pass over the last ``hours`` would ship."""
    since = None if hours is None else int(now_ms - hours * 3_600_000)
    try:
        data = read_screen_time(since, STREAMS)
    except ScreenTimeUnavailable as exc:
        sys.stderr.write(f"device records unavailable: {exc}\n")
        return 1
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
    if fold:
        with tempfile.TemporaryDirectory(prefix="jarvis-device-records-") as tmp:
            directory = Path(tmp) / imports.IMPORTS_DIRNAME
            state = State(Path(tmp) / "state.db")
            report = Report()
            ship(
                Path(tmp) / devices.NAMES_FILE,
                state,
                _Local(directory),
                report,
                now_ms,
                read=lambda _since, _streams: data,
                lookup=lambda _bundle: None,
            )
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
