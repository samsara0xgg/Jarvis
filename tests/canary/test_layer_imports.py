"""H5 — ``lint-imports`` exit code surfaced as a pytest failure.

Per ADR 0001 § Acceptance criterion H5:

> Redundant with ``lint-imports`` but uses pytest to surface a clear
> failure inside the test suite.

Run ``./.venv/bin/lint-imports`` as a subprocess; assert exit 0. On
failure, print stderr + stdout so the autonomous loop sees the actual
broken contract right next to the assertion.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.canary._helpers import repo_root

_LINT_IMPORTS_REL_PATH = Path(".venv") / "bin" / "lint-imports"


def test_lint_imports_passes() -> None:
    """``lint-imports`` exits 0 — no cross-sibling or upward imports."""
    root = repo_root()
    binary = root / _LINT_IMPORTS_REL_PATH
    if not binary.exists():
        pytest.skip(f"lint-imports not installed at {binary}; install dev deps")

    proc = subprocess.run(  # noqa: S603 — binary path is computed from repo layout, no shell.
        [str(binary)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if proc.returncode != 0:
        sys.stderr.write("=== lint-imports stdout ===\n")
        sys.stderr.write(proc.stdout)
        sys.stderr.write("\n=== lint-imports stderr ===\n")
        sys.stderr.write(proc.stderr)

    assert proc.returncode == 0, (
        f"H5 — lint-imports returned {proc.returncode}; layer DAG broken (see stderr above)"
    )
