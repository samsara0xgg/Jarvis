"""ADR 0304: synced iPhone/iPad app use. The Swift helper reads hand-built SEGB v1 and v2 files."""

from __future__ import annotations

import json
import sqlite3
import struct
import subprocess
import sys
import zlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from jarvis.surface import mac_screen_time as mst

if TYPE_CHECKING:
    from pathlib import Path

CF_TO_UNIX = 978_307_200
PHONE = "AAAAAAAA-1111-2222-3333-444444444444"
PAD = "BBBBBBBB-1111-2222-3333-444444444444"


def _varint(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append(n & 0x7F | 0x80)
        n >>= 7
    return bytes([*out, n])


def _record(bundle: str, gained: int, unix_s: float) -> bytes:
    """One AppInFocus protobuf: reason (1), kind (2), gained (3), CFAbsoluteTime (4), bundle (6)."""
    reason = b"com.apple.SpringBoard.transitionReason.appswitcher"
    return (
        b"\x0a" + _varint(len(reason)) + reason
        + b"\x10\x01"
        + b"\x18" + _varint(gained)
        + b"\x21" + struct.pack("<d", unix_s - CF_TO_UNIX)
        + b"\x32" + _varint(len(bundle)) + bundle.encode()
        + b"\x48\x01"  # field 9 as a varint: an unknown field the reader must step over
    )  # fmt: skip


def segb_v1(records: list[tuple[int, bytes]]) -> bytes:
    """56-byte header (end offset first, magic last), 32-byte record headers, 8-aligned data."""
    body = b""
    for state, data in records:
        body += struct.pack("<iiddIi", len(data), state, 0.0, 0.0, zlib.crc32(data), 0) + data
        body += b"\0" * (-(56 + len(body)) % 8)
    return struct.pack("<I", 56 + len(body)) + b"\0" * 48 + b"SEGB" + body


def segb_v2(records: list[tuple[int, bytes]]) -> bytes:
    """32-byte header, 4-aligned records (crc, unknown, data), then a 16-byte trailer entry each."""
    body = b""
    trailer = b""
    for state, data in records:
        body += struct.pack("<Ii", zlib.crc32(data), 0) + data
        trailer += struct.pack("<iid", len(body), state, 0.0)
        body += b"\0" * (-len(body) % 4)
    return b"SEGB" + struct.pack("<id", len(records), 0.0) + b"\0" * 16 + body + trailer


def _ms(s: str) -> int:
    return int(_utc(s) * 1000)


def _utc(s: str) -> float:
    return datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp()


@pytest.fixture
def biome(tmp_path: Path) -> Path:
    """A Biome folder with a phone (v1 segment, named) and an iPad (v2 segment, unnamed)."""
    root = tmp_path / "Library" / "Biome"
    remote = root / "streams" / "restricted" / "App.InFocus" / "remote"
    (remote / PHONE).mkdir(parents=True)
    (remote / PAD).mkdir()
    # Phone: a v1 segment holding a written pair, a deleted (zeroed) record and a later "in".
    (remote / PHONE / "700000000000000").write_bytes(
        segb_v1(
            [
                (1, _record("com.apple.mobilesafari", 1, _utc("2026-10-18T10:00:00"))),
                (1, _record("com.apple.mobilesafari", 0, _utc("2026-10-18T10:20:00"))),
                (3, b"\0" * 40),
                (1, _record("com.burbn.instagram", 1, _utc("2026-10-18T11:00:00"))),
            ]
        )
    )
    # iPad: a v2 segment with one pair.
    (remote / PAD / "700000000000001").write_bytes(
        segb_v2(
            [
                (1, _record("com.apple.Maps", 1, _utc("2026-10-18T12:00:00"))),
                (1, _record("com.apple.Maps", 0, _utc("2026-10-18T12:05:00"))),
            ]
        )
    )
    (root / "sync").mkdir()
    with sqlite3.connect(root / "sync" / "sync.db") as db:
        db.execute(
            "CREATE TABLE DevicePeer (device_identifier, ids_device_identifier, name, platform)"
        )
        db.execute("INSERT INTO DevicePeer VALUES (?, 'ids-1', 'Allen iPhone', 2)", (PHONE,))
    return root


@pytest.mark.skipif(sys.platform != "darwin", reason="the helper is a macOS app")
def test_helper_reads_v1_and_v2_segments(biome: Path, tmp_path: Path) -> None:
    """Deleted records and those before --since-ms are left out; a missing folder has its reason."""
    app = mst.ensure_screen_time_app()
    binary = app / "Contents" / "MacOS" / "jarvis-screen-time"
    out = tmp_path / "out.json"
    since = int(_utc("2026-10-18T10:30:00") * 1000)
    subprocess.run(  # noqa: S603 - repo-built binary, temp fixture paths
        [str(binary), str(out), "--root", str(biome), "--since-ms", str(since)],
        check=True,
        timeout=30,
    )
    data = json.loads(out.read_text())
    assert {d["id"]: d["name"] for d in data["devices"]} == {PHONE: "Allen iPhone", PAD: None}
    assert [(e["device"], e["bundle_id"], e["focus"]) for e in data["events"]] == [
        (PHONE, "com.burbn.instagram", "in"),
        (PAD, "com.apple.Maps", "in"),
        (PAD, "com.apple.Maps", "out"),
    ]
    assert data["events"][0]["at_ms"] == int(_utc("2026-10-18T11:00:00") * 1000)

    subprocess.run(  # noqa: S603
        [str(binary), str(out), "--root", str(biome / "nope")], check=True, timeout=30
    )
    assert json.loads(out.read_text()) == {"error": "no_remote_streams"}


def test_pairing_and_summary() -> None:
    """An unpaired "in" is cut at the next event or 30 minutes and marked in the summary."""

    def ev(bundle: str, focus: str, when: str) -> dict[str, object]:
        return {"device": PHONE, "bundle_id": bundle, "focus": focus, "at_ms": _ms(when)}

    events = [
        ev("a", "in", "2026-10-18T10:00:00"),
        ev("a", "out", "2026-10-18T10:20:00"),
        ev("b", "in", "2026-10-18T11:00:00"),
        ev("c", "in", "2026-10-18T11:10:00"),  # b never closed: cut at this event
        ev("c", "in", "2026-10-18T11:12:00"),  # repeated "in" keeps the start
        ev("c", "out", "2026-10-18T11:20:00"),
        ev("d", "in", "2026-10-18T13:00:00"),  # nothing follows: capped at 30 min
    ]
    assert [(b, (e - s) // 60000, ok) for b, s, e, ok in mst.pair_intervals(events)] == [
        ("a", 20, True),
        ("b", 10, False),
        ("c", 10, True),
        ("d", 30, False),
    ]
    text = mst.summarize(
        {"devices": [{"id": PHONE, "name": "Allen iPhone"}], "events": events}, UTC
    )
    assert "Allen iPhone: 7 events, 2026-10-18 10:00 -> 2026-10-18 13:00" in text
    assert "2026-10-18  total 70 min" in text
    assert "d" in text
    assert "30.0*" in text


def test_helper_errors_become_reasons() -> None:
    """The helper's error object becomes a raised reason; a good answer comes through."""
    with pytest.raises(mst.ScreenTimeUnavailable, match="Full Disk Access"):
        mst.parse_helper_output('{"error": "no_access"}')
    with pytest.raises(mst.ScreenTimeUnavailable, match="no_remote_streams"):
        mst.parse_helper_output('{"error": "no_remote_streams"}')
    with pytest.raises(mst.ScreenTimeUnavailable, match="no usable answer"):
        mst.parse_helper_output("")
    assert mst.parse_helper_output('{"devices": [], "events": []}') == {"devices": [], "events": []}
