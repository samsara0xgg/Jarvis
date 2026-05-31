"""Unit tests for the Result Interpreter (ADR § Gate contracts, Post-action row).

Each row of the semantics → (ClaimType, EvidenceLevel) mapping emits
the expected pair. Build fake RawResultLike + ActionRequest +
tmp_path-backed SQLite Event Log; no LLM involvement.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.result_interpreter import (
    emit_stash_conflict_surfacing,
    result_interpreter,
)
from jarvis.shared import ActionRequest, CallerPrincipal, Event, ResultSemantics
from jarvis.state.event_log import emit_event, iter_events, open_event_log

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping
    from pathlib import Path


@dataclass(frozen=True)
class _FakeRawResult:
    """RawResultLike-compatible record for unit tests."""

    action_id: str
    semantics: ResultSemantics
    payload: Mapping[str, Any]
    tool_output: str | None = None
    error: str | None = None


def _seed_source_event(conn: sqlite3.Connection) -> Event:
    """Append a single `action.result_observed` row to seed source_event_id."""
    return emit_event(
        conn,
        type="action.result_observed",
        payload={"action_id": "A1", "semantics": "verification"},
        correlation={"action_id": "A1"},
    )


def _action_request() -> ActionRequest:
    """Default ActionRequest used as the originator."""
    return ActionRequest(
        action_id="A1",
        tool_name="verify_diff",
        target_entity_ref="task_X",
        caller_principal=CallerPrincipal.JARVIS_LLM,
        risk_level="L0",
        arguments={},
        authorization_lease=None,
        run_id="R1",
        turn_id="T1",
    )


@pytest.mark.parametrize(
    ("semantics", "expected_claim_type", "expected_evidence_level"),
    [
        ("ack", "Execution", "executed"),
        ("observation", "Artifact", "observed"),
        ("verification", "Postcondition", "verified"),
        ("report", "Report", "reported"),
        ("error", "Limitation", "reported"),
    ],
)
def test_result_interpreter_maps_semantics_to_claim_evidence(
    tmp_path: Path,
    semantics: ResultSemantics,
    expected_claim_type: str,
    expected_evidence_level: str,
) -> None:
    """Each ADR table row emits the expected (ClaimType, EvidenceLevel)."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source = _seed_source_event(conn)
        raw = _FakeRawResult(
            action_id="A1",
            semantics=semantics,
            payload={"artifact_path": "/tmp/test.json"},
            error="predicate_failed" if semantics == "error" else None,
        )

        claim_event, evidence_event = result_interpreter(
            raw,
            source_event_id=source.event_uid,
            action_request=_action_request(),
            conn=conn,
        )

        assert claim_event.type == "claim.created"
        assert claim_event.payload["type"] == expected_claim_type
        assert claim_event.payload["subject_ref"] == "task_X"
        assert claim_event.payload["produced_by_event_id"] == source.event_uid

        assert evidence_event.type == "evidence.attached"
        assert evidence_event.payload["level"] == expected_evidence_level
        assert evidence_event.payload["claim_id"] == claim_event.payload["claim_id"]


def test_result_interpreter_emits_two_separate_events(tmp_path: Path) -> None:
    """ADR Acceptance A8: claim and evidence are SEPARATE events."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source = _seed_source_event(conn)
        raw = _FakeRawResult(
            action_id="A1",
            semantics="verification",
            payload={"artifact_path": "/tmp/test.json", "content_hash": "abc123"},
        )
        result_interpreter(
            raw,
            source_event_id=source.event_uid,
            action_request=_action_request(),
            conn=conn,
        )

        rows = [
            e
            for e in iter_events(conn)
            if e.type in ("claim.created", "evidence.attached")
        ]
        assert len(rows) == 2
        assert rows[0].type == "claim.created"
        assert rows[1].type == "evidence.attached"
        # ADR A8: no single event lumps both shapes. claim.created carries
        # `statement`/`type` and MUST NOT carry `evidence_id`;
        # evidence.attached carries `evidence_id`/`level` and MUST NOT
        # carry `statement` or `type`.
        for row in iter_events(conn):
            payload_keys = set(row.payload.keys())
            if row.type == "claim.created":
                assert "evidence_id" not in payload_keys
            elif row.type == "evidence.attached":
                assert "statement" not in payload_keys
                assert "type" not in payload_keys


def test_result_interpreter_propagates_artifact_path_to_evidence(tmp_path: Path) -> None:
    """``artifact_path`` / ``content_hash`` from raw.payload land in evidence."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source = _seed_source_event(conn)
        raw = _FakeRawResult(
            action_id="A1",
            semantics="verification",
            payload={"artifact_path": "/tmp/diff.json", "content_hash": "sha-abc"},
        )
        _claim_event, evidence_event = result_interpreter(
            raw,
            source_event_id=source.event_uid,
            action_request=_action_request(),
            conn=conn,
        )

        assert evidence_event.payload["artifact_path"] == "/tmp/diff.json"
        assert evidence_event.payload["content_hash"] == "sha-abc"


def test_result_interpreter_subject_ref_override(tmp_path: Path) -> None:
    """Explicit subject_ref_override wins over action_request.target_entity_ref."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source = _seed_source_event(conn)
        raw = _FakeRawResult(
            action_id="A1",
            semantics="ack",
            payload={},
        )
        claim_event, _evidence_event = result_interpreter(
            raw,
            source_event_id=source.event_uid,
            action_request=_action_request(),
            conn=conn,
            subject_ref_override="task_override",
        )

        assert claim_event.payload["subject_ref"] == "task_override"


# --- J13: stash-pop conflict surfacing -------------------------------------


def test_emit_stash_conflict_surfacing_emits_artifact_and_limitation(
    tmp_path: Path,
) -> None:
    """J13: a stash-pop conflict surfaces worker.artifact_observed + a Limitation pair.

    The runtime hands this L3 helper the conflict patch path + reason
    (primitives, not the L4 ``StashConflictArtifact``); it must emit one
    ``worker.artifact_observed(kind=stash_conflict)`` correlated to the
    spawn_worker action plus a ``Limitation`` ``claim.created`` +
    ``evidence.attached(relation=limits, level=reported)`` pair.
    """
    patch_path = tmp_path / "run_R1" / "conflict.patch"
    patch_path.parent.mkdir(parents=True)
    patch_path.write_text(
        "diff --git a/a.txt b/a.txt\n@@ -1 +1 @@\n-base\n+allen edit\n",
        encoding="utf-8",
    )
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        source = _seed_source_event(conn)
        artifact_event, claim_event, evidence_event = emit_stash_conflict_surfacing(
            conn,
            patch_path=patch_path,
            reason="stash_pop_conflict",
            run_id="R1",
            action_id="A1",
            task_id="task_X",
            source_event_id=source.event_uid,
            correlation={"run_id": "R1", "task_id": "task_X", "turn_id": "T1"},
        )

        assert artifact_event.type == "worker.artifact_observed"
        assert artifact_event.payload["kind"] == "stash_conflict"
        assert artifact_event.payload["artifact_path"] == str(patch_path)
        assert artifact_event.payload["action_id"] == "A1"
        assert artifact_event.payload["run_id"] == "R1"
        assert artifact_event.payload["content_hash"], "content_hash must be non-empty"

        assert claim_event.type == "claim.created"
        assert claim_event.payload["type"] == "Limitation"
        assert claim_event.payload["subject_ref"] == "task_X"

        assert evidence_event.type == "evidence.attached"
        assert evidence_event.payload["relation"] == "limits"
        assert evidence_event.payload["level"] == "reported"
        assert evidence_event.payload["claim_id"] == claim_event.payload["claim_id"]
        assert evidence_event.payload["artifact_path"] == str(patch_path)

        # All three rows persisted, in emission order, chained off the source.
        tail = [e.type for e in iter_events(conn)][-3:]
        assert tail == ["worker.artifact_observed", "claim.created", "evidence.attached"]
