"""Canary - ``task.verified`` requires ``diff_nonempty == True``.

Cross-layer invariant pinned by Fix 2 Option A (amended ADR-0002
§ Evidence ladder paradox row): the Result Interpreter must NEVER
emit a ``Postcondition`` Claim at ``level=verified`` (and therefore
the dispatcher must never emit ``task.verified``) when the
observation slot reports ``diff_nonempty == False``. The honest
outcome for the empty-diff + verify-pass paradox is ``task.no_op``
(spec §8.9 — a code task requires both an artifact-change signal
AND verification passed).

This canary walks every empty-diff permutation through
:func:`interpret_verify_diff_bundle` and asserts:

  1. The verdict returned is never ``"verified"`` when
     ``diff_nonempty == False``.
  2. No event of type ``claim.created`` with ``type=="Postcondition"``
     and matching ``evidence.level=="verified"`` appears in the
     emitted tuple.

Unit tests in ``test_result_interpreter_ladder.py`` cover individual
ladder rows; this canary's job is to lock down the cross-row
invariant so a regression elsewhere (e.g. a future ladder change
that re-introduces the empty-diff promotion) trips here, not just
in the per-row test.

Runtime canary (not AST scan) because the invariant is behavioural -
the property holds across the interpreter's branching logic, which
is harder to prove statically than "field X is in payload literal".
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import pytest

from jarvis.decision.result_interpreter import interpret_verify_diff_bundle
from jarvis.shared import (
    ActionRequest,
    CallerPrincipal,
    RawResult,
    RawResultBundle,
    ResultSemantics,
)
from jarvis.state.event_log import emit_event, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


_SUBJECT = "task_canary"
_GOAL = "ensure task.verified never fires on empty diff"


@dataclass(frozen=True)
class _FakeVerdict:
    """Reviewer-verdict-shaped stand-in for the canary."""

    verdict: Literal["ok", "fail"]
    reasons: tuple[str, ...] = ("canary",)
    malformed: bool = False


def _action_request() -> ActionRequest:
    return ActionRequest(
        action_id="A1",
        tool_name="verify_diff",
        target_entity_ref=_SUBJECT,
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id="R1",
        turn_id="T1",
        payload={"verify_command": "uv run pytest -x"},
    )


def _observation_slot(*, diff_nonempty: bool) -> RawResult:
    return RawResult(
        action_id="A1",
        semantics="observation",
        payload={
            "diff_text_preview": "" if not diff_nonempty else "diff --git a/x b/x\n",
            "diff_nonempty": diff_nonempty,
        },
        tool_output=None,
        error=None,
    )


def _verification_slot(
    *, semantics: ResultSemantics, exit_code: int
) -> RawResult:
    return RawResult(
        action_id="A1",
        semantics=semantics,
        payload={
            "verify_command": "uv run pytest -x",
            "exit_code": exit_code,
        },
        tool_output=None,
        error=None if semantics == "verification" else "predicate_failed",
    )


def _seed_observation_event(conn: sqlite3.Connection) -> str:
    evt = emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A1", "semantics": "observation"},
        correlation={"action_id": "A1"},
    )
    return evt.event_uid


def _seed_verification_event(conn: sqlite3.Connection, *, semantics: str) -> str:
    evt = emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A1", "semantics": semantics},
        correlation={"action_id": "A1"},
    )
    return evt.event_uid


def _evidence_rows(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in events if e.type == "evidence.attached"]


def _claim_rows(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in events if e.type == "claim.created"]


# Every empty-diff permutation the F2 ladder can produce. The
# canary's job is to prove the verdict never lifts to "verified"
# AND no Postcondition Claim @ level=verified row ever lands.
# Each tuple is (label, verify_present, exit_code, reviewer_verdict).
_EMPTY_DIFF_SCENARIOS: tuple[
    tuple[str, bool, int | None, str | None], ...
] = (
    ("empty_diff_no_verify", False, None, None),
    ("empty_diff_verify_pass_no_reviewer", True, 0, None),
    ("empty_diff_verify_pass_reviewer_ok", True, 0, "ok"),
    ("empty_diff_verify_pass_reviewer_fail", True, 0, "fail"),
    ("empty_diff_verify_fail_no_reviewer", True, 1, None),
    ("empty_diff_verify_fail_reviewer_ok", True, 1, "ok"),
    ("empty_diff_verify_fail_reviewer_fail", True, 1, "fail"),
)


@pytest.mark.parametrize(
    ("label", "verify_present", "exit_code", "reviewer_verdict"),
    _EMPTY_DIFF_SCENARIOS,
    ids=[s[0] for s in _EMPTY_DIFF_SCENARIOS],
)
def test_canary_task_verified_requires_diff(
    tmp_path: Path,
    label: str,  # noqa: ARG001 - parametrize label surfaces in test id.
    verify_present: bool,  # noqa: FBT001 - parametrize dimension.
    exit_code: int | None,
    reviewer_verdict: str | None,
) -> None:
    """No empty-diff scenario may produce verdict='verified' or a verified Postcondition row.

    Pins the Fix 2 Option A invariant across every reviewer x verify
    permutation: when ``diff_nonempty == False``, the F2 ladder must
    never resolve to ``task.verified``. The honest event for the
    empty-diff + verify-pass paradox is ``task.no_op``.
    """
    slots: tuple[RawResult, ...] = (_observation_slot(diff_nonempty=False),)
    if verify_present and exit_code is not None:
        sem: ResultSemantics = "verification" if exit_code == 0 else "error"
        slots = (
            *slots,
            _verification_slot(semantics=sem, exit_code=exit_code),
        )

    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source_event_ids = {"observation": _seed_observation_event(conn)}
        if verify_present and exit_code is not None:
            sem = "verification" if exit_code == 0 else "error"
            source_event_ids[sem] = _seed_verification_event(conn, semantics=sem)

        verdict_obj = (
            _FakeVerdict(verdict="ok" if reviewer_verdict == "ok" else "fail")
            if reviewer_verdict is not None
            else None
        )

        events, verdict = interpret_verify_diff_bundle(
            RawResultBundle(slots=slots),
            source_event_ids_by_semantics=source_event_ids,
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=verdict_obj,
        )

    # Invariant 1: verdict never lifts to "verified" on empty diff.
    assert verdict != "verified", (
        f"Fix 2 Option A invariant violated: empty-diff path "
        f"({verify_present=}, {exit_code=}, {reviewer_verdict=}) "
        f"returned verdict='verified' - this would emit task.verified "
        f"per spec §8.9 violation"
    )

    # Invariant 2: no Postcondition Claim @ level=verified row emitted.
    postcondition_claim_ids = {
        c["claim_id"] for c in _claim_rows(events) if c["type"] == "Postcondition"
    }
    verified_evidence = [
        ev
        for ev in _evidence_rows(events)
        if ev.get("level") == "verified"
        and ev.get("claim_id") in postcondition_claim_ids
    ]
    assert not verified_evidence, (
        f"Fix 2 Option A invariant violated: empty-diff path "
        f"({verify_present=}, {exit_code=}, {reviewer_verdict=}) "
        f"emitted a Postcondition Claim @ level=verified - this would "
        f"trip task.verified derivation: {verified_evidence!r}"
    )
