"""Event Log replay into typed conversation history without audibility inflation."""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.packet import assemble_packet
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from tests.support.conversation_history import (
    FULL,
    PREFIX,
    activation_event,
    input_event,
    record_playback,
    show_display,
)

if TYPE_CHECKING:
    from pathlib import Path



def test_late_checkpoint_cannot_extend_terminal_prefix(tmp_path: Path) -> None:
    """A stale callback after interruption cannot make the unheard suffix history."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        terminal = record_playback(conn, "surface.playback_interrupted", {})
        record_playback(
            conn,
            "surface.playback_checkpoint",
            {
                "heard_text": FULL,
                "heard_text_hash": hashlib.sha256(FULL.encode()).hexdigest(),
                "heard_through_sequence": 1,
            },
        )
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert heard is not None
        assert heard.text == PREFIX
        assert heard.source_event_uid == terminal.event_uid


def test_history_bound_is_explicit_and_does_not_hide_current_input(tmp_path: Path) -> None:
    """The bounded window reports truncation rather than inventing complete old context."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        for index in range(25):
            input_event(conn, str(index))
        history = fold_conversation_history(iter_events(conn), max_turns=20)
        assert history.truncated
        assert len(history.turns) == 20
        assert history.turns[0].turn_id == "5"
        assert history.turns[-1].turn_id == "24"


def test_inconsistent_chunk_history_cannot_be_a_spoken_transcript(tmp_path: Path) -> None:
    """Conflicting sequence reuse is surfaced, not silently joined into prior speech."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        for text in ("first", "different"):
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": "previous",
                    "response_id": "R",
                    "text": text,
                    "sequence": 0,
                },
            )
        record_playback(conn, "surface.playback_completed", {})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize(
    "fields",
    [
        {
            "heard_text": "Never generated.",
            "heard_text_hash": hashlib.sha256(b"Never generated.").hexdigest(),
        },
        {"speech_text_hash": "wrong"},
        {"heard_through_sequence": 5},
        {"heard_text": "\ud800", "heard_text_hash": "wrong"},
        {"cursor_quality": ["estimated"]},
    ],
)
def test_real_log_rejects_unbound_or_malformed_heard_evidence(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    """A valid database row and self-hash alone cannot create a spoken transcript."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        record_playback(conn, "surface.playback_checkpoint", fields)
        history = fold_conversation_history(iter_events(conn))
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize(("channel", "activate"), [("document", True), ("speech", False)])
def test_document_and_legacy_unactivated_rows_remain_unheard(
    tmp_path: Path, channel: str, *, activate: bool
) -> None:
    """Panel contents and cursor-shaped rows lack an eligible speech activation."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn, channel=channel, activate=activate)
        record_playback(conn, "surface.playback_checkpoint", {})
        assert (
            fold_conversation_history(iter_events(conn)).turns[0].responses[0].spoken_heard is None
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"submitted_samples": 1},
        {
            "heard_through_sequence": 0,
            "heard_text": PREFIX,
            "heard_text_hash": hashlib.sha256(PREFIX.encode()).hexdigest(),
        },
        {
            "heard_text": "Conflicting longer text.",
            "heard_text_hash": hashlib.sha256(b"Conflicting longer text.").hexdigest(),
        },
    ],
)
def test_checkpoint_regression_or_conflict_invalidates_heard(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    """Monotonic sequence, samples and exact segment prefix are all required."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        completed_prefix = {
            "heard_text": FULL,
            "heard_text_hash": hashlib.sha256(FULL.encode()).hexdigest(),
            "heard_through_sequence": 1,
        }
        record_playback(conn, "surface.playback_checkpoint", completed_prefix)
        record_playback(conn, "surface.playback_checkpoint", {**completed_prefix, **fields})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize(("session", "generation"), [("S", 2), ("S2", 2)])
def test_old_lease_cannot_extend_new_activation_prefix(
    tmp_path: Path, session: str, generation: int
) -> None:
    """A longer late old-generation prefix cannot replace a newer legal cursor."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        record_playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        activation_event(conn, surface, chunks, session=session, generation=generation)
        current = record_playback(
            conn,
            "surface.playback_checkpoint",
            {"session_id": session, "playback_generation_id": generation},
        )
        record_playback(
            conn,
            "surface.playback_checkpoint",
            {
                "heard_text": FULL,
                "heard_text_hash": hashlib.sha256(FULL.encode()).hexdigest(),
                "heard_through_sequence": 1,
            },
        )
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert history.consistent
        assert heard is not None
        assert heard.text == PREFIX
        assert heard.source_event_uid == current.event_uid
        assert heard.session_id == session
        assert heard.playback_generation_id == generation


def test_generated_audit_text_never_enters_conversational_prompt(tmp_path: Path) -> None:
    """A large private audit draft remains outside the context budget and transcript."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        draft = "AUDIT-ONLY-PRIVATE-DRAFT" * 50_000
        emit_event(
            conn,
            type="response.completed",
            payload={
                "turn_id": "previous",
                "response_id": "R",
                "response_group_id": "G",
                "response_hash": hashlib.sha256(FULL.encode()).hexdigest(),
                "generated_text": draft,
                "generated_text_hash": hashlib.sha256(draft.encode()).hexdigest(),
            },
        )
        trigger = input_event(conn, "next")
        packet = assemble_packet(trigger, conn)
        assert packet.conversation_history is not None
        assert (
            packet.conversation_history.turns[0].responses[0].audit_generated_hash
            == hashlib.sha256(draft.encode()).hexdigest()
        )


@pytest.mark.parametrize("cursor", [None, "empty", "unknown"])
def test_new_lease_without_more_heard_text_preserves_prior_proof(
    tmp_path: Path, cursor: str | None
) -> None:
    """Starting/replaying a response cannot undo words already supported by a cursor."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        proof = record_playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        activation_event(conn, surface, chunks, session="S", generation=2)
        if cursor is not None:
            record_playback(
                conn,
                "surface.playback_checkpoint",
                {
                    "playback_generation_id": 2,
                    "heard_text": "",
                    "heard_text_hash": hashlib.sha256(b"").hexdigest(),
                    "heard_through_sequence": None,
                    "submitted_samples": 0,
                    "cursor_quality": "unknown" if cursor == "unknown" else "estimated",
                },
            )
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert heard is not None
        assert history.consistent
        assert heard.text == PREFIX
        assert heard.source_event_uid == proof.event_uid


def test_playback_generation_cannot_be_rebound_to_another_session(tmp_path: Path) -> None:
    """The canonical response/generation terminal identity still has one session owner."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        show_display(conn)
        record_playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        activation_event(conn, surface, chunks, session="other-session", generation=1)
        record_playback(conn, "surface.playback_checkpoint", {"session_id": "other-session"})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize("fact_type", ["surface.response_chunk", "surface.response_emitted"])
def test_projection_caps_response_text_and_reports_unknown_heard(
    tmp_path: Path, fact_type: str
) -> None:
    """A large durable response cannot expand the per-response context projection."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        input_event(conn, "previous")
        emit_event(
            conn,
            type=fact_type,
            payload={
                "turn_id": "previous",
                "response_id": "R",
                "sequence": 0,
                "text": "x" * 100_000,
            },
        )
        history = fold_conversation_history(iter_events(conn))
        assert history.truncated
        record = history.turns[0].responses[0]
        assert record.truncated
        assert len(record.panel_available) <= 65_536
        assert record.spoken_heard is None
