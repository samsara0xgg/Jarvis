"""ADR-0005 §5.2: /inherent/asr-submit happy and error paths.

Exercises the real handler that supersedes the ADR-0003 501 stub.
The ``voice_pipeline_callable`` injected via :class:`InherentDeps`
is a per-test fake that returns a stub :class:`Event` or raises one of
``voice_pipeline.VoicePipelineEmptyError`` / ``VoiceInputBusyError`` /
``RuntimeError`` so each status code in §5.2 is covered without
touching real ASR.
"""
from __future__ import annotations

import io
import wave
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jarvis.runtime import inherent_loop
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_pipeline
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.shared import Event


def _build_wav_bytes(*, duration_s: float = 0.5, sample_rate: int = 16000) -> bytes:
    """Build a tiny synthetic mono PCM16 WAV blob for upload."""
    n_samples = int(duration_s * sample_rate)
    pcm = (b"\x10\x00") * n_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _noop_submit(text: str) -> None:
    """Stand-in for the ADR-0003 text submit callable (never invoked by these tests)."""
    _ = text


def _make_deps(pipeline_callable: Callable[[bytes, str, str, str], Event]) -> InherentDeps:
    """Build :class:`InherentDeps` wired to the test's fake voice pipeline."""
    return InherentDeps(
        submit_callable=_noop_submit,
        broadcaster=InherentBroadcaster(),
        voice_pipeline_callable=pipeline_callable,
    )


def test_asr_submit_happy_path_returns_transcript() -> None:
    """Happy path: WAV in → 200 + normalized transcript + server-minted turn_id."""
    fake_event = MagicMock()
    fake_event.payload = {"transcript": "你好"}

    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001 — fake echoes a fixed event
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        return fake_event

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))
    wav = _build_wav_bytes()

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["transcript"] == "你好"
    assert body["turn_id"].startswith("T")


def test_asr_submit_too_large_returns_413() -> None:
    """6 MB upload exceeds ADR-0005 §5.2's 5 MB cap → 413; pipeline must not run."""
    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        pytest.fail("pipeline should not be called for oversize uploads")

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))
    big = b"R" * (6 * 1024 * 1024)

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", big, "audio/wav")},
    )

    assert resp.status_code == 413


def test_asr_submit_returns_422_on_empty_utterance() -> None:
    """``VoicePipelineEmptyError`` → 422 ``{"detail": "empty"}`` per §5.2."""
    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        msg = "no speech"
        raise voice_pipeline.VoicePipelineEmptyError(msg)

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))
    wav = _build_wav_bytes()

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 422
    assert resp.json()["detail"] == "empty"


def test_asr_submit_returns_503_on_busy() -> None:
    """``VoiceInputBusyError`` (wake mid-turn) → 503 per §5.2."""
    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        msg = "lock held"
        raise voice_pipeline.VoiceInputBusyError(msg)

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))
    wav = _build_wav_bytes()

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 503


def test_asr_submit_returns_500_on_internal_error() -> None:
    """Unexpected ``RuntimeError`` → 500 (logged with turn_id)."""
    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        msg = "boom"
        raise RuntimeError(msg)

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))
    wav = _build_wav_bytes()

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 500


def test_asr_submit_returns_415_on_wrong_content_type() -> None:
    """Non-WAV upload (``audio/mpeg``) → 415; pipeline must not run."""
    def fake_pipeline(
        audio_bytes: bytes,  # noqa: ARG001
        turn_id: str,  # noqa: ARG001
        channel: str,  # noqa: ARG001
        language: str,  # noqa: ARG001
    ) -> Event:
        pytest.fail("pipeline should not be called for unsupported content types")

    deps = _make_deps(fake_pipeline)
    client = TestClient(create_app(deps))

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.mp3", b"ID3-fake-mp3", "audio/mpeg")},
    )

    assert resp.status_code == 415


def test_asr_submit_ptt_path_does_not_broadcast_phases(tmp_path: Path) -> None:
    """PTT HTTP path must not emit op:voice phase envelopes (ADR-0005 §6).

    Swift drives the card state from the HTTP response body, NOT from
    WS envelopes. Wiring a shared VoicePipeline between wake + PTT used
    to incorrectly fan ``voice("accepted", ...)`` / ``voice("empty", ...)``
    envelopes onto the WS for PTT, causing duplicate / unexpected UI
    transitions. The PTT-specific adapter constructed by the runtime
    must pass ``broadcast=False`` to the real :class:`VoicePipeline`,
    silencing the phase broadcasts while keeping the WS hot for the
    wake path on the same pipeline instance.
    """
    recognizer = MagicMock(spec=voice_asr.AsrRecognizer)
    recognizer.recognize.return_value = voice_asr.TranscriptionResult(
        text="你好", confidence=0.9, language_detected=None, emotion=None,
    )
    normalizer = voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False)
    broadcaster = MagicMock(spec=InherentBroadcaster)

    db_path = tmp_path / "events.db"
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=recognizer,
        normalizer=normalizer,
        broadcaster=broadcaster,  # broadcaster wired — but PTT adapter must silence it
        artifacts_dir=tmp_path,
    )

    ptt_callable = inherent_loop._build_voice_pipeline_callable(pipeline)  # noqa: SLF001
    deps = InherentDeps(
        submit_callable=_noop_submit,
        broadcaster=broadcaster,
        voice_pipeline_callable=ptt_callable,
    )
    client = TestClient(create_app(deps))
    wav = _build_wav_bytes()

    resp = client.post(
        "/inherent/asr-submit",
        files={"audio": ("u.wav", wav, "audio/wav")},
    )

    assert resp.status_code == 200
    # NO phase broadcasts should have fired for the PTT path.
    broadcaster.broadcast_voice_sync.assert_not_called()
