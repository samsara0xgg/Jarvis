"""ADR-0003 D4 + F2 — CLI refuses to run while the daemon owns the lock.

The one-shot ``python -m jarvis "<text>"`` path must probe
``${runtime_root}/daemon.lock`` BEFORE bootstrap/fork; if a live daemon
holds the lock, exit 2 with stderr that mentions the holder pid AND a
pointer to the HTTP submit endpoint so the operator knows their two
remediation options (kill the daemon, or POST through it).

This test pre-acquires the lock from THIS test process, then spawns the
CLI as a subprocess and asserts the refusal path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

from jarvis.deployment.process_lock import acquire_exclusive

if TYPE_CHECKING:
    from pathlib import Path


def test_cli_refuses_to_run_when_lock_held(tmp_path: Path) -> None:
    """Jarvis "..." returns exit 2 + stderr mentioning the holder PID."""
    lock_path = tmp_path / "daemon.lock"

    with acquire_exclusive(lock_path):
        # While inside the context manager, the test process holds the lock
        # (containing this process's PID written by _write_pid in
        # process_lock.acquire_exclusive).
        result = subprocess.run(  # noqa: S603 — sys.executable is trusted; argv is fully controlled.
            [
                sys.executable,
                "-m",
                "jarvis",
                "test text",
                "--runtime-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}; stderr={result.stderr!r}; "
        f"stdout={result.stdout!r}"
    )
    assert "daemon running" in result.stderr.lower(), result.stderr
    assert str(os.getpid()) in result.stderr, result.stderr
