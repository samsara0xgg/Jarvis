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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

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
    """J3: ``thread/start.cwd`` equals ``task.created.payload["repo_path"]`` byte-for-byte.

    The cwd is set on the Codex ``thread/start`` JSON-RPC request, which
    is internal to the subprocess and not mirrored into the Event Log.
    Asserting it byte-for-byte requires a spawn-argv capture seam —
    deferred to Increment-1b (see the burn design doc). No fixture
    dependency so this skip never triggers a live burn.
    """
    pytest.skip(
        "J3 cwd lives on the Codex thread/start RPC, not the Event Log; "
        "needs a spawn-argv capture seam (Increment-1b follow-up).",
    )


def test_j4_heartbeat_emitted_for_long_turns(real_python_repo: Path) -> None:
    """J4: turns >= 30s emit at least one ``worker.heartbeat`` chained to ``action.running``."""
    pytest.skip(_SKELETON_SKIP)


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


def test_j6_codex_client_dead_at_verify_diff_observation(real_python_repo: Path) -> None:
    """J6: ``CodexAppServerClient.is_alive()`` is False when ``action.result_observed`` for verify_diff emits."""
    pytest.skip(_SKELETON_SKIP)


def test_j7_cost_recorded_kind_codex(live_real_codex_happy: dict[str, Any]) -> None:
    """J7: at least one ``cost.recorded`` row with ``kind == "codex"`` correlated to the spawn_worker action."""
    costs = _payloads(live_real_codex_happy, "cost.recorded")
    codex_costs = [c for c in costs if c.get("kind") == "codex"]
    kinds = [c.get("kind") for c in costs]
    assert codex_costs, f"no cost.recorded with kind=codex; kinds={kinds}"
    assert any(c.get("run_id") for c in codex_costs), "codex cost not correlated to a run_id"


def test_j8_codex_crash_emits_action_failed(real_python_repo: Path) -> None:
    """J8: Codex subprocess crash → ``action.failed`` with ``error="codex_subprocess_crashed"``; no ``task.verified``.

    Negative-path appendix: events 1-26 identical to happy path; at
    evt 27, ``action.failed`` replaces the normal ``worker.reported``
    path. Surface utterance contains "Codex 跑挂了，没新 diff".
    """
    pytest.skip(_SKELETON_SKIP)


def test_j9_codex_timeout_emits_timeout_assumed(real_python_repo: Path) -> None:
    """J9: Codex deadline exceeded → ``action.timeout_assumed``; subprocess killed via ``close(timeout=3.0)``.

    Negative-path appendix: identical to crash except
    ``action.timeout_assumed`` replaces ``action.failed``; surface
    utterance contains "Codex 超时，未完成".
    """
    pytest.skip(_SKELETON_SKIP)


def test_j10_all_four_sandbox_c_flags_present_in_popen_args(real_python_repo: Path) -> None:
    """J10: spawn argv contains all four ``-c`` flags: model, reasoning_effort, sandbox_mode, writable_roots."""
    pytest.skip(_SKELETON_SKIP)


def test_j11_submit_report_mcp_tool_reachable(live_real_codex_happy: dict[str, Any]) -> None:
    """J11: ``submit_report`` MCP tool reachable; proven by a clean ``worker.reported.status == "ok"``.

    The MCP ``tools/list`` reply is internal to the Codex subprocess and
    not observable from jarvis. The handler emits ``worker.report_missing``
    + a degraded ``status == "report_missing"`` when Codex completes a
    turn WITHOUT calling ``submit_report``. A clean ``status == "ok"`` and
    the absence of any ``worker.report_missing`` event is therefore the
    real reachability proof — Codex could not have produced an ``ok``
    report through a tool it could not list and call. The byte-level
    ``mcp_servers.jarvis-tools`` argv check is a spawn-argv-seam
    follow-up (Increment-1b), like J10.
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
    pytest.skip(_SKELETON_SKIP)


def test_j13_dirty_tree_stash_pop_conflict_emits_conflict_patch(real_python_repo: Path) -> None:
    """J13: dirty-tree case — ``git stash push -u`` → codex → ``git stash pop`` conflict → ``conflict.patch`` artifact."""
    pytest.skip(_SKELETON_SKIP)


# --- K — Real notification side-effects -----------------------------------


def test_k1_say_subprocess_invoked_with_voice_flag(real_python_repo: Path) -> None:
    """K1: ``say`` subprocess started with ``-v Tingting`` (or configured voice) + voice-channel text as last arg."""
    pytest.skip(_SKELETON_SKIP)


def test_k2_osascript_notification_truncated_at_240(real_python_repo: Path) -> None:
    """K2: ``osascript -e 'display notification ...'`` invoked exactly once per emission; body truncated at 240 chars."""
    pytest.skip(_SKELETON_SKIP)


def test_k3_cli_parent_exits_within_100ms_no_sqlite_write(real_python_repo: Path) -> None:
    """K3: CLI parent exits within 100ms of the long-run classifier match; parent's PID never opens ``~/.jarvis/mac_events.db``."""
    pytest.skip(_SKELETON_SKIP)


def test_k4_detached_child_writes_worker_reported_after_parent_exits(
    real_python_repo: Path,
) -> None:
    """K4: detached child writes ``worker.reported``; a fresh ``open_event_log()`` in a separate process observes the row."""
    pytest.skip(_SKELETON_SKIP)


def test_k5_reviewer_fail_path_uses_limitation_phrasing(real_python_repo: Path) -> None:
    """K5: reviewer-verdict-fail path delivers limitation utterance through ``say``, not a completion claim."""
    pytest.skip(_SKELETON_SKIP)


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
    pytest.skip(_SKELETON_SKIP)


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
