"""Tier-2 L4 acceptance — real Codex produces empty diff.

Per ADR-0002 § Acceptance L4 (Step 20). Gated by ``--live-codex
--live-llm``.

Scenario
--------

The D-1 task is intentionally trivial — e.g. ``goal="List the Python
files in the repo, but do not modify anything."``. Codex resolves
the task by listing files in its turn output and never writes to the
working tree. The downstream ``git diff`` capture is empty.

Expected L3 outcome (ADR L4 row):

- Execution Claim at ``level=executed`` (spawn_worker ran to
  completion — the action terminated normally).
- Limitation Claim at ``level=reported, relation=limits`` recording
  the missing artifact (per spec §8.5 rule 6).
- NO Postcondition Claim at ``level=verified``.
- NO ``task.verified``.
- References the F2 ladder ``diff_nonempty == False`` row.

Cost estimate: ~$0.50 per invocation (one short Codex turn that
produces only text output, no edits).
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
def real_python_repo_trivial(tmp_path: Path) -> Iterator[Path]:
    """Throw-away git repo where the D-1 task is "look but don't touch".

    Same shape as the happy-path fixture; the empty-diff outcome is
    driven by the goal phrasing passed to Codex (the seeded
    ``task.created.payload["goal"]``), not by the repo contents.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "src.py").write_text("# nothing to see here\n", encoding="utf-8")
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


def test_l4_empty_diff_emits_execution_plus_limitation_no_verified(
    real_python_repo_trivial: Path,
) -> None:
    """L4: empty diff → Execution Claim at executed + Limitation Claim at reported; no ``task.verified``.

    Asserts:
    - ``worker.artifact_observed.payload.diff_nonempty == False``.
    - At least one ``claim.created`` with claim class Execution at
      ``level=executed`` (the spawn_worker action settled).
    - At least one ``claim.created`` with claim class Limitation at
      ``level=reported, relation=limits``.
    - NO ``claim.created`` with ``level=verified``.
    - NO ``task.verified``.
    """
    pytest.skip(_SKELETON_SKIP)
