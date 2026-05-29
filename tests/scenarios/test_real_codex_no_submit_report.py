"""Tier-2 J12 acceptance — Codex completes a turn without calling submit_report.

Per ADR-0002 § Acceptance J12 (Step 20) + spec §3.5.8. Gated by
``--live-codex --live-llm``.

Scenario
--------

Codex completes a turn cleanly (``turn/completed`` fires) but never calls
the ``submit_report`` MCP tool, so the spawn_worker handler's post-turn
guard emits ``worker.report_missing`` + a Limitation Claim
(``relation=limits, level=reported``) and NO ``task.verified``.

Status: LIVE BURN DEFERRED (by design)
--------------------------------------

Increment 2 investigation (2026-05-28, see docs/progress.md § Increment 2
"Variant 2 — J12") concluded the live burn should be deferred, consistent
with Allen's earlier P-0010 X-decision:

- spec §3.5.8 opens by stating that prompt-only enforcement is unreliable
  and mandates the *deterministic post-turn guard* (inject the tool; on
  zero captures emit ``worker.report_missing`` + Limitation). That guard
  is the load-bearing contract and is already proven RED→GREEN by the
  deterministic unit test
  ``tests/unit/test_spawn_worker_real.py::test_spawn_worker_submit_report_missing_emits_report_missing``
  (stubs ``run_codex_action(... submit_report=None)``).
- The hardcoded "You MUST call submit_report" AGENTS.md prompt
  (``jarvis/execution/codex_action.py`` ``_JARVIS_AGENTS_MD``) is an
  ADR-0002 §644 layer-1 *soft nudge*, not a spec requirement. A
  fully-wired Codex 0.130 almost always honours it, so an organic
  no-call cannot be deterministically induced. Every historical *live*
  ``worker.report_missing`` came from a broken precondition (B-0004 auth,
  B-0007 MCP startup, P-0010 sparse prompt), never a clean organic
  refusal. Per P-0010, this is by-design and live J12 validation was
  explicitly deferred ("cannot be fully validated with real Codex 0.130 +
  sparse prompts").
- Do NOT toggle/remove the production prompt to force the miss — that
  would game the negative and contradict the never-patch-prompts rule,
  while proving nothing the unit test does not already nail.

TODO (optional follow-up, NOT blocking): if a live J12 burn is wanted,
induce ``worker.report_missing`` via a real broken precondition — e.g.
point the ``jarvis-tools`` MCP server at an unreachable command via
spawn-time config (conftest env/path only, ADR-0002 G2-clean — no
``LLMClient``/``decide``/tool-output replacement) so the tool genuinely
never lists/dispatches and the guard fires. Then assert
``worker.report_missing`` + the Limitation Claim + no ``task.verified``.

This named test is kept (skipped) so a ``--collect-only`` run still shows
the J12 entry in the Tier-2 inventory.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.live_codex, pytest.mark.live_llm]


_J12_DEFERRED_SKIP = (
    "J12 live burn deferred by design (P-0010 X-decision + spec §3.5.8): the "
    "load-bearing post-turn guard is deterministically covered by "
    "tests/unit/test_spawn_worker_real.py::"
    "test_spawn_worker_submit_report_missing_emits_report_missing. A real, "
    "fully-wired Codex 0.130 cannot be made to organically skip submit_report "
    "without gaming the prompt. See module docstring for the optional "
    "broken-precondition live seam."
)


def test_j12_no_submit_report_emits_worker_report_missing() -> None:
    """J12: turn completed with no ``submit_report`` → ``worker.report_missing`` + Limitation Claim.

    Live burn deferred by design — the ``worker.report_missing`` guard is
    deterministically unit-covered (see module docstring). This named stub
    preserves the J12 entry in the Tier-2 ``--collect-only`` inventory.
    """
    pytest.skip(_J12_DEFERRED_SKIP)
