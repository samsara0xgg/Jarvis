"""ADR 0323: the Mac's daily Pi backup carries the nightly check-log reports, and nothing else.

``scripts/pi_brain_backup.sh`` is run for real, with ``ssh``, ``scp`` and ``sqlite3`` replaced by
shims that act on a fake Pi home on this machine (the script's Pi-side Python runs unchanged).
What is asserted is what the owner relies on: the databases and the dated reports arrive in the
day's folder, env and other logs stay behind, a Pi with no report yet is a note rather than a failed
backup, and the copied event log can be checked where it landed.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.cli import main as cli_main
from jarvis.state import event_log
from tests.integration.test_cli_check_log import _Clock, _make_root

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pi_brain_backup.sh"

_SSH = """#!/bin/sh
while [ "$1" = -o ]; do shift 2; done
shift
exec env HOME="$PI_HOME" sh -c "$*"
"""
_SCP = """#!/bin/sh
while [ "$1" = -q ] || [ "$1" = -o ]; do if [ "$1" = -o ]; then shift 2; else shift; fi; done
src="${1#*:}"
cd "$PI_HOME" && cp $src "$2"
"""
_SQLITE3 = """#!/bin/sh
exec python3 -c 'import sqlite3, sys
db = sqlite3.connect("file:" + sys.argv[2] + "?mode=ro", uri=True)
print(db.execute(sys.argv[3].rstrip(";")).fetchone()[0])' "$@"
"""
REPORT = {"ok": True, "violated": 0, "invariants": []}


@dataclass
class _Pi:
    mac_home: Path
    pi_home: Path
    bin: Path

    @property
    def pi_root(self) -> Path:
        return self.pi_home / ".jarvis"

    def backup(self) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.mac_home), "PI_HOME": str(self.pi_home), "JARVIS_PI_HOST": "pi",
        }
        return subprocess.run(  # noqa: S603 - the repo's own script, with shimmed ssh and scp.
            ["/bin/bash", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=120,
            env=env,
        )

    def day(self) -> Path:
        (folder,) = (self.mac_home / ".jarvis" / "backups" / "pi-brain").iterdir()
        return folder


@pytest.fixture
def pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Pi:
    """A Mac home, a Pi home with an event log, memory.db, env and a log, and the shims."""
    clock = _Clock()
    monkeypatch.setattr(event_log, "_now_epoch_ms", lambda: clock.now)
    shims = tmp_path / "bin"
    shims.mkdir()
    for name, body in (("ssh", _SSH), ("scp", _SCP), ("sqlite3", _SQLITE3)):
        (shims / name).write_text(body, encoding="utf-8")
        (shims / name).chmod(0o755)
    host = _Pi(tmp_path / "mac", tmp_path / "pi", shims)
    host.mac_home.mkdir()
    (host.pi_root / "logs").mkdir(parents=True)
    _make_root(host.pi_root, clock, spoiled=False)
    import sqlite3  # noqa: PLC0415 - a plain database beside the event log, as the Pi has.

    with sqlite3.connect(host.pi_root / "memory.db") as conn:
        conn.execute("CREATE TABLE turns (words TEXT)")
    (host.pi_root / "env").write_text("OPENAI_API_KEY=not-a-key\n", encoding="utf-8")
    (host.pi_root / "logs" / "llm-io.jsonl").write_text("{}\n", encoding="utf-8")
    return host


def test_the_reports_travel_with_the_databases_and_nothing_else_does(pi: _Pi) -> None:
    """Databases and reports arrive; env and other logs stay."""
    for day in ("2026-10-19", "2026-10-20"):
        report = pi.pi_root / "logs" / f"check-log-{day}.json"
        report.write_text(json.dumps(REPORT), encoding="utf-8")
    done = pi.backup()
    assert done.returncode == 0, done.stderr
    assert sorted(one.name for one in pi.day().iterdir()) == [
        "check-log-2026-10-19.json", "check-log-2026-10-20.json", "mac_events.db", "memory.db",
    ]
    copied = pi.day() / "check-log-2026-10-20.json"
    assert json.loads(copied.read_text(encoding="utf-8")) == REPORT
    assert "check-log reports ok (2)" in done.stdout
    assert not list(pi.pi_home.glob(".jarvis-backup-stage-*"))


def test_a_pi_without_a_report_yet_is_a_note_and_not_a_failed_backup(pi: _Pi) -> None:
    """Exit 0, the databases copied and a note on stderr."""
    done = pi.backup()
    assert done.returncode == 0, done.stderr
    assert sorted(one.name for one in pi.day().iterdir()) == ["mac_events.db", "memory.db"]
    assert "no check-log reports pulled" in done.stderr
    assert "backup done: 2 file(s)" in done.stdout


def test_the_copied_event_log_is_checked_where_it_landed(
    pi: _Pi, capsys: pytest.CaptureFixture[str],
) -> None:
    """The day's folder is a runtime root: check-log passes on the copy."""
    assert pi.backup().returncode == 0
    capsys.readouterr()
    code = cli_main(["check-log", "--runtime-root", str(pi.day()), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["ok"]) == (0, True)
    assert report["window"]["events"] == 3
