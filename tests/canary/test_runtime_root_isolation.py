"""Canary — runtime-root isolates SQLite writes to the requested root.

B-NEW-5 regression guard. The CLI's runtime-root override (via
``--runtime-root <path>`` argv OR ``JARVIS_RUNTIME_ROOT=<path>`` env)
MUST route all SQLite writes (the Event Log) to ``<path>/mac_events.db``
and leave the default home root's database untouched. Without this
invariant the operator's "test against /tmp" smoke would silently
double-write into ``~/.jarvis/mac_events.db``.

Test invariants:

- The override-target runtime root is created and gains a
  ``mac_events.db`` with at least one event after a single one-shot run.
- The default home runtime root's Event Log event count is unchanged
  before/after the one-shot run (or the home file does not exist, which
  also satisfies the isolation invariant).

The test invokes the real ``python -m jarvis`` subprocess with
``--no-detach`` so we stay in the synchronous Day-1 path (no fork; the
test process can observe the child's exit). The utterance
``"hello"`` does NOT match the long-run classifier so ``--no-detach``
is belt-and-suspenders. The runtime root is routed via
``JARVIS_RUNTIME_ROOT`` env so the subprocess argv stays literal (ruff
S603 / hardening).

LLM dependency: the synchronous turn hits the configured LLM. The
canary SKIPS when ``OPENROUTER_PROXY_KEY`` is missing (the CI default).
Local invocation with a real key exercises the full path.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.deployment import DEFAULT_RUNTIME_ROOT_LITERAL

# Generous wall-clock bound. The synchronous "hello" turn hits the LLM,
# which can take several seconds; we want a bound that lets the test
# complete on a slow runner but bails out before the suite times out.
_SUBPROCESS_TIMEOUT_S: float = 60.0


def _event_count(db_path: Path) -> int:
    """Return the row count of the ``events`` table in ``db_path``, or 0 if absent."""
    if not db_path.exists():
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute("SELECT COUNT(*) FROM events")
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except sqlite3.OperationalError:
        # Table not yet created — treat as zero.
        return 0
    finally:
        conn.close()


def test_runtime_root_isolates_event_log_writes(tmp_path: Path) -> None:
    """``--runtime-root <tmp>`` writes only under ``<tmp>``, not under home.

    B-NEW-5 positive-path regression guard. Verifies the existing
    ``RuntimePaths`` isolation invariant — required for the CLI's
    fail-fast (added in the same commit) to be a useful refusal rather
    than a false positive.
    """
    if not os.environ.get("OPENROUTER_PROXY_KEY"):
        pytest.skip(
            "B-NEW-5 canary requires OPENROUTER_PROXY_KEY for the "
            "synchronous one-shot turn (LLM call). Set it locally to run.",
        )

    user_root = (tmp_path / "iso-root").resolve()
    home_db = Path(DEFAULT_RUNTIME_ROOT_LITERAL).expanduser().resolve() / "mac_events.db"

    home_count_before = _event_count(home_db)

    # Route the runtime root via JARVIS_RUNTIME_ROOT (env var) rather
    # than --runtime-root (argv); both feed _resolve_runtime_root, but
    # the env var keeps subprocess argv free of tmp_path strings (ruff
    # S603: literal argv is preferred for the subprocess call shape).
    result = subprocess.run(
        [sys.executable, "-m", "jarvis", "--no-detach", "hello"],
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_S,
        capture_output=True,
        text=True,
        # Inherit env so the LLM client can read OPENROUTER_PROXY_KEY etc.,
        # plus override JARVIS_RUNTIME_ROOT to point at the test root.
        env={**os.environ, "JARVIS_RUNTIME_ROOT": str(user_root)},
    )

    # Sanity: the subprocess must run to completion (regardless of the
    # turn's success). If the bootstrap crashed before opening SQLite,
    # the user-root would be empty and the isolation assertion would
    # spuriously fail.
    assert result.returncode in {0, 1}, (
        f"unexpected exit code {result.returncode}; stderr={result.stderr!r}"
    )

    user_db = user_root / "mac_events.db"
    assert user_db.exists(), (
        f"--runtime-root did not create the Event Log at {user_db}; "
        f"stderr={result.stderr!r}"
    )
    user_count = _event_count(user_db)
    assert user_count >= 1, (
        f"--runtime-root ran but {user_db} has 0 events; "
        f"stderr={result.stderr!r}"
    )

    home_count_after = _event_count(home_db)
    assert home_count_after == home_count_before, (
        f"runtime-root isolation breached: home Event Log {home_db} grew "
        f"from {home_count_before} to {home_count_after} events while "
        f"the operator passed --runtime-root {user_root}."
    )
