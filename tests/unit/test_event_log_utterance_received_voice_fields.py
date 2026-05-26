"""ADR-0005 §7: utterance.received gains voice-adapter optional fields."""
from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from jarvis.state.event_log import (
    EventTypeRegistry,
    emit_event,
    open_event_log,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a fresh Event Log at tmp_path/events.db and close on teardown."""
    with closing(open_event_log(tmp_path / "events.db")) as conn:
        yield conn


def test_utterance_received_accepts_voice_optional_fields(conn: sqlite3.Connection) -> None:
    """All four new optional fields are emitted without registry rejection."""
    ev = emit_event(
        conn,
        type="utterance.received",
        payload={
            "transcript": "你好",
            "turn_id": "T1a2b3c4d",
            "channel": "inherent_wake",
            "language": "zh-CN",
            "confidence": 0.9,
            "language_detected": "zh-CN",
            "emotion": "HAPPY",
            "audio_artifact_ref": "data/voice_artifacts/T1a2b3c4d.wav",
        },
    )
    assert ev.payload["confidence"] == 0.9
    assert ev.payload["emotion"] == "HAPPY"
    assert ev.payload["audio_artifact_ref"].endswith(".wav")


def test_utterance_received_registry_lists_new_optional_fields() -> None:
    """Registry entry advertises the four new optional fields."""
    schema = EventTypeRegistry.get("utterance.received")
    assert schema is not None
    for new_field in ("confidence", "language_detected", "emotion", "audio_artifact_ref"):
        assert new_field in schema.optional_payload, f"{new_field} missing from optional_payload"
