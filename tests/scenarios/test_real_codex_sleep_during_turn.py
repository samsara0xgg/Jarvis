"""Tier-2 K7 / K8 acceptance — simulated sleep/wake mid-Codex-turn.

Per ADR-0002 § Acceptance K7 + K8 (Step 20). Gated by
``--live-codex --live-llm``.

Scenario
--------

A real Codex turn is in flight. The test injects ``StubPowerObserver``
(from ``tests/unit/test_sleep_wake.py``) via the ``observer_factory``
seam of ``install_power_observer``. A background thread calls
``stub.simulate_sleep()`` mid-turn (e.g. while the spawn_worker
handler is in its ``take_notification`` loop) and later
``stub.simulate_wake()``.

Expected outcome (ADR K7 + K8 rows, spec §3.7.8):

- ``worker.suspended_by_sleep(run_id=R_Y, action_id=A3)`` fires
  before the simulated sleep.
- ``mac.sleeping`` event lands.
- On wake: ``mac.awake(slept_for_ms)`` fires.
- ``reconcile_after_wake()`` emits ``worker.terminated_by_sleep`` +
  ``action.timeout_assumed`` for the orphan Codex run.
- A Limitation Claim with ``relation=limits, level=executed`` is
  attached.
- ``task.verified`` is NOT emitted.
- K8: calling ``reconcile_after_wake()`` twice in succession does
  not emit a second ``action.timeout_assumed`` for the same
  ``action_id`` (idempotence).

Cost estimate: ~$0.30 per invocation (one short Codex turn, killed
mid-flight by the simulated sleep).

Note
----

The unit-tier coverage of the sleep/wake invariants lives in
``tests/unit/test_sleep_wake.py``; this Tier-2 file exercises the
same code path against a real Codex round-trip to confirm the fold
between the L4 worker loop and the L6 power observer holds end-to-
end (which the unit tests cannot verify).
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


_SKELETON_SKIP = (
    "Live-codex test scaffold; full assertions land when run against a real "
    "Codex CLI on Allen's Mac (ADR-0002 Step 20)."
)


@pytest.fixture
def real_python_repo_long_task(tmp_path: Path) -> Iterator[Path]:
    """Throw-away repo whose D-1 task takes long enough to interrupt mid-turn.

    The goal phrasing drives turn duration; the fixture itself is
    structurally identical to the happy-path repo.
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


def test_k7_sleep_mid_turn_fires_suspended_then_terminated_by_sleep(
    real_python_repo_long_task: Path,
) -> None:
    """K7: induced sleep mid-Codex-turn → ``worker.suspended_by_sleep`` before sleep, ``worker.terminated_by_sleep`` + ``action.timeout_assumed`` after wake; Limitation Claim at ``level=executed``; no ``task.verified``.

    Setup:
    - Install StubPowerObserver via ``observer_factory``.
    - Run ``run_turn(utterance="昨天那个 task 给 Codex 跑一下...")`` in
      a thread.
    - From a separate thread, call ``stub.simulate_sleep()`` once the
      ``take_notification`` loop has registered at least one
      ``action.running`` event for the spawn_worker action.
    - After a short delay, call ``stub.simulate_wake()``.
    - Run ``reconcile_after_wake(conn)``.

    Assertions:
    - ``worker.suspended_by_sleep`` event present, correlated to the
      spawn_worker ``action_id``.
    - ``mac.sleeping`` + ``mac.awake`` events present.
    - ``worker.terminated_by_sleep`` + ``action.timeout_assumed`` for
      the orphan ``action_id``.
    - At least one ``claim.created`` Limitation at ``level=executed,
      relation=limits``.
    - NO ``task.verified``.
    """
    pytest.skip(_SKELETON_SKIP)


def test_k8_reconcile_after_wake_is_idempotent(
    real_python_repo_long_task: Path,
) -> None:
    """K8: calling ``reconcile_after_wake()`` twice does not emit a second ``action.timeout_assumed`` for the same ``action_id``.

    Asserts the no-duplicate-emit invariant on the second call.
    (The unit tier already covers the LLM-free idempotence path in
    ``tests/unit/test_sleep_wake.py``; this Tier-2 variant runs the
    same invariant against a live-Codex orphan action.)
    """
    pytest.skip(_SKELETON_SKIP)
