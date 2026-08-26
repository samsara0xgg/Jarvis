"""ADR-0003 D4 + F2 — CLI refuses to run while the daemon owns the lock.

The one-shot ``python -m jarvis "<text>"`` path must probe
``${runtime_root}/daemon.lock`` BEFORE bootstrap/fork; if a live daemon
holds the lock, exit 2 with stderr that mentions the holder pid AND a
pointer to the HTTP submit endpoint so the operator knows their two
remediation options (kill the daemon, or POST through it).

ADR-0009 D2 moved that refusal behind ``--no-forward``: a held lock now
makes the bare command a thin client of the daemon instead. The flag is
the documented way for scripts that depend on the exit-2 refusal to keep
it, so this test asserts it through the flag.

This test pre-acquires the lock from THIS test process, then spawns the
CLI as a subprocess and asserts the refusal path.

``HOME`` is redirected at the tmp dir because the B-NEW-5 cross-root
guard runs BEFORE the refusal and probes the *default* runtime root's
lock (``~/.jarvis/daemon.lock``, resolved through ``expanduser``). Left
pointing at the real home, this test would refuse for B-NEW-5's reason —
with a different message and Allen's daemon pid — whenever a daemon
happens to be running on the machine.
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
                "--no-forward",
                "--runtime-root",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "HOME": str(tmp_path)},
        )

    assert result.returncode == 2, (
        f"expected exit 2, got {result.returncode}; stderr={result.stderr!r}; "
        f"stdout={result.stdout!r}"
    )
    assert "daemon running" in result.stderr.lower(), result.stderr
    assert str(os.getpid()) in result.stderr, result.stderr
