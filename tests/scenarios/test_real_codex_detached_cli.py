"""Tier-2 K3/K4 acceptance — real detached CLI, real Codex, real daemon child.

Per ADR-0002 § Tier 2 K (K3, K4) + § Daemon / CLI contract (lines
1046-1098). The live counterpart of
``tests/unit/test_cli_fork_detach.py``: the unit file proves the
classifier and the parent-side mechanics with a doomed child; this burn
lets the detached child live — it bootstraps, drives a REAL Codex turn
to completion, and writes the result into the Event Log long after the
parent is gone.

Scenario
--------

A ``task.created`` is seeded into a tmp runtime root by the TEST process
(which then closes its connection). ``python -m jarvis "<utterance>"``
is spawned as a real subprocess; the canonical D-day utterance matches
``_LONG_RUN_RE``, so the parent prints the fixed ack, forks, and
``os._exit(0)``s. The daemonized grandchild re-bootstraps the runtime,
installs the power observer, runs the full turn (real Codex + reviewer
LLM), and emits ``worker.reported`` → ... → ``surface.response_emitted``
into the SQLite Event Log at the tmp root.

- **K3** — the parent's lifetime adds ZERO Event Log rows (the parent
  never opens SQLite), the ack lands on the operator's stdout, and the
  parent exits quickly. The PID-attribution half of the ADR row (parent
  PID never opens the db) is structurally enforced by
  ``test_canary_daemon_ack_before_fork`` (no bootstrap call before
  ``fork_detach``) + ``test_classifier_no_sqlite_open``; events carry no
  writer PID, so the live-observable proxy is the row-count freeze
  across the parent's lifetime.
- **K4** — a fresh ``open_event_log()`` from a separate process (this
  test process) observes the detached child's ``worker.reported`` row
  strictly AFTER the parent has exited, and the child carried the turn
  all the way to ``surface.response_emitted`` (detached delivery).

The goal is the benign additive NOTES.md change (mirrors the L1 route-B
burn) with no ``verify_command`` — the ladder outcome is irrelevant
here; K3/K4 pin the process contract, not the evidence contract.

Cost estimate: ~$1 per invocation (one Codex turn + reviewer LLM call).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_detached_cli.py \\
        --live-codex --live-llm
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from os import environ
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.cli import _QUICK_ACK_PHRASE
from jarvis.runtime import bootstrap_runtime_app
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    from collections.abc import Iterator


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
# Matches _LONG_RUN_RE (跑 / 给 codex / 审核) → ack-then-fork path.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# Benign, additive goal (mirrors the L1 route-B burn): Codex creates a
# top-level NOTES.md. No verify_command — the ladder stays observation-
# only, which is irrelevant to the K3/K4 process contract.
_DETACHED_GOAL: str = (
    "Create a top-level NOTES.md file at the repository root that briefly "
    "describes this demo project. Make only that one additive change."
)

# How long the detached child gets to finish the whole turn (real Codex
# turn + reviewer LLM + interpret + emit). The default Codex turn budget
# is 10 minutes; leave headroom for reviewer + interpretation.
_CHILD_TURN_BUDGET_S: float = 900.0

# Parent must release the terminal quickly. Matches the unit-test bound
# (interpreter cold-start dominates; the ADR's 100ms row measures from
# classifier match, which is not externally observable).
_PARENT_WALL_BUDGET_S: float = 15.0


def _git(repo: Path, *args: str) -> None:
    """Run a git subcommand in ``repo`` (test-fixture helper)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _load_trace(db_path: Path) -> list[dict[str, Any]]:
    """Return every ``events`` row as a dict (rowid-ordered, payload parsed)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM events ORDER BY rowid").fetchall()
    finally:
        conn.close()
    trace: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        payload_json = record.get("payload_json")
        if isinstance(payload_json, str):
            record["payload"] = json.loads(payload_json)
        trace.append(record)
    return trace


def _payloads(capture: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    """All payloads of ``event_type`` in the captured trace (in order)."""
    return [event["payload"] for event in capture["trace"] if event["type"] == event_type]


def _count_events_fresh(db_path: Path) -> int:
    """Row count via a FRESH ``open_event_log`` connection (ADR K4 wording)."""
    conn = open_event_log(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0])
    finally:
        conn.close()


def _has_event_fresh(db_path: Path, event_type: str) -> bool:
    """True iff ``event_type`` exists, via a fresh ``open_event_log`` connection."""
    conn = open_event_log(db_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)
        ).fetchone()
        return int(row[0]) > 0
    finally:
        conn.close()


@pytest.fixture(scope="module")
def live_detached_cli(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Spawn the real fork-detach CLI ONCE; wait for the child; freeze the trace.

    Module-scoped so the real subprocess + Codex turn run a single time
    across the K3/K4 assertions. The test process seeds ``task.created``
    and CLOSES its connection before the spawn, so every subsequent row
    is attributable to the CLI's process tree.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("detached_repo")
    _git(repo, "init", "-q")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(
        "def test_truthy() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    root = tmp_path_factory.mktemp("detached_root")
    runtime = bootstrap_runtime_app(runtime_root=root)
    db_path = runtime.runtime_paths.event_log
    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _DETACHED_GOAL,
            "source": "manual",
            "repo_path": str(repo),
        },
        ts_epoch_ms=yesterday_ms,
    )
    # Close BEFORE the spawn: from here on, only the CLI's process tree
    # writes; the test process re-opens fresh connections to observe.
    runtime.conn.close()

    rows_before_spawn = _count_events_fresh(db_path)

    spawn_started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "jarvis", _UTTERANCE],
        check=False,
        timeout=60,
        capture_output=True,
        text=True,
        env={**environ, "JARVIS_RUNTIME_ROOT": str(root)},
    )
    parent_wall_s = time.monotonic() - spawn_started
    parent_exit_epoch_ms = int(time.time() * 1000)
    rows_at_parent_exit = _count_events_fresh(db_path)

    # Wait for the detached child to carry the turn to completion. Each
    # probe is a FRESH open_event_log from this (separate) process.
    deadline = time.monotonic() + _CHILD_TURN_BUDGET_S
    while time.monotonic() < deadline:
        if _has_event_fresh(db_path, "surface.response_emitted"):
            break
        time.sleep(5.0)
    else:
        trace_so_far = _load_trace(db_path)
        types = [event["type"] for event in trace_so_far]
        pytest.fail(
            f"detached child did not reach surface.response_emitted within "
            f"{_CHILD_TURN_BUDGET_S:.0f}s; events so far: {types} "
            f"(child stderr is /dev/null by design — inspect {db_path} and "
            f"the Codex artifact dirs under {root})"
        )
    time.sleep(0.5)  # let any straggling emit finalize before the snapshot

    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_detached_cli_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "proc": proc,
        "parent_wall_s": parent_wall_s,
        "parent_exit_epoch_ms": parent_exit_epoch_ms,
        "rows_before_spawn": rows_before_spawn,
        "rows_at_parent_exit": rows_at_parent_exit,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    yield captured


def test_k3_parent_acks_exits_fast_and_writes_nothing(
    live_detached_cli: dict[str, Any],
) -> None:
    """K3: parent prints the ack, exits quickly, and adds zero Event Log rows.

    The row-count freeze across the parent's lifetime is the live-
    observable proxy for "the parent's PID never opens SQLite" (events
    carry no writer PID; the structural half is enforced by
    ``test_canary_daemon_ack_before_fork`` + ``test_classifier_no_sqlite_open``).
    The child's first write lands only after its own bootstrap, well
    after the parent's ``os._exit(0)``.
    """
    cap = live_detached_cli
    proc: subprocess.CompletedProcess[str] = cap["proc"]

    assert proc.returncode == 0, (
        f"parent exit code {proc.returncode}; stderr={proc.stderr!r} "
        "(rc=2 means a daemon holds a runtime-root lock — stop `jarvis serve` "
        "before this burn)"
    )
    assert _QUICK_ACK_PHRASE in proc.stdout, (
        f"ack not on parent stdout: {proc.stdout!r}"
    )
    assert cap["parent_wall_s"] < _PARENT_WALL_BUDGET_S, (
        f"parent took {cap['parent_wall_s']:.2f}s; expected < "
        f"{_PARENT_WALL_BUDGET_S:.0f}s (ack-then-fork must release the "
        "terminal immediately)"
    )
    assert cap["rows_at_parent_exit"] == cap["rows_before_spawn"], (
        f"Event Log grew from {cap['rows_before_spawn']} to "
        f"{cap['rows_at_parent_exit']} rows during the parent's lifetime — "
        "the parent process tree wrote to SQLite before the operator got "
        "the terminal back"
    )


def test_k4_detached_child_writes_worker_reported_after_parent_exit(
    live_detached_cli: dict[str, Any],
) -> None:
    """K4: the detached child's worker.reported is observed by a separate process.

    The fixture's poll loop already proved observability via fresh
    ``open_event_log()`` connections from this process; here we pin the
    row's content and ordering: the report exists, is ``status=ok``, was
    written strictly AFTER the parent exited, and the child carried the
    turn all the way to a delivered surface response.
    """
    cap = live_detached_cli

    reported = _payloads(cap, "worker.reported")
    assert reported, "no worker.reported — the detached child never finished a Codex turn"
    assert reported[0]["status"] == "ok", (
        f"worker.reported.status={reported[0]['status']!r}; expected 'ok'"
    )

    reported_ts = [
        event["ts_epoch_ms"]
        for event in cap["trace"]
        if event["type"] == "worker.reported"
    ]
    assert min(reported_ts) > cap["parent_exit_epoch_ms"], (
        f"worker.reported ts {min(reported_ts)} predates parent exit "
        f"{cap['parent_exit_epoch_ms']} — the report must come from the "
        "detached child, not the parent's lifetime"
    )

    emitted = _payloads(cap, "surface.response_emitted")
    assert emitted, (
        "no surface.response_emitted — the detached child stopped short of "
        "delivering the response"
    )
