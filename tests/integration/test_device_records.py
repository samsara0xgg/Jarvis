"""ADR 0305: what else his iPhone and iPad sync to the Mac, from the helper to the day line.

Acceptance checks, on synthetic data and no network:

- the real helper (built from ``native/screen_time``) reads Media.NowPlaying, Device.Wireless.
  Bluetooth and the four Siri.Remembers streams from a fixture Biome folder (``--root``) written as
  SEGB v2 segments: every field into its named place, a deleted record and the tombstone folder
  skipped, and no ``streams`` in the answer unless asked for (macOS only);
- the Mac step ships one row per record with a stable key (records that share a millisecond or a
  uuid stay apart, a send again adds nothing), caps the long text, moves its cursors only after the
  brain kept the batch, reads everything the first time and when a device or stream is new, and
  hands the apps' names to the ``device-apps`` source;
- ADR 0309: the helper reads the Mac's own Photos, Messages and Safari databases (fixtures, by
  column name, the words decoded from a typedstream), and the step ships a row per photo, message
  and visit under its own id, so a change is a newer revision of the same row, with its own
  window and a whole read once a day;
- the day line folds the rows into ``media``, ``earbuds``, ``messages`` and ``phone_call``, and says
  ``device_records`` is missing only when a shipped store holds no such row.
"""

from __future__ import annotations

import json
import logging
import plistlib
import shutil
import sqlite3
import struct
import subprocess
import sys
from contextlib import closing
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest

from jarvis.shared.about import clean_about
from jarvis.state import device_day, imports
from jarvis.state.day_line import DaySources, day_window, fold_day, parse_day
from jarvis.state.event_log import open_event_log
from jarvis.state.shipments import Shipments
from jarvis.surface import mac_ship_devices as devices
from jarvis.surface import mac_ship_records as records
from jarvis.surface.mac_screen_time import ScreenTimeUnavailable, ensure_screen_time_app
from jarvis.surface.mac_ship import ship_config
from jarvis.surface.mac_ship_core import Report, ShipFailed, State, digest

if TYPE_CHECKING:
    from pathlib import Path

ZONE = ZoneInfo("America/Vancouver")
SEC = 1000
MIN = 60 * SEC
H = 60 * MIN
PHONE = "AAAAAAAA-1111-2222-3333-444444444444"
PAD = "BBBBBBBB-1111-2222-3333-444444444444"
MAC = "CCCCCCCC-1111-2222-3333-444444444444"
DEVICES = [
    {"id": PHONE, "name": None, "model": "iPhone17,2"},
    {"id": PAD, "name": None, "model": "iPad16,3"},
    {"id": MAC, "name": None, "model": "Mac17,2"},
    {"id": "DDDDDDDD", "name": None},
]
DAY0, _ = day_window(parse_day("2026-10-10"), ZONE)
PODS = "AA:BB:CC:DD:EE:FF"
KINDS = ("media", "earbuds", "messages", "phone_call")


def at(minutes: float) -> int:
    """Milliseconds ``minutes`` after local midnight of the test day."""
    return DAY0 + int(minutes * MIN)


# --- the helper, on a fixture Biome folder -----------------------------------------------------


def _varint(n: int) -> bytes:
    out = b""
    while n > 0x7F:
        out += bytes([n & 0x7F | 0x80])
        n >>= 7
    return out + bytes([n])


def _field(num: int, value: Any) -> bytes:  # noqa: ANN401 - a protobuf value of any wire type
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return _varint(num << 3) + _varint(value)
    if isinstance(value, float):
        return _varint(num << 3 | 1) + struct.pack("<d", value)
    raw = value.encode() if isinstance(value, str) else value
    return _varint(num << 3 | 2) + _varint(len(raw)) + raw


def _message(*fields: tuple[int, Any]) -> bytes:
    return b"".join(_field(num, value) for num, value in fields)


def _cf(unix_ms: int) -> float:
    """A CFAbsoluteTime (seconds since 2001) for unix milliseconds."""
    return unix_ms / 1000 - 978_307_200


def _segb(entries: list[tuple[bytes, int, float]]) -> bytes:
    """A SEGB v2 segment of ``(data, state, CFAbsoluteTime)`` entries."""
    body, trailer = b"", b""
    for data, state, when in entries:
        body += b"\0" * (-len(body) % 4)
        record = b"\0" * 8 + (data if state == 1 else b"\0" * len(data))
        body += record
        trailer += struct.pack("<iid", len(body), state, when)
    return b"SEGB" + struct.pack("<i", len(entries)) + b"\0" * 24 + body + trailer


def _put(root: Path, stream: str, device: str, name: str, entries: list[Any]) -> None:
    folder = root / "streams" / "restricted" / stream / "remote" / device
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(_segb(entries))
    (folder / "tombstone").mkdir(exist_ok=True)
    (folder / "tombstone" / name).write_bytes(_segb(entries))  # a deletion log: never read


def _siri(  # noqa: PLR0913 - the fields of a Siri record
    uuid: str,
    intent: str,
    domain: str,
    bundle: str,
    direction: int,
    unix_s: float,
    entries: list[bytes],
    chat: str = "",
    duration: float = 0.0,
) -> bytes:
    top = _message(
        (1, uuid),
        (2, intent),
        (3, domain),
        (4, bundle),
        (5, 1),
        (6, direction),
        (8, unix_s),
        (9, duration),
        (11, '{"isGroup":false}'),
        (12, chat),
    )
    return _message((1, top), *[(2, e) for e in entries])


def _entry(role: str, value: str, kind: str, info: dict[str, str]) -> bytes:
    return _message((1, role), (2, _message((1, value), (2, kind), (4, json.dumps(info)))))


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None, reason="needs macOS and swiftc"
)
def test_the_helper_reads_each_stream_into_named_fields(tmp_path: Path) -> None:
    """The helper reads each stream into named fields and leaves deleted records and tombstones."""
    root = tmp_path / "Biome"
    when = 1_790_000_000_000
    playing = _message(
        (2, _cf(when)),
        (3, 1),
        (4, "Song"),
        (5, "Singer"),
        (6, 249),
        (8, "a lyric line"),
        (14, _message((1, 2), (2, 3), (3, f"{PODS}-tacl"))),
        (15, "com.netease.cloudmusic"),
    )
    unknown = _message((2, _cf(when + 1000)), (3, 2), (6, 4_294_967_295), (15, "com.x"))
    _put(
        root,
        "Media.NowPlaying",
        PHONE,
        "1",
        [
            (playing, 1, _cf(when)),
            (unknown, 1, _cf(when + 1000)),
            (playing, 3, _cf(when + 2000)),
        ],
    )
    pods = _message(
        (1, PODS),
        (2, "Pods"),
        (3, 8212),
        (4, 1),
        (5, 21),
        (6, 61),
        (7, 1),
        (8, 3),
        (9, 1),
        (10, 1),
        (11, 76),
    )
    _put(root, "Device.Wireless.Bluetooth", PHONE, "1", [(pods, 1, _cf(when + 5000))])
    message = _siri(
        "UUID-1",
        "INSendMessageIntent",
        "Messages",
        "com.apple.MobileSMS",
        1,
        when / 1000,
        [
            _entry("speakableGroupName", "Study group", "MessageGroup", {"name": "Study group"}),
            _entry(
                "recipientHandles",
                "+15550100",
                "PersonHandle",
                {"name": "Alex", "type": "phone", "label": "mobile", "countryCode": "+1"},
            ),
        ],
        chat="any;-;+15550100",
    )
    _put(root, "Siri.Remembers.MessageHistory", PHONE, "1", [(message, 1, _cf(when))])
    audio = _siri(
        "UUID-2",
        "INPlayMediaIntent",
        "Media",
        "com.netease.cloudmusic",
        0,
        when / 1000 + 1,
        [
            _entry(
                "items",
                "m1",
                "MediaItem",
                {"title": "Song", "artist": "Singer", "mediaType": "song"},
            )
        ],
    )
    _put(root, "Siri.Remembers.AudioHistory", PAD, "1", [(audio, 1, _cf(when))])
    call = _siri(
        "UUID-3",
        "INStartCallIntent",
        "Calls",
        "com.apple.InCallService",
        2,
        when / 1000 + 2,
        [_entry("contacts", "id1:ABPerson", "Person", {"name": "Sam"})],
        duration=27.0,
    )
    _put(root, "Siri.Remembers.CallHistory", PHONE, "1", [(call, 1, _cf(when))])

    binary = ensure_screen_time_app() / "Contents" / "MacOS" / "jarvis-screen-time"
    out = tmp_path / "out.json"
    subprocess.run(  # noqa: S603 - the repo's own helper on a fixture folder
        [
            str(binary),
            str(out),
            "--root",
            str(root),
            "--home",
            str(tmp_path),
            "--streams",
            records.STREAMS,
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    answer = json.loads(out.read_text())
    streams = answer["streams"]

    assert answer["events"] == []
    assert [d["id"] for d in answer["devices"]] == [PHONE, PAD]
    assert streams["now_playing"] == [
        {
            "device": PHONE,
            "at_ms": when,
            "state": 1,
            "title": "Song",
            "artist": "Singer",
            "duration_s": 249,
            "text": "a lyric line",
            "route": f"{PODS}-tacl",
            "route_codes": [2, 3],
            "bundle_id": "com.netease.cloudmusic",
        },
        {"device": PHONE, "at_ms": when + 1000, "state": 2, "bundle_id": "com.x"},
    ]  # the deleted record is gone, a duration of 4294967295 is no duration
    assert streams["bluetooth"] == [
        {
            "device": PHONE,
            "at_ms": when + 5000,
            "address": PODS,
            "connected": True,
            "name": "Pods",
            "product_id": 8212,
            "vendor_id": 76,
            "battery": [61, 1, 3],
            "extra": {"5": 21, "9": 1, "10": 1},
        }
    ]
    by_source = {r["source"]: r for r in streams["interactions"]}
    assert set(by_source) == {"message", "audio", "call"}
    assert by_source["message"] == {
        "device": PHONE,
        "source": "message",
        "uuid": "UUID-1",
        "at_ms": when,
        "intent": "INSendMessageIntent",
        "domain": "Messages",
        "bundle_id": "com.apple.MobileSMS",
        "origin": 1,
        "direction": 1,
        "is_group": False,
        "conversation_id": "any;-;+15550100",
        "entries": [
            {
                "role": "speakableGroupName",
                "value": "Study group",
                "type": "MessageGroup",
                "name": "Study group",
            },
            {
                "role": "recipientHandles",
                "value": "+15550100",
                "type": "PersonHandle",
                "name": "Alex",
                "handle_type": "phone",
                "label": "mobile",
                "country": "+1",
            },
        ],
    }
    assert by_source["call"]["duration_s"] == 27
    assert by_source["audio"]["device"] == PAD
    assert by_source["audio"]["entries"] == [
        {
            "role": "items",
            "value": "m1",
            "type": "MediaItem",
            "title": "Song",
            "artist": "Singer",
            "media_type": "song",
        }
    ]

    # since: only what is at or after it; without --streams the answer is the old one
    subprocess.run(  # noqa: S603
        [
            str(binary),
            str(out),
            "--root",
            str(root),
            "--home",
            str(tmp_path),
            "--streams",
            records.STREAMS,
            "--since-ms",
            str(when + 3000),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    later = json.loads(out.read_text())["streams"]
    assert later["now_playing"] == []
    assert len(later["bluetooth"]) == 1
    subprocess.run(  # noqa: S603
        [str(binary), str(out), "--root", str(root)], check=True, capture_output=True, timeout=60
    )
    assert "streams" not in json.loads(out.read_text())


# --- the Mac step ---------------------------------------------------------------------------


def np(
    when: int,
    state: int = 1,
    device: str = PHONE,
    **more: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """A helper-shaped now-playing record."""
    return {
        "device": device,
        "at_ms": when,
        "state": state,
        "route": "Speaker",
        "route_codes": [4, 2],
        "bundle_id": "com.netease.cloudmusic",
        **more,
    }


def bt(
    when: int, battery: list[int], *, connected: bool = True, device: str = PHONE
) -> dict[str, Any]:
    """A helper-shaped Bluetooth record of the earbuds."""
    return {
        "device": device,
        "at_ms": when,
        "address": PODS,
        "name": "Pods",
        "product_id": 8212,
        "vendor_id": 76,
        "connected": connected,
        "battery": battery,
    }


def siri(  # noqa: PLR0913 - the fields of a Siri record
    source: str,
    when: int,
    uuid: str,
    entries: list[dict[str, Any]],
    device: str = PHONE,
    bundle: str = "com.apple.MobileSMS",
    direction: int = 1,
    **more: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """A helper-shaped Siri record."""
    return {
        "device": device,
        "source": source,
        "uuid": uuid,
        "at_ms": when,
        "bundle_id": bundle,
        "intent": "INSendMessageIntent",
        "domain": "Messages",
        "origin": 4,
        "direction": direction,
        "is_group": False,
        "entries": entries,
        **more,
    }


def person(role: str, name: str) -> dict[str, Any]:
    """A Person entry."""
    return {"role": role, "type": "Person", "value": f"id-{name}", "name": name}


def handle(role: str, value: str, name: str | None = None) -> dict[str, Any]:
    """A PersonHandle entry."""
    return {
        "role": role,
        "type": "PersonHandle",
        "value": value,
        "handle_type": "phone",
        **({"name": name} if name else {}),
    }


class _Intake:
    """The brain's intake, called in place of HTTP; keeps what each send added."""

    def __init__(self, shipments: Shipments) -> None:
        self.shipments = shipments
        self.added: list[int] = []
        self.fail = False

    def send_entries(self, source: str, body: bytes) -> None:
        if self.fail:
            msg = "the brain is not reachable"
            raise ShipFailed(msg)
        self.added.append(self.shipments.receive_entries(source, body).added)

    def send_file(self, source: str, relpath: str, data: bytes) -> str:  # pragma: no cover
        raise AssertionError((source, relpath, len(data)))


class Mac:
    """One Mac terminal's shipper state, with the helper's answer settable."""

    def __init__(self, tmp_path: Path) -> None:
        """Empty stores."""
        self.directory = tmp_path / "brain" / imports.IMPORTS_DIRNAME
        self.intake = _Intake(Shipments(self.directory, min_free_bytes=0))
        self.state = State(tmp_path / "mac" / "state.db")
        self.names = tmp_path / "mac" / "app-names.json"
        self.streams: dict[str, list[dict[str, Any]]] = {
            "now_playing": [],
            "bluetooth": [],
            "interactions": [],
            "photos": [],
            "messages": [],
            "safari": [],
        }
        self.reads: list[tuple[int | None, str | None]] = []
        self.local_reads: list[int | None] = []
        self.unavailable: dict[str, str] = {}
        self.store: dict[str, tuple[str | None, str | None] | None] = {}

    def read(
        self, since_ms: int | None, streams: str | None, local_since_ms: int | None
    ) -> dict[str, Any]:
        """The records at or after ``since_ms``; of the Mac's databases, those touched later."""
        self.reads.append((since_ms, streams))
        self.local_reads.append(local_since_ms)

        def _after(at: int, name: str) -> bool:
            floor = local_since_ms if name in records.LOCAL_STREAMS else since_ms
            return floor is None or at >= floor

        kept = {
            name: [
                r
                for r in rows
                if _after(r["seen_ms"] if name in records.LOCAL_STREAMS else r["at_ms"], name)
            ]
            for name, rows in self.streams.items()
        }
        return {"devices": DEVICES, "events": [], "streams": kept, "unavailable": self.unavailable}

    def pass_(self, now_ms: int) -> None:
        """One pass of the step at ``now_ms``."""
        records.ship(
            self.names,
            self.state,
            self.intake,
            Report(),
            now_ms,
            read=self.read,
            lookup=lambda bundle: self.store.get(bundle, (None, None)),
        )

    def keys(self, kind: str) -> list[str]:
        """The keys the brain holds for one kind of record row."""
        with closing(imports.open_shipped(self.directory)) as conn:
            found = conn.execute(
                "SELECT key FROM import_entries WHERE source = ? AND kind = ?",
                (imports.RECORDS_SOURCE, kind),
            )
            return sorted(key for (key,) in found)

    def rows(self, kind: str, source: str = imports.RECORDS_SOURCE) -> list[imports.Entry]:
        """The newest revision of each shipped row of one kind."""
        with closing(imports.open_shipped(self.directory)) as conn:
            return imports.newest(conn, source, kinds=(kind,), by_prefix=True)


@pytest.fixture
def mac(tmp_path: Path) -> Mac:
    """A fresh Mac terminal."""
    return Mac(tmp_path)


def test_one_row_per_record_in_the_shapes_the_brain_keeps(mac: Mac) -> None:
    """One row per record, in the shape of its kind, with a key that ends in the fields' digest."""
    mac.store = {"com.netease.cloudmusic": ("网易云音乐", "Music")}
    mac.streams["now_playing"] = [
        np(
            at(600),
            title="Song",
            artist="Singer",
            duration_s=249,
            text="a lyric",
            route=f"{PODS}-tacl",
            route_codes=[2, 3],
            media_type="song",
        ),
        np(at(601), 3, device=PAD),
        np(at(602), device=MAC),  # a Mac: TimeSink's
    ]
    mac.streams["bluetooth"] = [bt(at(590), [61, 1, 3], connected=True)]
    mac.streams["interactions"] = [
        siri(
            "message",
            at(603),
            "U1",
            [
                person("recipients", "Alex"),
                handle("recipientHandles", "+15550100", "Alex"),
                {
                    "role": "speakableGroupName",
                    "type": "MessageGroup",
                    "value": "G",
                    "name": "Study",
                },
                {"role": "sentMessages", "type": "Message", "value": "m", "identifier": "MID"},
            ],
            conversation_id="chat1",
        ),
        siri(
            "call",
            at(604),
            "U2",
            [person("contacts", "Sam")],
            bundle="com.apple.InCallService",
            direction=2,
            duration_s=27,
            intent="INStartCallIntent",
            domain="Calls",
        ),
        siri(
            "audio",
            at(605),
            "U3",
            [{"role": "items", "type": "MediaItem", "value": "m1", "title": "Song", "artist": "S"}],
            bundle="com.netease.cloudmusic",
            direction=0,
            intent="INPlayMediaIntent",
            domain="Media",
        ),
    ]
    mac.pass_(at(700))

    (playing, stopped) = mac.rows(imports.RECORD_NOW_PLAYING)
    assert playing.fields == {
        "device_id": PHONE,
        "device": "iPhone",
        "at_ms": at(600),
        "state": 1,
        "title": "Song",
        "artist": "Singer",
        "duration_s": 249,
        "text": "a lyric",
        "media_type": "song",
        "route": f"{PODS}-tacl",
        "route_codes": [2, 3],
        "bundle_id": "com.netease.cloudmusic",
    }
    assert (stopped.fields["device"], stopped.fields["state"]) == ("iPad", 3)
    full = digest(playing.fields)
    assert f"now_playing:{PHONE}:{at(600)}-{full[:8]}:{full}" in mac.keys("now_playing")
    assert (playing.zone, playing.at_ms) == ("UTC", at(600))
    (pods,) = mac.rows(imports.RECORD_BLUETOOTH)
    assert pods.fields == {
        "device_id": PHONE,
        "device": "iPhone",
        "at_ms": at(590),
        "address": PODS,
        "name": "Pods",
        "product_id": 8212,
        "vendor_id": 76,
        "connected": True,
        "battery": [61, 1, 3],
    }
    (message,) = mac.rows(imports.RECORD_MESSAGE)
    assert message.fields == {
        "device_id": PHONE,
        "device": "iPhone",
        "at_ms": at(603),
        "uuid": "U1",
        "bundle_id": "com.apple.MobileSMS",
        "intent": "INSendMessageIntent",
        "domain": "Messages",
        "origin": 4,
        "direction": "sent",
        "conversation_id": "chat1",
        "is_group": False,
        "chat_name": "Study",
        "people": [
            {"role": "recipients", "name": "Alex", "contact_id": "id-Alex"},
            {
                "role": "recipientHandles",
                "name": "Alex",
                "handle": "+15550100",
                "handle_type": "phone",
            },
        ],
        "message_ids": ["MID"],
    }
    (call,) = mac.rows(imports.RECORD_CALL)
    assert call.fields["direction"] == "received"
    assert call.fields["duration_s"] == 27
    assert call.fields["people"] == [{"role": "contacts", "name": "Sam", "contact_id": "id-Sam"}]
    (intent,) = mac.rows(imports.RECORD_INTENT)
    assert intent.fields == {
        "device_id": PHONE,
        "device": "iPhone",
        "at_ms": at(605),
        "uuid": "U3",
        "bundle_id": "com.netease.cloudmusic",
        "intent": "INPlayMediaIntent",
        "domain": "Media",
        "origin": 4,
        "is_group": False,
        "stream": "audio",
        "items": [
            {"role": "items", "value": "m1", "type": "MediaItem", "title": "Song", "artist": "S"}
        ],
    }
    # the app's name goes to the device-apps source, where the day line already looks
    named = {
        r.fields["bundle_id"]: r.fields["app"]
        for r in mac.rows(imports.DEVICE_NAME, imports.DEVICE_SOURCE)
    }
    assert named == {
        "com.netease.cloudmusic": "网易云音乐",
        "com.apple.MobileSMS": "信息",
        "com.apple.InCallService": "电话",
    }
    # the cursors are the newest time of each stream and device, plus one
    assert devices.held(mac.state, records.CURSOR_PREFIX) == {
        f"now_playing:{PHONE}": str(at(600) + 1),
        f"now_playing:{PAD}": str(at(601) + 1),
        f"bluetooth:{PHONE}": str(at(590) + 1),
        f"interactions:{PHONE}": str(at(605) + 1),
    }


def test_records_that_share_a_moment_or_a_uuid_stay_apart_and_an_exact_copy_is_one(
    mac: Mac,
) -> None:
    """Records at the same millisecond or with the same uuid are rows of their own."""
    same = np(at(600), title="Song")
    mac.streams["now_playing"] = [same, np(at(600), 3), dict(same)]
    mac.streams["interactions"] = [
        siri("audio", at(601), "U1", [{"role": "items", "value": "a"}]),
        siri("audio", at(601), "U1", [{"role": "items", "value": "b"}]),
    ]
    mac.pass_(at(700))
    assert len(mac.rows(imports.RECORD_NOW_PLAYING)) == 2
    assert len(mac.rows(imports.RECORD_INTENT)) == 2


def test_a_send_again_adds_nothing_and_a_row_is_sent_once(mac: Mac) -> None:
    """A send again adds nothing, and a later pass sends only the new records."""
    mac.streams["now_playing"] = [np(at(600), title="A")]
    mac.pass_(at(700))
    mac.state.apply([("DELETE FROM rows", ())])  # as if the shipper had forgotten
    mac.pass_(at(700) + 16 * MIN)
    assert mac.intake.added == [1, 0]  # sent again: the brain holds it
    mac.streams["now_playing"].append(np(at(720), title="B"))
    mac.pass_(at(700) + 32 * MIN)
    assert mac.intake.added == [1, 0, 1]
    mac.pass_(at(700) + 48 * MIN)
    assert mac.intake.added == [1, 0, 1]  # nothing new: nothing sent


def test_the_step_waits_a_quarter_hour_reads_all_the_first_time_then_from_the_cursor(
    mac: Mac,
) -> None:
    """The first read takes everything; later ones start before the oldest cursor, not too far."""
    mac.streams["now_playing"] = [np(at(600))]
    mac.pass_(at(700))
    mac.pass_(at(700) + 5 * MIN)
    assert mac.reads == [(None, records.STREAMS)]
    mac.pass_(at(700) + 16 * MIN)
    assert mac.reads[1] == (at(600) + 1 - records.OVERLAP_MS, records.STREAMS)
    # a quiet stream is not read back more than three days
    later = at(600) + 10 * 24 * H
    mac.pass_(later)
    assert mac.reads[2][0] == later - records.LOOKBACK_MS


def test_a_device_or_stream_met_late_is_read_from_the_start(mac: Mac) -> None:
    """A device or stream the cursors have not met is read from the start."""
    mac.streams["now_playing"] = [np(at(600))]
    mac.pass_(at(700))
    mac.streams["bluetooth"] = [bt(at(100), [1, 2, 3], device=PAD)]  # old, new to us
    mac.pass_(at(700) + 16 * MIN)
    assert mac.reads[-1][0] is None
    assert [r.fields["device"] for r in mac.rows(imports.RECORD_BLUETOOTH)] == ["iPad"]


def test_cursors_and_row_memory_move_only_after_the_brain_kept_the_batch(mac: Mac) -> None:
    """A batch the brain refuses moves nothing; the next pass sends it all."""
    mac.streams["now_playing"] = [np(at(600), title="A"), np(at(601), title="B")]
    mac.intake.fail = True
    with pytest.raises(ShipFailed):
        mac.pass_(at(700))
    assert devices.held(mac.state, records.CURSOR_PREFIX) == {}
    assert mac.state.conn.execute("SELECT count(*) FROM rows").fetchone()[0] == 0
    mac.intake.fail = False
    mac.pass_(at(700) + 16 * MIN)
    assert len(mac.rows(imports.RECORD_NOW_PLAYING)) == 2
    assert mac.reads[1][0] is None  # no cursor yet: all again


def test_long_text_is_kept_up_to_four_kilobytes_and_says_so(mac: Mac) -> None:
    """A row's text is cut at 4096 bytes on a character, and ``text_truncated`` says so."""
    mac.streams["now_playing"] = [
        np(at(600), title="Post", text="词" * 2000),
        np(at(601), text="short"),
    ]
    mac.pass_(at(700))
    long, short = mac.rows(imports.RECORD_NOW_PLAYING)
    assert len(long.fields["text"].encode()) == 4095  # 1365 characters of three bytes
    assert long.fields["text_truncated"] is True
    assert "text_truncated" not in short.fields
    assert records.clip("ab", 1) == ("a", True)


def test_a_helper_with_no_access_logs_once_and_does_nothing(
    mac: Mac, caplog: pytest.LogCaptureFixture
) -> None:
    """A helper with no access logs once and ships nothing."""

    def refused(_since: int | None, _streams: str | None, _local: int | None) -> dict[str, Any]:
        msg = "no_access: grant Full Disk Access to the test helper"
        raise ScreenTimeUnavailable(msg)

    records.WARNED.clear()
    with caplog.at_level(logging.WARNING):
        for later in (0, 16 * MIN):
            records.ship(mac.names, mac.state, mac.intake, Report(), at(700) + later, read=refused)
    assert len([r for r in caplog.records if "device records" in r.message]) == 1
    assert mac.rows(imports.RECORD_NOW_PLAYING) == []


def test_shipping_is_on_with_mac_ship_and_off_when_asked(tmp_path: Path) -> None:
    """Records ship with mac_ship by default, and apart from the apps."""
    on = ship_config({"mac_ship": {"enabled": True}}, tmp_path)
    off = ship_config(
        {"mac_ship": {"enabled": True, "device_records": {"enabled": False}}}, tmp_path
    )
    assert on is not None
    assert on.device_records == tmp_path / "mac-ship" / "app-names.json"
    assert off is not None
    assert (off.device_records, off.device_names is not None) == (None, True)


# --- the day line ----------------------------------------------------------------------------


def _fold(mac: Mac, tmp_path: Path, now_ms: int, day: str = "2026-10-10") -> Any:  # noqa: ANN401
    start, end = day_window(parse_day(day), ZONE)
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        return fold_day(conn, start, end, now_ms, DaySources(ZONE, shipped=mac.directory))


def _of(line: Any, kind: str) -> list[dict[str, Any]]:  # noqa: ANN401
    return [i for i in line.items if i["kind"] == kind]


def test_media_blocks_join_a_gap_of_five_minutes_drop_the_short_and_name_the_route(
    mac: Mac, tmp_path: Path
) -> None:
    """Playing joins within five minutes per app, tops the titles, names the route."""
    mac.store = {"com.netease.cloudmusic": ("网易云音乐", "Music")}
    pods = f"{PODS}-tacl"
    mac.streams["bluetooth"] = [bt(at(500), [1, 2, 3], connected=True)]
    mac.streams["now_playing"] = [
        # A 3 min, B 4 min, a 2 min pause, B 3 min more: one block of ten minutes on the earbuds
        np(at(600), title="A", artist="X", duration_s=180, route=pods),
        np(at(603), title="B", duration_s=240, route=pods),
        np(at(607), 2, title="B", route=pods),
        np(at(609), title="B", duration_s=240, route=pods),
        np(at(612), 3, route=pods),
        # one minute of a video: under two minutes, dropped
        np(at(700), bundle_id="com.ss.iphone.ugc.Aweme", title="V", duration_s=60),
        np(at(703), 3, bundle_id="com.ss.iphone.ugc.Aweme"),
        # nine minutes of a track of unknown length on a Bluetooth output nobody named
        np(at(800), title="C", route="11:22:33:44:55:66-tsco"),
        np(at(809), 3),
        # a stop and a play at the very same moment, the stop first or second: it plays two minutes
        np(at(900), 3),
        np(at(900), title="D", duration_s=120),
        np(at(902), 3),
        # a track cannot play longer than it is: 60 s of a track whose stop comes at 40 minutes
        np(at(1000), title="E", duration_s=60),
        np(at(1040), 3),
        # the iPad at the same time: a block of its own
        np(at(601), device=PAD, title="P", duration_s=300),
        np(at(606), 3, device=PAD),
    ]
    mac.pass_(at(1500))
    media = _of(_fold(mac, tmp_path, at(1500)), "media")

    assert [(m["device"], m["start_ms"], m["end_ms"], m["active_s"]) for m in media] == [
        ("iPhone", at(600), at(612), 600),
        ("iPad", at(601), at(606), 300),
        ("iPhone", at(800), at(809), 540),
        ("iPhone", at(900), at(902), 120),
    ]
    first = media[0]
    assert first["id"] == f"media-iPhone-{at(600)}"
    assert (first["lane"], first["state"]) == ("you", "done")
    assert (first["app"], first["bundle"]) == ("网易云音乐", "com.netease.cloudmusic")
    assert first["titles"] == [{"title": "B", "artist": None}, {"title": "A", "artist": "X"}]
    assert first["route"] == "Pods"  # a Bluetooth row names this address
    assert media[2]["route"] == "Bluetooth"  # nobody named this one
    assert media[3]["titles"] == [{"title": "D", "artist": None}]


def test_the_earbuds_name_is_the_route_of_their_address_and_titles_stop_at_five(
    mac: Mac, tmp_path: Path
) -> None:
    """A Bluetooth output a bluetooth row names is that name; a block names five titles."""
    mac.streams["bluetooth"] = [bt(at(500), [1, 2, 3], connected=True)]
    mac.streams["now_playing"] = [
        np(at(600 + n), title=f"T{n}", duration_s=60, route=f"{PODS.lower()}-tacl")
        for n in range(7)
    ] + [np(at(607), 3)]
    mac.pass_(at(1500))
    (block,) = _of(_fold(mac, tmp_path, at(1500)), "media")
    assert block["route"] == "Pods"
    assert [t["title"] for t in block["titles"]] == ["T0", "T1", "T2", "T3", "T4"]
    assert device_day.route_name("Built-In Receiver", {}) == "Built-In Receiver"
    assert device_day.route_name(None, {}) is None


def test_earbuds_span_connect_to_disconnect_with_the_battery_at_both(
    mac: Mac, tmp_path: Path
) -> None:
    """Connected spans of the earbuds, going on while recent, a point when it has gone quiet."""
    mac.streams["bluetooth"] = [
        bt(at(500), [100, 57, 57], connected=True),
        bt(at(505), [99, 57, 57], connected=True),  # a repeat: the span began at 500
        bt(at(560), [91, 62, 61], connected=False),
        bt(at(1300), [80, 80, 80]),  # still connected, an hour and a half before the clock
        bt(at(-1800), [10, 10, 10]),  # connected a day and a half before, never heard to stop
    ]
    mac.pass_(at(1400))
    line = _fold(mac, tmp_path, at(1400))
    done, going = _of(line, "earbuds")
    assert done == {
        "id": f"earbuds-iPhone-{at(500)}",
        "kind": "earbuds",
        "lane": "you",
        "state": "done",
        "start_ms": at(500),
        "end_ms": at(560),
        "device": "iPhone",
        "device_name": "Pods",
        "battery_connect": [100, 57, 57],
        "battery_disconnect": [91, 62, 61],
    }
    assert (going["end_ms"], going["state"], going["battery_disconnect"]) == (None, "now", None)
    old = _of(_fold(mac, tmp_path, at(1400), "2026-10-08"), "earbuds")
    assert [(o["start_ms"], o["end_ms"]) for o in old] == [(at(-1800), at(-1800))]


def test_messages_come_in_bursts_per_conversation_and_calls_stay_apart_from_timesink_calls(
    mac: Mac, tmp_path: Path
) -> None:
    """Message bursts per conversation of an app, Siri's calls as their own kind."""
    mac.store = {}
    alex = [person("recipients", "Alex"), handle("recipientHandles", "+15550100", "Alex")]
    group = [{"role": "speakableGroupName", "type": "MessageGroup", "value": "G", "name": "Study"}]
    mac.streams["interactions"] = [
        siri("message", at(600), "A1", alex, conversation_id="c1"),
        siri("message", at(610), "A2", alex, conversation_id="c1", direction=2),
        siri("message", at(635), "A3", alex, conversation_id="c1"),  # 25 min on: the same burst
        siri("message", at(700), "A4", alex, conversation_id="c1"),  # 65 min on: a new one
        siri(
            "message",
            at(605),
            "G1",
            group,
            conversation_id="c2",
            is_group=True,
            bundle="com.hammerandchisel.discord",
        ),
        siri("message", at(606), "H1", [handle("recipientHandles", "+15550111")]),  # a handle only
        siri(
            "call",
            at(900),
            "K1",
            [person("contacts", "Sam")],
            direction=2,
            duration_s=90,
            bundle="com.apple.InCallService",
            intent="INStartCallIntent",
            domain="Calls",
        ),
        siri("call", at(950), "K2", [], duration_s=0, bundle="com.apple.InCallService"),
        siri("audio", at(960), "I1", []),  # stored, never drawn
    ]
    mac.pass_(at(1500))
    line = _fold(mac, tmp_path, at(1500))

    by_start = {m["start_ms"]: m for m in _of(line, "messages")}
    assert (
        by_start[at(600)]["conversation"],
        by_start[at(600)]["sent"],
        by_start[at(600)]["received"],
        by_start[at(600)]["end_ms"],
    ) == ("Alex", 2, 1, at(635))
    assert by_start[at(700)]["conversation"] == "Alex"
    assert by_start[at(606)]["conversation"] == "+15550111"
    chat = by_start[at(605)]
    assert (chat["conversation"], chat["is_group"], chat["app"]) == (
        "Study",
        True,
        "com.hammerandchisel.discord",
    )
    assert chat["id"].startswith("messages-")
    assert chat["id"].endswith(f"-{at(605)}")
    assert len(_of(line, "messages")) == 4

    calls = _of(line, "phone_call")
    assert [
        (c["start_ms"], c["end_ms"], c["direction"], c["person"], c["duration_s"]) for c in calls
    ] == [
        (at(900), at(900) + 90 * SEC, "received", "Sam", 90),
        (at(950), at(950), "sent", None, 0),
    ]
    assert calls[0]["id"] == f"phone_call-iPhone-{at(900)}-K1"
    assert (calls[0]["lane"], calls[0]["state"], calls[0]["device"]) == ("you", "done", "iPhone")
    assert _of(line, "call") == []  # kind "call" stays TimeSink's


def test_device_records_never_says_it_is_missing(mac: Mac, tmp_path: Path) -> None:
    """With no record, or no shipped store, nothing is drawn and nothing is named missing."""
    mac.intake.shipments.receive_entries(
        "timesink",
        b'{"key":"x:1:aaaaaaaaaaaa","kind":"x","at_ms":1,"text":"","zone":"UTC","fields":{}}',
    )
    line = _fold(mac, tmp_path, at(700))
    assert "device_records" not in line.missing
    assert [i["kind"] for i in line.items if i["kind"] in KINDS] == []
    start, end = day_window(parse_day("2026-10-10"), ZONE)
    with closing(open_event_log(tmp_path / "events2.db")) as conn:
        bare = fold_day(conn, start, end, at(700), DaySources(ZONE))
    assert "device_records" not in bare.missing


def test_the_phone_may_have_the_new_kinds_open() -> None:
    """The new item kinds are kinds the phone can have open."""
    for kind in KINDS:
        assert clean_about({"kind": kind, "id": "x"})["kind"] == kind


# --- ADR 0309: this Mac's own databases (Photos, Messages, Safari) --------------------------------


def _typedstream(text: str) -> bytes:
    """An NSAttributedString typedstream as Messages archives it: the words after ``NSString``."""
    raw = text.encode()
    length = bytes([len(raw)]) if len(raw) < 0x80 else b"\x81" + struct.pack("<H", len(raw))
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84"
        b"\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + raw
        + b"\x86\x84\x02iI\x01"
    )


def _place(address: str, city: str) -> bytes:
    """A Photos reverse-geocoded location: a keyed archive of a postal address."""
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$top": {"root": plistlib.UID(1)},
            "$objects": [
                "$null",
                {
                    "addressString": plistlib.UID(2),
                    "postalAddress": plistlib.UID(3),
                    "countryCode": plistlib.UID(4),
                    "isHome": False,
                },
                address,
                {"_city": plistlib.UID(5), "_street": plistlib.UID(0)},  # no street: "$null"
                "CA",
                city,
            ],
        },
        fmt=plistlib.FMT_BINARY,
    )


def _photos(home: Path, cf: float) -> None:
    """A Photos library with a live photo, a screenshot and a trashed burst clip."""
    folder = home / "Pictures" / "Photos Library.photoslibrary" / "database"
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "Photos.sqlite")) as db:
        db.executescript(
            """
            CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZUUID VARCHAR, ZKIND INTEGER,
                ZKINDSUBTYPE INTEGER, ZDATECREATED REAL, ZADDEDDATE REAL, ZMODIFICATIONDATE REAL,
                ZLATITUDE REAL, ZLONGITUDE REAL, ZWIDTH INTEGER, ZHEIGHT INTEGER, ZFAVORITE INTEGER,
                ZTRASHEDSTATE INTEGER, ZPLAYBACKSTYLE INTEGER, ZAVALANCHEKIND INTEGER,
                ZISDETECTEDSCREENSHOT INTEGER, ZMOMENT INTEGER, ZFILENAME VARCHAR);
            CREATE TABLE ZADDITIONALASSETATTRIBUTES (ZASSET INTEGER, ZORIGINALFILENAME VARCHAR,
                ZTIMEZONENAME VARCHAR, ZREVERSELOCATIONDATA BLOB);
            CREATE TABLE ZEXTENDEDATTRIBUTES (ZASSET INTEGER, ZCAMERAMAKE VARCHAR, ZISO INTEGER);
            CREATE TABLE ZMOMENT (Z_PK INTEGER PRIMARY KEY, ZTITLE VARCHAR);
            INSERT INTO ZMOMENT VALUES (1, 'Lake day');
            INSERT INTO ZEXTENDEDATTRIBUTES VALUES (1, 'Apple', 64);
            INSERT INTO ZADDITIONALASSETATTRIBUTES VALUES (2, 'Screenshot.png', NULL, NULL);
            """
        )
        place = _place("1 Main St", "Town")
        db.execute(
            "INSERT INTO ZADDITIONALASSETATTRIBUTES"
            " VALUES (1, 'IMG_0001.HEIC', 'America/Vancouver', ?)",
            (place,),
        )
        db.executemany(
            "INSERT INTO ZASSET VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "UUID-LIVE",
                    0,
                    2,
                    cf,
                    cf + 5,
                    None,
                    49.5,
                    -123.1,
                    4032,
                    3024,
                    1,
                    0,
                    3,
                    0,
                    0,
                    1,
                    "a.heic",
                ),
                (
                    2,
                    "UUID-SHOT",
                    0,
                    0,
                    cf + 60,
                    cf + 61,
                    cf + 99,
                    -180.0,
                    -180.0,
                    1170,
                    2532,
                    0,
                    0,
                    1,
                    0,
                    1,
                    None,
                    "b.png",
                ),
                (
                    3,
                    "UUID-CLIP",
                    1,
                    0,
                    cf + 120,
                    cf + 121,
                    None,
                    0.0,
                    0.0,
                    1920,
                    1080,
                    0,
                    1,
                    4,
                    1,
                    0,
                    None,
                    "c.mov",
                ),
            ],
        )
        db.commit()


def _chats(home: Path, cf: float) -> None:
    """A Messages database: words in `text`, words only in `attributedBody`, none, and a tapback."""
    folder = home / "Library" / "Messages"
    folder.mkdir(parents=True)
    ns = 1_000_000_000
    with closing(sqlite3.connect(folder / "chat.db")) as db:
        db.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT,
                attributedBody BLOB, service TEXT, date INTEGER, date_read INTEGER,
                date_delivered INTEGER,
                is_from_me INTEGER, handle_id INTEGER, associated_message_guid TEXT,
                associated_message_type INTEGER, reply_to_guid TEXT);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT,
                display_name TEXT, style INTEGER, service_name TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT,
                mime_type TEXT, uti TEXT, transfer_name TEXT, total_bytes INTEGER,
                is_sticker INTEGER, is_outgoing INTEGER, transfer_state INTEGER);
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            INSERT INTO handle VALUES (1, '+15550100'), (2, 'sam@example.com');
            INSERT INTO chat VALUES (1, 'chat-guid', 'chat-id', 'Study group', 43, 'iMessage');
            INSERT INTO chat_message_join VALUES (1, 1), (1, 2), (1, 3);
            INSERT INTO chat_handle_join VALUES (1, 2), (1, 1);
            INSERT INTO attachment VALUES
                (1, 'A1', '~/pic.jpg', 'image/jpeg', 'public.jpeg', 'pic.jpg', 2048, 0, 1, 5);
            INSERT INTO message_attachment_join VALUES (1, 1);
            """
        )
        wide = "词" * 100  # 300 bytes: a length that needs the two-byte form
        db.executemany(
            "INSERT INTO message VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    1,
                    "G-PLAIN",
                    "hello",
                    None,
                    "iMessage",
                    cf * ns,
                    (cf + 30) * ns,
                    None,
                    1,
                    1,
                    None,
                    0,
                    None,
                ),
                (
                    2,
                    "G-BODY",
                    None,
                    _typedstream("from the body"),
                    "SMS",
                    (cf + 60) * ns,
                    0,
                    (cf + 61) * ns,
                    0,
                    1,
                    None,
                    0,
                    "G-PLAIN",
                ),
                (
                    3,
                    "G-WIDE",
                    None,
                    _typedstream(wide),
                    "iMessage",
                    (cf + 90) * ns,
                    0,
                    None,
                    0,
                    1,
                    None,
                    0,
                    None,
                ),
                (
                    4,
                    "G-NONE",
                    None,
                    None,
                    "iMessage",
                    (cf + 120) * ns,
                    0,
                    None,
                    0,
                    1,
                    "p:0/G-PLAIN",
                    2001,
                    None,
                ),  # in no chat
            ],
        )
        db.commit()


def _safari(home: Path, cf: float) -> None:
    """A Safari history with two visits at one moment to one address."""
    folder = home / "Library" / "Safari"
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "History.db")) as db:
        db.executescript(
            """
            CREATE TABLE history_items (id INTEGER PRIMARY KEY, url TEXT);
            CREATE TABLE history_visits (id INTEGER PRIMARY KEY, history_item INTEGER,
                visit_time REAL, title TEXT, load_successful INTEGER, origin INTEGER,
                redirect_source INTEGER,
                redirect_destination INTEGER);
            INSERT INTO history_items VALUES (1, 'https://example.com/a');
            """
        )
        db.executemany(
            "INSERT INTO history_visits VALUES (?,?,?,?,?,?,?,?)",
            [(1, 1, cf, "Page A", 1, 0, None, 2), (2, 1, cf, None, 1, 1, 1, None)],
        )
        db.commit()


def _helper_on(home: Path, *more: str) -> dict[str, Any]:
    """The helper's answer for the three databases under ``home`` and nothing from Biome."""
    binary = ensure_screen_time_app() / "Contents" / "MacOS" / "jarvis-screen-time"
    out = home / "out.json"
    subprocess.run(  # noqa: S603 - the repo's own helper on fixture databases
        [
            *(str(binary), str(out), "--root", str(home / "none"), "--home", str(home)),
            *("--streams", "photos,messages,safari", *more),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return json.loads(out.read_text())  # type: ignore[no-any-return]


def _only(row: dict[str, Any], *names: str) -> dict[str, Any]:
    """The named fields of a record, the missing ones as ``None``."""
    return {name: row.get(name) for name in names}


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None, reason="needs macOS and swiftc"
)
def test_the_helper_reads_photos_messages_and_safari_into_named_fields(tmp_path: Path) -> None:
    """Each database is read by column name, whole or from a time, with the words decoded."""
    when = 1_790_000_000_000
    cf = when / 1000 - 978_307_200
    _photos(tmp_path, cf)
    _chats(tmp_path, cf)
    _safari(tmp_path, cf)
    answer = _helper_on(tmp_path)
    assert "unavailable" not in answer

    live, shot, clip = answer["streams"]["photos"]
    assert _only(live, "uuid", "kind", "live", "burst", "favorite", "latitude", "moment_title") == {
        "uuid": "UUID-LIVE",
        "kind": "photo",
        "live": True,
        "burst": False,
        "favorite": True,
        "latitude": 49.5,
        "moment_title": "Lake day",
    }
    assert _only(live, "original_filename", "camera_make", "iso", "added_ms", "modified_ms") == {
        "original_filename": "IMG_0001.HEIC",
        "camera_make": "Apple",
        "iso": 64,
        "added_ms": when + 5000,
        "modified_ms": None,
    }
    assert live["place"] == {
        "address": "1 Main St",
        "city": "Town",
        "country_code": "CA",
        "is_home": False,
    }  # no street: the archive's "$null" is not a street
    assert _only(shot, "screenshot", "latitude", "modified_ms") == {
        "screenshot": True,
        "latitude": None,  # -180 is Photos' "no location"
        "modified_ms": when + 99_000,
    }
    assert _only(clip, "kind", "trashed", "burst", "seen_ms") == {
        "kind": "video",
        "trashed": True,
        "burst": True,
        "seen_ms": when + 121_000,
    }

    plain, body, wide, bare = answer["streams"]["messages"]
    assert _only(plain, "text", "text_source", "from_me", "read_ms", "service") == {
        "text": "hello",
        "text_source": "text",
        "from_me": True,
        "read_ms": when + 30_000,
        "service": "iMessage",
    }
    assert _only(body, "text", "text_source", "delivered_ms", "reply_to_guid") == {
        "text": "from the body",
        "text_source": "attributed_body",
        "delivered_ms": when + 61_000,
        "reply_to_guid": "G-PLAIN",
    }
    assert _only(body, "chat_display_name", "is_group", "handle", "participants") == {
        "chat_display_name": "Study group",
        "is_group": True,
        "handle": "+15550100",
        "participants": ["+15550100", "sam@example.com"],
    }
    assert wide["text"] == "词" * 100
    assert _only(bare, "text", "chat_guid", "associated_guid", "associated_type") == {
        "text": None,
        "chat_guid": None,
        "associated_guid": "p:0/G-PLAIN",
        "associated_type": 2001,
    }
    assert plain["attachments"] == [
        {
            "guid": "A1",
            "filename": "~/pic.jpg",
            "transfer_name": "pic.jpg",
            "mime_type": "image/jpeg",
            "uti": "public.jpeg",
            "bytes": 2048,
            "sticker": False,
            "outgoing": True,
            "transfer_state": 5,
        }
    ]

    first, second = answer["streams"]["safari"]
    assert _only(first, "url", "title", "origin", "redirect_destination", "load_successful") == {
        "url": "https://example.com/a",
        "title": "Page A",
        "origin": 0,
        "redirect_destination": 2,
        "load_successful": True,
    }
    assert _only(second, "at_ms", "origin", "redirect_source", "title") == {
        "at_ms": first["at_ms"],
        "origin": 1,
        "redirect_source": 1,
        "title": None,
    }

    # from a time: only rows made, changed or read at or after it
    later = _helper_on(tmp_path, "--local-since-ms", str(when + 95_000))
    assert [r["uuid"] for r in later["streams"]["photos"]] == ["UUID-SHOT", "UUID-CLIP"]
    assert [r["guid"] for r in later["streams"]["messages"]] == ["G-NONE"]
    assert later["streams"]["safari"] == []

    # a database that is not there is said, not an error
    (tmp_path / "Library" / "Safari" / "History.db").unlink()
    gone = _helper_on(tmp_path)
    assert gone["unavailable"] == {"safari": "missing"}
    assert gone["streams"]["safari"] == []
    assert len(gone["streams"]["photos"]) == 3


def local(
    stream: str,
    when: int,
    ident: str,
    seen: int | None = None,
    **more: Any,  # noqa: ANN401
) -> dict[str, Any]:
    """A helper-shaped record of the Mac's own databases."""
    keys = {"photos": "uuid", "messages": "guid", "safari": "url"}
    extra = {"visit_id": 1} if stream == "safari" else {}
    return {"at_ms": when, "seen_ms": seen or when, keys[stream]: ident, **extra, **more}


def test_a_photo_a_message_and_a_visit_are_rows_of_their_own_and_a_change_is_a_newer_revision(
    mac: Mac,
) -> None:
    """A record's key names it, so a later change is the same row at a newer revision."""
    mac.streams["photos"] = [local("photos", at(100), "UUID-1", kind="photo", favorite=False)]
    mac.streams["messages"] = [local("messages", at(110), "G-1", text="hello", from_me=True)]
    mac.streams["safari"] = [
        local("safari", at(120), "https://a.example/", visit_id=7, origin=1),
        local("safari", at(120), "https://a.example/", visit_id=8, origin=0, redirect_source=7),
    ]
    mac.pass_(at(700))
    (photo,) = mac.rows(imports.RECORD_PHOTO)
    (message,) = mac.rows(imports.RECORD_CHAT_MESSAGE)
    first, second = mac.rows(imports.RECORD_SAFARI_VISIT)
    (photo_key,) = mac.keys(imports.RECORD_PHOTO)
    (message_key,) = mac.keys(imports.RECORD_CHAT_MESSAGE)
    assert photo_key == f"photo:mac:UUID-1:{digest(photo.fields)}"
    assert message_key == f"chat_message:mac:G-1:{digest(message.fields)}"
    assert (photo.fields["device_id"], photo.fields["device"]) == ("mac", "Mac")
    assert message.text == "hello"
    assert (first.fields["visit_id"], second.fields["visit_id"]) == (
        7,
        8,
    )  # one moment, one address
    assert mac.intake.added == [4]

    # he favourites the photo and reads the message: the same rows, a newer revision each
    mac.streams["photos"] = [
        local("photos", at(100), "UUID-1", at(710), kind="photo", favorite=True)
    ]
    mac.streams["messages"] = [
        local("messages", at(110), "G-1", at(712), text="hello", from_me=True, read_ms=at(712))
    ]
    mac.pass_(at(700) + 16 * MIN)
    assert mac.intake.added == [4, 2]
    (photo,) = mac.rows(imports.RECORD_PHOTO)
    (message,) = mac.rows(imports.RECORD_CHAT_MESSAGE)
    assert photo.fields["favorite"] is True
    assert message.fields["read_ms"] == at(712)
    assert len(mac.keys(imports.RECORD_PHOTO)) == 2
    mac.pass_(at(700) + 32 * MIN)  # nothing changed: nothing sent
    assert mac.intake.added == [4, 2]


def test_words_and_addresses_are_kept_up_to_four_kilobytes_and_say_so(mac: Mac) -> None:
    """A message's text, a visit's address and its title are cut at 4096 bytes and marked."""
    mac.streams["messages"] = [local("messages", at(110), "G-1", text="词" * 2000)]
    mac.streams["safari"] = [
        local("safari", at(120), "https://a.example/" + "x" * 5000, title="t" * 5000)
    ]
    mac.pass_(at(700))
    (message,) = mac.rows(imports.RECORD_CHAT_MESSAGE)
    (visit,) = mac.rows(imports.RECORD_SAFARI_VISIT)
    assert len(message.fields["text"].encode()) == 4095
    assert message.fields["text_truncated"] is True
    assert len(visit.fields["url"]) == 4096
    assert visit.fields["url_truncated"] is True
    assert len(visit.fields["title"]) == 4096
    assert visit.fields["title_truncated"] is True


def test_the_mac_databases_have_their_own_window_and_are_read_whole_once_a_day(mac: Mac) -> None:
    """The window catches rows touched late; the daily whole read catches rows that arrive late."""
    mac.streams["messages"] = [local("messages", at(600), "G-1", text="a")]
    mac.pass_(at(700))
    assert mac.local_reads == [None]  # nothing met yet: all of it
    mac.pass_(at(700) + 16 * MIN)
    assert mac.local_reads[1] == at(600) + 1 - records.LOCAL_OVERLAP_MS
    quiet = at(600) + 20 * 24 * H  # a quiet database is not read back more than the lookback
    mac.pass_(quiet)
    assert mac.local_reads[2] is None  # a day has passed since the whole read
    mac.pass_(quiet + 16 * MIN)
    assert mac.local_reads[3] == quiet + 16 * MIN - records.LOCAL_LOOKBACK_MS
    # an old message that iCloud brings down late is outside the window but not the daily read
    mac.streams["messages"].append(local("messages", at(600) - 30 * 24 * H, "G-OLD", text="late"))
    mac.pass_(quiet + 32 * MIN)
    assert len(mac.rows(imports.RECORD_CHAT_MESSAGE)) == 1
    mac.pass_(quiet + 25 * H)
    assert mac.local_reads[-1] is None
    assert len(mac.rows(imports.RECORD_CHAT_MESSAGE)) == 2


def test_a_database_that_cannot_be_read_is_said_once_and_the_rest_ships(
    mac: Mac, caplog: pytest.LogCaptureFixture
) -> None:
    """A missing grant for one database logs once; the other kinds still ship."""
    mac.unavailable = {"photos": "no_access"}
    mac.streams["safari"] = [local("safari", at(120), "https://a.example/")]
    records.WARNED.clear()
    with caplog.at_level(logging.WARNING):
        mac.pass_(at(700))
        mac.pass_(at(700) + 16 * MIN)
    assert len([r for r in caplog.records if "photos unavailable" in r.message]) == 1
    assert len(mac.rows(imports.RECORD_SAFARI_VISIT)) == 1
    assert mac.rows(imports.RECORD_PHOTO) == []
