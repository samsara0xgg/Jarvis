"""ADR 0323: ``jarvis check-log`` through the real CLI entry, against a real WAL event log.

What the nightly timer relies on: the exit code says clean (0), violated (1) or unusable (2);
the check changes nothing on disk, even while a writer holds the log open or the daemon holds
its lock; and what it prints is ids, types, counts and times, never what Allen said.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.cli import main as cli_main
from jarvis.deployment.process_lock import acquire_exclusive
from jarvis.state import event_log
from jarvis.state.event_log import emit_event, open_event_log
from tests.integration.test_log_invariants import MIN, NOW, SECRET_SAMPLES

if TYPE_CHECKING:
    from pathlib import Path

MARKER = "PRIVATE-WORDS-OF-THE-OWNER"
SECRET = SECRET_SAMPLES["github_token"]
WEIRD_TYPE = f"weird type {MARKER}"


class _Clock:
    now = NOW - 30 * MIN


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """The event log's clock, a past constant: the check anchors at the log's head."""
    clock = _Clock()
    monkeypatch.setattr(event_log, "_now_epoch_ms", lambda: clock.now)
    return clock


def _say(conn: Any, clock: _Clock, words: str) -> None:  # noqa: ANN401 - a sqlite3 connection.
    clock.now += MIN
    emit_event(
        conn, type="surface.conversation_words",
        payload={"turn_id": "T", "reason": "r", "transcript": words},
    )


def _make_root(root: Path, clock: _Clock, *, spoiled: bool) -> None:
    """A closed WAL log (its WAL checkpointed away on close), clean or with three faults."""
    (root / "memory" / "audio").mkdir(parents=True)
    conn = open_event_log(root / "mac_events.db")
    try:
        for _ in range(3):
            _say(conn, clock, "nothing private")
        if spoiled:
            _say(conn, clock, f"{MARKER} {SECRET}")
            conn.execute(
                "INSERT INTO events (event_uid, type, schema_version, ts_epoch_ms, payload_json, "
                "actor, ingestion_node) VALUES ('w', ?, 1, ?, ?, 'x', 'mac')",
                (WEIRD_TYPE, clock.now, json.dumps({"transcript": MARKER})),
            )
            conn.commit()
    finally:
        conn.close()


def _run(*args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = cli_main(["check-log", *args])
    out, err = capsys.readouterr()
    return code, out, err


def _snapshot(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under the root: size, mtime in ns and bytes."""
    return {
        str(path.relative_to(root)): (
            path.stat().st_size, path.stat().st_mtime_ns, path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_clean_log_exits_zero(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 0 and a clean result line for a log that holds."""
    _make_root(tmp_path, clock, spoiled=False)
    code, out, err = _run("--runtime-root", str(tmp_path), capsys=capsys)
    assert (code, err) == (0, "")
    assert "result: clean" in out
    assert "OK        event.type_registered" in out


def test_a_violated_log_exits_one_and_names_the_invariants_and_their_events(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 1, each violated invariant with its count and a shape name."""
    _make_root(tmp_path, clock, spoiled=True)
    code, out, err = _run("--runtime-root", str(tmp_path), capsys=capsys)
    assert (code, err) == (1, "")
    assert "VIOLATED  event.type_registered: 1 violation(s)" in out
    assert "VIOLATED  payload.no_secret_shapes: 1 violation(s)" in out
    assert "(github_token)" in out
    assert "result: 2 of " in out


def test_the_json_report_has_the_documented_shape(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """The keys the nightly report and any reader of it rely on."""
    _make_root(tmp_path, clock, spoiled=True)
    args = ("--runtime-root", str(tmp_path), "--json", "--examples", "1")
    code, out, _err = _run(*args, capsys=capsys)
    report = json.loads(out)
    assert code == 1
    assert report["ok"] is False
    assert report["violated"] == 2
    assert report["event_log"] == str(tmp_path.resolve() / "mac_events.db")
    assert report["window"]["events"] == 5
    assert report["window"]["until"].endswith("Z")
    by_name = {one["name"]: one for one in report["invariants"]}
    secret = by_name["payload.no_secret_shapes"]
    assert (secret["status"], secret["violations"]) == ("violated", 1)
    assert set(secret["examples"][0]) == {"event_id", "ts", "note"}
    assert secret["rule"]
    assert secret["ref"]
    assert by_name["event.clock_not_backwards"]["status"] == "ok"


def test_a_root_without_a_log_exits_two_with_a_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Exit 2, a reason on stderr, a JSON error object, and no directory created."""
    code, out, err = _run("--runtime-root", str(tmp_path / "nowhere"), capsys=capsys)
    assert code == 2
    assert out == ""
    assert "no event log" in err
    assert not (tmp_path / "nowhere").exists()

    code, out, _err = _run("--runtime-root", str(tmp_path / "nowhere"), "--json", capsys=capsys)
    assert code == 2
    assert json.loads(out)["ok"] is False


@pytest.mark.parametrize("days", ["0", "-1", "nan", "abc", "99999"])
def test_a_window_that_is_no_number_of_days_is_a_usage_error(
    tmp_path: Path, days: str, capsys: pytest.CaptureFixture[str],
) -> None:
    """Argparse refuses with exit 2 before any log is opened."""
    with pytest.raises(SystemExit) as stop:
        cli_main(["check-log", "--runtime-root", str(tmp_path), "--since-days", days])
    assert stop.value.code == 2
    assert "since-days" in capsys.readouterr().err


def test_it_changes_nothing_on_a_log_nobody_has_open(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """Names, sizes, mtimes and bytes of every file are the same afterwards."""
    _make_root(tmp_path, clock, spoiled=True)
    before = _snapshot(tmp_path)
    assert not any(name.endswith(("-wal", "-shm")) for name in before)
    _run("--runtime-root", str(tmp_path), capsys=capsys)
    _run("--runtime-root", str(tmp_path), "--json", capsys=capsys)
    assert _snapshot(tmp_path) == before


def test_it_sees_a_live_writers_rows_and_changes_neither_the_log_nor_its_wal(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """Rows still in the WAL are read; the log and the WAL are untouched."""
    (tmp_path / "memory" / "audio").mkdir(parents=True)
    writer = open_event_log(tmp_path / "mac_events.db")
    try:
        for _ in range(4):
            _say(writer, clock, "still being written")
        assert (tmp_path / "mac_events.db-wal").stat().st_size > 0  # the rows live in the WAL
        log = tmp_path / "mac_events.db"
        wal = tmp_path / "mac_events.db-wal"
        before = (
            log.read_bytes(), log.stat().st_mtime_ns, wal.read_bytes(), wal.stat().st_mtime_ns,
        )
        code, out, _err = _run("--runtime-root", str(tmp_path), "--json", capsys=capsys)
        assert code == 0
        assert json.loads(out)["window"]["events"] == 4  # read through the WAL
        after = (log.read_bytes(), log.stat().st_mtime_ns, wal.read_bytes(), wal.stat().st_mtime_ns)
        assert after == before
        # The writer was not disturbed: it still appends.
        _say(writer, clock, "after the check")
    finally:
        writer.close()


def test_it_runs_while_the_daemon_holds_its_lock_and_leaves_the_lock_alone(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str],
) -> None:
    """No lock is taken or refused, and the lock file is not touched."""
    _make_root(tmp_path, clock, spoiled=False)
    lock = tmp_path / "daemon.lock"
    with acquire_exclusive(lock):
        before = (lock.read_bytes(), lock.stat().st_mtime_ns)
        code, out, err = _run("--runtime-root", str(tmp_path), capsys=capsys)
        assert (code, err) == (0, "")
        assert "result: clean" in out
        assert (lock.read_bytes(), lock.stat().st_mtime_ns) == before
    assert str(os.getpid()) == before[0].decode().strip()


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_nothing_the_owner_said_reaches_the_output(
    tmp_path: Path, clock: _Clock, capsys: pytest.CaptureFixture[str], *, as_json: bool,
) -> None:
    """Text and JSON, stdout and stderr: no words, no secret, no odd type name."""
    _make_root(tmp_path, clock, spoiled=True)
    args = ["--runtime-root", str(tmp_path), "--examples", "20"]
    code, out, err = _run(*args, *(["--json"] if as_json else []), capsys=capsys)
    assert code == 1
    assert "VIOLATED" in out or '"violated"' in out
    for text in (out, err):
        assert MARKER not in text
        assert SECRET not in text
        assert "weird type" not in text


def test_the_command_is_wired_through_python_dash_m_jarvis(
    tmp_path: Path, clock: _Clock,
) -> None:
    """What the nightly unit runs: ``python -m jarvis check-log --json`` and its exit code."""
    _make_root(tmp_path, clock, spoiled=True)
    env = {
        key: value for key, value in os.environ.items()
        if key not in {"JARVIS_RUNTIME_ROOT", "JARVIS_LOG_LEVEL"}
    } | {"JARVIS_RUNTIME_ROOT": str(tmp_path), "HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [sys.executable, "-m", "jarvis", "check-log", "--json"],
        capture_output=True, text=True, timeout=60, check=False, env=env, cwd=tmp_path,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    assert report["violated"] == 2
    assert MARKER not in result.stdout + result.stderr
