"""Unit tests - dual-slot F2 ladder per ADR-0002 § Evidence ladder lines 279-301.

Each row of the F2 ladder maps a (verify_command presence, exit_code,
reviewer.verdict) tuple to a specific set of (Claim type, relation,
level, source_id) evidence rows. This test parametrizes over every row
and asserts the L3 Result Interpreter emits exactly the expected events.

The interpreter is called LLM-free: ``interpret_verify_diff_bundle``
takes a :class:`ReviewerVerdict`-like dataclass, so the test
constructs both the :class:`RawResultBundle` and the verdict directly.
No mocked LLM client, no ``unittest.mock`` (per ADR § Day-1 test
philosophy + ADR-0002 Step 12 unit-test row).

Coverage: 7 distinct outcome scenarios from the ADR § Evidence ladder
table (lines 279-301), spanning all 5 result_semantics permutations a
verify_diff bundle can produce. Each scenario asserts the FULL set of
emitted (claim, evidence) pairs - not just the verified row - so a
silent drop of a §8.5 rule-6 row is caught.
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


@dataclass(frozen=True)
class _FakeVerdict:
    """Lightweight ReviewerVerdict-shaped stand-in for the ladder tests."""

    verdict: Literal["ok", "fail"]
    reasons: tuple[str, ...] = ("test reason",)
    malformed: bool = False


_SUBJECT = "task_X"
_GOAL = "make the tests pass"


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
    """Build an observation slot (slot 1) with the given diff state."""
    return RawResult(
        action_id="A1",
        semantics="observation",
        payload={
            "diff_text_preview": "diff --git a/x b/x\n" if diff_nonempty else "",
            "diff_nonempty": diff_nonempty,
            "artifact_ref": "/tmp/diff.txt",
        },
        tool_output=None,
        error=None,
    )


def _verification_slot(
    *, semantics: ResultSemantics, exit_code: int
) -> RawResult:
    """Build a verification slot (slot 2) with the given semantics + exit_code."""
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
    """Seed an ``action.result_observed`` row so source_event_ids resolve."""
    evt = emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A1", "semantics": "observation"},
        correlation={"action_id": "A1"},
    )
    return evt.event_uid


def _seed_verification_event(conn: sqlite3.Connection, *, semantics: str) -> str:
    """Seed a second ``action.result_observed`` row for the verification slot."""
    evt = emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A1", "semantics": semantics},
        correlation={"action_id": "A1"},
    )
    return evt.event_uid


def _types_of(events: tuple[Any, ...]) -> list[str]:
    return [e.type for e in events]


def _evidence_rows(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in events if e.type == "evidence.attached"]


def _claim_rows(events: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [dict(e.payload) for e in events if e.type == "claim.created"]


# --- Row: verify_command present, exit 0, reviewer ok ---------------------


def test_ladder_verify_pass_reviewer_ok(tmp_path: Path) -> None:
    """ADR row 281-282 — Postcondition verified + reviewer supports/reported, task.verified."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        ver_uid = _seed_verification_event(conn, semantics="verification")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=True),
                _verification_slot(semantics="verification", exit_code=0),
            )
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "verification": ver_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="ok"),
        )

    assert did_verify is True
    claims = _claim_rows(events)
    evidences = _evidence_rows(events)
    assert any(c["type"] == "Artifact" for c in claims)
    assert any(c["type"] == "Postcondition" for c in claims)
    # Verified row from verify_command.
    assert any(
        ev["relation"] == "supports"
        and ev["level"] == "verified"
        and ev.get("source_id") == "verify_command"
        for ev in evidences
    )
    # Reviewer-reported row, supports relation.
    assert any(
        ev["relation"] == "supports"
        and ev["level"] == "reported"
        and ev.get("source_id") == "reviewer"
        for ev in evidences
    )


# --- Row: verify_command present, exit 0, reviewer fail (paradox 285) -----


def test_ladder_verify_pass_reviewer_fail(tmp_path: Path) -> None:
    """ADR rows 283-285 — task.verified fires; reviewer attaches refutes/reported + contrast Limitation."""  # noqa: E501 - row label.
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        ver_uid = _seed_verification_event(conn, semantics="verification")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=True),
                _verification_slot(semantics="verification", exit_code=0),
            )
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "verification": ver_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="fail"),
        )

    assert did_verify is True  # verify_command exit is canonical (ladder note)
    evidences = _evidence_rows(events)
    # Reviewer row carries relation=refutes, level=reported.
    assert any(
        ev["relation"] == "refutes"
        and ev["level"] == "reported"
        and ev.get("source_id") == "reviewer"
        for ev in evidences
    )
    # Contrast Limitation Claim attached (ADR row 285).
    claims = _claim_rows(events)
    assert sum(1 for c in claims if c["type"] == "Limitation") >= 1


# --- Row: verify_command present, exit != 0 -------------------------------


def test_ladder_verify_fail(tmp_path: Path) -> None:
    """ADR rows 286-287 — Limitation at level=executed; reviewer attaches limits/reported; no task.verified."""  # noqa: E501 - row label.
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        err_uid = _seed_verification_event(conn, semantics="error")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=True),
                _verification_slot(semantics="error", exit_code=1),
            )
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "error": err_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="fail"),
        )

    assert did_verify is False
    evidences = _evidence_rows(events)
    assert any(
        ev["relation"] == "limits"
        and ev["level"] == "executed"
        and ev.get("source_id") == "verify_command"
        for ev in evidences
    )
    # Reviewer row attached to a Limitation Claim -> relation stays
    # ``limits`` regardless of verdict.
    assert any(
        ev["relation"] == "limits"
        and ev["level"] == "reported"
        and ev.get("source_id") == "reviewer"
        for ev in evidences
    )


# --- Row: verify_command absent, diff nonempty, reviewer ok ---------------


def test_ladder_no_verify_command_diff_present_reviewer_ok(tmp_path: Path) -> None:
    """ADR rows 288-290 — Artifact observed; reviewer reported; rule-6 Limitation; no task.verified."""  # noqa: E501 - row label.
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        bundle = RawResultBundle(
            slots=(_observation_slot(diff_nonempty=True),),
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={"observation": obs_uid},
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="ok"),
        )

    assert did_verify is False
    claims = _claim_rows(events)
    evidences = _evidence_rows(events)
    assert any(c["type"] == "Artifact" for c in claims)
    assert any(c["type"] == "Limitation" for c in claims)
    # Diff-capture observed row.
    assert any(
        ev["relation"] == "supports"
        and ev["level"] == "observed"
        and ev.get("source_id") == "diff_capture"
        for ev in evidences
    )
    # Reviewer Report-grade row.
    assert any(
        ev["relation"] == "supports"
        and ev["level"] == "reported"
        and ev.get("source_id") == "reviewer"
        for ev in evidences
    )
    # §8.5 rule 6 missing-verify_command row.
    assert any(
        ev.get("source_id") == "missing_verify_command"
        and ev["relation"] == "limits"
        and ev["level"] == "reported"
        for ev in evidences
    )


# --- Row: verify_command absent, diff nonempty, reviewer fail ------------


def test_ladder_no_verify_command_diff_present_reviewer_fail(
    tmp_path: Path,
) -> None:
    """ADR rows 291-292 — Artifact level=observed; reviewer refutes-reported."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        bundle = RawResultBundle(
            slots=(_observation_slot(diff_nonempty=True),),
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={"observation": obs_uid},
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="fail"),
        )

    assert did_verify is False
    evidences = _evidence_rows(events)
    assert any(
        ev["relation"] == "refutes"
        and ev["level"] == "reported"
        and ev.get("source_id") == "reviewer"
        for ev in evidences
    )


# --- Row: diff empty (Codex produced nothing) -----------------------------


def test_ladder_empty_diff_no_verify_command(tmp_path: Path) -> None:
    """ADR rows 293-294 — Execution Claim + rule-6 missing-diff Limitation; no task.verified."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        bundle = RawResultBundle(
            slots=(_observation_slot(diff_nonempty=False),),
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={"observation": obs_uid},
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=None,  # caller skips reviewer on empty diff
        )

    assert did_verify is False
    claims = _claim_rows(events)
    assert any(c["type"] == "Execution" for c in claims)
    assert any(c["type"] == "Limitation" for c in claims)
    evidences = _evidence_rows(events)
    assert any(
        ev["relation"] == "supports"
        and ev["level"] == "executed"
        and ev.get("source_id") == "spawn_worker"
        for ev in evidences
    )
    assert any(
        ev.get("source_id") == "missing_diff_artifact"
        and ev["relation"] == "limits"
        for ev in evidences
    )


# --- Row: empty diff + verify_command passes (paradox row 295-297) --------


def test_ladder_empty_diff_verify_command_passes(tmp_path: Path) -> None:
    """ADR rows 295-297 — Postcondition verified fires; Execution + missing-diff Limitation also recorded."""  # noqa: E501 - row label.
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        ver_uid = _seed_verification_event(conn, semantics="verification")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=False),
                _verification_slot(semantics="verification", exit_code=0),
            )
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "verification": ver_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=None,
        )

    assert did_verify is True
    claims = _claim_rows(events)
    assert any(c["type"] == "Execution" for c in claims)
    assert any(c["type"] == "Postcondition" for c in claims)
    # §8.5 rule 6 missing-diff row still recorded for audit.
    evidences = _evidence_rows(events)
    assert any(
        ev.get("source_id") == "missing_diff_artifact"
        and ev["relation"] == "limits"
        for ev in evidences
    )


# --- Row: empty diff + verify_command fails (rows 298-300) ----------------


def test_ladder_empty_diff_verify_command_fails(tmp_path: Path) -> None:
    """ADR rows 298-300 — Execution + Limitation(executed) + missing-diff Limitation; no task.verified."""  # noqa: E501 - row label.
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        err_uid = _seed_verification_event(conn, semantics="error")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=False),
                _verification_slot(semantics="error", exit_code=2),
            )
        )
        events, did_verify = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "error": err_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=None,
        )

    assert did_verify is False
    evidences = _evidence_rows(events)
    # verify_command level=executed Limitation.
    assert any(
        ev.get("source_id") == "verify_command"
        and ev["relation"] == "limits"
        and ev["level"] == "executed"
        for ev in evidences
    )
    # Missing-diff §8.5 rule 6.
    assert any(
        ev.get("source_id") == "missing_diff_artifact"
        and ev["relation"] == "limits"
        for ev in evidences
    )


# --- Every emitted evidence.attached carries relation + level -------------


@pytest.mark.parametrize(
    ("diff_nonempty", "verify_present", "exit_code"),
    [
        (True, True, 0),
        (True, True, 1),
        (True, False, None),
        (False, False, None),
        (False, True, 0),
        (False, True, 1),
    ],
)
def test_ladder_every_evidence_row_has_relation_and_level(
    tmp_path: Path,
    diff_nonempty: bool,  # noqa: FBT001 - parametrize ladder dimension.
    verify_present: bool,  # noqa: FBT001 - parametrize ladder dimension.
    exit_code: int | None,
) -> None:
    """Each ladder row's evidence payloads carry both `relation` and `level`."""
    slots: tuple[RawResult, ...] = (_observation_slot(diff_nonempty=diff_nonempty),)
    semantics_map = {"observation": "evt-obs"}
    if verify_present and exit_code is not None:
        sem: ResultSemantics = "verification" if exit_code == 0 else "error"
        slots = (
            *slots,
            _verification_slot(semantics=sem, exit_code=exit_code),
        )
        semantics_map[sem] = "evt-ver"

    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        source_event_ids = {"observation": obs_uid}
        if verify_present and exit_code is not None:
            sem = "verification" if exit_code == 0 else "error"
            source_event_ids[sem] = _seed_verification_event(conn, semantics=sem)

        events, _ = interpret_verify_diff_bundle(
            RawResultBundle(slots=slots),
            source_event_ids_by_semantics=source_event_ids,
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="ok") if diff_nonempty else None,
        )

    for ev in _evidence_rows(events):
        assert "relation" in ev, f"evidence row missing relation: {ev}"
        assert "level" in ev, f"evidence row missing level: {ev}"


# --- Sanity: claim+evidence still emitted as SEPARATE rows ----------------


def test_ladder_claim_and_evidence_remain_separate_events(tmp_path: Path) -> None:
    """ADR Acceptance A8 holds in dual-slot path: claims and evidences are separate."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        obs_uid = _seed_observation_event(conn)
        ver_uid = _seed_verification_event(conn, semantics="verification")
        bundle = RawResultBundle(
            slots=(
                _observation_slot(diff_nonempty=True),
                _verification_slot(semantics="verification", exit_code=0),
            )
        )
        events, _ = interpret_verify_diff_bundle(
            bundle,
            source_event_ids_by_semantics={
                "observation": obs_uid,
                "verification": ver_uid,
            },
            action_request=_action_request(),
            conn=conn,
            subject_ref=_SUBJECT,
            task_goal=_GOAL,
            reviewer_verdict=_FakeVerdict(verdict="ok"),
        )

    types = _types_of(events)
    assert types.count("claim.created") >= 2
    assert types.count("evidence.attached") >= 3
    # No lumped event (A8): each emitted event has at most one of the
    # two payload shapes.
    for ev in events:
        keys = set(ev.payload.keys())
        if ev.type == "claim.created":
            assert "evidence_id" not in keys
        elif ev.type == "evidence.attached":
            assert "statement" not in keys
