"""Response rows for the incremental TTS pipeline, and the pipeline built on a fake provider."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.state.conversation import fold_conversation_history
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.lifecycle_terminal import terminalize_playback, terminalize_response
from jarvis.surface import voice_media, voice_tts
from tests.support.media import FakeProvider, RecordingBroadcaster, make_player, media_config

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from jarvis.shared import Event


def sha256_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class Trail:
    """Emit one incremental playback trail row by row, in Event Log order."""

    def __init__(self, conn: sqlite3.Connection, *, response_id: str = "R") -> None:
        self.conn = conn
        self.response_id = response_id
        self.chunks: list[Event] = []
        self.started: Event | None = None
        self.prepared: list[str] = []
        emit_event(
            conn,
            type="utterance.received",
            payload={"turn_id": "T", "transcript": "synthetic", "channel": "voice"},
        )
        self.open = emit_event(
            conn,
            type="surface.response_open",
            payload={
                "turn_id": "T",
                "query": "q",
                "kind": "stream",
                "response_id": response_id,
                "response_group_id": "G",
                "phase": "final",
                "channel": "speech",
            },
        )

    def _base(self) -> dict[str, Any]:
        return {
            "session_id": "S",
            "response_id": self.response_id,
            "turn_id": "T",
            "playback_generation_id": 1,
        }

    def chunk(self, text: str) -> Event:
        event = emit_event(
            self.conn,
            type="surface.response_chunk",
            payload={
                "turn_id": "T",
                "response_id": self.response_id,
                "sequence": len(self.chunks),
                "text": text,
                "segment_hash": sha256_of(text),
            },
        )
        self.chunks.append(event)
        return event

    def start(self, *, first_segment: str, source: Event | None = None) -> Event:
        self.started = emit_event(
            self.conn,
            type="surface.playback_started",
            payload={
                **self._base(),
                "phase": "final",
                "channel": "speech",
                "speech_text_hash": sha256_of(first_segment),
                "incremental": True,
            },
            source_event_id=(source or self.open).event_uid,
        )
        return self.started

    def prepare(self, sequence: int, *, source: Event | None = None) -> None:
        assert self.started is not None
        chunk = source or self.chunks[sequence]
        text = str(chunk.payload.get("text") or chunk.payload["voice_text"])
        if source is not None:
            committed = "".join(str(item.payload["text"]) for item in self.chunks)
            text = str(chunk.payload["voice_text"])[len(committed) :]
        self.prepared.append(text)
        emit_event(
            self.conn,
            type="surface.playback_segment_prepared",
            payload={
                **self._base(),
                "sequence": sequence,
                "speech_text": text,
                "speech_text_hash": sha256_of(text),
                "segment_hash": sha256_of(text),
                "source_chunk_event_uid": chunk.event_uid,
            },
            source_event_id=self.started.event_uid,
        )

    def _cursor(self, through: int) -> dict[str, Any]:
        heard = "".join(self.prepared[: through + 1])
        return {
            **self._base(),
            "heard_through_sequence": through,
            "submitted_samples": 160 * (through + 1),
            "heard_text": heard,
            "heard_text_hash": sha256_of(heard),
            "cursor_quality": "estimated",
        }

    def checkpoint(self, through: int) -> None:
        assert self.started is not None
        emit_event(
            self.conn,
            type="surface.playback_checkpoint",
            payload=self._cursor(through),
            source_event_id=self.started.event_uid,
        )

    def emitted(self, voice_text: str) -> Event:
        return emit_event(
            self.conn,
            type="surface.response_emitted",
            payload={
                "turn_id": "T",
                "text": voice_text,
                "voice_text": voice_text,
                "response_id": self.response_id,
                "phase": "final",
                "channel": "speech",
            },
        )

    def terminal(self, event_type: str, *, through: int) -> None:
        assert self.started is not None
        payload = self._cursor(through)
        if event_type == "surface.playback_completed":
            payload["speech_text_hash"] = sha256_of("".join(self.prepared))
        else:
            payload["reason"] = "synthetic stop"
        terminalize_playback(
            self.conn,
            event_type=event_type,
            payload=payload,
            source_event_id=self.started.event_uid,
        )


def spoken_rows(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    history = fold_conversation_history(iter_events(conn))
    record = history.turns[0].responses[0]
    heard = record.spoken_heard.text if record.spoken_heard is not None else None
    return history.consistent and record.consistent, heard


def make_pipeline(  # noqa: PLR0913 - one knob per pipeline option a test sets
    db_path: Path,
    provider: FakeProvider,
    *,
    speak_from_segments: bool,
    response_timeout_s: float | None = None,
    broadcaster: RecordingBroadcaster | None = None,
    silent_intent_channels: frozenset[str] = frozenset(),
) -> tuple[voice_media.StreamingTTSPipeline, voice_tts.AudioStreamPlayer]:
    player = make_player()
    config = replace(media_config(), speak_from_segments=speak_from_segments)
    if response_timeout_s is not None:
        config = replace(config, response_timeout_s=response_timeout_s)
    pipeline = voice_media.StreamingTTSPipeline(
        provider=provider,
        player=player,
        conn_factory=lambda: open_event_log(db_path),
        boot_high_water_id=0,
        config=config,
        broadcaster=broadcaster,
        start_player=False,
        silent_intent_channels=silent_intent_channels,
    )
    return pipeline, player


def row_of(conn: sqlite3.Connection, event: Event) -> tuple[int, Event]:
    found = conn.execute("SELECT id FROM events WHERE event_uid = ?", (event.event_uid,)).fetchone()
    assert found is not None
    return int(found[0]), event


def append_open(
    conn: sqlite3.Connection,
    response_id: str,
    *,
    kind: str = "stream",
    attention_channel: str | None = None,
) -> tuple[int, Event]:
    payload: dict[str, Any] = {
        "turn_id": "T-" + response_id,
        "query": "q",
        "kind": kind,
        "response_id": response_id,
        "response_group_id": "G-" + response_id,
        "phase": "final",
        "channel": "speech",
    }
    if attention_channel is not None:
        payload["attention_channel"] = attention_channel
    return row_of(conn, emit_event(conn, type="surface.response_open", payload=payload))


def append_chunk(
    conn: sqlite3.Connection, response_id: str, sequence: int, text: str
) -> tuple[int, Event]:
    return row_of(
        conn,
        emit_event(
            conn,
            type="surface.response_chunk",
            payload={
                "turn_id": "T-" + response_id,
                "text": text,
                "response_id": response_id,
                "response_group_id": "G-" + response_id,
                "sequence": sequence,
                "phase": "final",
                "channel": "speech",
                "segment_hash": sha256_of(text),
            },
        ),
    )


def append_emitted(
    conn: sqlite3.Connection, response_id: str, voice_text: str
) -> tuple[int, Event]:
    return row_of(
        conn,
        emit_event(
            conn,
            type="surface.response_emitted",
            payload={
                "turn_id": "T-" + response_id,
                "text": voice_text,
                "voice_text": voice_text,
                "response_id": response_id,
                "response_group_id": "G-" + response_id,
                "phase": "final",
                "channel": "speech",
            },
        ),
    )


def tts_rows(
    conn: sqlite3.Connection, event_type: str, response_id: str
) -> list[tuple[int, dict[str, Any], str | None]]:
    found = conn.execute(
        "SELECT id, payload_json, source_event_id FROM events WHERE type = ? "
        "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
        (event_type, response_id),
    ).fetchall()
    return [(int(row[0]), json.loads(str(row[1])), row[2]) for row in found]


def wait_for_rows(conn: sqlite3.Connection, event_type: str, response_id: str, count: int) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if len(tts_rows(conn, event_type, response_id)) >= count:
            return
        time.sleep(0.005)
    pytest.fail(f"{event_type} x{count} for {response_id} never became durable")


def append_cancel(conn: sqlite3.Connection, response_id: str) -> tuple[int, Event]:
    outcome = terminalize_response(
        conn,
        event_type="response.cancelled",
        payload={
            "response_id": response_id,
            "response_group_id": "G-" + response_id,
            "turn_id": "T-" + response_id,
            "reason": "operator_request",
        },
    )
    return row_of(conn, outcome.event)
