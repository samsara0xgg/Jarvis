"""ADR 0294: the Mac terminal copies TimeSink and Claude Code sessions into the brain.

Acceptance checks against the real intake, the real routes and device-token guard, the real
shipper and a fixture TimeSink database and synthetic session logs:

- only a paired device's own token opens the routes; a body is kept raw, read into
  ``import_entries`` with a revision key, and sent again it adds nothing and is "already held";
  a bad line refuses the whole body; a disk below the floor refuses with 507 and keeps nothing;
- a picture is kept whole under its path with its SHA-256, the same bytes again are 200, other
  bytes under the path 409;
- ``newest`` takes the last revision of each row; everything lands in ``imports/shipped.db`` and
  memory.db is never created or touched;
- the mirror copies every table of a fixture database from its schema (a table added later too),
  with UTC times, keeps a row that changes only when it first appears, settles or is an hour old,
  and ships a changed old row at once;
- a session record holds counts, titles, models, cost, recap and repo, and nothing that was said;
  it grows from the byte offset, and a growing one ships at most every 30 minutes;
- through HTTP: a refused batch leaves the cursor, the next pass sends it again, a sent one is
  never sent twice.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient

from jarvis.state import imports
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
    unpair_device,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.state.shipments import Shipments, ShipRefused
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.mac_ship import HttpSink, ShipConfig, Shipper
from jarvis.surface.mac_ship_core import Conflict, ShipFailed, State
from jarvis.surface.mac_ship_timesink import TimesinkConfig, utc_ms

if TYPE_CHECKING:
    from collections.abc import Iterator

    from httpx2 import Response  # what the test client answers with

REMOTE = "100.87.250.92"
LOOPBACK = "127.0.0.1"
MIN = 60_000
HOUR = 60 * MIN
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360f8cfc0f01f0005000201a5f645400000000049454e44ae426082",
)
JPG_LIKE = b"\xff\xd8\xff\xe0" + b"\x00" * 64


def _line(key: str, at_ms: int = 1_792_386_107_282, **fields: object) -> bytes:
    return json.dumps({
        "key": key, "at_ms": at_ms, "kind": "span", "text": "t", "zone": "UTC", "fields": fields,
    }).encode()


@dataclass
class Brain:
    """One app with the intake wired, a paired Mac, and a client on each side."""

    root: Path
    intake: Shipments
    key: str
    token: str
    loopback: TestClient
    remote: TestClient
    free: list[int]

    def post(
        self, source: str, body: bytes, *, auth: str | None = None, via: str = "remote",
    ) -> Response:
        """POST a shipment with the Mac's token unless ``auth`` says otherwise."""
        client = self.remote if via == "remote" else self.loopback
        return client.post(
            f"/inherent/imports/{source}", content=body,
            headers={"Authorization": f"Bearer {self.token if auth is None else auth}"},
        )

    def put(self, source: str, rel: str, data: bytes) -> Response:
        """PUT one file with the Mac's token."""
        return self.remote.put(
            f"/inherent/imports/{source}/files/{rel}", content=data,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def rows(self) -> list[tuple[str, str]]:
        """``(source, key)`` of every entry, in the order they were kept."""
        with closing(imports.open_shipped(self.root / imports.IMPORTS_DIRNAME)) as conn:
            return list(conn.execute("SELECT source, key FROM import_entries ORDER BY rowid"))


@pytest.fixture
def brain(tmp_path: Path) -> Iterator[Brain]:
    """The brain with the intake wired and a Mac paired."""
    root = tmp_path / "brain"
    root.mkdir()
    free = [100 * 1024**3]
    intake = Shipments(root / imports.IMPORTS_DIRNAME, free_bytes=lambda _p: free[0])
    app = create_app(InherentDeps(
        submit_callable=lambda _t: "T", broadcaster=InherentBroadcaster(),
        device_name=functools.partial(device_name_for_token, root), imports_intake=intake,
    ))
    key = local_key(root)
    require_local_key(
        app, functools.partial(local_key_matches, key), extra_hosts=[REMOTE],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    token = pair_device(root, "macbook")
    with (
        TestClient(app, base_url=f"http://{LOOPBACK}:8006", client=(LOOPBACK, 50000)) as loop,
        TestClient(app, base_url=f"http://{REMOTE}:8006", client=(REMOTE, 50000)) as remote,
    ):
        yield Brain(root, intake, key, token, loop, remote, free)


# --- the routes ----------------------------------------------------------------------------


def test_only_a_paired_devices_own_token_opens_the_routes(brain: Brain) -> None:
    """The Mac's token from the tailnet opens them; the local key is 403, the rest 401."""
    body = _line("span:1:aaaaaaaaaaaa")

    assert brain.post("timesink", body).status_code == 201
    assert brain.post("timesink", body, auth=brain.key, via="loopback").status_code == 403
    assert [
        brain.post("timesink", body, auth="nope").status_code,
        brain.post("timesink", body, auth=brain.key).status_code,
        brain.post("timesink", body, auth="nope", via="loopback").status_code,
    ] == [401, 401, 401]
    unpair_device(brain.root, "macbook")
    assert brain.post("timesink", body).status_code == 401
    assert brain.put("timesink", "2026-10-18/1.png", PNG).status_code == 401
    assert len(brain.rows()) == 1


def test_a_body_is_kept_raw_and_sent_again_it_adds_nothing(brain: Brain) -> None:
    """Raw bytes on disk, entries in shipped.db and none in memory.db; again is 200 already held."""
    body = _line("span:1:aaaaaaaaaaaa", title="a") + b"\n" + _line("span:2:bbbbbbbbbbbb")
    first = brain.post("timesink", body)
    again = brain.post("timesink", body)

    assert first.status_code == 201
    assert first.json() == {
        "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "added": 2, "held": 0,
        "conflicting": 0, "already_held": False,
    }
    assert again.status_code == 200
    assert (again.json()["already_held"], again.json()["added"], again.json()["held"]) == (
        True, 0, 2,
    )
    kept = list((brain.root / "imports" / "timesink").glob("*-shipment.jsonl"))
    assert [p.read_bytes() for p in kept] == [body]
    assert len(brain.rows()) == 2
    assert not (brain.root / "memory.db").exists()


def test_a_changed_row_is_a_new_revision_and_newest_takes_the_last(brain: Brain) -> None:
    """Revision keys keep both versions; ``newest`` gives one per row; memory.db is untouched."""
    brain.post("timesink", _line("span:1:aaaaaaaaaaaa", end="05:00"))
    brain.post("timesink", _line("span:1:bbbbbbbbbbbb", end="05:05"))
    brain.post("timesink", _line("span:2:cccccccccccc", end="05:01"))

    with closing(imports.open_shipped(brain.root / imports.IMPORTS_DIRNAME)) as conn:
        newest = imports.newest(conn, "timesink")
        everything = imports.entries(conn, source="timesink")

    assert len(brain.rows()) == 3
    assert sorted(e.fields["end"] for e in newest) == ["05:01", "05:05"]
    assert not (brain.root / "memory.db").exists()
    assert len(everything) == 3


def test_a_bad_line_refuses_the_whole_body_and_keeps_nothing(brain: Brain) -> None:
    """422 for a line that is not an entry, 404 for a source nobody ships, 400 for empty."""
    good = _line("span:1:aaaaaaaaaaaa")
    bad = json.dumps({"key": "x", "kind": "span", "at_ms": "soon", "fields": {}}).encode()

    assert brain.post("timesink", good + b"\n" + bad).status_code == 422
    assert brain.post("timesink", good + b"\nnot json").status_code == 422
    assert brain.post("wechat_pay", good).status_code == 404
    assert brain.post("timesink", b"\n").status_code == 400
    assert brain.rows() == []
    assert not (brain.root / "imports" / "timesink").exists() or not list(
        (brain.root / "imports" / "timesink").iterdir(),
    )


def test_a_full_disk_refuses_507_and_keeps_nothing(brain: Brain) -> None:
    """Below the floor nothing is written; once there is room the same bytes go in."""
    brain.free[0] = 1024
    body = _line("span:1:aaaaaaaaaaaa")

    assert brain.post("timesink", body).status_code == 507
    assert brain.put("timesink", "2026-10-18/1.png", PNG).status_code == 507
    assert brain.rows() == []
    assert list((brain.root / "imports").rglob("*.png")) == []
    brain.free[0] = 100 * 1024**3
    assert brain.post("timesink", body).status_code == 201


def test_a_picture_is_kept_whole_and_other_bytes_under_its_path_are_refused(brain: Brain) -> None:
    """201 with the digest, 200 for the same bytes, 409 for others; path and bytes are checked."""
    first = brain.put("timesink", "2026-10-18/1792385749279.png", PNG)
    again = brain.put("timesink", "2026-10-18/1792385749279.png", PNG)
    other = brain.put("timesink", "2026-10-18/1792385749279.png", PNG + b"x")

    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 409)
    assert first.json() == {"sha256": hashlib.sha256(PNG).hexdigest(), "bytes": len(PNG)}
    stored = brain.root / "imports" / "timesink" / "files" / "2026-10-18" / "1792385749279.png"
    assert stored.read_bytes() == PNG
    assert stored.stat().st_mode & 0o777 == 0o600
    assert brain.put("timesink", "2026-10-18/2.png", b"not a picture").status_code == 415
    assert brain.put("timesink", "2026-10-18/2.txt", PNG).status_code == 422
    assert brain.put("timesink", "2026-10-18/2.png", b"").status_code == 400
    assert brain.put("claude-code", "2026-10-18/2.png", PNG).status_code == 404
    for rel in ("a/../../x.png", ".hidden/1.png", "a//1.png"):
        with pytest.raises(ShipRefused):
            brain.intake.receive_file("timesink", rel, PNG)
    assert sorted(p.name for p in stored.parent.iterdir()) == ["1792385749279.png"]


# --- the mirror ----------------------------------------------------------------------------


def _stamp(moment_ms: int) -> str:
    return datetime.fromtimestamp(moment_ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class Recorder:
    """A sink that keeps what it is sent, and can be told to refuse."""

    def __init__(self) -> None:
        """Nothing sent yet."""
        self.bodies: list[tuple[str, bytes]] = []
        self.files: dict[str, bytes] = {}
        self.down = False

    def send_entries(self, source: str, body: bytes) -> None:
        """Keep one body."""
        if self.down:
            msg = "down"
            raise ShipFailed(msg)
        self.bodies.append((source, body))

    def send_file(self, source: str, relpath: str, data: bytes) -> str:
        """Keep one file."""
        if self.down:
            msg = "down"
            raise ShipFailed(msg)
        if relpath in self.files and self.files[relpath] != data:
            msg = "other bytes"
            raise Conflict(msg)
        self.files[f"{source}/{relpath}"] = data
        return hashlib.sha256(data).hexdigest()

    def entries(self) -> list[dict[str, object]]:
        """Every entry sent, in order."""
        return [json.loads(line) for _, body in self.bodies for line in body.splitlines()]

    def entry_keys(self) -> list[str]:
        """Every key sent, in order."""
        return [str(entry["key"]) for entry in self.entries()]


def _fixture_db(path: Path, now_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE grdb_migrations (identifier TEXT NOT NULL PRIMARY KEY);
        INSERT INTO grdb_migrations VALUES ('v1');
        CREATE TABLE span (id INTEGER PRIMARY KEY AUTOINCREMENT, start DATETIME NOT NULL,
            "end" DATETIME NOT NULL, appName TEXT NOT NULL, title TEXT);
        CREATE TABLE capture (id INTEGER PRIMARY KEY AUTOINCREMENT, at DATETIME NOT NULL,
            lastSeenAt DATETIME NOT NULL, text TEXT NOT NULL, imagePath TEXT);
        CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE verdict (appBundleID TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
            prob DOUBLE NOT NULL, at DATETIME NOT NULL, PRIMARY KEY (appBundleID, title));
    """)
    start = _stamp(now_ms - 10 * MIN)
    conn.execute("INSERT INTO span (start, \"end\", appName, title) VALUES (?, ?, 'Xcode', 'Main')",
                 (start, _stamp(now_ms - 9 * MIN)))
    conn.execute(
        "INSERT INTO capture (at, lastSeenAt, text, imagePath) VALUES (?, ?, 'hello OCR', ?)",
        (start, start, "2026-10-19/shot.png"),
    )
    conn.execute("INSERT INTO setting VALUES ('jevEnabled', 'true')")
    conn.execute("INSERT INTO verdict VALUES ('com.x', 'T', 0.9, ?)", (start,))
    conn.commit()
    return conn


def _shipper(tmp_path: Path, sink: Recorder, db: Path, *, images: bool = False) -> Shipper:
    config = ShipConfig(
        tmp_path / "state" / "state.db", timesink=TimesinkConfig(db, images=images),
    )
    return Shipper(config, sink, State(config.state_path))


def test_the_mirror_copies_every_table_from_the_schema_in_utc(tmp_path: Path) -> None:
    """Every table but the migration ledger, whole rows, UTC ms, keys with the row's identity."""
    now = int(time.time() * 1000)
    conn = _fixture_db(tmp_path / "timesink.sqlite", now)
    sink = Recorder()
    shipper = _shipper(tmp_path, sink, tmp_path / "timesink.sqlite")

    shipper.poll(now)

    entries = {str(e["kind"]): e for e in sink.entries()}
    assert sorted(entries) == ["capture", "setting", "span", "verdict"]
    span = entries["span"]
    assert span["at_ms"] == utc_ms(_stamp(now - 10 * MIN))
    assert span["fields"] == {
        "id": 1, "start": _stamp(now - 10 * MIN), "end": _stamp(now - 9 * MIN),
        "appName": "Xcode", "title": "Main",
    }
    assert str(span["key"]).startswith("span:1:")
    assert (entries["capture"]["text"], entries["span"]["text"]) == ("hello OCR", "Main")
    assert str(entries["setting"]["key"]).startswith('setting:["jevEnabled"]:')
    assert str(entries["verdict"]["key"]).startswith('verdict:["com.x","T"]:')
    assert entries["setting"]["at_ms"] == now  # no time column: when it was seen
    assert utc_ms("2026-10-19 05:01:47.282") == 1_792_386_107_282

    # a table TimeSink adds later is mirrored without a code change; a quiet pass sends nothing
    conn.execute("CREATE TABLE budget (categoryID TEXT PRIMARY KEY, dailySeconds INTEGER)")
    conn.execute("INSERT INTO budget VALUES ('work', 3600)")
    conn.commit()
    before = len(sink.entries())
    shipper.poll(now + MIN)
    assert [str(e["kind"]) for e in sink.entries()[before:]] == ["budget"]
    after = len(sink.entries())
    assert shipper.poll(now + 2 * MIN).skipped["timesink/poll"] == 1
    assert len(sink.entries()) == after


def test_a_row_still_changing_ships_when_it_first_appears_settles_or_is_an_hour_old(
    tmp_path: Path,
) -> None:
    """An open span whose end grows is not shipped on every change."""
    now = int(time.time() * 1000)
    db = tmp_path / "timesink.sqlite"
    conn = _fixture_db(db, now)
    sink = Recorder()
    shipper = _shipper(tmp_path, sink, db)

    def spans() -> list[str]:
        return [k for k in sink.entry_keys() if k.startswith("span:")]

    def grow(minutes: int) -> None:
        conn.execute('UPDATE span SET "end" = ? WHERE id = 1', (_stamp(now + minutes * MIN),))
        conn.commit()

    shipper.poll(now)
    assert len(spans()) == 1  # first appearance
    grow(5)
    shipper.poll(now + 5 * MIN)
    grow(10)
    shipper.poll(now + 10 * MIN)
    assert len(spans()) == 1  # changed twice in a row: nothing in between is shipped
    shipper.poll(now + 15 * MIN)
    assert len(spans()) == 2  # it stopped: the settled version ships once
    shipper.poll(now + 20 * MIN)
    assert len(spans()) == 2

    for step in range(1, 14):  # it never stops: shipped again once the last one is an hour old
        grow(20 + step * 5)
        shipper.poll(now + (15 + step * 5) * MIN)
    assert len(spans()) == 3
    assert len(set(spans())) == 3  # three distinct revision keys of the one row


def test_a_changed_row_older_than_two_days_ships_at_once_in_the_daily_sweep(
    tmp_path: Path,
) -> None:
    """A row past 48 h is settled by age; the daily sweep finds it."""
    now = int(time.time() * 1000)
    db = tmp_path / "timesink.sqlite"
    conn = _fixture_db(db, now)
    sink = Recorder()
    shipper = _shipper(tmp_path, sink, db)
    shipper.poll(now)
    before = len(sink.entry_keys())

    later = now + 50 * HOUR
    conn.execute("UPDATE span SET title = 'Renamed' WHERE id = 1")
    conn.commit()
    shipper.poll(later)  # sweep is due (a day on), the row is older than 48 h

    new = [e for e in sink.entries()[before:] if e["kind"] == "span"]
    assert [e["text"] for e in new] == ["Renamed"]


@pytest.mark.skipif(not Path("/usr/bin/sips").exists(), reason="macOS sips")
def test_a_picture_ships_once_byte_for_byte_with_its_digest_and_size(tmp_path: Path) -> None:
    """The original goes as it is; an entry keeps the digests, sizes and imagePath."""
    now = int(time.time() * 1000)
    db = tmp_path / "timesink.sqlite"
    _fixture_db(db, now).close()
    shot = tmp_path / "captures" / "2026-10-19" / "shot.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(PNG)
    sink = Recorder()
    shipper = _shipper(tmp_path, sink, db, images=True)

    shipper.poll(now)
    shipper.poll(now + 6 * MIN)

    assert sink.files == {"timesink/2026-10-19/shot.png": PNG}
    image = next(e for e in sink.entries() if e["kind"] == "capture_image")
    digest = hashlib.sha256(PNG).hexdigest()
    assert image["fields"] == {
        "capture_id": 1, "imagePath": "2026-10-19/shot.png", "original_sha256": digest,
        "original_bytes": len(PNG), "original_width": 1, "original_height": 1,
        "shipped_sha256": digest, "shipped_bytes": len(PNG), "shipped_width": 1,
        "shipped_height": 1, "scale": 1.0, "quality": 1.0,
    }
    assert [e["kind"] for e in sink.entries()].count("capture_image") == 1


# --- sessions ------------------------------------------------------------------------------

SESSION = "0c3135fe-5d47-430f-933a-498ed40e5484"


def _log(*lines: dict[str, object]) -> bytes:
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _entry(kind: str, stamp: str, **more: object) -> dict[str, object]:
    return {"type": kind, "timestamp": stamp, "sessionId": SESSION, **more}


def _session_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)  # noqa: S603, S607
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "remote", "add", "origin",  # noqa: S607
         "https://leaky-user:leaky-pass@example.com/o/r.git"], check=True,
    )
    log = tmp_path / ".claude" / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    log.parent.mkdir(parents=True)
    common = {"cwd": str(repo), "gitBranch": "main", "entrypoint": "cli", "isSidechain": False}
    log.write_bytes(_log(
        _entry("user", "2026-10-19T05:00:00.000Z", message={"content": "BODY-user"}, **common),
        _entry("assistant", "2026-10-19T05:00:05.000Z", message={
            "id": "m1", "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": "BODY-assistant"},
                        {"type": "tool_use", "name": "Bash", "input": {"command": "BODY-in"}}],
        }, **common),
        _entry("user", "2026-10-19T05:00:06.000Z", message={"content": [
            {"type": "tool_result", "content": "BODY-tool-output"}]}, **common),
        _entry("system", "2026-10-19T05:00:07.000Z", subtype="turn_duration", durationMs=7000,
               **common),
        _entry("system", "2026-10-19T05:30:00.000Z", subtype="away_summary", uuid="u1",
               content="Recap: did the thing.", **common),
        {"type": "custom-title", "customTitle": "My title", "sessionId": SESSION},
        {"type": "ai-title", "aiTitle": "AI title", "sessionId": SESSION},
        {"type": "cost-state", "sessionId": SESSION, "totalCostUSD": 1.5, "totalDuration": 99,
         "totalLinesAdded": 3, "totalLinesRemoved": 1,
         "modelUsage": {"claude-sonnet-5-5": {"n": 1}}},
        _entry("pr-link", "2026-10-19T05:31:00.000Z", prNumber=7, prRepository="o/r",
               prUrl="https://github.com/o/r/pull/7"),
    ))
    desk = tmp_path / "desk" / "x"
    desk.mkdir(parents=True)
    (desk / "local_1.json").write_text(json.dumps(
        {"cliSessionId": SESSION, "title": "Desktop title", "titleSource": "user"},
    ))
    return tmp_path / ".claude" / "projects", log, tmp_path / "desk"


def test_a_session_record_holds_what_the_log_says_and_nothing_that_was_said(
    tmp_path: Path,
) -> None:
    """Counts, titles, models, cost, recap, PR and repo; no prompt, answer, tool input or output."""
    root, _log_file, desk = _session_fixture(tmp_path)
    sink = Recorder()
    config = ShipConfig(tmp_path / "state.db", session_roots=(("a", root),), desktop_titles=desk)
    shipper = Shipper(config, sink, State(config.state_path))
    now = int(time.time() * 1000)

    shipper.poll(now)

    (one,) = sink.entries()
    fields = one["fields"]
    assert isinstance(fields, dict)
    assert str(one["key"]).startswith(f"session:a:{SESSION}:")
    assert (one["kind"], one["text"], one["at_ms"]) == ("session", "My title", 1_792_386_000_000)
    assert (fields["user_prompts"], fields["assistant_messages"], fields["tool_uses"]) == (1, 1, 1)
    assert (fields["turn_count"], fields["turn_duration_ms"]) == (1, 7000)
    assert fields["models"] == ["claude-sonnet-5-5"]
    assert fields["git_branches"] == ["main"]
    assert fields["entrypoints"] == ["cli"]
    assert fields["cost_state"]["totalCostUSD"] == 1.5
    assert fields["custom_title"] == ["My title"]
    assert fields["ai_title"] == ["AI title"]
    assert (fields["desktop_title"], fields["desktop_title_source"]) == ("Desktop title", "user")
    assert fields["away_summaries"] == [
        {"timestamp": "2026-10-19T05:30:00.000Z", "uuid": "u1", "content": "Recap: did the thing."},
    ]
    assert fields["pr_links"][0]["prUrl"] == "https://github.com/o/r/pull/7"
    assert fields["git_toplevel"] == str((tmp_path / "work" / "repo").resolve())
    assert fields["git_origin"] == "https://example.com/o/r.git"
    assert (fields["first_timestamp"], fields["last_timestamp"]) == (
        "2026-10-19T05:00:00.000Z", "2026-10-19T05:31:00.000Z",
    )
    blob = sink.bodies[0][1].decode()
    assert "BODY-" not in blob
    assert "leaky" not in blob


def test_a_growing_session_ships_at_most_every_half_hour_and_when_it_goes_quiet(
    tmp_path: Path,
) -> None:
    """Read from the offset on, whole lines only; the shipping interval is 30 minutes."""
    root, log, desk = _session_fixture(tmp_path)
    sink = Recorder()
    config = ShipConfig(tmp_path / "state.db", session_roots=(("a", root),), desktop_titles=desk)
    shipper = Shipper(config, sink, State(config.state_path))
    now = int(time.time() * 1000)
    shipper.poll(now)
    assert len(sink.entries()) == 1

    def append(data: bytes, at_ms: int) -> None:
        with log.open("ab") as out:
            out.write(data)
        os.utime(log, ns=(at_ms * 1_000_000, at_ms * 1_000_000))  # the file's clock is the test's

    os.utime(log, ns=(now * 1_000_000, now * 1_000_000))
    extra = _log(_entry("user", "2026-10-19T06:00:00.000Z", message={"content": "BODY-two"}))
    append(extra[: len(extra) // 2], now + 5 * MIN)  # a line still being written is not read
    shipper.poll(now + 5 * MIN)
    append(extra[len(extra) // 2 :], now + 10 * MIN)
    shipper.poll(now + 10 * MIN)
    assert len(sink.entries()) == 1  # grown, but shipped less than 30 minutes ago

    shipper.poll(now + 31 * MIN)
    assert len(sink.entries()) == 2
    assert sink.entries()[1]["fields"]["user_prompts"] == 2  # type: ignore[index]
    shipper.poll(now + 40 * MIN)
    assert len(sink.entries()) == 2  # unchanged: nothing more

    append(_log(_entry("user", "2026-10-19T06:10:00.000Z", message={"content": "BODY-3"})),
           now + 44 * MIN)
    shipper.poll(now + 45 * MIN)
    assert len(sink.entries()) == 2  # fresh change, last shipped 14 minutes ago
    shipper.poll(now + 80 * MIN)  # the file has been quiet for more than 30 minutes
    assert len(sink.entries()) == 3
    assert "BODY-" not in "".join(body.decode() for _, body in sink.bodies)


# --- through HTTP ---------------------------------------------------------------------------


def _bridge(client: TestClient) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        answer = client.request(
            request.method, request.url.raw_path.decode(), content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
        )
        return httpx.Response(answer.status_code, content=answer.content)

    return httpx.MockTransport(handle)


@pytest.mark.skipif(shutil.which("git") is None, reason="git")
def test_through_http_a_refused_batch_is_sent_again_and_a_kept_one_never_twice(
    tmp_path: Path, brain: Brain,
) -> None:
    """The cursor moves only on the brain's 2xx; a full disk is retried; a kept batch is final."""
    now = int(time.time() * 1000)
    db = tmp_path / "timesink.sqlite"
    _fixture_db(db, now).close()
    root, _log_path, desk = _session_fixture(tmp_path)
    config = ShipConfig(
        tmp_path / "ship" / "state.db", timesink=TimesinkConfig(db, images=False),
        session_roots=(("a", root),), desktop_titles=desk,
    )
    shipper = Shipper(config, HttpSink("http://brain", brain.token, _bridge(brain.remote)),
                      State(config.state_path))

    brain.free[0] = 1024
    refused = shipper.poll(now)
    assert len(refused.failed) == 2
    assert brain.rows() == []

    brain.free[0] = 100 * 1024**3
    kept = shipper.poll(now + MIN)
    assert kept.failed == []
    held = brain.rows()
    assert sorted({source for source, _ in held}) == ["claude-code", "timesink"]
    assert len(held) == 5  # four tables' rows and one session

    shipper.poll(now + 2 * MIN)
    assert brain.rows() == held
    with closing(imports.open_shipped(brain.root / imports.IMPORTS_DIRNAME)) as conn:
        assert len(imports.newest(conn, "timesink")) == 4
        assert [e.kind for e in imports.newest(conn, "claude-code")] == ["session"]
    assert imports.SHIPPED_SOURCES == (
        "timesink", "claude-code", "device-apps", "device-records",
    )


def test_shipping_is_off_in_the_shipped_config_and_the_switch_reads_its_parts(
    tmp_path: Path,
) -> None:
    """``mac_ship.enabled`` is false as shipped; turned on, the parts and defaults are read."""
    import yaml  # noqa: PLC0415

    from jarvis.surface.mac_ship import ship_config  # noqa: PLC0415

    shipped = yaml.safe_load((Path(__file__).parents[2] / "config" / "jarvis.yaml").read_text())
    assert ship_config(shipped, tmp_path) is None

    shipped["mac_ship"]["enabled"] = True
    config = ship_config(shipped, tmp_path)
    assert config is not None
    assert config.state_path == tmp_path / "mac-ship" / "state.db"
    assert config.poll_interval_s == 300
    assert config.timesink is not None
    assert (config.timesink.image_scale, config.timesink.image_quality) == (1.0, 1.0)
    assert [account for account, _ in config.session_roots] == ["a", "b"]
    shipped["mac_ship"]["claude_code"]["enabled"] = False
    shipped["mac_ship"]["timesink"]["enabled"] = False
    off = ship_config(shipped, tmp_path)
    assert off is not None
    assert (off.timesink, off.session_roots) == (None, ())
