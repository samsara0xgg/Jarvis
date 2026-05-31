"""Tier-2 K7 / K8 acceptance — simulated sleep/wake mid-Codex-turn.

Per ADR-0002 § Acceptance K7 + K8 (Step 20) + spec §3.7.8. Gated by
``--live-codex --live-llm``.

Scenario
--------

A real Codex turn is in flight on the main thread (``run_turn`` blocks
inside the synchronous Codex poll loop, exactly as J8/J9). An injector
thread:

1. waits for ``run.started`` to commit (the codex handler's step 4, which
   fires only after the ``codex --version`` preflight passes and a run_id
   is minted), then **SIGSTOP**s the live codex run subprocess so it can
   neither complete the turn nor emit ``worker.reported`` — freezing a
   deterministic in-progress window. Freezing on first ``pgrep`` sighting
   instead lands SIGSTOP on the ``--version`` preflight subprocess,
   fail-closing the action with ``codex_version_too_low`` before any run
   exists;
2. on its OWN event-log connection (SQLite is single-connection-per-
   thread), installs a :class:`StubPowerObserver` via the
   ``observer_factory`` seam and fires ``simulate_sleep()`` then
   ``simulate_wake()``, exactly as the IOPM kernel callbacks would;
3. SIGCONT + SIGKILLs the frozen worker so the main-thread poll loop
   observes the dead subprocess and ``run_turn`` returns promptly.

``simulate_sleep`` emits ``mac.sleeping`` + one ``worker.suspended_by_sleep``
per in-progress action; ``simulate_wake`` emits ``mac.awake`` then
``reconcile_after_wake``, which fail-closes the orphan spawn action with
``worker.terminated_by_sleep`` + ``action.timeout_assumed``. The synchronous
poll loop's own terminal (crash or timeout, depending on trigger ordering)
is incidental — either way the F2 ladder derives a Limitation and never
reaches ``task.verified``.

Expected outcome (ADR K7 + K8 rows, spec §3.7.8):

- ``mac.sleeping`` + ``mac.awake`` present.
- ``worker.suspended_by_sleep`` for the in-progress spawn action.
- ``worker.terminated_by_sleep`` + ``action.timeout_assumed`` from
  ``reconcile_after_wake``.
- a ``Limitation`` ``claim.created`` backed by ``relation=limits``
  evidence (``reported``-grade, per the J9-proven timeout/crash contract —
  the skeleton's pre-impl ``level=executed`` guess is superseded).
- NO ``task.verified`` and NO ``task.no_op``.
- K8: a post-hoc ``reconcile_after_wake`` against the already-closed live
  orphan is a no-op (returns 0, emits no second ``action.timeout_assumed``).

The unit-tier coverage of the sleep/wake invariants lives in
``tests/unit/test_sleep_wake.py``; this Tier-2 file exercises the same
fold against a real Codex round-trip to confirm the L4-worker-loop ↔ L6
power-observer seam holds end-to-end (which the unit tests cannot verify).

Cost estimate: ~$0.50 per invocation (one short Codex turn, frozen and
killed mid-flight by the simulated sleep).

Invocation::

    uv run pytest tests/scenarios/test_real_codex_sleep_during_turn.py \\
        --live-codex --live-llm
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

from jarvis.deployment.sleep_wake import install_power_observer, reconcile_after_wake
from jarvis.runtime import bootstrap_runtime_app, run_turn
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


# The canonical D-day utterance (ADR § Tier 2 invocation), verbatim CJK.
_UTTERANCE: str = "昨天那个 task 给 codex 跑一下，做完审核了再告诉我。"  # noqa: RUF001

# Exhaustive goal so the turn stays busy past spawn (the injector freezes
# it the moment the subprocess appears, but a long goal widens the window
# between the codex spawn and any chance of completion).
_SLEEP_ORPHAN_GOAL: str = (
    "Perform a thorough, repository-wide quality pass: add exhaustive "
    "Google-style docstrings to every function, write a comprehensive pytest "
    "suite covering every edge case, add complete type annotations, and author "
    "a detailed ARCHITECTURE.md. Be exhaustive; do not stop early."
)

# Safety net: if the injector never finds the codex child, the frozen turn
# must not hang for the 600s default. Bounds a missed injection to ~2 min.
_SAFETY_BUDGET_S: str = "120"


class StubPowerObserver:
    """Manual-trigger stub satisfying the ``PowerObserver`` protocol.

    Mirrors ``tests/unit/test_sleep_wake.py::StubPowerObserver`` (defined
    locally to avoid a cross-test-module import). ``simulate_sleep`` /
    ``simulate_wake`` fire the callbacks that :func:`install_power_observer`
    registered, exactly as the IOPM kernel would.
    """

    def __init__(self) -> None:
        """Initialize an unwired stub (callbacks set by :meth:`register`)."""
        self._before_sleep: Callable[[], None] | None = None
        self._on_wake: Callable[[], None] | None = None
        self.shutdown_called: bool = False

    def register(
        self,
        *,
        before_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        """Record the sleep/wake callbacks for later manual firing."""
        self._before_sleep = before_sleep
        self._on_wake = on_wake

    def shutdown(self) -> None:
        """Mark shutdown so teardown is observable."""
        self.shutdown_called = True

    def simulate_sleep(self) -> None:
        """Fire the before-sleep callback exactly as the kernel would."""
        assert self._before_sleep is not None, "register() must precede simulate_sleep()"
        self._before_sleep()

    def simulate_wake(self) -> None:
        """Fire the on-wake callback exactly as the kernel would."""
        assert self._on_wake is not None, "register() must precede simulate_wake()"
        self._on_wake()


def _git(repo: Path, *args: str) -> None:
    """Run a git subcommand in ``repo`` (test-fixture helper)."""
    subprocess.run(["git", "-C", str(repo), *args], check=True)


def _has_codex_child(ppid: int) -> bool:
    """True if the pytest process has a live ``codex`` subprocess child."""
    found = subprocess.run(
        ["pgrep", "-P", str(ppid), "-f", "codex"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(found.stdout.strip())


def _signal_codex_children(ppid: int, sig: str) -> None:
    """Send ``sig`` (e.g. ``-STOP`` / ``-CONT`` / ``-9``) to codex children."""
    subprocess.run(["pkill", sig, "-P", str(ppid), "-f", "codex"], check=False)


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


def _inject_sleep_wake(db_path: Path, ppid: int, holder: dict[str, Any]) -> None:
    """Freeze the live Codex worker, then fire a sleep/wake cycle against the log.

    Runs on a background thread while ``run_turn`` blocks the main thread.
    Records success/error in ``holder`` for the test thread to assert.
    """
    try:
        # 1. Wait for run.started on the log BEFORE freezing anything.
        #    run.started is the codex handler's step 4 — it fires only
        #    AFTER the `codex --version` preflight (step 1) passes and a
        #    run_id is minted (step 2). Freezing the moment `pgrep` first
        #    sees a codex child instead lands SIGSTOP on the --version
        #    preflight subprocess, which returns empty output and
        #    fail-closes the action with `codex_version_too_low` before any
        #    run exists (the original K7/K8 live-burn failure mode).
        conn = open_event_log(db_path)
        try:
            started_deadline = time.monotonic() + 90.0
            while time.monotonic() < started_deadline:
                if any(evt.type == "run.started" for evt in iter_events(conn)):
                    break
                time.sleep(0.2)
            else:
                msg = "run.started never committed within 90s — codex never registered a run"
                raise TimeoutError(msg)

            # 2. The run is registered. Wait for the codex RUN subprocess
            #    (handler step 6) to spawn, then FREEZE it so it cannot
            #    emit a terminal before the sleep/wake injection completes.
            child_deadline = time.monotonic() + 15.0
            while time.monotonic() < child_deadline and not _has_codex_child(ppid):
                time.sleep(0.1)
            _signal_codex_children(ppid, "-STOP")

            # 3. Fire the sleep/wake callbacks against the registered run.
            #    _in_progress_actions keys the in-flight action off
            #    run.started, so simulate_sleep emits worker.suspended_by_sleep.
            stub = StubPowerObserver()
            install_power_observer(conn, observer_factory=lambda: stub)
            stub.simulate_sleep()
            time.sleep(0.3)
            stub.simulate_wake()
        finally:
            conn.close()
        holder["injected"] = True
    except Exception as exc:  # noqa: BLE001 — surface any injector failure to the test thread.
        holder["error"] = exc
    finally:
        # 3. Unfreeze + kill the worker so the main-thread poll loop sees a
        #    dead subprocess and run_turn returns promptly.
        _signal_codex_children(ppid, "-CONT")
        _signal_codex_children(ppid, "-9")


@pytest.fixture(scope="module")
def live_real_codex_sleep_orphan(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """Run a real Codex turn ONCE, inject sleep/wake mid-flight; freeze the trace.

    Module-scoped so the cloud LLM + real Codex subprocess are exercised a
    single time across the K7 + K8 assertions.
    """
    pytest.importorskip("openai")

    repo = tmp_path_factory.mktemp("sleep_orphan_repo")
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

    root = tmp_path_factory.mktemp("sleep_orphan_root")
    os.environ["JARVIS_RUNTIME_ROOT"] = str(root)
    runtime = bootstrap_runtime_app(runtime_root=root)

    yesterday_ms = int(time.time() * 1000) - 26 * 3600 * 1000
    emit_event(
        runtime.conn,
        type="task.created",
        payload={
            "task_id": "task_X",
            "goal": _SLEEP_ORPHAN_GOAL,
            "source": "manual",
            "repo_path": str(repo),
            "verify_command": f"{sys.executable} -m pytest -x -q",
        },
        ts_epoch_ms=yesterday_ms,
    )

    db_path = runtime.runtime_paths.event_log
    holder: dict[str, Any] = {}
    injector = threading.Thread(
        target=_inject_sleep_wake, args=(db_path, os.getpid(), holder), daemon=True
    )
    injector.start()
    os.environ["JARVIS_CODEX_TURN_TIMEOUT_S"] = _SAFETY_BUDGET_S
    try:
        result = run_turn(runtime, utterance=_UTTERANCE)
    finally:
        os.environ.pop("JARVIS_CODEX_TURN_TIMEOUT_S", None)
    injector.join(timeout=5.0)
    # Defensive: ensure no frozen codex child lingers past the turn.
    _signal_codex_children(os.getpid(), "-CONT")
    _signal_codex_children(os.getpid(), "-9")
    time.sleep(0.3)  # let any straggling emit finalize before the snapshot

    trace = _load_trace(db_path)

    artifact_dir = Path(__file__).resolve().parent.parent / "_artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    dump_path = artifact_dir / "real_codex_sleep_orphan_trace.json"
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
        "holder": holder,
        "dump_path": dump_path,
    }
    try:
        yield captured
    finally:
        runtime.conn.close()


def test_k7_sleep_mid_turn_orphans_run_no_task_verified(
    live_real_codex_sleep_orphan: dict[str, Any],
) -> None:
    """K7: induced sleep mid-turn → suspended/terminated by sleep, Limitation, no task.verified.

    Asserts the sleep/wake fold on the captured trace: the spawn action was
    suspended then fail-closed by ``reconcile_after_wake``, the F2 ladder
    derived a Limitation, and ``task.verified`` never fired.
    """
    cap = live_real_codex_sleep_orphan

    assert cap["holder"].get("error") is None, (
        f"injector thread raised: {cap['holder'].get('error')!r}"
    )
    assert cap["holder"].get("injected") is True, "sleep/wake was never injected"

    types = [event["type"] for event in cap["trace"]]
    assert "run.started" in types, "spawn_worker never reached run.started"

    # (a) The sleep/wake fold: mac.* bracket + per-action suspension.
    assert "mac.sleeping" in types, "no mac.sleeping event from simulate_sleep"
    assert "mac.awake" in types, "no mac.awake event from simulate_wake"
    suspended = _payloads(cap, "worker.suspended_by_sleep")
    assert suspended, (
        "no worker.suspended_by_sleep — the sleep landed outside the in-progress "
        "window (codex completed or never started before the freeze)"
    )
    assert all(p.get("action_id") for p in suspended), "suspended_by_sleep missing action_id"

    # (b) reconcile_after_wake fail-closed the orphan spawn action.
    assert "worker.terminated_by_sleep" in types, "reconcile did not terminate the orphan"
    assert "action.timeout_assumed" in types, "reconcile did not emit action.timeout_assumed"

    # (c) Outcome: a Limitation claim with relation=limits evidence; the
    #     orphaned run can never reach a verified postcondition.
    claims = _payloads(cap, "claim.created")
    limitation_ids = {c["claim_id"] for c in claims if c["type"] == "Limitation"}
    assert limitation_ids, "no Limitation claim for the sleep-orphaned run"
    evidence = _payloads(cap, "evidence.attached")
    assert any(
        e["claim_id"] in limitation_ids and e.get("relation") == "limits" for e in evidence
    ), "Limitation claim has no relation=limits evidence row"
    assert not any(c["type"] == "Postcondition" for c in claims), (
        "a sleep-orphaned run must not produce a Postcondition claim"
    )
    assert not any(e.get("level") == "verified" for e in evidence), (
        "a sleep-orphaned run must not produce verified-level evidence"
    )

    # (d) No completion of either kind.
    assert not _payloads(cap, "task.verified"), "task.verified must not fire for a lost worker"
    assert not _payloads(cap, "task.no_op"), "task.no_op must not fire for a lost worker"


def test_k8_reconcile_after_wake_is_idempotent_against_live_orphan(
    live_real_codex_sleep_orphan: dict[str, Any],
) -> None:
    """K8: a second reconcile_after_wake over the already-closed live orphan is a no-op.

    The spawn action carries a terminal event after the turn, so a fresh
    ``reconcile_after_wake`` finds nothing in-progress: it returns 0 and
    emits no second ``action.timeout_assumed`` for the same action_id.
    """
    cap = live_real_codex_sleep_orphan
    db_path: Path = cap["db_path"]

    before = sum(1 for event in cap["trace"] if event["type"] == "action.timeout_assumed")
    assert before >= 1, "fixture should have produced at least one action.timeout_assumed"

    conn = open_event_log(db_path)
    try:
        closed = reconcile_after_wake(conn)
        after = sum(1 for evt in iter_events(conn) if evt.type == "action.timeout_assumed")
    finally:
        conn.close()

    assert closed == 0, (
        f"reconcile_after_wake re-closed {closed} already-terminated action(s) — not idempotent"
    )
    assert after == before, (
        f"action.timeout_assumed count changed from {before} to {after} on re-reconcile"
    )
