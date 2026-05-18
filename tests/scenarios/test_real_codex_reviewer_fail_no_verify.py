"""Tier-2 L5 acceptance — reviewer fail with no verify_command.

Per ADR-0002 § Acceptance L5 (Step 20). Gated by ``--live-codex --live-llm``.

Scenario
--------

D-1 seeds a task on a repo with NO test-framework markers (no
``pyproject.toml + tests/`` or ``pytest.ini``, no ``Cargo.toml + tests/``,
no ``package.json + jest.config.*``, no ``Makefile`` with a ``test``
target) so ``verify_command_detect`` returns ``None`` and the
``task.created.optional_payload`` carries no ``verify_command``.

D-day runs Codex against that task. The task goal is crafted so that
Codex produces a non-empty diff (``diff_nonempty == True``) but the
diff does not fulfill the goal — the L3 reviewer should return
``verdict="fail"`` with at least one reason.

Expected L5 outcome (ADR L5 row, line 1774):

- Exactly ONE ``action.result_observed`` for the verify_diff action —
  slot 1 only, ``result_semantics="observation"``; slot 2 (verification)
  is omitted because the task has no ``verify_command`` per spec §3.5.7.
- Artifact Claim at ``level=observed, relation=supports,
  source=diff_capture`` (the diff is observed evidence).
- Limitation Claim at ``level=reported, relation=refutes,
  source_type=llm, source_id=reviewer`` (the reviewer refutes the
  Postcondition claim that the goal was met).
- NO Postcondition Claim with ``level=verified``.
- NO ``task.verified`` event.
- The reviewer's own Report-level evidence row never rises to
  ``level=verified``; reviewer alone cannot establish ``verified`` per
  spec §13.2 I10 (claim ≤ evidence).

Cost estimate: ~$1.50 per invocation (one Codex turn + one OpenRouter
LLM call for the L3 reviewer).
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
def real_repo_without_verify_framework(tmp_path: Path) -> Iterator[Path]:
    """Throw-away git repo with NO test-framework markers.

    ``jarvis.execution.verify_command_detect`` returns ``None`` on this
    shape (no pyproject + tests/, no Cargo.toml, no package.json +
    jest.config, no Makefile test target), so the D-1
    ``task.created.optional_payload`` carries no ``verify_command``.
    That drives L4 ``verify_diff`` into single-slot mode (observation
    only, per spec §3.5.7) and forces L5's reviewer-only Limitation
    path.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "README.md").write_text(
        "# Demo\n\nA throw-away repo with no test-framework markers.\n",
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


def test_l5_reviewer_fail_no_verify_command_emits_limitation_at_reported(
    real_repo_without_verify_framework: Path,
) -> None:
    """L5: reviewer.verdict=fail + verify_command absent + diff_nonempty=True.

    Expected outcome (ADR-0002 line 1774):

    - Exactly ONE ``action.result_observed`` for the verify_diff
      action (slot 1 ``result_semantics="observation"``; slot 2 is
      omitted because ``task.verify_command`` is ``None`` per spec
      §3.5.7).
    - One ``claim.created`` with ``type="Artifact"``,
      ``level=observed``, ``relation=supports``,
      ``source=diff_capture``.
    - One ``claim.created`` with ``type="Limitation"``,
      ``level=reported``, ``relation=refutes``, ``source_type="llm"``,
      ``source_id="reviewer"``; payload records reviewer
      ``verdict="fail"`` plus at least one ``reason``.
    - NO ``claim.created`` with ``level=verified``.
    - NO ``task.verified`` event.
    - reviewer's Report-level evidence row stays at ``level=reported``
      (claim ≤ evidence per spec §13.2 I10).
    """
    pytest.skip(_SKELETON_SKIP)
