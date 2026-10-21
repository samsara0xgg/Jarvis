"""ADR 0324: the nightly unit, run the way systemd would run it.

systemd is not available to a test, so this reads ``jarvis-check-log.service``, expands
its ``ExecStartPre``/``ExecStart`` lines with systemd's own rules (word splitting first, then
``%h``, ``%%``, ``$$`` and ``${VAR}``) and runs the result with the unit's environment in a fake
home whose ``jarvis-brain/.venv/bin/python`` is this interpreter. What is asserted is what the
owner relies on: a dated report appears under ``logs/`` for a clean, a violated and an unopenable
log, the unit's exit code carries the check's, nothing half-written is left behind, and old
reports go.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from jarvis.state import event_log
from tests.integration.test_cli_check_log import MARKER, _Clock, _make_root

UNITS = Path(__file__).resolve().parents[2] / "jarvis" / "deployment" / "systemd"
SERVICE = UNITS / "jarvis-check-log.service"
TIMER = UNITS / "jarvis-check-log.timer"
DAY_S = 86400


def _lines(unit: Path, key: str) -> list[str]:
    return [
        line.split("=", 1)[1].strip()
        for line in unit.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{key}=")
    ]


def _expand(word: str, home: Path, env: dict[str, str]) -> str:
    """One word after systemd's substitutions; an escape it does not know is an error."""
    def specifier(match: re.Match[str]) -> str:
        if match.group(1) not in {"h", "%"}:
            message = f"specifier %{match.group(1)} is not emulated"
            raise AssertionError(message)
        return str(home) if match.group(1) == "h" else "%"

    def variable(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        return env[match.group(1) or match.group(2)]

    word = re.sub(r"%(.)", specifier, word)
    return re.sub(r"\$\$|\$\{(\w+)\}|\$(\w+)", variable, word)


def _argv(line: str, home: Path, env: dict[str, str]) -> list[str]:
    return [_expand(word, home, env) for word in shlex.split(line.lstrip("-"))]


def _environment(home: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for assignment in _lines(SERVICE, "Environment"):
        name, value = assignment.split("=", 1)
        env[name] = _expand(value, home, env)
    return env


def _run(line: str, home: Path) -> subprocess.CompletedProcess[str]:
    env = _environment(home)
    return subprocess.run(  # noqa: S603 - the argv is the unit file's, expanded.
        _argv(line, home, env), capture_output=True, text=True, check=False, timeout=120,
        cwd=_expand(_lines(SERVICE, "WorkingDirectory")[0], home, env),
        env={"PATH": os.environ["PATH"], **env}, umask=0o077,
    )


@dataclass
class _Host:
    home: Path
    clock: _Clock

    @property
    def root(self) -> Path:
        return self.home / ".jarvis"

    def reports(self) -> list[Path]:
        return sorted((self.root / "logs").glob("check-log-*"))

    def run(self, line: str) -> subprocess.CompletedProcess[str]:
        return _run(line, self.home)


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Host:
    """A home as the unit expects it: the repo's virtualenv beside ``.jarvis``."""
    clock = _Clock()
    monkeypatch.setattr(event_log, "_now_epoch_ms", lambda: clock.now)
    shim = tmp_path / "jarvis-brain" / ".venv" / "bin" / "python"
    shim.parent.mkdir(parents=True)
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    (tmp_path / ".jarvis").mkdir()
    return _Host(tmp_path, clock)


def test_a_clean_log_leaves_a_dated_report_and_the_unit_succeeds(host: _Host) -> None:
    """Exit 0, one check-log-<date>.json, mode 0600, ok true."""
    _make_root(host.root, host.clock, spoiled=False)
    done = host.run(_lines(SERVICE, "ExecStart")[0])
    assert (done.returncode, done.stderr) == (0, "")
    (report,) = host.reports()
    assert re.fullmatch(r"check-log-\d{4}-\d{2}-\d{2}\.json", report.name)
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True
    assert oct(report.stat().st_mode & 0o777) == "0o600"


def test_a_violated_log_fails_the_unit_but_still_leaves_the_report(host: _Host) -> None:
    """Exit 1 (the unit fails) and the report that says why."""
    _make_root(host.root, host.clock, spoiled=True)
    done = host.run(_lines(SERVICE, "ExecStart")[0])
    assert done.returncode == 1
    (report,) = host.reports()
    text = report.read_text(encoding="utf-8")
    assert json.loads(text)["violated"] == 2
    assert MARKER not in text


def test_a_root_with_no_log_fails_the_unit_with_a_report_saying_why(host: _Host) -> None:
    """Exit 2 and a report holding the error."""
    done = host.run(_lines(SERVICE, "ExecStart")[0])
    assert done.returncode == 2
    (report,) = host.reports()
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is False


def test_a_report_is_renamed_into_place_so_the_backup_never_sees_half_of_one(host: _Host) -> None:
    """Only .json files are left; the .tmp is renamed away."""
    line = _lines(SERVICE, "ExecStart")[0]
    assert '> "$$out.tmp"' in line
    assert 'mv "$$out.tmp" "$$out"' in line
    _make_root(host.root, host.clock, spoiled=False)
    host.run(line)
    assert [one.suffix for one in host.reports()] == [".json"]


def test_reports_older_than_sixty_days_go_before_each_run_and_nothing_else_does(
    host: _Host,
) -> None:
    """ExecStartPre deletes by name and age, and a missing logs directory is harmless."""
    logs = host.root / "logs"
    logs.mkdir()
    old, recent = logs / "check-log-old.json", logs / "check-log-recent.json"
    other = logs / "llm-io.jsonl"
    for path, days in ((old, 61), (recent, 59), (other, 400)):
        path.write_text("{}", encoding="utf-8")
        stamp = path.stat().st_mtime - days * DAY_S
        os.utime(path, (stamp, stamp))
    (before,) = _lines(SERVICE, "ExecStartPre")
    assert before.startswith("-")
    assert host.run(before).returncode == 0
    assert sorted(one.name for one in logs.iterdir()) == ["check-log-recent.json", "llm-io.jsonl"]
    # No logs directory yet (the first night): the `-` prefix makes the failure harmless to systemd.
    other.unlink()
    recent.unlink()
    logs.rmdir()
    assert host.run(before).returncode != 0


def test_the_unit_and_the_timer_say_what_the_owner_expects() -> None:
    """Oneshot, 0077, the brain unit's root and directory, a persistent 06:30 timer."""
    service = SERVICE.read_text(encoding="utf-8")
    timer = TIMER.read_text(encoding="utf-8")
    assert _lines(SERVICE, "Type") == ["oneshot"]
    assert _lines(SERVICE, "UMask") == ["0077"]
    brain = (UNITS / "jarvis-brain.service").read_text(encoding="utf-8")
    assert "Environment=JARVIS_RUNTIME_ROOT=%h/.jarvis" in brain
    assert "Environment=JARVIS_RUNTIME_ROOT=%h/.jarvis" in service
    assert "WorkingDirectory=%h/jarvis-brain" in service
    assert "ExecStart=/bin/sh -c" in service
    assert " check-log --json" in service
    assert _lines(TIMER, "OnCalendar") == ["*-*-* 06:30:00"]
    assert _lines(TIMER, "Persistent") == ["true"]
    assert _lines(TIMER, "WantedBy") == ["timers.target"]
    assert "[Install]" not in service
    assert "WantedBy" not in service
    assert timer.count("[Timer]") == 1
