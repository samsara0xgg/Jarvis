"""Wave 5 live burn: background Codex workers, a real cancel, and the pump.

ADR-0008 §8 build order Step 4. Every turn here runs the real composition
root against a real on-disk Event Log, the real cloud LLM behind the
OpenRouter proxy preset, and a real ``codex app-server`` subprocess — with
the Wave-5 switches ON (``realtime.actions.true_async_workers`` and
``realtime.input.intent_pump``), which is the configuration the hermetic
suites in ``tests/integration/test_wave5_*.py`` can only approximate.

Three burns, one per acceptance property of the Step-4 row:

1. ``test_live_background_worker_answers_a_second_utterance`` — a real
   ``spawn_worker`` dispatch returns while Codex is still running, and a
   second utterance is answered by the real LLM inside that window.
2. ``test_live_cancel_kills_the_codex_process_and_frees_the_repo`` — a
   cancel really terminates the OS process (``os.kill(pid, 0)`` raises),
   writes exactly one canonical terminal, and the runner-owned finalizer
   restores the pre-task stash and releases the repository quarantine.
3. ``test_live_crash_after_claim_recovers_once_without_replaying_history``
   — an input claimed by a daemon that then died is adopted exactly once by
   the restarted pump, pre-adoption utterances are never replayed, and
   nothing is dispatched twice.

Timings are written to ``~/.jarvis/realtime-run/wave5-burn.json`` and the
diagnostic trace to ``~/.jarvis/realtime-run/wave5-trace.jsonl`` so the burn
document quotes measured numbers rather than impressions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.execution.action_runner import CancelAccepted
from jarvis.runtime import bootstrap_runtime_app, drive_turn
from jarvis.runtime.inherent_loop import (
    _boot_intent_pump_in_thread,
    _start_intent_pump,
)
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl
from jarvis.state.event_log import emit_event, open_event_log
from jarvis.state.input_claim import InputClaimed, claim_input_once
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from jarvis.runtime import JarvisRuntime, RunTurnResult
    from jarvis.shared import Event

pytestmark = pytest.mark.live_llm

_BURN_DIR = Path.home() / ".jarvis" / "realtime-run"
_TRACE_PATH = _BURN_DIR / "wave5-trace.jsonl"
_RESULTS_PATH = _BURN_DIR / "wave5-burn.json"

_PROMPT_CODEX = "昨天那个 task 给 codex 跑一下 做完审核了再告诉我"
_PROMPT_CHAT = "用一句话说明天空为什么是蓝色的"
_PROMPT_HISTORY_A = "这是上礼拜说过的话 不要再答一次"
_PROMPT_HISTORY_B = "这也是上礼拜说过的话"
_PROMPT_RECOVERED = "用一句话说明潮汐是怎么形成的"

_CODEX_GOAL = (
    "Add a module-level docstring to tests/test_demo.py explaining what it "
    "verifies, and create a top-level NOTES.md describing the demo project. "
    "Do not change any test logic."
)

_WORKER_TRIGGER_TIMEOUT_S = 900.0
"""Per-trigger wait for the turn that owns a background worker.

Production's ``drive_turn`` default is 5 s, which is fine while a declared-
async tool is awaited inside ``decide``. With ``true_async_workers`` on the
wait spans the whole Codex turn, so the burn passes the budget the ADR's
turn actually needs.
"""

_RUNNING_WAIT_S = 300.0
"""How long the burn waits for the write-exclusive ActionRun to open."""

_WORKER_TURN_WAIT_S = 1800.0
"""How long the burn waits for the whole Codex turn to unwind."""

_CANCEL_BUDGET_S = 180.0
"""Budget for `cancel request -> quiesced -> terminal` on a live Codex turn."""

_PID_WAIT_S = 180.0
"""How long the burn waits for the real ``codex app-server`` child to appear."""

_PUMP_POLL_INTERVAL_S = 0.01
_PUMP_QUEUE_CAPACITY = 8
_PUMP_MAX_CONCURRENT_TURNS = 2
_RECOVERY_BUDGET_S = 300.0
_PUMP_SETTLE_S = 8.0
"""Quiet window after the recovered answer in which a double dispatch would show."""

_RESPONSE_TERMINALS = ("response.completed", "response.cancelled", "response.failed")
_ACTION_TERMINALS = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)
_PS_FIELDS = 3
_STASH_SHA_LEN = 40


# --- harness ---------------------------------------------------------------


def _wave5_overlay(tmp_path: Path) -> Path:
    """Build a repo overlay whose config has every Wave-5 switch turned on.

    The burn must exercise the flag-ON graph, and flipping the shipped file
    would be a rollout change smuggled in as a test fixture.
    ``bootstrap_runtime_app`` derives the prompt, pricing and Tier-0 paths
    from the config file's grandparent, so the overlay mirrors that whole
    shape and every file except ``jarvis.yaml`` is the real repository's,
    copied byte-for-byte.

    Returns the overlay's ``config/jarvis.yaml`` path.
    """
    source = repo_root()
    overlay = tmp_path / "overlay"
    for relative in (
        Path("config") / "tier0_patterns.yaml",
        Path("config") / "confirm_grammar.yaml",
        Path("config") / "file_targets.yaml",
        Path("prompts") / "jarvis_v1.md",
        Path("data") / "pricing.json",
    ):
        target = overlay / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / relative).read_bytes())

    shipped = yaml.safe_load((source / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    realtime = shipped["realtime"]
    realtime["enabled"] = True
    realtime["concurrency_safety"]["transactional_event_append"] = True
    realtime["concurrency_safety"]["lifecycle_terminal_cas"] = True
    realtime["response"]["response_run_lifecycle"] = True
    realtime["response"]["independent_response_cancel"] = True
    realtime["actions"]["action_runner"] = True
    realtime["actions"]["true_async_workers"] = True
    realtime["actions"]["max_concurrent_runs"] = 4
    realtime["input"]["intent_pump"] = True
    realtime["input"]["queue_capacity"] = _PUMP_QUEUE_CAPACITY
    realtime["input"]["max_concurrent_turns"] = _PUMP_MAX_CONCURRENT_TURNS
    path = overlay / "config" / "jarvis.yaml"
    path.write_text(yaml.safe_dump(shipped, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def live_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[JarvisRuntime]:
    """Bootstrap a real runtime with the Wave-4 and Wave-5 switches on."""
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path))
    _BURN_DIR.mkdir(parents=True, exist_ok=True)
    # The env var, not a direct call: `bootstrap_runtime_app` configures the
    # exporter itself from `JARVIS_REALTIME_TRACE_JSONL`, and passing it any
    # other way means bootstrap immediately turns the exporter back off.
    monkeypatch.setenv("JARVIS_REALTIME_TRACE_JSONL", str(_TRACE_PATH))
    runtime = bootstrap_runtime_app(
        config_path=_wave5_overlay(tmp_path),
        runtime_root=tmp_path / "root",
    )
    assert runtime.action_flags.action_runner is True
    assert runtime.action_flags.true_async_workers is True
    assert runtime.input_flags.intent_pump is True
    assert runtime.response_flags.response_run_lifecycle is True
    assert runtime.action_runner is not None
    assert runtime.tool_registry.background_async is True
    try:
        yield runtime
    finally:
        if runtime.action_runner is not None:
            # `cancel=True` is what the daemon passes: a burn that failed
            # mid-turn must not leave a real Codex subprocess behind.
            runtime.action_runner.shutdown(cancel=True, reason="burn_teardown")
        runtime.conn.close()
        configure_realtime_trace_jsonl(None)


@pytest.fixture
def throwaway_repo(tmp_path: Path) -> Path:
    """A disposable git repo Codex may freely mutate.

    Deliberately under ``tmp_path`` and never the checkout this burn runs
    from: `spawn_worker` stashes the working tree before it starts, so
    pointing it at a live worktree would set aside real work.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)  # noqa: S607
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_demo.py").write_text(
        "def test_truthy() -> None:\n    assert True\n",
        encoding="utf-8",
    )
    commit_argv = [
        "git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
        "commit", "-q", "-m", "init",
    ]
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)  # noqa: S603, S607
    subprocess.run(commit_argv, check=True)  # noqa: S603
    return repo


def _emit_intent(runtime: JarvisRuntime, turn_id: str, transcript: str) -> Event:
    """Emit the real ``surface.user_intent`` row a turn is driven from."""
    return emit_event(
        runtime.conn,
        type="surface.user_intent",
        payload={
            "transcript": transcript,
            "turn_id": turn_id,
            "channel": "cli_stdin",
            "language": "zh-CN",
        },
        correlation={"turn_id": turn_id},
    )


def _seed_yesterdays_task(runtime: JarvisRuntime, repo: Path, task_id: str) -> None:
    """Seed exactly one open task created 26 h ago, against the throwaway repo."""
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": task_id,
            "goal": _CODEX_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": "true",
        },
        ts_epoch_ms=int(time.time() * 1000) - 26 * 3600 * 1000,
        correlation={"task_id": task_id},
    )


def _rows(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return ``ts_epoch_ms`` + decoded payload for one event type."""
    return [
        {"ts_epoch_ms": int(row[0]), "payload": json.loads(row[1])}
        for row in conn.execute(
            "SELECT ts_epoch_ms, payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def _rows_of_turn(
    conn: sqlite3.Connection,
    event_type: str,
    turn_id: str,
) -> list[dict[str, Any]]:
    """Return one event type's rows correlated to a single turn."""
    return [
        {"ts_epoch_ms": int(row[0]), "payload": json.loads(row[1])}
        for row in conn.execute(
            "SELECT ts_epoch_ms, payload_json FROM events WHERE type = ? "
            "AND json_extract(correlation_json, '$.turn_id') = ? ORDER BY id ASC",
            (event_type, turn_id),
        )
    ]


def _rows_of_action(
    conn: sqlite3.Connection,
    event_type: str,
    action_id: str,
) -> list[dict[str, Any]]:
    """Return one event type's rows whose payload names a single action."""
    return [row for row in _rows(conn, event_type) if row["payload"].get("action_id") == action_id]


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


def _terminals_of_action(conn: sqlite3.Connection, action_id: str) -> list[dict[str, Any]]:
    """Return every canonical action terminal belonging to one action."""
    found: list[dict[str, Any]] = []
    for event_type in _ACTION_TERMINALS:
        found.extend(
            {**row, "type": event_type}
            for row in _rows_of_action(conn, event_type, action_id)
        )
    return found


def _ordered_types(conn: sqlite3.Connection) -> list[str]:
    """Return every event type in append order."""
    return [str(row[0]) for row in conn.execute("SELECT type FROM events ORDER BY id ASC")]


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float) -> bool:
    """Poll ``predicate`` until it is true or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _drive_on_own_connection(
    runtime: JarvisRuntime,
    *,
    user_intent_event: Event,
    trigger_timeout_s: float,
) -> RunTurnResult:
    """Run ``drive_turn`` the way the daemon's turn worker thread runs it."""
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(
            replace(runtime, conn=conn),
            user_intent_event=user_intent_event,
            available_surfaces=frozenset(),
            streaming_enabled=True,
            trigger_timeout_s=trigger_timeout_s,
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


@contextlib.contextmanager
def _drained_pool(runtime: JarvisRuntime, *, workers: int) -> Iterator[ThreadPoolExecutor]:
    """A thread pool whose live ActionRuns are drained when the burn fails.

    Without the drain, a failed assertion would leave a real Codex
    subprocess running and block the pool's own shutdown for the whole
    900 s turn budget, turning one bad assert into a stalled burn.
    """
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        yield pool
    except BaseException:
        if runtime.action_runner is not None:
            runtime.action_runner.shutdown(cancel=True, reason="burn_aborted", timeout_s=60.0)
        raise
    finally:
        pool.shutdown(wait=True)


def _await_write_exclusive_running(
    conn: sqlite3.Connection,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    """Block until a write-exclusive ActionRun is on the log; return its row."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rows = [
            row
            for row in _rows(conn, "action.running")
            if row["payload"].get("resource_mode") == "write_exclusive"
        ]
        if rows:
            return rows[0]
        time.sleep(0.02)
    pytest.fail(f"no write-exclusive action.running within {timeout_s}s — Codex never dispatched")


# --- the real OS process -----------------------------------------------------


def _codex_app_server_pids() -> frozenset[int]:
    """Return the pids of every ``codex app-server`` child of this process.

    The production code exposes no handle on the subprocess:
    ``CodexAppServerClient`` owns its ``Popen`` privately and no event
    carries the pid, so the only honest way to check "the process really
    died" is to ask the OS about our own children.
    """
    listing = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,command="],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    mine = os.getpid()
    found: set[int] = set()
    for line in listing.stdout.splitlines():
        parts = line.split(maxsplit=2)
        if len(parts) != _PS_FIELDS:
            continue
        pid_raw, ppid_raw, command = parts
        if not pid_raw.isdigit() or not ppid_raw.isdigit() or int(ppid_raw) != mine:
            continue
        if "codex" in command and "app-server" in command:
            found.add(int(pid_raw))
    return frozenset(found)


def _pid_alive(pid: int) -> bool:
    """Return whether the OS still knows this pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover — our own child is always signalable.
        return True
    return True


def _await_codex_pid(*, timeout_s: float) -> int:
    """Block until exactly one ``codex app-server`` child exists; return its pid."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pids = _codex_app_server_pids()
        if len(pids) == 1:
            return next(iter(pids))
        time.sleep(0.05)
    pytest.fail(
        f"no single `codex app-server` child of pid {os.getpid()} within {timeout_s}s "
        f"(found {sorted(_codex_app_server_pids())})",
    )


# --- results ----------------------------------------------------------------


def _git_head_sha() -> str:
    """Return the worktree's HEAD sha, so a number can be traced to a build."""
    shown = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_root()), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    return shown.stdout.strip() if shown.returncode == 0 else "unknown"


def _record(name: str, measurements: dict[str, Any]) -> None:
    """Append one burn's measurements to the burn results file."""
    existing: dict[str, Any] = {}
    if _RESULTS_PATH.exists():
        existing = json.loads(_RESULTS_PATH.read_text(encoding="utf-8"))
    existing[name] = {
        **measurements,
        "git_head_sha": _git_head_sha(),
        "recorded_at_wall_clock": datetime.now(tz=UTC).isoformat(timespec="milliseconds"),
    }
    _RESULTS_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _now_wall_clock() -> str:
    """Return the absolute wall time a burn started, for the results file."""
    return datetime.now(tz=UTC).isoformat(timespec="milliseconds")


# --- burn 1: background dispatch + a second utterance -----------------------

_WORKER_TURN_ID = "T-w5-bg-worker"
_SECOND_TURN_ID = "T-w5-bg-chat"


@pytest.mark.live_codex
def test_live_background_worker_answers_a_second_utterance(
    live_runtime: JarvisRuntime,
    throwaway_repo: Path,
) -> None:
    """A real Codex worker keeps running while a second real turn is answered.

    ADR-0008 D9: "submit returns after dispatch, not after the action
    finishes". The burn is the whole point of the row — with the Wave-4B
    inline dispatch, the second utterance could not have been answered at
    all until Codex was done, because `dispatch` held the turn.
    """
    started_at = _now_wall_clock()
    _seed_yesterdays_task(live_runtime, throwaway_repo, "task_w5_bg")
    conn = live_runtime.conn

    worker_intent = _emit_intent(live_runtime, _WORKER_TURN_ID, _PROMPT_CODEX)
    worker_began = time.monotonic()
    with _drained_pool(live_runtime, workers=1) as pool:
        future = pool.submit(
            _drive_on_own_connection,
            live_runtime,
            user_intent_event=worker_intent,
            trigger_timeout_s=_WORKER_TRIGGER_TIMEOUT_S,
        )
        running = _await_write_exclusive_running(conn, timeout_s=_RUNNING_WAIT_S)
        worker_action_id = str(running["payload"]["action_id"])
        assert str(running["payload"]["resource_keys"]).startswith("repo:")

        # The dispatch has already returned: Codex is running, and nothing
        # has quiesced, reported, or terminalized for it.
        assert _rows_of_action(conn, "worker.quiesced", worker_action_id) == []
        assert _rows_of_action(conn, "worker.reported", worker_action_id) == []
        assert _terminals_of_action(conn, worker_action_id) == []
        assert not future.done(), "the Codex turn finished before the second utterance"

        second_intent = _emit_intent(live_runtime, _SECOND_TURN_ID, _PROMPT_CHAT)
        second_began = time.monotonic()
        second = drive_turn(
            live_runtime,
            user_intent_event=second_intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        second_total_ms = int((time.monotonic() - second_began) * 1000)

        # Real output, produced with the worker still alive underneath it.
        assert second.response_plan.text.strip()
        assert _rows_of_action(conn, "worker.quiesced", worker_action_id) == []
        assert _terminals_of_action(conn, worker_action_id) == []
        assert not future.done(), "the Codex worker finished before the second turn did"

        worker = future.result(timeout=_WORKER_TURN_WAIT_S)
    worker_total_ms = int((time.monotonic() - worker_began) * 1000)

    # The second turn's own L5 rows name its own turn, and its first
    # user-visible output landed while the worker was running.
    intent_rows = _rows_of_turn(conn, "surface.user_intent", _SECOND_TURN_ID)
    open_rows = _rows_of_turn(conn, "surface.response_open", _SECOND_TURN_ID)
    emitted_rows = _rows_of_turn(conn, "surface.response_emitted", _SECOND_TURN_ID)
    assert len(intent_rows) == 1
    assert len(open_rows) == 1, "the second turn produced no surface.response_open"
    assert len(emitted_rows) == 1
    utterance_to_first_output_ms = open_rows[0]["ts_epoch_ms"] - intent_rows[0]["ts_epoch_ms"]

    # Exactly one canonical terminal and one cleanup for the worker, with
    # cleanup strictly after quiescence — the runner-owned finalizer.
    terminals = _terminals_of_action(conn, worker_action_id)
    assert len(terminals) == 1, terminals
    quiesced = _rows_of_action(conn, "worker.quiesced", worker_action_id)
    assert quiesced, "the worker never quiesced"
    cleanups = _rows_of_action(conn, "action.cleanup_completed", worker_action_id)
    assert len(cleanups) == 1, cleanups
    types = _ordered_types(conn)
    assert types.index("worker.quiesced") < types.index("action.cleanup_completed")
    assert live_runtime.action_runner is not None
    assert live_runtime.action_runner.leases.live_scopes() == ()

    running_to_cleanup_ms = cleanups[0]["ts_epoch_ms"] - running["ts_epoch_ms"]
    # The dispatch really did return early: the second turn finished, start
    # to end, inside the worker's own lifetime.
    assert running_to_cleanup_ms > second_total_ms

    _record(
        "background_second_utterance",
        {
            "started_at_wall_clock": started_at,
            "worker_transcript": _PROMPT_CODEX,
            "second_transcript": _PROMPT_CHAT,
            "worker_action_id": worker_action_id,
            "worker_resource_key": str(running["payload"]["resource_keys"]),
            "worker_terminal_type": terminals[0]["type"],
            "worker_running_to_cleanup_ms": running_to_cleanup_ms,
            "worker_turn_total_ms": worker_total_ms,
            "worker_iterations": worker.iterations,
            "worker_quiesced_rows": len(quiesced),
            "cleanup_verification_outcome": cleanups[0]["payload"]["verification_outcome"],
            "second_turn_utterance_to_first_output_ms": utterance_to_first_output_ms,
            "second_turn_total_ms": second_total_ms,
            "second_turn_answer_chars": len(second.response_plan.text),
            "second_turn_iterations": second.iterations,
            "worker_alive_through_second_turn": True,
        },
    )


# --- burn 2: cancel a live Codex process ------------------------------------

_CANCEL_TURN_ID = "T-w5-cancel"
_DIRTY_FILE_NAME = "allen-notes.txt"


def _assert_one_cancel_terminal(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    turn_id: str,
) -> tuple[list[dict[str, Any]], str]:
    """Assert one canonical terminal, that it is the cancel, and return its stash ref.

    ADR-0008 D9 gives ``action.cancelled`` to whoever asked for the cancel,
    and the handler deliberately writes nothing on that path — so a second
    terminal here would mean the worker raced its own canceller.
    """
    terminals = _terminals_of_action(conn, action_id)
    assert len(terminals) == 1, terminals
    assert terminals[0]["type"] == "action.cancelled"
    cancelled = terminals[0]["payload"]
    assert cancelled["reason"] == "user_stop"
    assert cancelled["cancellation_mode"] == "terminate_process"
    assert cancelled["requested_by_turn_id"] == turn_id
    stash_ref = cancelled.get("stash_ref")
    assert isinstance(stash_ref, str), cancelled
    assert len(stash_ref) == _STASH_SHA_LEN, stash_ref
    return terminals, stash_ref


@pytest.mark.live_codex
def test_live_cancel_kills_the_codex_process_and_frees_the_repo(
    live_runtime: JarvisRuntime,
    throwaway_repo: Path,
) -> None:
    """A cancel really stops the child process and the finalizer really cleans up.

    ADR-0008 D9. Three facts that used to be collapsed are checked apart
    here: the OS process is gone, exactly one canonical terminal exists, and
    the runner's finalizer put Allen's pre-task stash back before releasing
    the repository.
    """
    started_at = _now_wall_clock()
    # An uncommitted file, so `isolate_pretask_changes` really stashes and
    # the finalizer really has something to restore.
    dirty = throwaway_repo / _DIRTY_FILE_NAME
    dirty.write_text("uncommitted work that must survive the cancel\n", encoding="utf-8")
    _seed_yesterdays_task(live_runtime, throwaway_repo, "task_w5_cancel")
    conn = live_runtime.conn

    intent = _emit_intent(live_runtime, _CANCEL_TURN_ID, _PROMPT_CODEX)
    with _drained_pool(live_runtime, workers=2) as pool:
        turn_future = pool.submit(
            _drive_on_own_connection,
            live_runtime,
            user_intent_event=intent,
            trigger_timeout_s=_WORKER_TRIGGER_TIMEOUT_S,
        )
        running = _await_write_exclusive_running(conn, timeout_s=_RUNNING_WAIT_S)
        action_id = str(running["payload"]["action_id"])
        runner = live_runtime.action_runner
        assert runner is not None

        # A real child process exists, which also orders the check below:
        # `spawn_worker_handler` stashes (step 5) before it spawns Codex
        # (step 6), so a live `app-server` means the stash already happened.
        pid = _await_codex_pid(timeout_s=_PID_WAIT_S)
        assert _pid_alive(pid)
        assert not dirty.exists(), "the pre-task stash did not clear the tree"
        context = runner.context_of(action_id)
        assert context is not None
        assert context.cancellation_mode == "terminate_process"

        requested_at = time.monotonic()
        cancel_future = pool.submit(
            runner.cancel_action,
            action_id,
            reason="user_stop",
            timeout_s=_CANCEL_BUDGET_S,
            cancellation_mode=context.cancellation_mode,
        )
        # Poll the OS, not the log: the claim under test is that the child
        # is really gone, not that a row says somebody asked it to go.
        assert _wait_until(lambda: not _pid_alive(pid), timeout_s=_CANCEL_BUDGET_S), (
            f"codex pid {pid} still alive {_CANCEL_BUDGET_S}s after the cancel request"
        )
        process_gone_ms = int((time.monotonic() - requested_at) * 1000)
        outcome = cancel_future.result(timeout=_CANCEL_BUDGET_S)
        terminal_ms = int((time.monotonic() - requested_at) * 1000)
        assert isinstance(outcome, CancelAccepted), outcome

        turn = turn_future.result(timeout=_WORKER_TURN_WAIT_S)
    turn_unwound_ms = int((time.monotonic() - requested_at) * 1000)

    terminals, stash_ref = _assert_one_cancel_terminal(conn, action_id, turn_id=_CANCEL_TURN_ID)

    # The runner-owned finalizer: quiescence, then stash restore, then the
    # cleanup terminal, then a free repository.
    quiesced = _rows_of_action(conn, "worker.quiesced", action_id)
    assert quiesced, "the cancelled worker never quiesced"
    cleanups = _rows_of_action(conn, "action.cleanup_completed", action_id)
    assert len(cleanups) == 1, cleanups
    types = _ordered_types(conn)
    assert types.index("worker.quiesced") < types.index("action.cleanup_completed")
    assert dirty.exists(), "the finalizer did not restore the pre-task stash"
    assert live_runtime.action_runner is not None
    assert live_runtime.action_runner.leases.live_scopes() == ()

    # The turn itself ended as a limitation, exactly once.
    assert len(_rows(conn, "response.started")) == 1
    response_terminals = [row for kind in _RESPONSE_TERMINALS for row in _rows(conn, kind)]
    assert len(response_terminals) == 1
    assert turn.response_plan.text.strip()

    _record(
        "cancel_live_process",
        {
            "started_at_wall_clock": started_at,
            "transcript": _PROMPT_CODEX,
            "worker_action_id": action_id,
            "worker_resource_key": str(running["payload"]["resource_keys"]),
            "codex_pid": pid,
            "cancel_request_to_process_gone_ms": process_gone_ms,
            "cancel_request_to_terminal_ms": terminal_ms,
            "cancel_request_to_turn_unwound_ms": turn_unwound_ms,
            "terminal_to_cleanup_ms": (
                cleanups[0]["ts_epoch_ms"] - terminals[0]["ts_epoch_ms"]
            ),
            "canonical_terminals": len(terminals),
            "terminal_type": terminals[0]["type"],
            "stash_ref_len": len(stash_ref),
            "stash_restored": True,
            "cleanup_verification_outcome": cleanups[0]["payload"]["verification_outcome"],
            "live_scopes_after_cleanup": 0,
            "worker_quiesced_rows": len(quiesced),
            "turn_answer_chars": len(turn.response_plan.text),
            "turn_answer_text": turn.response_plan.text.strip(),
            "turn_iterations": turn.iterations,
        },
    )


# --- burn 3: crash after claim, no replay, no double dispatch ---------------

_HISTORY_TURN_IDS = ("T-w5-hist-a", "T-w5-hist-b")
_CRASHED_TURN_ID = "T-w5-crashed"


async def _restart_the_pump(runtime: JarvisRuntime) -> dict[str, int | None]:
    """Start the real intent pump and watch a recovered turn reach an answer.

    Nothing is stubbed: the recovered trigger is driven by the production
    ``_intent_worker`` -> ``_drive_turn_in_worker_thread`` -> ``drive_turn``
    path against the real LLM, which is the difference between this and the
    hermetic pump suite.
    """
    began = time.monotonic()
    tasks = await _start_intent_pump(
        runtime,
        poll_interval_s=_PUMP_POLL_INTERVAL_S,
        queue_capacity=_PUMP_QUEUE_CAPACITY,
        max_concurrent_turns=_PUMP_MAX_CONCURRENT_TURNS,
    )
    recovery_ms: int | None = None
    answer_ms: int | None = None
    try:
        deadline = time.monotonic() + _RECOVERY_BUDGET_S
        while time.monotonic() < deadline:
            if recovery_ms is None and _count(runtime.conn, "response.started") >= 1:
                recovery_ms = int((time.monotonic() - began) * 1000)
            if _count(runtime.conn, "surface.response_emitted") >= 1:
                answer_ms = int((time.monotonic() - began) * 1000)
                break
            await asyncio.sleep(_PUMP_POLL_INTERVAL_S)
        # Quiet window: a second dispatch of the same claim would land here.
        await asyncio.sleep(_PUMP_SETTLE_S)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"restart_to_recovery_ms": recovery_ms, "restart_to_answer_ms": answer_ms}


def test_live_crash_after_claim_recovers_once_without_replaying_history(
    live_runtime: JarvisRuntime,
) -> None:
    """A claim orphaned by a dead daemon is adopted once, and history stays history.

    ADR-0008 D8's three acceptance properties in one live run: crash after
    claim, historical inputs not replayed, no double dispatch. The "crash"
    is the honest one — the claim is durable on disk and the consumer that
    made it is gone, which is exactly the state a killed daemon leaves.
    """
    started_at = _now_wall_clock()
    conn = live_runtime.conn

    # Two utterances from before this consumer ever existed.
    for turn_id, transcript in zip(
        _HISTORY_TURN_IDS,
        (_PROMPT_HISTORY_A, _PROMPT_HISTORY_B),
        strict=True,
    ):
        _emit_intent(live_runtime, turn_id, transcript)

    # Boot 1: the daemon adopts the input stream. The watermark it writes is
    # what makes the two rows above history rather than a backlog.
    boot = _boot_intent_pump_in_thread(live_runtime.runtime_paths.event_log)
    assert boot.adopted_now is True
    assert boot.recovered == (), boot.recovered
    assert _count(conn, "consumer.adopted") == 1

    # A real utterance, durably claimed — and then the daemon dies before it
    # dispatches anything. The claim survives; the in-memory consumer does not.
    crashed_intent = _emit_intent(live_runtime, _CRASHED_TURN_ID, _PROMPT_RECOVERED)
    claim = claim_input_once(conn, trigger_event=crashed_intent)
    assert isinstance(claim, InputClaimed)
    assert claim.turn_id == _CRASHED_TURN_ID
    assert _count(conn, "turn.started") == 1
    assert _count(conn, "surface.response_emitted") == 0

    measured = asyncio.run(_restart_the_pump(live_runtime))

    # Adopted exactly once: one watermark, one claim, one answer.
    assert _count(conn, "consumer.adopted") == 1
    turn_starts = _rows(conn, "turn.started")
    assert len(turn_starts) == 1, turn_starts
    assert turn_starts[0]["payload"]["turn_id"] == _CRASHED_TURN_ID
    assert _count(conn, "response.started") == 1
    assert len([row for kind in _RESPONSE_TERMINALS for row in _rows(conn, kind)]) == 1

    emitted = _rows(conn, "surface.response_emitted")
    assert len(emitted) == 1, emitted
    assert emitted[0]["payload"]["turn_id"] == _CRASHED_TURN_ID
    answer = str(emitted[0]["payload"]["text"])
    assert answer.strip()

    # The watermark suppressed the history: no claim, no turn, no answer.
    for turn_id in _HISTORY_TURN_IDS:
        assert _rows_of_turn(conn, "turn.started", turn_id) == []
        assert _rows_of_turn(conn, "surface.response_emitted", turn_id) == []

    assert measured["restart_to_recovery_ms"] is not None, "the claim was never recovered"
    assert measured["restart_to_answer_ms"] is not None, "the recovered turn never answered"

    _record(
        "crash_after_claim_recovery",
        {
            "started_at_wall_clock": started_at,
            "transcript": _PROMPT_RECOVERED,
            "historical_intents": len(_HISTORY_TURN_IDS),
            "history_replayed": 0,
            "crashed_turn_id": _CRASHED_TURN_ID,
            "adoption_row_id": boot.adoption_row_id,
            "consumer_adopted_rows": _count(conn, "consumer.adopted"),
            "turn_started_rows": len(turn_starts),
            "responses_emitted": len(emitted),
            "restart_to_recovery_ms": measured["restart_to_recovery_ms"],
            "restart_to_answer_ms": measured["restart_to_answer_ms"],
            "settle_after_answer_s": _PUMP_SETTLE_S,
            "answer_chars": len(answer),
        },
    )
