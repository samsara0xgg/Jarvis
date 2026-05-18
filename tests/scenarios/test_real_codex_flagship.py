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

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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


# --- J — Real Codex round-trip --------------------------------------------


def test_j1_codex_version_preflight_below_min_raises(real_python_repo: Path) -> None:
    """J1: ``codex --version`` < 0.125.0 at handler registration raises CodexVersionTooLowError.

    Covered structurally by the canary
    ``test_canary_codex_version_preflight`` (unit-tier); this Tier-2
    test verifies the preflight runs against the REAL ``codex`` binary
    on PATH at scenario bootstrap.
    """
    pytest.skip(_SKELETON_SKIP)


def test_j2_codex_initialize_within_5s(real_python_repo: Path) -> None:
    """J2: ``CodexAppServerClient.initialize()`` returns within 5s; OAuth refresh path covered."""
    pytest.skip(_SKELETON_SKIP)


def test_j3_thread_start_cwd_matches_repo_path(real_python_repo: Path) -> None:
    """J3: ``thread/start.cwd`` equals ``task.created.payload["repo_path"]`` byte-for-byte."""
    pytest.skip(_SKELETON_SKIP)


def test_j4_heartbeat_emitted_for_long_turns(real_python_repo: Path) -> None:
    """J4: turns >= 30s emit at least one ``worker.heartbeat`` chained to ``action.running``."""
    pytest.skip(_SKELETON_SKIP)


def test_j5_turn_completed_nonempty_diff_marks_reported_ok(real_python_repo: Path) -> None:
    """J5: ``turn/completed`` with non-empty diff → ``worker.reported.status == "ok"``."""
    pytest.skip(_SKELETON_SKIP)


def test_j6_codex_client_dead_at_verify_diff_observation(real_python_repo: Path) -> None:
    """J6: ``CodexAppServerClient.is_alive()`` is False when ``action.result_observed`` for verify_diff emits."""
    pytest.skip(_SKELETON_SKIP)


def test_j7_cost_recorded_kind_codex(real_python_repo: Path) -> None:
    """J7: at least one ``cost.recorded`` row with ``kind == "codex"`` correlated to the spawn_worker action."""
    pytest.skip(_SKELETON_SKIP)


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


def test_j11_submit_report_mcp_tool_reachable(real_python_repo: Path) -> None:
    """J11: ``submit_report`` MCP tool reachable; ``item/tool_call`` notification captured exactly once.

    Per ADR-0002 J11: the MCP ``tools/list`` reply is internal to the
    Codex subprocess and not observable from jarvis; the captured
    ``item/tool_call`` with ``tool_name == "submit_report"`` is the
    real reachability proof (Codex cannot call a tool it could not
    list). All four ``mcp_servers.jarvis-tools.{command,args,
    startup_timeout_sec,tool_timeout_sec}`` flags must be present in
    the spawn ``Popen.args``.
    """
    pytest.skip(_SKELETON_SKIP)


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
    real_python_repo: Path,
) -> None:
    """K6: ``surface.response_emitted.payload.delivered_via`` lists physical surfaces; ``attention_channel`` carries the L3 logical channel."""
    pytest.skip(_SKELETON_SKIP)


# --- L — Evidence ladder enforcement (live-Codex variants) ----------------


def test_l1_no_verify_command_emits_observation_only_slot_with_limitation(
    real_python_repo: Path,
) -> None:
    """L1: ``task.created`` without ``verify_command`` → one ``action.result_observed(observation)``; Artifact Claim at observed + Limitation Claim per §8.5 rule 6; no ``task.verified``."""
    pytest.skip(_SKELETON_SKIP)


def test_l2_verify_pytest_pass_plus_reviewer_ok_emits_task_verified(
    real_python_repo: Path,
) -> None:
    """L2: ``verify_command="pytest"`` + exit 0 + reviewer ok → TWO ``action.result_observed`` (observation + verification); Postcondition Claim with verified+reported evidence rows; ``task.verified`` emitted."""
    pytest.skip(_SKELETON_SKIP)
