"""Event Log replay into typed conversation history without audibility inflation."""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision import decide
from jarvis.decision.conversation import conversation_history_note
from jarvis.decision.llm import LLMClient
from jarvis.decision.packet import assemble_packet
from jarvis.runtime import _wave4_response_activation, drive_turn
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback
from tests.integration.test_conversational_turn_no_gate_downgrade import _build_ctx, _StubLLMClient
from tests.integration.test_wave4a_response_run import _make_runtime, _realtime_config

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.decision.llm import ChatResult
    from jarvis.shared import Event

_PREFIX = "Ice absorbs heat."
_FULL = "Ice absorbs heat. Water becomes warmer."


def _input(conn: sqlite3.Connection, turn_id: str) -> Event:
    return emit_event(
        conn,
        type="utterance.received",
        payload={
            "turn_id": turn_id,
            "transcript": "synthetic " + turn_id,
            "channel": "voice",
        },
    )


def _display(conn: sqlite3.Connection, *, channel: str = "speech", activate: bool = True) -> None:
    _input(conn, "previous")
    chunks = []
    for sequence, text in enumerate((_PREFIX, _FULL[len(_PREFIX) :])):
        chunks.append(
            emit_event(
                conn,
                type="surface.response_chunk",
                payload={
                    "turn_id": "previous",
                    "response_id": "R",
                    "text": text,
                    "sequence": sequence,
                },
            )
        )
    surface = emit_event(
        conn,
        type="surface.response_emitted",
        payload={
            "turn_id": "previous",
            "response_id": "R",
            "text": _FULL,
            "phase": "final",
            "channel": channel,
        },
    )
    if activate:
        _activation(conn, surface, chunks, session="S", generation=1)


def _activation(
    conn: sqlite3.Connection, surface: Event, chunks: list[Event], *, session: str, generation: int
) -> Event:
    start = emit_event(
        conn,
        type="surface.playback_started",
        payload={
            "session_id": session,
            "response_id": "R",
            "turn_id": "previous",
            "playback_generation_id": generation,
            "phase": "final",
            "channel": surface.payload["channel"],
            "speech_text_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
        },
        source_event_id=surface.event_uid,
    )
    for chunk in chunks:
        text = chunk.payload["text"]
        emit_event(
            conn,
            type="surface.playback_segment_prepared",
            payload={
                "session_id": session,
                "response_id": "R",
                "turn_id": "previous",
                "playback_generation_id": generation,
                "sequence": chunk.payload["sequence"],
                "speech_text": text,
                "speech_text_hash": hashlib.sha256(text.encode()).hexdigest(),
                "segment_hash": hashlib.sha256(text.encode()).hexdigest(),
                "source_chunk_event_uid": chunk.event_uid,
            },
            source_event_id=start.event_uid,
        )
    return start


def _playback(
    conn: sqlite3.Connection,
    event_type: str,
    fields: dict[str, Any],
) -> Event:
    payload = {
        "session_id": "S",
        "response_id": "R",
        "turn_id": "previous",
        "playback_generation_id": 1,
        "heard_through_sequence": 0,
        "submitted_samples": 160,
        "heard_text": _PREFIX,
        "heard_text_hash": hashlib.sha256(_PREFIX.encode()).hexdigest(),
        "speech_text_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
        "cursor_quality": "estimated",
        "reason": "synthetic stop",
    }
    payload.update(fields)
    activations = [
        event
        for event in iter_events(conn)
        if event.type == "surface.playback_started"
        and event.payload["session_id"] == payload["session_id"]
        and event.payload["playback_generation_id"] == payload["playback_generation_id"]
    ]
    source = activations[-1].event_uid if activations else None
    if event_type == "surface.playback_checkpoint":
        return emit_event(conn, type=event_type, payload=payload, source_event_id=source)
    return terminalize_playback(
        conn, event_type=event_type, payload=payload, source_event_id=source
    ).event


@pytest.mark.parametrize(
    ("kind", "fields", "expected"),
    [
        ("surface.playback_checkpoint", {}, _PREFIX),
        ("surface.playback_interrupted", {}, _PREFIX),
        ("surface.playback_failed", {}, _PREFIX),
        ("surface.playback_completed", {}, _PREFIX),
        ("surface.playback_completed", {"heard_text": None}, None),
        ("surface.playback_completed", {"heard_text_hash": "wrong"}, None),
        ("surface.playback_checkpoint", {"cursor_quality": "unknown"}, None),
        ("surface.playback_interrupted", {"cursor_quality": "physical"}, None),
        ("surface.playback_completed", {"heard_through_sequence": None}, None),
        ("surface.playback_completed", {"submitted_samples": 0}, None),
        (
            "surface.playback_interrupted",
            {
                "heard_text": "",
                "heard_text_hash": hashlib.sha256(b"").hexdigest(),
                "heard_through_sequence": None,
            },
            "",
        ),
    ],
)
def test_playback_evidence_controls_heard_channel(
    tmp_path: Path,
    kind: str,
    fields: dict[str, Any],
    expected: str | None,
) -> None:
    """Completed/failed labels and available full text cannot replace explicit evidence."""
    path = tmp_path / "events.db"
    with contextlib.closing(open_event_log(path)) as conn:
        _display(conn)
        proof = _playback(conn, kind, fields)
        trigger = _input(conn, "next")
        packet = assemble_packet(trigger, conn)
        note = conversation_history_note(packet)
        assert note is not None
        body = json.loads(note.rsplit("\n", 1)[-1])
        output = body["turns"][0]["responses"][0]
        assert output["panel_available"] == _FULL
        assert "audit_generated" not in output
        if expected is None:
            assert output["spoken_heard"] is None
        else:
            assert output["spoken_heard"]["text"] == expected
            assert output["spoken_heard"]["source_event_uid"] == proof.event_uid
            assert output["spoken_heard"]["cursor_quality"] == "estimated"
    with contextlib.closing(open_event_log(path)) as conn:
        replay = assemble_packet(trigger, conn)
        assert replay.conversation_history == packet.conversation_history
        assert conversation_history_note(replay) == note


def test_generation_and_display_without_playback_never_become_heard(tmp_path: Path) -> None:
    """There is no inference from a surface final or response completion to audibility."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        trigger = _input(conn, "next")
        note = conversation_history_note(assemble_packet(trigger, conn))
        assert note is not None
        output = json.loads(note.rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
        assert output["spoken_heard"] is None
        assert output["panel_available"] == _FULL


def test_late_checkpoint_cannot_extend_terminal_prefix(tmp_path: Path) -> None:
    """A stale callback after interruption cannot make the unheard suffix history."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        terminal = _playback(conn, "surface.playback_interrupted", {})
        _playback(
            conn,
            "surface.playback_checkpoint",
            {
                "heard_text": _FULL,
                "heard_text_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
                "heard_through_sequence": 1,
            },
        )
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert heard is not None
        assert heard.text == _PREFIX
        assert heard.source_event_uid == terminal.event_uid


def test_history_bound_is_explicit_and_does_not_hide_current_input(tmp_path: Path) -> None:
    """The bounded window reports truncation rather than inventing complete old context."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        for index in range(25):
            _input(conn, str(index))
        history = fold_conversation_history(iter_events(conn), max_turns=20)
        assert history.truncated
        assert len(history.turns) == 20
        assert history.turns[0].turn_id == "5"
        assert history.turns[-1].turn_id == "24"


def test_inconsistent_chunk_history_cannot_be_a_spoken_transcript(tmp_path: Path) -> None:
    """Conflicting sequence reuse is surfaced, not silently joined into prior speech."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
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
        _playback(conn, "surface.playback_completed", {})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize("enabled", [False, True])
def test_typed_history_reaches_real_decide_prompt_only_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> None:
    """The actual L3 message builder keeps heard/panel fields distinct at model input."""
    ctx, conn, client = _build_ctx(tmp_path, draft_text="水分子更自由地运动。")
    messages: list[dict[str, Any]] = []
    original = client.chat

    def capture(**kwargs: Any) -> Any:  # noqa: ANN401 - capture the scripted provider boundary
        messages.extend(kwargs["messages"])
        return original(**kwargs)

    monkeypatch.setattr(client, "chat", capture)
    try:
        _display(conn)
        _playback(conn, "surface.playback_interrupted", {})
        trigger = _input(conn, "next")
        result = decide(trigger, replace(ctx, typed_conversation_history=enabled))
        assert result.response_plan is not None
        assert client.chat_calls == 1
        notes = [
            item["content"]
            for item in messages
            if isinstance(item.get("content"), str)
            and item["content"].startswith("[typed conversation history]")
        ]
        assert len(notes) == int(enabled)
        if enabled:
            output = json.loads(notes[0].rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
            assert output["spoken_heard"]["text"] == _PREFIX
            assert output["panel_available"] == _FULL
            assert not any(
                item["role"] == "assistant" and item["content"] == _FULL for item in messages
            )
    finally:
        conn.close()


@pytest.mark.parametrize("enabled", [False, True])
def test_runtime_threads_validated_history_flag_to_actual_model_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enabled: bool,
) -> None:
    """Run the real driver, decision, projections and renderer around a scripted LLM."""
    runtime = _make_runtime(tmp_path, lifecycle=True)
    config = _realtime_config(lifecycle=True, cancel=False)
    config["realtime"]["response"]["typed_conversation_history"] = enabled
    runtime = replace(
        runtime, config=config, response_flags=_wave4_response_activation(config).flags
    )
    messages: list[dict[str, Any]] = []
    fixture = _StubLLMClient("水分子更自由地运动。")

    def capture(_client: LLMClient, **kwargs: Any) -> ChatResult:  # noqa: ANN401 - scripted provider boundary
        messages.extend(kwargs["messages"])
        return fixture.chat(**kwargs)

    monkeypatch.setattr(LLMClient, "chat", capture)
    try:
        _display(runtime.conn)
        _playback(runtime.conn, "surface.playback_interrupted", {})
        trigger = _input(runtime.conn, "next")
        result = drive_turn(runtime, user_intent_event=trigger, available_surfaces=frozenset())
        assert result.response_plan is not None
        notes = [
            item["content"]
            for item in messages
            if "[typed conversation history]" in item["content"]
        ]
        assert len(notes) == int(enabled)
        if enabled:
            response = json.loads(notes[0].rsplit("\n", 1)[-1])["turns"][0]["responses"][0]
            assert response["spoken_heard"]["text"] == _PREFIX
            assert response["panel_available"] == _FULL
    finally:
        runtime.conn.close()


@pytest.mark.parametrize(
    ("value", "lifecycle", "parent", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        ("true", True, True, False),
        (1, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
def test_typed_history_activation_is_explicit_and_lifecycle_bound(
    value: object,
    *,
    lifecycle: bool,
    parent: bool,
    expected: bool,
) -> None:
    """Absent/non-boolean/unsupported activation cannot alter legacy prompt context."""
    config = _realtime_config(lifecycle=lifecycle, cancel=False, enabled=parent)
    config["realtime"]["response"]["typed_conversation_history"] = value
    assert _wave4_response_activation(config).flags.typed_conversation_history is expected


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
        _display(conn)
        _playback(conn, "surface.playback_checkpoint", fields)
        history = fold_conversation_history(iter_events(conn))
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize(("channel", "activate"), [("document", True), ("speech", False)])
def test_document_and_legacy_unactivated_rows_remain_unheard(
    tmp_path: Path, channel: str, *, activate: bool
) -> None:
    """Panel contents and cursor-shaped rows lack an eligible speech activation."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn, channel=channel, activate=activate)
        _playback(conn, "surface.playback_checkpoint", {})
        assert (
            fold_conversation_history(iter_events(conn)).turns[0].responses[0].spoken_heard is None
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"submitted_samples": 1},
        {
            "heard_through_sequence": 0,
            "heard_text": _PREFIX,
            "heard_text_hash": hashlib.sha256(_PREFIX.encode()).hexdigest(),
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
        _display(conn)
        completed_prefix = {
            "heard_text": _FULL,
            "heard_text_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
            "heard_through_sequence": 1,
        }
        _playback(conn, "surface.playback_checkpoint", completed_prefix)
        _playback(conn, "surface.playback_checkpoint", {**completed_prefix, **fields})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize(("session", "generation"), [("S", 2), ("S2", 2)])
def test_old_lease_cannot_extend_new_activation_prefix(
    tmp_path: Path, session: str, generation: int
) -> None:
    """A longer late old-generation prefix cannot replace a newer legal cursor."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        _playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        _activation(conn, surface, chunks, session=session, generation=generation)
        current = _playback(
            conn,
            "surface.playback_checkpoint",
            {"session_id": session, "playback_generation_id": generation},
        )
        _playback(
            conn,
            "surface.playback_checkpoint",
            {
                "heard_text": _FULL,
                "heard_text_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
                "heard_through_sequence": 1,
            },
        )
        history = fold_conversation_history(iter_events(conn))
        heard = history.turns[0].responses[0].spoken_heard
        assert history.consistent
        assert heard is not None
        assert heard.text == _PREFIX
        assert heard.source_event_uid == current.event_uid
        assert heard.session_id == session
        assert heard.playback_generation_id == generation


def test_generated_audit_text_never_enters_conversational_prompt(tmp_path: Path) -> None:
    """A large private audit draft remains outside the context budget and transcript."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        draft = "AUDIT-ONLY-PRIVATE-DRAFT" * 50_000
        emit_event(
            conn,
            type="response.completed",
            payload={
                "turn_id": "previous",
                "response_id": "R",
                "response_group_id": "G",
                "response_hash": hashlib.sha256(_FULL.encode()).hexdigest(),
                "generated_text": draft,
                "generated_text_hash": hashlib.sha256(draft.encode()).hexdigest(),
            },
        )
        trigger = _input(conn, "next")
        packet = assemble_packet(trigger, conn)
        note = conversation_history_note(packet)
        assert note is not None
        assert "AUDIT-ONLY-PRIVATE-DRAFT" not in note
        assert len(note) < 13_000
        assert packet.conversation_history is not None
        assert (
            packet.conversation_history.turns[0].responses[0].audit_generated_hash
            == hashlib.sha256(draft.encode()).hexdigest()
        )


def test_prompt_has_serialized_total_text_and_response_bounds(tmp_path: Path) -> None:
    """Long panel/user payloads and many responses produce explicit bounded omissions."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        for turn_index in range(22):
            turn_id = str(turn_index)
            emit_event(
                conn,
                type="utterance.received",
                payload={"turn_id": turn_id, "transcript": "长问题" * 1000},
            )
            for response_index in range(10):
                emit_event(
                    conn,
                    type="surface.response_emitted",
                    payload={
                        "turn_id": turn_id,
                        "response_id": f"R{turn_id}-{response_index}",
                        "text": "长文档" * 1000,
                    },
                )
        trigger = _input(conn, "next")
        note = conversation_history_note(assemble_packet(trigger, conn))
        assert note is not None
        encoded = note.rsplit("\n", 1)[-1]
        assert len(encoded) <= 12_000
        assert json.loads(encoded)["truncated"]


@pytest.mark.parametrize("cursor", [None, "empty", "unknown"])
def test_new_lease_without_more_heard_text_preserves_prior_proof(
    tmp_path: Path, cursor: str | None
) -> None:
    """Starting/replaying a response cannot undo words already supported by a cursor."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        proof = _playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        _activation(conn, surface, chunks, session="S", generation=2)
        if cursor is not None:
            _playback(
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
        assert heard.text == _PREFIX
        assert heard.source_event_uid == proof.event_uid


def test_playback_generation_cannot_be_rebound_to_another_session(tmp_path: Path) -> None:
    """The canonical response/generation terminal identity still has one session owner."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _display(conn)
        _playback(conn, "surface.playback_interrupted", {})
        events = tuple(iter_events(conn))
        surface = next(event for event in events if event.type == "surface.response_emitted")
        chunks = [event for event in events if event.type == "surface.response_chunk"]
        _activation(conn, surface, chunks, session="other-session", generation=1)
        _playback(conn, "surface.playback_checkpoint", {"session_id": "other-session"})
        history = fold_conversation_history(iter_events(conn))
        assert not history.consistent
        assert history.turns[0].responses[0].spoken_heard is None


@pytest.mark.parametrize("fact_type", ["surface.response_chunk", "surface.response_emitted"])
def test_projection_caps_response_text_and_reports_unknown_heard(
    tmp_path: Path, fact_type: str
) -> None:
    """A large durable response cannot expand the per-response context projection."""
    with contextlib.closing(open_event_log(tmp_path / "events.db")) as conn:
        _input(conn, "previous")
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
