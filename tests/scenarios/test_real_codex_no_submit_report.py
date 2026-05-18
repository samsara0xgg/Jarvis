"""Tier-2 J12 acceptance — Codex completes a turn without calling submit_report.

Per ADR-0002 § Acceptance J12 (Step 20). Gated by ``--live-codex
--live-llm``.

Scenario
--------

Codex completes a turn cleanly (``turn/completed`` fires) but never
calls the ``submit_report`` MCP tool. Setup options:

1. Inject a system prompt that explicitly instructs Codex not to call
   ``submit_report``.
2. Use a task whose resolution requires zero tool calls (Codex
   answers purely from its model output and never touches the MCP
   surface).

Expected L3 outcome (ADR J12 row, spec §3.5.8):

- ``worker.report_missing`` event emitted (the spawn_worker handler
  detects the absence of any ``item/tool_call`` with
  ``tool_name == "submit_report"``).
- Limitation Claim attached with ``relation=limits, level=reported``.
- NO Postcondition Claim at ``level=verified``.
- NO ``task.verified``.

Cost estimate: ~$0.50 per invocation (one short Codex turn).
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
def real_python_repo_no_tools(tmp_path: Path) -> Iterator[Path]:
    """Repo whose D-1 task does not require any MCP tool calls.

    The goal phrasing is such that Codex can answer from the model
    output alone. The fixture itself is identical to the happy-path
    repo; the J12 outcome is driven by the task ``goal`` text + an
    optional system-prompt override.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
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


def test_j12_no_submit_report_emits_worker_report_missing(
    real_python_repo_no_tools: Path,
) -> None:
    """J12: turn completed with no ``submit_report`` call → ``worker.report_missing`` event + Limitation Claim.

    Asserts:
    - At least one ``worker.report_missing`` event in the log.
    - At least one ``claim.created`` Limitation at ``level=reported,
      relation=limits``.
    - NO ``claim.created`` Postcondition at ``level=verified``.
    - NO ``task.verified``.
    """
    pytest.skip(_SKELETON_SKIP)
