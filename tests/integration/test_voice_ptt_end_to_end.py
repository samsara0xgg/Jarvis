"""ADR-0005 integration smoke: PTT WAV -> utterance.received -> drive_turn dispatch.

Wires the real :class:`VoicePipeline` (mocked recognizer + no-op normalizer)
behind the real ``/inherent/asr-submit`` handler and verifies the full
happy-path lands an ``utterance.received`` row in the L2 event log with
the normalized transcript + ``inherent_ptt`` channel marker.
"""
from __future__ import annotations

import io
import json
import wave
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared import Event


def _wav(duration_s: float = 0.5) -> bytes:
    """Build a tiny synthetic mono PCM16 WAV blob — small but above RMS floor."""
    pcm = b"\x10\x00" * int(16000 * duration_s)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm)
    return buf.getvalue()


def test_ptt_end_to_end_creates_utterance_received_row(tmp_path: Path) -> None:
    """POST /inherent/asr-submit -> emit utterance.received with inherent_ptt channel."""
    db_path = tmp_path / "events.db"
    conn = open_event_log(db_path)

    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好",
        confidence=0.9,
        language_detected="zh-CN",
        emotion=None,
    )
    normalizer = voice_asr.AsrNormalizer(
        corrections=[],
        aliases={},
        fuzzy_enabled=False,
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=recognizer,
        normalizer=normalizer,
        broadcaster=None,
        artifacts_dir=tmp_path,
    )

    # run_turn is keyword-only but voice_pipeline_callable expects positional.
    def _ptt_callable(
        audio_bytes: bytes,
        turn_id: str,
        channel: str,
        language: str,
    ) -> Event:
        return pipeline.run_turn(
            audio_bytes=audio_bytes,
            turn_id=turn_id,
            channel=channel,
            language=language,
        )

    deps = InherentDeps(
        submit_callable=lambda _text: None,
        broadcaster=InherentBroadcaster(),
        voice_pipeline_callable=_ptt_callable,
    )
    app = create_app(deps)
    client = TestClient(app)

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", _wav(), "audio/wav")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["transcript"] == "你好"
    assert body["status"] == "accepted"
    assert body["turn_id"].startswith("T")

    # Verify the event log row landed with the right shape.
    row = conn.execute(
        "SELECT type, payload_json FROM events ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert row is not None
    assert row[0] == "utterance.received"
    payload = json.loads(row[1])
    assert payload["transcript"] == "你好"
    assert payload["channel"] == "inherent_ptt"
    assert payload["language"] == "zh-CN"
    assert payload["turn_id"] == body["turn_id"]
    conn.close()
