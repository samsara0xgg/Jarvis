"""Tier-2 J + K + L acceptance — real Codex flagship scenario.

Per ADR 0002 § Acceptance Tier 2 J/K/L (Step 20). Requires both
``--live-codex`` and ``--live-llm`` pytest flags to run. The full
flagship scenario per ADR § Scenario:

D-1 (yesterday)::

    jarvis "今天给我做 implement-rate-limiter，repo 是 <repo>"

D-day (today)::

    jarvis "昨天那个 task 给 Codex 跑一下，做完审核了再告诉我。"

Required environment (Tier-2 invocation):

- ``codex`` CLI >= 0.125.0 on PATH (J1 preflight gate).
- ``OPENROUTER_PROXY_KEY`` env var set (real OpenRouter key, > 20
  chars, not a stub — enforced by ``verify_api_key_present``).

Cost envelope (ADR-0002 Open Question 11): a single happy-path run
spends approximately $0.75 - $3.00 in real Codex + OpenRouter calls.
The full J-sweep (this file + the four sibling ``test_real_codex_*``
files) totals approximately $4 - $15 per invocation.

Invocation::

    uv run pytest tests/scenarios/test_real_codex_flagship.py \\
        --live-codex --live-llm

Skeleton policy
---------------

Per ADR-0002 Step 20 brief, each invariant J1-J13 / K1-K8 / L1-L2 is
a NAMED test function so the inventory matches the ADR Tier-2
listing. Test bodies that cannot be meaningfully asserted without a
live Codex round-trip call :func:`pytest.skip` with an explanatory
reason — those skips fire only under ``--live-codex --live-llm``
(otherwise the module-level skip from ``pytest_collection_modifyitems``
takes precedence). Assertions that can be made via event-log
inspection on the captured runtime are implemented inline; the rest
carry ``# TODO`` markers for follow-up.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.pre_emit_phrases import COMPLETION_REGEXES, LIMITATION_REGEXES
from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event

if TYPE_CHECKING:
    from collections.abc import Iterator

# Every test in this file requires BOTH flags: live cloud LLM (for the
# L3 decision rounds + reviewer) AND a real Codex subprocess (for the
# L4 spawn_worker handler). The module-level mark is additive with
# any per-test marks the conftest skip logic inspects keywords on each
# item, so module-level is sufficient.
pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# Skeleton-policy skip reason shared by every test in this file (and
# its J/K/L siblings). When the test inventory is fleshed out against
# a real Mac with Codex installed, individual test bodies will replace
# this skip with concrete assertions. The skip fires ONLY when both
# --live-codex and --live-llm are passed (otherwise the conftest
# collection-modify skip wins first).
_SKELETON_SKIP = (
    "Live-codex test scaffold; full assertions land when run against a real "
    "Codex CLI on Allen's Mac (ADR-0002 Step 20 deferred to first live run)."
)


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def real_python_repo(tmp_path: Path) -> Iterator[Path]:
    """Throw-away git repo with a pyproject + tests/ for the D-1 task.

    A minimal repo whose ``verify_command`` detector resolves to
    ``uv run pytest -x`` (per ``jarvis/execution/verify_command_detect.py``).
    Codex sees a real working tree it can edit; the verify_command
    fixture exits 0 by default (one passing test). Tests that need
    a failing verify_command override the test file in the body.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(
        "def test_truthy() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        check=True,
    )
    yield repo


# --- Increment-1 shared happy-path capture (live burn) --------------------

# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# Low-risk additive goal: Codex edits the tree (non-empty diff) without
# touching test logic, so the verify_command pytest stays green and the
# happy path reaches task.verified. Codex non-determinism is accepted
# (see docs/superpowers/specs/2026-05-28-real-codex-tier2-jkl-burn-design.md).
_HAPPY_GOAL: str = (
    "Add a module-level docstring to tests/test_demo.py explaining what it "
    "verifies, and create a top-level NOTES.md describing the demo project. "
    "Do not change any test logic; all existing tests must still pass."
)


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


@pytest.fixture(scope="module")
def live_real_codex_happy(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex flagship happy path ONCE; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised
    a single time across every J/K/L assertion in this file. Seeds the
    D-1 ``task.created`` with ``repo_path`` + ``verify_command`` (the
    P-0003 fix) so ``spawn_worker`` runs Codex against a real git tree
    and ``verify_diff`` runs a real ``pytest`` whose exit 0 lets the
    happy path reach ``task.verified``.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("real_codex_repo")
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

    root = tmp_path_factory.mktemp("real_codex_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _HAPPY_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    result = run_turn(runtime, utterance=_UTTERANCE)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    # Persist the full trace for offline assertion design (avoids re-burning).
    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_happy_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


def _payloads(capture: dict[str, Any], event_type: str) -> list[dict[str, Any]]:
    """All payloads of ``event_type`` in the captured happy-path trace (in order)."""
    return [event["payload"] for event in capture["trace"] if event["type"] == event_type]


# --- J9 timeout capture (separate short-budget live burn) -----------------

# Wall-clock budget (seconds) forced onto the Codex turn for J9 so the
# real driver deadline trips in ~seconds instead of the 600s default.
# Large enough to clear the initialize/thread-start/turn-start handshake
# (so the deadline trips INSIDE the poll loop → action.timeout_assumed,
# not a handshake-phase action.failed) yet far below any realistic turn
# completion time for the goal below.
_TIMEOUT_BUDGET_S: str = "15"

# Deliberately open-ended goal Codex cannot finish inside the budget, so
# the deadline — not a fast turn/completed — is what ends the turn.
_TIMEOUT_GOAL: str = (
    "Perform a thorough, repository-wide quality pass: add exhaustive "
    "Google-style docstrings to every function, write a comprehensive "
    "pytest suite covering every edge case for each module, add complete "
    "type annotations throughout, and author a detailed ARCHITECTURE.md. "
    "Be exhaustive; do not stop early."
)


@pytest.fixture(scope="module")
def live_real_codex_timeout(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run the real-Codex turn ONCE under a short budget; freeze the trace.

    Sibling negative-path burn to :func:`live_real_codex_happy`: seeds the
    same canonical D-1 ``task.created`` but forces a short per-turn budget
    via ``JARVIS_CODEX_TURN_TIMEOUT_S`` so the real ``run_codex_action``
    deadline trips, issues ``turn/interrupt`` + ``close(timeout=3.0)``, and
    ``spawn_worker_handler`` maps the interrupted turn to
    ``action.timeout_assumed`` (no ``worker.reported``). The env override
    is popped immediately after ``run_turn`` so it cannot leak into the
    module's other (happy-path) live fixture regardless of fixture order.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("real_codex_timeout_repo")
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

    root = tmp_path_factory.mktemp("real_codex_timeout_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _TIMEOUT_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    os.environ["JARVIS_CODEX_TURN_TIMEOUT_S"] = _TIMEOUT_BUDGET_S
    try:
        result = run_turn(runtime, utterance=_UTTERANCE)
    finally:
        # Scope the override tightly to this run_turn so the module's
        # happy-path fixture still gets the 600s default.
        os.environ.pop("JARVIS_CODEX_TURN_TIMEOUT_S", None)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_timeout_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


# --- J4 heartbeat capture (lowered-cadence live burn) ---------------------

# Heartbeat cadence (seconds) forced onto the Codex turn for J4 so a
# ``worker.heartbeat`` lands within the first idle ticks instead of needing
# a 30s real turn. A generous turn budget bounds a Codex hang to ~2 min (not
# the 600s default) while still letting the ~15s happy turn complete
# normally; heartbeats chain to action.running regardless of whether the
# turn completes or trips the budget.
_HEARTBEAT_INTERVAL_BUDGET_S: str = "3"
_HEARTBEAT_HANG_GUARD_S: str = "120"


@pytest.fixture(scope="module")
def live_real_codex_heartbeat(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run a real-Codex turn ONCE under a lowered heartbeat cadence; freeze the trace.

    Sibling to :func:`live_real_codex_happy`: seeds the same canonical D-1
    ``task.created`` but forces a 3s heartbeat interval via
    ``JARVIS_CODEX_HEARTBEAT_INTERVAL_S`` so the real ``run_codex_action``
    poll loop emits ``worker.heartbeat`` within the first idle ticks rather
    than needing a 30s turn. A generous ``JARVIS_CODEX_TURN_TIMEOUT_S`` hang
    guard bounds a Codex hang; both overrides are popped immediately after
    ``run_turn`` so they cannot leak into the module's other live fixtures.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("real_codex_heartbeat_repo")
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

    root = tmp_path_factory.mktemp("real_codex_heartbeat_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _HAPPY_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    os.environ["JARVIS_CODEX_HEARTBEAT_INTERVAL_S"] = _HEARTBEAT_INTERVAL_BUDGET_S
    os.environ["JARVIS_CODEX_TURN_TIMEOUT_S"] = _HEARTBEAT_HANG_GUARD_S
    try:
        result = run_turn(runtime, utterance=_UTTERANCE)
    finally:
        # Scope both overrides tightly to this run_turn so the module's
        # happy-path fixture still gets the 30s / 600s defaults.
        os.environ.pop("JARVIS_CODEX_HEARTBEAT_INTERVAL_S", None)
        os.environ.pop("JARVIS_CODEX_TURN_TIMEOUT_S", None)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_heartbeat_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


# --- J8 crash capture (real Codex killed mid-turn) ------------------------

# action.failed error tags that a SIGKILLed Codex subprocess can produce.
# The targeted one is ``codex_subprocess_crashed`` (poll-loop is_alive
# detection — the kill lands mid-turn after the handshake). The handshake
# variants are accepted as a fallback so the live test never flakes on
# kill timing; the exact ``codex_subprocess_crashed`` mapping is proven
# deterministically by
# ``tests/unit/test_codex_action.py::test_run_codex_action_dead_subprocess_maps_to_crash_not_timeout``.
_CRASH_ERROR_FAMILY: frozenset[str] = frozenset(
    {
        "codex_subprocess_crashed",
        "codex_initialize_failed",
        "codex_thread_start_failed",
        "codex_turn_start_failed",
        "codex_spawn_failed",
    }
)

# Safety net: bound a watchdog miss to this turn budget instead of the
# 600s default, so a kill that never lands fails the burn in ~minutes,
# not ten. The watchdog kills well before this elapses on the happy path.
_CRASH_SAFETY_BUDGET_S: str = "120"


def _kill_codex_child_after_grace(
    ppid: int, *, grace_s: float = 12.0, window_s: float = 90.0
) -> None:
    """Wait for the Codex subprocess to spawn, let the turn get underway, then SIGKILL it.

    ``run_turn`` blocks the main thread inside the synchronous Codex poll
    loop, so the kill is issued from this watchdog thread. It polls for
    the ``codex`` child of the pytest process (``pgrep -P`` scoped to our
    own children, ``-f`` to match the codex argv), waits ``grace_s`` to
    clear the initialize/thread-start/turn-start handshake so the kill
    lands INSIDE the poll loop (→ ``codex_subprocess_crashed``), then
    ``pkill -9``. No-match exit codes are ignored.
    """
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        found = subprocess.run(
            ["pgrep", "-P", str(ppid), "-f", "codex"],
            capture_output=True,
            text=True,
            check=False,
        )
        if found.stdout.strip():
            time.sleep(grace_s)
            subprocess.run(
                ["pkill", "-9", "-P", str(ppid), "-f", "codex"],
                check=False,
            )
            return
        time.sleep(0.5)


@pytest.fixture(scope="module")
def live_real_codex_crash(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run a real Codex turn ONCE and SIGKILL the worker mid-turn; freeze the trace.

    Sibling negative-path burn to :func:`live_real_codex_happy`: a
    watchdog thread kills the real ``codex`` subprocess while the turn is
    in flight, so the poll loop observes ``is_alive() is False`` before
    ``turn/completed`` and ``spawn_worker_handler`` maps the dead worker
    to ``action.failed`` (no ``worker.reported``). A short safety budget
    bounds a watchdog miss; it is popped immediately after ``run_turn`` so
    it cannot leak into the module's other live fixtures.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("real_codex_crash_repo")
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

    root = tmp_path_factory.mktemp("real_codex_crash_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _HAPPY_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    killer = threading.Thread(
        target=_kill_codex_child_after_grace, args=(os.getpid(),), daemon=True
    )
    killer.start()
    os.environ["JARVIS_CODEX_TURN_TIMEOUT_S"] = _CRASH_SAFETY_BUDGET_S
    try:
        result = run_turn(runtime, utterance=_UTTERANCE)
    finally:
        os.environ.pop("JARVIS_CODEX_TURN_TIMEOUT_S", None)
    killer.join(timeout=2.0)
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    db_path = runtime.runtime_paths.event_log
    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_crash_trace.json"
    dump_path.write_text(
        json.dumps(trace, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {
        "runtime": runtime,
        "result": result,
        "db_path": db_path,
        "repo": repo,
        "trace": trace,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


# --- J — Real Codex round-trip --------------------------------------------


def test_j1_codex_version_preflight_below_min_raises(
    live_real_codex_happy: dict[str, Any],
) -> None:
    """J1: ``codex --version`` < 0.125.0 at handler registration raises CodexVersionTooLowError.

    Covered structurally by the canary
    ``test_canary_codex_version_preflight`` (unit-tier); this Tier-2
    test verifies the preflight ran against the REAL ``codex`` binary on
    PATH — proven by the happy path reaching ``run.started`` with no
    ``action.failed(error="codex_version_too_low")``.
    """
    types = [event["type"] for event in live_real_codex_happy["trace"]]
    assert "run.started" in types
    failures = _payloads(live_real_codex_happy, "action.failed")
    assert not any(p.get("error") == "codex_version_too_low" for p in failures)


def test_j2_codex_initialize_within_5s(live_real_codex_happy: dict[str, Any]) -> None:
    """J2: ``CodexAppServerClient.initialize()`` returns within 5s; OAuth refresh path covered.

    The sub-5s latency bound is not event-log-observable; this Tier-2
    test asserts the provable half — ``initialize()`` necessarily
    succeeded because the live Codex turn proceeded to ``run.started``
    and ``worker.reported``. The latency bound is a unit-tier concern.
    """
    types = [event["type"] for event in live_real_codex_happy["trace"]]
    assert "run.started" in types
    assert "worker.reported" in types


def test_j3_thread_start_cwd_matches_repo_path() -> None:
    """J3: ``thread/start.cwd`` equals the run ``cwd`` byte-for-byte.

    The cwd is set on the Codex ``thread/start`` JSON-RPC request, which
    is internal to the subprocess and not mirrored into the Event Log, so
    it cannot be asserted from a scenario trace. It IS proven at the
    spawn-argv boundary by the unit test
    ``tests/unit/test_codex_action.py::test_run_codex_action_thread_start_cwd_matches_repo_path``
    (the FakeClient ``request_log`` captures the outgoing ``cwd`` param
    without a live spawn). This stub keeps J3 in the Tier-2 enumeration.
    """
    pytest.skip(
        "J3 thread/start cwd is not Event-Log-observable; proven by "
        "tests/unit/test_codex_action.py::"
        "test_run_codex_action_thread_start_cwd_matches_repo_path.",
    )


def test_j4_heartbeat_emitted_for_long_turns(
    live_real_codex_heartbeat: dict[str, Any],
) -> None:
    """J4: a turn idling past the heartbeat interval emits >=1 ``worker.heartbeat`` chained to ``action.running``.

    The live cadence is lowered to 3s (``JARVIS_CODEX_HEARTBEAT_INTERVAL_S``)
    so a real turn crosses the interval in seconds rather than the 30s
    production default; the chaining invariant (ADR-0002 J4) is identical at
    any interval — every ``worker.heartbeat`` carries a ``source_event_id``
    equal to the spawn_worker ``action.running`` event's ``event_uid``. The
    30s production threshold itself is proven separately by the A3 live run
    (docs/live-run-bugs.md P-0008: 19 heartbeats 30s apart) and by the
    deterministic unit test ``tests/unit/test_codex_action.py::
    test_run_codex_action_respects_heartbeat_interval_param``.

    Event-log-observable invariants asserted on the captured trace:

    - the run reached ``run.started`` and emitted at least one
      ``worker.heartbeat``;
    - at least one heartbeat's ``source_event_id`` equals an
      ``action.running`` event's ``event_uid`` (the J4 cause-chain);
    - every chained heartbeat payload carries the spawn_worker correlation
      (``action_id``, ``run_id``) and an ``elapsed_ms`` reading.
    """
    cap = live_real_codex_heartbeat
    trace = cap["trace"]
    types = [event["type"] for event in trace]

    assert "run.started" in types, "spawn_worker never reached run.started"

    heartbeats = [event for event in trace if event["type"] == "worker.heartbeat"]
    assert heartbeats, (
        "no worker.heartbeat emitted; the Codex poll loop never crossed the "
        f"3s idle cadence. types={types}"
    )

    running_uids = {
        event["event_uid"] for event in trace if event["type"] == "action.running"
    }
    assert running_uids, "no action.running event for heartbeats to chain to"

    chained = [hb for hb in heartbeats if hb.get("source_event_id") in running_uids]
    assert chained, (
        "no worker.heartbeat chains back to action.running (J4); heartbeat "
        f"source_event_ids={[hb.get('source_event_id') for hb in heartbeats]}, "
        f"action.running uids={running_uids}"
    )

    for hb in chained:
        payload = hb["payload"]
        assert payload.get("action_id"), "heartbeat payload missing action_id"
        assert payload.get("run_id"), "heartbeat payload missing run_id"
        assert "elapsed_ms" in payload, "heartbeat payload missing elapsed_ms"


def test_j5_turn_completed_nonempty_diff_marks_reported_ok(
    live_real_codex_happy: dict[str, Any],
) -> None:
    """J5: ``turn/completed`` with non-empty diff → ``worker.reported.status == "ok"``."""
    reported = _payloads(live_real_codex_happy, "worker.reported")
    assert len(reported) == 1
    assert reported[0]["status"] == "ok"
    artifacts = _payloads(live_real_codex_happy, "worker.artifact_observed")
    assert artifacts, "no worker.artifact_observed — diff was not captured"
    assert artifacts[0]["kind"] == "diff"
    assert artifacts[0]["content_hash"], "diff content_hash empty (expected non-empty diff)"


def test_j6_codex_client_dead_at_verify_diff_observation(
    live_real_codex_happy: dict[str, Any],
) -> None:
    """J6: verify_diff observes its result only after the Codex worker is closed.

    The literal invariant — ``CodexAppServerClient.is_alive()`` is False at
    the moment verify_diff's ``action.result_observed`` emits — is not
    Event-Log-observable: ``run_codex_action`` closes the client before it
    returns, so the object no longer exists when the downstream verify_diff
    step runs. That mechanism is pinned by ``tests/unit/test_codex_client.py
    ::test_close_makes_is_alive_false``.

    Here we assert the observable shadow on the happy trace: every
    ``action.result_observed`` for the verify_diff action (the one whose
    action_id is NOT the spawn_worker's) is emitted strictly after
    ``worker.reported`` — the spawn_worker terminal signal, after which
    ``run_codex_action`` has returned and ``close(timeout=3.0)`` has killed
    the subprocess. The verify_diff slots carry the §3.5.7 dual-slot
    semantics (observation [+ verification]), confirming the right action
    was matched.
    """
    cap = live_real_codex_happy
    trace = cap["trace"]

    reported_idxs = [i for i, e in enumerate(trace) if e["type"] == "worker.reported"]
    assert len(reported_idxs) == 1, (
        f"expected exactly one worker.reported; got {len(reported_idxs)}"
    )
    reported_idx = reported_idxs[0]
    spawn_action_id = trace[reported_idx]["payload"]["action_id"]

    verify_observed = [
        (i, event)
        for i, event in enumerate(trace)
        if event["type"] == "action.result_observed"
        and event["payload"].get("action_id") != spawn_action_id
    ]
    assert verify_observed, (
        "no verify_diff action.result_observed (action_id distinct from the "
        f"spawn_worker action {spawn_action_id!r})"
    )

    # J6 core: verification observes its result only after the worker closed.
    assert all(i > reported_idx for i, _ in verify_observed), (
        "a verify_diff action.result_observed preceded worker.reported; "
        f"reported_idx={reported_idx}, "
        f"verify idxs={[i for i, _ in verify_observed]}"
    )

    # Confirm the matched rows are the verify_diff §3.5.7 dual-slot
    # observations, not some unrelated action.result_observed.
    semantics = {event["payload"].get("semantics") for _, event in verify_observed}
    assert semantics <= {"observation", "verification", "error"}, (
        f"unexpected verify_diff result semantics: {semantics}"
    )
    assert "observation" in semantics, (
        f"verify_diff slot-1 observation missing; semantics={semantics}"
    )


def test_j7_cost_recorded_kind_codex(live_real_codex_happy: dict[str, Any]) -> None:
    """J7: at least one ``cost.recorded`` row with ``kind == "codex"`` correlated to the spawn_worker action."""
    costs = _payloads(live_real_codex_happy, "cost.recorded")
    codex_costs = [c for c in costs if c.get("kind") == "codex"]
    kinds = [c.get("kind") for c in costs]
    assert codex_costs, f"no cost.recorded with kind=codex; kinds={kinds}"
    assert any(c.get("run_id") for c in codex_costs), "codex cost not correlated to a run_id"
    # A real turn always burns tokens. Codex 0.130 dropped the usage field
    # from turn/completed (cumulative usage streams on
    # thread/tokenUsage/updated); before 2026-06-10 every healthy turn
    # was ledgered as 0/0 and this test could not see it. Pin it.
    assert any(int(c.get("tokens_in") or 0) > 0 for c in codex_costs), (
        f"codex cost rows carry zero tokens_in — usage extraction is blind; rows={codex_costs}"
    )


def test_j8_codex_crash_emits_action_failed(
    live_real_codex_crash: dict[str, Any],
) -> None:
    """J8: real Codex SIGKILLed mid-turn → ``action.failed``; surface limitation.

    Negative-path appendix: the run is identical to the happy path until
    the worker is killed; instead of ``worker.reported``, ``action.failed``
    is emitted and the surface utterance contains "Codex 跑挂了，没新 diff".

    Event-log-observable invariants asserted on the captured trace:

    - the run reached ``run.started`` then produced exactly one
      ``action.failed`` whose ``error`` is in the crash family (targeted:
      ``codex_subprocess_crashed`` from the poll-loop ``is_alive`` probe);
      NO ``action.timeout_assumed`` (a killed worker is a crash, not a
      timeout), NO ``worker.reported``, NO ``worker.artifact_observed``;
    - ``task.executor_reported`` carries ``status="failed"``;
    - a ``Limitation`` ``claim.created`` backed by an
      ``evidence.attached(relation=limits, level=reported)`` row;
    - NO ``task.verified`` and NO ``task.no_op``;
    - exactly one ``surface.response_emitted`` whose text carries crash
      limitation phrasing (matches ``LIMITATION_REGEXES``, not
      ``COMPLETION_REGEXES``, and names the crash — ``跑挂``).
    """
    cap = live_real_codex_crash
    types = [event["type"] for event in cap["trace"]]

    # (a) Crash lifecycle: the run started, the worker died → action.failed.
    assert "run.started" in types, "spawn_worker never reached run.started"
    failed = _payloads(cap, "action.failed")
    assert len(failed) == 1, f"expected exactly one action.failed; types={types}"
    assert "action.timeout_assumed" not in types, (
        "a SIGKILLed worker must map to action.failed (crash), not "
        "action.timeout_assumed — watchdog likely missed the kill window"
    )
    err = failed[0].get("error")
    assert err in _CRASH_ERROR_FAMILY, (
        f"action.failed error {err!r} not in the crash family {sorted(_CRASH_ERROR_FAMILY)}"
    )
    assert "worker.reported" not in types, (
        "crash path must skip worker.reported (the turn never completed)"
    )
    assert "worker.artifact_observed" not in types, (
        "crash path must skip diff capture (no worker.artifact_observed)"
    )

    # (b) Task Ledger terminal row marks the run as failed.
    executor_reported = _payloads(cap, "task.executor_reported")
    assert executor_reported, "no task.executor_reported on the crash run"
    assert any(p.get("status") == "failed" for p in executor_reported), (
        f"expected a task.executor_reported(status=failed); got {executor_reported!r}"
    )

    # (c) Limitation claim backed by reported-level limits evidence.
    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c.get("type") == "Limitation"}
    assert limitation_ids, "no Limitation claim created on crash"
    evidence = _payloads(cap, "evidence.attached")
    reported_limits = [
        e
        for e in evidence
        if e.get("claim_id") in limitation_ids
        and e.get("relation") == "limits"
        and e.get("level") == "reported"
    ]
    assert reported_limits, (
        "no evidence.attached(relation=limits, level=reported) on the "
        f"Limitation claim; evidence={evidence!r}"
    )

    # (d) No completion of either kind.
    assert not _payloads(cap, "task.verified"), "task.verified must not fire on crash"
    assert not _payloads(cap, "task.no_op"), "task.no_op must not fire on crash"

    # (e) Surface carries crash-limitation framing, no completion language.
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1, f"expected exactly one surface.response_emitted; got {emitted!r}"
    text = emitted[0]["text"]
    assert any(rx.search(text) for rx in LIMITATION_REGEXES), (
        f"crash surface must use limitation phrasing; got: {text!r}"
    )
    assert not any(rx.search(text) for rx in COMPLETION_REGEXES), (
        f"crash surface must not claim completion; got: {text!r}"
    )
    assert "跑挂" in text, f"crash surface must name the crash; got: {text!r}"


def test_j9_codex_timeout_emits_timeout_assumed(
    live_real_codex_timeout: dict[str, Any],
) -> None:
    """J9: Codex deadline exceeded → ``action.timeout_assumed``; surface limitation.

    Negative-path appendix: identical to crash except
    ``action.timeout_assumed`` replaces ``action.failed``. A short
    ``JARVIS_CODEX_TURN_TIMEOUT_S`` budget forces the real
    ``run_codex_action`` deadline to trip, which issues ``turn/interrupt``
    + ``close(timeout=3.0)`` (the subprocess-kill is unconditional in the
    driver's ``_result`` close; not Event-Log-observable, so asserted
    structurally by ``tests/unit/test_codex_action.py`` rather than here).

    Event-log-observable invariants asserted on the captured trace:

    - exactly one ``action.timeout_assumed``; the run reached
      ``run.started`` but produced NO ``worker.reported`` (the turn never
      completed) and NO ``worker.artifact_observed`` (diff never captured);
    - ``task.executor_reported`` carries ``status="timeout"``;
    - a ``Limitation`` ``claim.created`` backed by an
      ``evidence.attached(relation=limits, level=reported)`` row;
    - NO ``task.verified`` and NO ``task.no_op``;
    - exactly one ``surface.response_emitted`` whose text carries timeout
      limitation phrasing (matches ``LIMITATION_REGEXES``, not
      ``COMPLETION_REGEXES``, and names the timeout — ``超时``).
    """
    cap = live_real_codex_timeout
    types = [event["type"] for event in cap["trace"]]

    # (a) Timeout lifecycle: the run started, the turn was cut off.
    assert "run.started" in types, "spawn_worker never reached run.started"
    assert types.count("action.timeout_assumed") == 1, (
        f"expected exactly one action.timeout_assumed; types={types}"
    )
    assert "worker.reported" not in types, (
        "timeout path must skip worker.reported (the turn never completed)"
    )
    assert "worker.artifact_observed" not in types, (
        "timeout path must skip diff capture (no worker.artifact_observed)"
    )

    # (b) Task Ledger terminal row marks the run as a timeout.
    executor_reported = _payloads(cap, "task.executor_reported")
    assert executor_reported, "no task.executor_reported on the timeout run"
    assert any(p.get("status") == "timeout" for p in executor_reported), (
        f"expected a task.executor_reported(status=timeout); got {executor_reported!r}"
    )

    # (c) Limitation claim backed by reported-level limits evidence.
    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c.get("type") == "Limitation"}
    assert limitation_ids, "no Limitation claim created on timeout"
    evidence = _payloads(cap, "evidence.attached")
    reported_limits = [
        e
        for e in evidence
        if e.get("claim_id") in limitation_ids
        and e.get("relation") == "limits"
        and e.get("level") == "reported"
    ]
    assert reported_limits, (
        "no evidence.attached(relation=limits, level=reported) on the "
        f"Limitation claim; evidence={evidence!r}"
    )

    # (d) No completion of either kind.
    assert not _payloads(cap, "task.verified"), "task.verified must not fire on timeout"
    assert not _payloads(cap, "task.no_op"), "task.no_op must not fire on timeout"

    # (e) Surface carries timeout-limitation framing, no completion language.
    emitted = _payloads(cap, "surface.response_emitted")
    assert len(emitted) == 1, f"expected exactly one surface.response_emitted; got {emitted!r}"
    text = emitted[0]["text"]
    assert any(rx.search(text) for rx in LIMITATION_REGEXES), (
        f"timeout surface must use limitation phrasing; got: {text!r}"
    )
    assert not any(rx.search(text) for rx in COMPLETION_REGEXES), (
        f"timeout surface must not claim completion; got: {text!r}"
    )
    assert "超时" in text, f"timeout surface must name the timeout; got: {text!r}"


def test_j10_all_four_sandbox_c_flags_present_in_popen_args(real_python_repo: Path) -> None:
    """J10: spawn argv contains all four ``-c`` flags: model, reasoning_effort, sandbox_mode, writable_roots.

    The ``-c`` flag slice is built by ``_build_extra_args`` and not mirrored
    into the Event Log. It IS proven at the spawn-argv boundary by the unit
    tests ``tests/unit/test_codex_action.py::test_build_extra_args_carries_all_required_keys``
    (asserts model / reasoning_effort / sandbox_mode / writable_roots, plus
    the MCP keys) and ``::test_build_extra_args_has_eleven_c_flags``. This
    stub keeps J10 in the Tier-2 enumeration.
    """
    pytest.skip(
        "J10 -c flags are not Event-Log-observable; proven by "
        "tests/unit/test_codex_action.py::test_build_extra_args_carries_all_required_keys "
        "(+ ::test_build_extra_args_has_eleven_c_flags).",
    )


def test_j11_submit_report_mcp_tool_reachable(live_real_codex_happy: dict[str, Any]) -> None:
    """J11: ``submit_report`` MCP tool reachable; proven by a clean ``worker.reported.status == "ok"``.

    The MCP ``tools/list`` reply is internal to the Codex subprocess and
    not observable from jarvis. The handler emits ``worker.report_missing``
    + a degraded ``status == "report_missing"`` when Codex completes a
    turn WITHOUT calling ``submit_report``. A clean ``status == "ok"`` and
    the absence of any ``worker.report_missing`` event is therefore the
    real reachability proof — Codex could not have produced an ``ok``
    report through a tool it could not list and call. The byte-level
    ``mcp_servers.jarvis-tools`` argv check is proven separately by
    ``tests/unit/test_codex_action.py::test_build_extra_args_carries_all_required_keys``
    (same spawn-argv boundary as J10).
    """
    reported = _payloads(live_real_codex_happy, "worker.reported")
    assert reported
    assert reported[0]["status"] == "ok"
    types = [event["type"] for event in live_real_codex_happy["trace"]]
    assert "worker.report_missing" not in types


def test_j12_no_submit_report_emits_report_missing(real_python_repo: Path) -> None:
    """J12: Codex completes turn without calling ``submit_report`` → ``worker.report_missing`` + Limitation Claim.

    See the dedicated file ``test_real_codex_no_submit_report.py`` for
    the full J12 scenario; this stub keeps the invariant present in the
    Tier-2 J inventory so a `--collect-only` run shows the complete
    J1-J13 enumeration.
    """
    pytest.skip(
        "J12 lives in tests/scenarios/test_real_codex_no_submit_report.py; its "
        "live burn is deferred by design (P-0010 X-decision + spec §3.5.8) and "
        "the post-turn guard is unit-proven by tests/unit/test_spawn_worker_real.py."
    )


def test_j13_dirty_tree_stash_pop_conflict_emits_conflict_patch(real_python_repo: Path) -> None:
    """J13: dirty-tree case — ``git stash push -u`` → codex → ``git stash pop`` conflict → ``conflict.patch`` artifact."""
    pytest.skip(_SKELETON_SKIP)


# --- K — Real notification side-effects -----------------------------------


def test_k1_say_subprocess_invoked_with_voice_flag(real_python_repo: Path) -> None:
    """K1: ``say`` subprocess started with ``-v Tingting`` (or configured voice) + voice-channel text as last arg.

    The ``say`` argv is built inside ``notify.deliver_voice`` and not
    mirrored into the Event Log. It IS proven at the subprocess boundary by
    ``tests/unit/test_notify.py::test_deliver_voice_spawns_say_with_tingting``
    (asserts ``["say", "-v", "Tingting", <text>]``). This stub keeps K1 in
    the Tier-2 enumeration.
    """
    pytest.skip(
        "K1 say argv is not Event-Log-observable; proven by "
        "tests/unit/test_notify.py::test_deliver_voice_spawns_say_with_tingting.",
    )


def test_k2_osascript_notification_truncated_at_240(real_python_repo: Path) -> None:
    """K2: ``osascript -e 'display notification ...'`` invoked exactly once per emission; body truncated at 240 chars.

    The ``osascript`` argv + 240-char truncation are built inside
    ``notify.deliver_banner`` and not mirrored into the Event Log. They ARE
    proven at the subprocess boundary by
    ``tests/unit/test_notify.py::test_deliver_banner_short_body`` (one
    ``/usr/bin/osascript -e`` call) and ``::test_deliver_banner_truncates_at_240``
    (body > 240 → 239 chars + ellipsis). This stub keeps K2 in the Tier-2
    enumeration.
    """
    pytest.skip(
        "K2 osascript argv/truncation is not Event-Log-observable; proven by "
        "tests/unit/test_notify.py::test_deliver_banner_short_body "
        "(+ ::test_deliver_banner_truncates_at_240).",
    )


def test_k3_cli_parent_exits_within_100ms_no_sqlite_write(real_python_repo: Path) -> None:
    """K3: CLI parent exits within 100ms of the long-run classifier match; parent's PID never opens ``~/.jarvis/mac_events.db``."""
    pytest.skip(_SKELETON_SKIP)


def test_k4_detached_child_writes_worker_reported_after_parent_exits(
    real_python_repo: Path,
) -> None:
    """K4: detached child writes ``worker.reported``; a fresh ``open_event_log()`` in a separate process observes the row."""
    pytest.skip(_SKELETON_SKIP)


def test_k5_reviewer_fail_path_uses_limitation_phrasing(real_python_repo: Path) -> None:
    """K5: reviewer-verdict-fail path delivers limitation utterance through ``say``, not a completion claim.

    Blocked on the B-0005/B-0006 Attention-channel design gap: Limitation-class
    responses currently deliver via ``stdout`` only, so the ADR K5 row (voice
    delivery of the limitation utterance) cannot pass against shipped behavior.
    The limitation *text* contract is live-proven by L3
    (``test_real_codex_verify_fail.py``) and the reviewer-fail evidence contract
    by L5 (``test_real_codex_reviewer_fail_no_verify.py``).
    """
    pytest.skip(
        "K5 blocked on B-0005/B-0006 (docs/live-run-bugs.md): Limitation-class "
        "responses deliver via stdout only until the Attention-channel routing "
        "is pinned by an ADR-0002 amendment. Burn live once that decision lands."
    )


def test_k6_surface_response_emitted_carries_delivered_via_and_attention_channel(
    live_real_codex_happy: dict[str, Any],
) -> None:
    """K6: ``surface.response_emitted.payload.delivered_via`` lists physical surfaces; ``attention_channel`` carries the L3 logical channel."""
    emitted = _payloads(live_real_codex_happy, "surface.response_emitted")
    assert len(emitted) == 1
    payload = emitted[0]
    delivered_via = payload["delivered_via"]
    assert isinstance(delivered_via, list)
    assert delivered_via, "delivered_via empty — no physical surface received the response"
    assert set(delivered_via) <= {"voice", "banner", "stdout"}
    assert payload["attention_channel"] == "voice_notify"


# --- L — Evidence ladder enforcement (live-Codex variants) ----------------


def test_l1_no_verify_command_emits_observation_only_slot_with_limitation(
    real_python_repo: Path,
) -> None:
    """L1: ``task.created`` without ``verify_command`` → one ``action.result_observed(observation)``; Artifact Claim at observed + Limitation Claim per §8.5 rule 6; no ``task.verified``."""
    pytest.skip(
        "L1 lives in tests/scenarios/test_real_codex_empty_diff.py (route B "
        "no-verify_command + route A true-empty-diff, both live-green); this "
        "stub keeps L1 in the Tier-2 enumeration."
    )


def test_l2_verify_pytest_pass_plus_reviewer_ok_emits_task_verified(
    live_real_codex_happy: dict[str, Any],
) -> None:
    """L2: ``verify_command="pytest"`` + exit 0 + reviewer ok → TWO ``action.result_observed`` (observation + verification); Postcondition Claim with verified+reported evidence rows; ``task.verified`` emitted."""
    observed = _payloads(live_real_codex_happy, "action.result_observed")
    verify_semantics = {
        p["semantics"]
        for p in observed
        if p.get("semantics") in {"observation", "verification"}
    }
    assert {"observation", "verification"} <= verify_semantics
    verification = [p for p in observed if p.get("semantics") == "verification"]
    assert verification
    assert verification[0].get("error") is None

    claims = _payloads(live_real_codex_happy, "claim.created")
    postcondition_ids = {c["claim_id"] for c in claims if c["type"] == "Postcondition"}
    assert postcondition_ids, "no Postcondition claim created"
    evidence = _payloads(live_real_codex_happy, "evidence.attached")
    verified_rows = [
        e
        for e in evidence
        if e["claim_id"] in postcondition_ids and e["level"] == "verified"
    ]
    assert verified_rows, "Postcondition claim has no verified-level evidence"

    verified = _payloads(live_real_codex_happy, "task.verified")
    assert len(verified) == 1
    assert verified[0]["task_id"] == "task_X"
    assert verified[0]["by"] == "jarvis"
