"""Tier-2 L3 acceptance — real Codex with failing verify_command.

Per ADR-0002 § Acceptance L3 (Step 20) and § Negative-path appendix
(``verify_command fails``). Gated by ``--live-codex --live-llm``.

Scenario
--------

D-1 seeds a task whose detected ``verify_command="pytest"``; the
fixture repo contains a test that intentionally fails (``assert
False``). D-day runs Codex against the task. After Codex returns,
the L4 ``verify_diff`` handler runs ``pytest`` against the fixture
repo and the exit code is non-zero.

Expected L3 outcome (ADR L3 row):

- Slot 1 ``action.result_observed`` with ``result_semantics="observation"``.
- Slot 2 ``action.result_observed`` with ``result_semantics="error"``
  (post_action_check predicate ``exit_code == 0`` did not match).
- Limitation Claim with ``level=executed, relation=limits,
  source_id=verify_command``.
- NO Postcondition Claim with ``level=verified``.
- NO ``task.verified``.
- ``surface.response_emitted.text`` matches at least one of
  ``{r"reported,?\\s*not\\s+verified", r"测试.{0,4}没过", r"未验证"}``.

Cost estimate: ~$1 per invocation (one Codex turn + one OpenRouter
LLM call for the L3 reviewer that still runs even on verify-fail).
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
def real_python_repo_with_failing_test(tmp_path: Path) -> Iterator[Path]:
    """Throw-away git repo whose ``verify_command="pytest"`` exits non-zero.

    Same shape as the happy-path fixture except the seed test asserts
    False — verify_diff slot 2 therefore reports ``result_semantics
    ="error"`` (per spec §3.5.7 fail-of-match) regardless of what
    Codex does to the working tree.
    """
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.1'\nrequires-python = '>=3.12'\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_fail.py").write_text(
        "def test_fail() -> None:\n    assert False, 'intentional failure for L3 acceptance'\n",
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


def test_l3_verify_fail_emits_limitation_at_executed(
    real_python_repo_with_failing_test: Path,
) -> None:
    """L3: verify_command exit != 0 → Limitation Claim at ``level=executed``; no ``task.verified``.

    Asserts:
    - Exactly two ``action.result_observed`` for the verify_diff action
      (slot 1 ``observation``, slot 2 ``error``).
    - At least one ``claim.created`` with ``relation=limits,
      level=executed, source_id=verify_command``.
    - NO ``claim.created`` with ``level=verified``.
    - NO ``task.verified``.
    - ``surface.response_emitted.text`` matches one of the canonical
      Limitation regexes from ``jarvis.decision.pre_emit_phrases``.
    """
    pytest.skip(_SKELETON_SKIP)


def test_l3_pre_emit_gate_verdict_force_limitation_language(
    real_python_repo_with_failing_test: Path,
) -> None:
    """L3 (cont.): the Pre-emit Gate emits ``verdict="force_limitation_language"`` on the verify-fail response action.

    Per ADR § Negative-path appendix: the gate refuses any
    completion-class phrasing in the final CLI output and forces
    Limitation framing. This invariant exercises the Day-2 wiring of
    ``COMPLETION_REGEXES`` + ``LIMITATION_REGEXES`` from
    ``jarvis.decision.pre_emit_phrases``.
    """
    pytest.skip(_SKELETON_SKIP)
