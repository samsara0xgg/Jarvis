"""Unit tests for :func:`jarvis.runtime.daemon.fork_detach` (Step 15 of ADR-0002).

PLATFORM SKIP: tests are skipped on Windows (no :func:`os.fork`).
Run on macOS and Linux only. The actual double-fork is exercised in a
subprocess so the test runner itself is never reparented mid-suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# Project root: this file lives at <root>/tests/unit/test_daemon.py — walk up
# two parents to reach the directory that contains the ``jarvis/`` package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

skip_windows = pytest.mark.skipif(
    sys.platform == "win32",
    reason="fork_detach uses POSIX os.fork (not on Windows)",
)


def _drive_script(body: str) -> str:
    """Render a driver script that imports ``fork_detach`` and runs ``body``.

    The script inserts the project root onto ``sys.path`` so the
    subprocess can ``import jarvis.runtime.daemon`` without an install
    step. ``body`` is dedented and prepended with a fixed preamble; the
    composed source is column-zero so the subprocess receives a clean
    Python module.
    """
    preamble = (
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "from jarvis.runtime.daemon import fork_detach\n"
    )
    return preamble + textwrap.dedent(body).strip("\n") + "\n"


def _wait_for_marker(path: Path, *, timeout_s: float = 2.0) -> None:
    """Poll until ``path`` exists or the timeout elapses; assert presence."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and not path.exists():
        time.sleep(0.05)
    assert path.exists(), f"marker file {path!s} never appeared within {timeout_s}s"


@skip_windows
def test_fork_detach_parent_returns_and_child_runs(tmp_path: Path) -> None:
    """Parent path returns 'parent' (caller exits 0); child runs reparented."""
    parent_marker = tmp_path / "parent.txt"
    child_marker = tmp_path / "child.txt"
    script_path = tmp_path / "drive.py"
    script_body = f"""
        result = fork_detach()
        if result == "parent":
            with open({str(parent_marker)!r}, "w") as fh:
                fh.write("parent")
            os._exit(0)
        # Child: write marker, then sleep briefly so the test can observe
        # the file before the grandchild exits.
        with open({str(child_marker)!r}, "w") as fh:
            fh.write("child")
        time.sleep(0.5)
    """
    script_path.write_text(_drive_script(script_body))

    result = subprocess.run(  # noqa: S603 — sys.executable + test-authored script path, no shell.
        [sys.executable, str(script_path)],
        check=False,
        timeout=5,
        capture_output=True,
    )
    assert result.returncode == 0, (
        f"parent did not exit cleanly: rc={result.returncode!r}, "
        f"stderr={result.stderr.decode(errors='replace')!r}"
    )
    assert parent_marker.exists()
    assert parent_marker.read_text() == "parent"

    _wait_for_marker(child_marker)
    assert child_marker.read_text() == "child"


@skip_windows
def test_fork_detach_redirects_streams_to_devnull(tmp_path: Path) -> None:
    """Child stdout writes go to /dev/null; the parent stdout stays empty."""
    child_marker = tmp_path / "child_ok.txt"
    script_path = tmp_path / "drive.py"
    script_body = f"""
        if fork_detach() == "parent":
            os._exit(0)
        # Child: writing to FD 1 should silently land in /dev/null.
        os.write(sys.stdout.fileno(), b"this goes to devnull\\n")
        with open({str(child_marker)!r}, "w") as fh:
            fh.write("ok")
    """
    script_path.write_text(_drive_script(script_body))

    result = subprocess.run(  # noqa: S603 — sys.executable + test-authored script path, no shell.
        [sys.executable, str(script_path)],
        check=False,
        timeout=5,
        capture_output=True,
    )
    assert result.returncode == 0
    assert b"this goes to devnull" not in result.stdout
    _wait_for_marker(child_marker)
    assert child_marker.read_text() == "ok"


@skip_windows
def test_fork_detach_child_cwd_is_root(tmp_path: Path) -> None:
    """After fork_detach, the child's cwd is ``/``."""
    cwd_marker = tmp_path / "cwd.txt"
    script_path = tmp_path / "drive.py"
    script_body = f"""
        if fork_detach() == "parent":
            os._exit(0)
        with open({str(cwd_marker)!r}, "w") as fh:
            fh.write(os.getcwd())
    """
    script_path.write_text(_drive_script(script_body))

    subprocess.run(  # noqa: S603 — sys.executable + test-authored script path, no shell.
        [sys.executable, str(script_path)],
        check=False,
        timeout=5,
        capture_output=True,
    )
    _wait_for_marker(cwd_marker)
    assert cwd_marker.read_text() == "/"
