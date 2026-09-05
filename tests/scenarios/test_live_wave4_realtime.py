"""Wave 4 live burn: real turns through the ResponseRun + ActionRunner path.

ADR-0008 §8 Steps 2 and 3, §9.5. This is a system test, not a unit test:
every turn runs the real composition root against a real on-disk Event Log
with the real cloud LLM behind the OpenRouter proxy preset, and with both
Wave-4 switches ON — which is the configuration the hermetic suite can only
approximate.

Three turns, each pinning one property the hermetic tests cannot:

1. ``test_live_conversational_turn`` — a real answer produces exactly one
   ``response.started`` and one ``response.completed``, and the L5 rows carry
   the L3 response identity.
2. ``test_live_cancelled_turn_leaves_actions_alone`` — a cancel issued while
   a real model call is in flight terminalizes exactly once and writes no
   ``action.*`` terminal (D1 / D10).
3. ``test_live_action_turn_through_the_runner`` — a real tool action is
   dispatched through the ActionRunner, reaches its canonical terminal
   through the same-transaction CAS, quiesces, and is released by the turn's
   cleanup finalizer.

Timings are written to ``~/.jarvis/realtime-run/wave4-burn.json`` and the
diagnostic trace to ``~/.jarvis/realtime-run/wave4-trace.jsonl`` so the burn
document quotes measured numbers rather than impressions.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from jarvis.decision.response_run import ResponseCancelledError
from jarvis.runtime import (
    bootstrap_runtime_app,
    drive_turn,
    make_response_cancel_callable,
)
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl
from jarvis.state.event_log import emit_event, open_event_log
from tests.canary._helpers import repo_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from jarvis.runtime import JarvisRuntime
    from jarvis.shared import Event

pytestmark = pytest.mark.live_llm

_BURN_DIR = Path.home() / ".jarvis" / "realtime-run"
_TRACE_PATH = _BURN_DIR / "wave4-trace.jsonl"
_RESULTS_PATH = _BURN_DIR / "wave4-burn.json"
_PROMPT_CHAT = "用一句话说明天空为什么是蓝色的"
_PROMPT_LONG = "用三段话讲讲量子纠缠的历史 从 EPR 论文讲到贝尔不等式的实验验证"
_PROMPT_TIME = "现在几点了"
_PROMPT_CODEX = "昨天那个 task 给 codex 跑一下 做完审核了再告诉我"
_CANCEL_SETTLE_S = 2.0
"""How long the cancel burn lets a real generation run before stopping it."""

_RESPONSE_TERMINALS = ("response.completed", "response.cancelled", "response.failed")
_ACTION_TERMINALS = (
    "action.result_observed",
    "action.failed",
    "action.timeout_assumed",
    "action.cancelled",
)


# --- harness ---------------------------------------------------------------


def _wave4_overlay(tmp_path: Path) -> Path:
    """Build a repo overlay whose config has every Wave-4 switch turned on.

    The burn must exercise the flag-ON graph, and flipping the shipped file
    would be a rollout change smuggled in as a test fixture. ``bootstrap_
    runtime_app`` derives the prompt, pricing and Tier-0 paths from the config
    file's grandparent, so the overlay mirrors that whole shape and every file
    except ``jarvis.yaml`` is the real repository's, copied byte-for-byte.

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
    realtime["actions"]["max_concurrent_runs"] = 4
    path = overlay / "config" / "jarvis.yaml"
    path.write_text(yaml.safe_dump(shipped, allow_unicode=True), encoding="utf-8")
    return path


@pytest.fixture
def live_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[JarvisRuntime]:
    """Bootstrap a real runtime with both Wave-4 switches on."""
    monkeypatch.setenv("JARVIS_RUNTIME_ROOT", str(tmp_path))
    _BURN_DIR.mkdir(parents=True, exist_ok=True)
    # The env var, not a direct call: `bootstrap_runtime_app` configures the
    # exporter itself from `JARVIS_REALTIME_TRACE_JSONL`, and passing it any
    # other way means bootstrap immediately turns the exporter back off.
    monkeypatch.setenv("JARVIS_REALTIME_TRACE_JSONL", str(_TRACE_PATH))
    runtime = bootstrap_runtime_app(
        config_path=_wave4_overlay(tmp_path),
        runtime_root=tmp_path / "root",
    )
    assert runtime.response_flags.response_run_lifecycle is True
    assert runtime.response_flags.independent_response_cancel is True
    assert runtime.action_flags.action_runner is True
    assert runtime.action_runner is not None
    try:
        yield runtime
    finally:
        if runtime.action_runner is not None:
            runtime.action_runner.shutdown()
        runtime.conn.close()
        configure_realtime_trace_jsonl(None)


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


def _rows(conn: sqlite3.Connection, event_type: str) -> list[dict[str, Any]]:
    """Return ``ts_epoch_ms`` + decoded payload for one event type."""
    return [
        {"ts_epoch_ms": int(row[0]), "payload": json.loads(row[1])}
        for row in conn.execute(
            "SELECT ts_epoch_ms, payload_json FROM events WHERE type = ? ORDER BY id ASC",
            (event_type,),
        )
    ]


def _count(conn: sqlite3.Connection, event_type: str) -> int:
    """Count rows of one event type."""
    row = conn.execute("SELECT COUNT(*) FROM events WHERE type = ?", (event_type,)).fetchone()
    assert row is not None
    return int(row[0])


def _terminal_rows(conn: sqlite3.Connection, types: tuple[str, ...]) -> list[dict[str, Any]]:
    """Return every row of a terminal family, in append order."""
    found: list[dict[str, Any]] = []
    for event_type in types:
        found.extend({**row, "type": event_type} for row in _rows(conn, event_type))
    return found


def _first_model_output_ms(conn: sqlite3.Connection, started_ms: int) -> int | None:
    """Return ms from the run opening to the first provider response.

    The route is non-streaming, so "first model output" is the moment the
    provider call returned — which L3 stamps as ``cost.recorded`` immediately
    after it. Reporting that rather than a fabricated token time keeps the
    number honest about what this build actually measures.
    """
    costs = _rows(conn, "cost.recorded")
    return costs[0]["ts_epoch_ms"] - started_ms if costs else None


def _record(name: str, measurements: dict[str, Any]) -> None:
    """Append one turn's measurements to the burn results file."""
    existing: dict[str, Any] = {}
    if _RESULTS_PATH.exists():
        existing = json.loads(_RESULTS_PATH.read_text(encoding="utf-8"))
    existing[name] = measurements
    _RESULTS_PATH.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


# --- burn 1: a real conversational turn -------------------------------------


def test_live_conversational_turn(live_runtime: JarvisRuntime) -> None:
    """One real answer: one started, one completed, L5 names the L3 run."""
    intent = _emit_intent(live_runtime, "T-live-1", _PROMPT_CHAT)

    began = time.monotonic()
    result = drive_turn(
        live_runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )
    total_ms = int((time.monotonic() - began) * 1000)

    conn = live_runtime.conn
    started = _rows(conn, "response.started")
    completed = _rows(conn, "response.completed")
    assert len(started) == 1
    assert len(completed) == 1
    assert len(_terminal_rows(conn, _RESPONSE_TERMINALS)) == 1
    response_id = started[0]["payload"]["response_id"]
    assert completed[0]["payload"]["response_id"] == response_id
    assert completed[0]["payload"]["response_hash"] == result.response_plan.response_hash

    for surface_type in ("surface.response_open", "surface.response_emitted"):
        payloads = _rows(conn, surface_type)
        assert payloads, surface_type
        assert payloads[0]["payload"]["response_id"] == response_id

    assert result.response_plan.text.strip()
    _record(
        "conversational",
        {
            "transcript": _PROMPT_CHAT,
            "answer_chars": len(result.response_plan.text),
            "first_model_output_ms": _first_model_output_ms(conn, started[0]["ts_epoch_ms"]),
            "total_turn_ms": total_ms,
            "response_started_to_terminal_ms": (
                completed[0]["ts_epoch_ms"] - started[0]["ts_epoch_ms"]
            ),
            "iterations": result.iterations,
        },
    )


# --- burn 2: cancel mid-response --------------------------------------------


def test_live_cancelled_turn_leaves_actions_alone(live_runtime: JarvisRuntime) -> None:
    """A cancel during a real model call terminalizes once and touches no action."""
    intent = _emit_intent(
        live_runtime,
        "T-live-2",
        _PROMPT_LONG,
    )
    cancel = make_response_cancel_callable(live_runtime)
    registry = live_runtime.response_runs
    assert registry is not None

    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _drive_on_own_connection,
            live_runtime,
            user_intent_event=intent,
            available_surfaces=frozenset(),
            streaming_enabled=True,
        )
        # Wait for the run to open, then cancel while the provider call is
        # still in flight — the point of the burn is that the terminal lands
        # against a real, slow generation rather than a scripted one.
        deadline = time.monotonic() + 30
        run_id: str | None = None
        while time.monotonic() < deadline:
            runs = registry.open_runs()
            if runs:
                run_id = runs[0].response_id
                break
            time.sleep(0.005)
        assert run_id is not None, "no ResponseRun opened within 30s"
        # Let the provider call genuinely get under way. Cancelling the
        # millisecond the run opens would measure an empty window and would
        # not be the "mid-response" case the ADR cares about.
        time.sleep(_CANCEL_SETTLE_S)
        assert not future.done(), "the turn finished before the cancel was issued"

        requested_at = time.monotonic()
        outcome = cancel(run_id, "generation", "user_stop")
        cancel_latency_ms = int((time.monotonic() - requested_at) * 1000)
        assert outcome == "cancelled"

        with pytest.raises(ResponseCancelledError):
            # Generous: the provider call has no cancellation seam until
            # ADR-0008 Step 6, so the turn keeps running until the model is
            # done even though its response terminal is already durable. That
            # gap is the number this burn exists to measure, not a hang.
            future.result(timeout=600)
        unwound_at = time.monotonic()
    total_ms = int((time.monotonic() - began) * 1000)

    conn = live_runtime.conn
    started = _rows(conn, "response.started")
    cancelled = _rows(conn, "response.cancelled")
    assert len(started) == 1
    assert len(cancelled) == 1
    assert len(_terminal_rows(conn, _RESPONSE_TERMINALS)) == 1
    assert cancelled[0]["payload"]["cancel_scope"] == "generation"
    assert cancelled[0]["payload"]["reason"] == "user_stop"
    assert _count(conn, "response.completed") == 0
    # D10: a response cancel is never an action cancel.
    for terminal in _ACTION_TERMINALS:
        assert _count(conn, terminal) == 0, terminal
    # Nothing was delivered for a response the operator stopped.
    assert _count(conn, "surface.response_emitted") == 0

    _record(
        "cancelled",
        {
            "settle_before_cancel_s": _CANCEL_SETTLE_S,
            "cancel_request_to_terminal_ms": cancel_latency_ms,
            "response_started_to_terminal_ms": (
                cancelled[0]["ts_epoch_ms"] - started[0]["ts_epoch_ms"]
            ),
            # Honest Step-3 limitation: the terminal is immediate, the
            # in-flight provider call is not cancellable until Step 6.
            "terminal_to_turn_unwound_ms": int((unwound_at - requested_at) * 1000),
            "total_turn_ms": total_ms,
            "action_terminals": 0,
        },
    )


def _drive_on_own_connection(runtime: JarvisRuntime, **kwargs: Any) -> Any:  # noqa: ANN401
    """Run ``drive_turn`` the way the daemon's turn worker thread runs it."""
    conn = open_event_log(runtime.runtime_paths.event_log)
    try:
        return drive_turn(replace(runtime, conn=conn), **kwargs)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()


# --- burn 3: a real action through the ActionRunner --------------------------


def test_live_action_turn_through_the_runner(live_runtime: JarvisRuntime) -> None:
    """A real tool action runs on the runner, terminalizes once, and cleans up."""
    intent = _emit_intent(live_runtime, "T-live-3", _PROMPT_TIME)

    began = time.monotonic()
    result = drive_turn(
        live_runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
    )
    total_ms = int((time.monotonic() - began) * 1000)

    conn = live_runtime.conn
    dispatched = _rows(conn, "action.dispatched")
    running = _rows(conn, "action.running")
    assert dispatched, "the turn dispatched no action at all"
    assert running, "no action reached action.running"

    # The runner, not the legacy inline arm, executed it.
    assert "resource_keys" in running[0]["payload"]
    assert running[0]["payload"]["resource_mode"] == "read_shared"
    assert _count(conn, "worker.quiesced") == len(running)

    # Exactly one canonical terminal per action, through the CAS.
    for action_id in {row["payload"]["action_id"] for row in running}:
        terminals = [
            row
            for row in _terminal_rows(conn, _ACTION_TERMINALS)
            if row["payload"].get("action_id") == action_id
        ]
        assert len(terminals) == 1, (action_id, terminals)

    started = _rows(conn, "response.started")
    completed = _rows(conn, "response.completed")
    assert len(started) == 1
    assert len(completed) == 1
    assert result.response_plan.text.strip()

    # A read-shared action carries no cleanup debt, so the lease is already
    # free by the time the turn's finalizer runs.
    assert live_runtime.action_runner is not None
    assert live_runtime.action_runner.leases.live_scopes() == ()

    _record(
        "action",
        {
            "transcript": _PROMPT_TIME,
            "tools_dispatched": len(dispatched),
            "answer_chars": len(result.response_plan.text),
            "first_model_output_ms": _first_model_output_ms(conn, started[0]["ts_epoch_ms"]),
            "total_turn_ms": total_ms,
            "response_started_to_terminal_ms": (
                completed[0]["ts_epoch_ms"] - started[0]["ts_epoch_ms"]
            ),
            "iterations": result.iterations,
            "worker_quiesced": _count(conn, "worker.quiesced"),
            "cleanup_completed": _count(conn, "action.cleanup_completed"),
        },
    )


# --- burn 4: a real Codex worker under a write-exclusive repo lease ----------


_CODEX_GOAL = (
    "Add a module-level docstring to tests/test_demo.py explaining what it "
    "verifies, and create a top-level NOTES.md describing the demo project. "
    "Do not change any test logic."
)


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


@pytest.mark.live_codex
def test_live_codex_worker_holds_the_repo_lease_through_cleanup(
    live_runtime: JarvisRuntime,
    throwaway_repo: Path,
) -> None:
    """A real Codex run takes the repo write-exclusively and is freed by cleanup.

    This is the burn the cheap tool action cannot give: `spawn_worker` is the
    only tool that stashes and mutates a working tree, so it is the only one
    whose lease must survive quiescence and be released by the turn's own
    cleanup finalizer (ADR-0008 D9 / F18 / F23).
    """
    seeded = emit_event(
        live_runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_burn",
            "goal": _CODEX_GOAL,
            "source": "manual",
            "repo_path": str(throwaway_repo),
            "verify_command": "true",
        },
        ts_epoch_ms=int(time.time() * 1000) - 26 * 3600 * 1000,
        correlation={"task_id": "task_burn"},
    )
    assert seeded.event_uid

    intent = _emit_intent(live_runtime, "T-live-4", _PROMPT_CODEX)
    began = time.monotonic()
    result = drive_turn(
        live_runtime,
        user_intent_event=intent,
        available_surfaces=frozenset(),
        streaming_enabled=True,
        trigger_timeout_s=900.0,
    )
    total_ms = int((time.monotonic() - began) * 1000)

    conn = live_runtime.conn
    running = [
        row
        for row in _rows(conn, "action.running")
        if row["payload"].get("resource_mode") == "write_exclusive"
    ]
    assert running, "no write-exclusive action ran; Codex was never dispatched"
    worker_action_id = running[0]["payload"]["action_id"]
    assert running[0]["payload"]["resource_keys"].startswith("repo:")

    # Quiescence, cleanup and exactly one canonical terminal.
    assert _count(conn, "worker.quiesced") >= 1
    terminals = [
        row
        for row in _terminal_rows(conn, _ACTION_TERMINALS)
        if row["payload"].get("action_id") == worker_action_id
    ]
    assert len(terminals) == 1, terminals
    cleanups = [
        row
        for row in _rows(conn, "action.cleanup_completed")
        if row["payload"]["action_id"] == worker_action_id
    ]
    assert len(cleanups) == 1, cleanups
    assert cleanups[0]["payload"]["resource_keys"].startswith("repo:")
    # The repository was quarantined until cleanup, and is free afterwards.
    assert live_runtime.action_runner is not None
    assert live_runtime.action_runner.leases.live_scopes() == ()

    started = _rows(conn, "response.started")
    assert len(started) == 1
    assert len(_terminal_rows(conn, _RESPONSE_TERMINALS)) == 1

    _record(
        "codex_worker",
        {
            "transcript": _PROMPT_CODEX,
            "worker_action_id": worker_action_id,
            "resource_key": running[0]["payload"]["resource_keys"],
            "terminal_type": terminals[0]["type"],
            "cleanup_verification_outcome": cleanups[0]["payload"]["verification_outcome"],
            "worker_quiesced": _count(conn, "worker.quiesced"),
            "running_to_cleanup_ms": (
                cleanups[0]["ts_epoch_ms"] - running[0]["ts_epoch_ms"]
            ),
            "total_turn_ms": total_ms,
            "iterations": result.iterations,
        },
    )
