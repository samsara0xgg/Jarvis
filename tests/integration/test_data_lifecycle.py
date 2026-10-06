"""ADR 0067/0068 — how long data stays, taking it out, erasing it, and data from a newer Jarvis.

Each check asserts what is left on disk under a temp runtime root, what the
``/inherent/data/*`` routes answer and hand over, or what opening a database
does to its ``user_version``.
"""

from __future__ import annotations

import io
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from jarvis.deployment import bootstrap_runtime, data
from jarvis.state import NewerDataError
from jarvis.state.event_log import open_event_log
from jarvis.state.memory_db import append_record, open_memory_db
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

_DAY = 86400


def _aged(path: Path, days: float) -> Path:
    path.write_bytes(b"x")
    stamp = time.time() - days * _DAY
    os.utime(path, (stamp, stamp))
    return path


def test_old_recordings_and_screenshots_go_and_forever_keeps_all(tmp_path: Path) -> None:
    """30 days for recordings, 7 for screenshots: only what is past its days is deleted."""
    audio, screens, kept_forever = tmp_path / "audio", tmp_path / "screens", tmp_path / "keep"
    for folder in (audio, screens, kept_forever):
        folder.mkdir()
    old_turn, new_turn = _aged(audio / "T1.m4a", 31), _aged(audio / "T2.m4a", 29)
    old_shot, new_shot = _aged(screens / "1_A.png", 8), _aged(screens / "2_A.png", 6)
    ancient = _aged(kept_forever / "T0.m4a", 400)

    gone = [data.delete_older_than(folder, days)
            for folder, days in ((audio, 30), (screens, 7), (kept_forever, None))]

    assert gone == [1, 1, 0]
    assert [one.exists() for one in (old_turn, new_turn, old_shot, new_shot)] == [
        False, True, False, True,
    ]
    assert ancient.exists()


def test_a_long_log_starts_over_while_its_writer_keeps_appending(tmp_path: Path) -> None:
    """Past the cap the log is cut; the writer's next line lands at the start; three files stay."""
    log = tmp_path / "daemon.err.log"
    writer = (
        "import sys\n"
        "for line in sys.stdin: sys.stderr.write(line); sys.stderr.flush()\n"
    )
    with log.open("a") as err:
        # The way launchd and the Electron app hand the daemon its stderr: opened for append.
        child = subprocess.Popen(  # noqa: S603 — this interpreter, fixed script.
            [sys.executable, "-c", writer], stdin=subprocess.PIPE, stderr=err, text=True,
        )
    assert child.stdin is not None

    def say(line: str) -> None:
        assert child.stdin is not None
        child.stdin.write(line + "\n")
        child.stdin.flush()
        for _ in range(100):
            if log.read_text().endswith(line + "\n"):
                return
            time.sleep(0.01)

    try:
        for round_ in range(4):
            say(f"round {round_} " + "x" * 200)
            assert data.rotate_logs(tmp_path, max_bytes=100) == 1
            assert log.stat().st_size == 0
        say("after the cut")
    finally:
        child.stdin.close()
        child.wait(timeout=5)

    assert log.read_text() == "after the cut\n"  # offset 0: no hole of zeros before it
    assert sorted(one.name for one in tmp_path.iterdir()) == [
        "daemon.err.log", "daemon.err.log.1", "daemon.err.log.2",
    ]
    assert (tmp_path / "daemon.err.log.1").read_text().startswith("round 3 ")
    assert (tmp_path / "daemon.err.log.2").read_text().startswith("round 2 ")


def _root_with_data(tmp_path: Path) -> tuple[Path, list[Path]]:
    """A runtime root holding a conversation, an event log, settings, media, keys and models."""
    root = bootstrap_runtime(tmp_path / "jarvis")
    append_record(
        root.root / "memory.db", record_id="R1", source="allen", text="明天多伦多的天气怎么样",
    )
    open_event_log(root.event_log).close()
    (root.root / "settings.yaml").write_text("assistant_name: Jarvis\n")
    audio = root.root / "memory" / "audio"
    screens = root.artifacts_root / "screen_artifacts"
    for folder in (audio, screens, root.root / "models", root.root / "logs", root.root / "mcp"):
        folder.mkdir(parents=True)
    (audio / "T1.m4a").write_bytes(b"aac")
    (screens / "1_A.png").write_bytes(b"png")
    (root.root / "env").write_text("OPENAI_API_KEY=sk-secret\n")
    (root.root / "mcp" / "gmail.json").write_text("{}")
    (root.root / "models" / "silero_vad.onnx").write_bytes(b"model")
    (root.root / "logs" / "daemon.err.log").write_text("boot\n")
    return root.root, [audio, screens]


def _client(root: Path, media: list[Path], erased: list[bool]) -> TestClient:
    """The data routes wired the way the daemon wires them."""
    async def export() -> Path:
        return data.export_zip(root, [
            root / "memory.db", root / "mac_events.db", root / "settings.yaml",
            root / "settings.json", *media,
        ])

    async def clear() -> int:
        return data.clear_files(media)

    def erase() -> None:
        data.request_erase(root)
        erased.append(True)

    return TestClient(create_app(InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        data_export=export, data_clear=clear, data_erase=erase,
    )))


def test_the_export_is_the_users_data_and_no_keys(tmp_path: Path) -> None:
    """One zip: the conversation readable from its memory.db, the recordings; no keys, no temp."""
    root, media = _root_with_data(tmp_path)
    reply = _client(root, media, []).get("/inherent/data/export")

    assert reply.status_code == 200
    assert reply.headers["content-type"] == "application/zip"
    assert 'filename="Jarvis-export-' in reply.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(reply.content))
    assert sorted(archive.namelist()) == [
        "artifacts/screen_artifacts/1_A.png", "mac_events.db", "memory.db",
        "memory/audio/T1.m4a", "settings.yaml",
    ]
    exported = tmp_path / "memory.db"
    exported.write_bytes(archive.read("memory.db"))
    with closing(sqlite3.connect(exported)) as conn:
        assert conn.execute("SELECT text FROM records").fetchall() == [("明天多伦多的天气怎么样",)]
    assert not list(root.glob("export-*")), "the temp zip is deleted once sent"


def test_clearing_recordings_keeps_the_conversation(tmp_path: Path) -> None:
    """Every recording and screenshot goes at once; memory.db is untouched."""
    root, media = _root_with_data(tmp_path)
    reply = _client(root, media, []).post("/inherent/data/clear-recordings")

    assert reply.json() == {"deleted": 2}
    assert [list(folder.iterdir()) for folder in media] == [[], []]
    with closing(open_memory_db(root / "memory.db")) as conn:
        assert conn.execute("SELECT count(*) FROM records").fetchone() == (1,)


def test_erase_empties_the_root_at_the_next_boot(tmp_path: Path) -> None:
    """The route only marks; the next bootstrap leaves models, logs and an empty artifacts/."""
    root, media = _root_with_data(tmp_path)
    erased: list[bool] = []
    reply = _client(root, media, erased).post("/inherent/data/erase")

    assert reply.status_code == 202
    assert erased == [True]
    assert (root / "memory.db").exists(), "nothing is deleted while this daemon runs"

    bootstrap_runtime(root)

    assert sorted(one.name for one in root.iterdir()) == ["artifacts", "logs", "models"]
    assert list((root / "artifacts").iterdir()) == []
    assert (root / "models" / "silero_vad.onnx").read_bytes() == b"model"
    bootstrap_runtime(root)  # no marker left: a second boot deletes nothing
    assert (root / "models").exists()


def test_erase_forgets_the_daemons_key_and_the_agents_windows_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0094: Startrail's Claude key has a Keychain item of its own; erasing forgets both."""
    root, _media = _root_with_data(tmp_path)
    calls, security = tmp_path / "security-calls", tmp_path / "security"
    security.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    security.chmod(0o755)
    monkeypatch.setattr("jarvis.deployment._SECURITY", str(security))
    monkeypatch.setattr("jarvis.deployment._uses_keychain", lambda: True)  # the macOS branch
    data.request_erase(root)

    bootstrap_runtime(root)

    resolved = root.resolve()
    assert calls.read_text().splitlines() == [
        f"delete-generic-password -s Jarvis -a {resolved}",
        f"delete-generic-password -s Jarvis -a {resolved / 'agents'}",
    ]


def _user_version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def test_data_from_a_newer_jarvis_is_refused_and_left_as_it_was(tmp_path: Path) -> None:
    """A higher user_version is refused, not stamped down; an unstamped memory.db becomes v1."""
    events, memory = tmp_path / "mac_events.db", tmp_path / "memory.db"
    open_event_log(events).close()
    append_record(memory, record_id="R1", source="allen", text="hi")
    assert (_user_version(events), _user_version(memory)) == (2, 1)

    with closing(sqlite3.connect(memory)) as conn:
        conn.execute("PRAGMA user_version = 0")  # a memory.db from before the stamp
    open_memory_db(memory).close()
    assert _user_version(memory) == 1

    for path, newer in ((events, 3), (memory, 2)):
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(f"PRAGMA user_version = {newer}")
        opener = open_event_log if path == events else open_memory_db
        with pytest.raises(NewerDataError, match="Update Jarvis"):
            opener(path)
        assert _user_version(path) == newer
