"""Display and playback rows that the conversation history folds."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from jarvis.state.event_log import emit_event, iter_events
from jarvis.state.lifecycle_terminal import terminalize_playback

if TYPE_CHECKING:
    import sqlite3

    from jarvis.shared import Event

PREFIX = "Ice absorbs heat."


FULL = "Ice absorbs heat. Water becomes warmer."


def input_event(conn: sqlite3.Connection, turn_id: str) -> Event:
    return emit_event(
        conn,
        type="utterance.received",
        payload={
            "turn_id": turn_id,
            "transcript": "synthetic " + turn_id,
            "channel": "voice",
        },
    )


def show_display(
    conn: sqlite3.Connection, *, channel: str = "speech", activate: bool = True
) -> None:
    input_event(conn, "previous")
    chunks = []
    for sequence, text in enumerate((PREFIX, FULL[len(PREFIX) :])):
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
            "text": FULL,
            "phase": "final",
            "channel": channel,
        },
    )
    if activate:
        activation_event(conn, surface, chunks, session="S", generation=1)


def activation_event(
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
            "speech_text_hash": hashlib.sha256(FULL.encode()).hexdigest(),
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


def record_playback(
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
        "heard_text": PREFIX,
        "heard_text_hash": hashlib.sha256(PREFIX.encode()).hexdigest(),
        "speech_text_hash": hashlib.sha256(FULL.encode()).hexdigest(),
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
