"""ADR-0005 voice_artifact_store — opt-in raw WAV retention."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from jarvis.surface import voice_artifact_store


@pytest.fixture
def pcm_audio() -> bytes:
    """Minimal 1-sample mono PCM16 frame."""
    return b"\x01\x00"


def test_persist_disabled_returns_none(
    pcm_audio: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ``JARVIS_VOICE_RETAIN_RAW=1`` persist is a no-op."""
    monkeypatch.delenv("JARVIS_VOICE_RETAIN_RAW", raising=False)
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T1",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is None
    assert list(tmp_path.iterdir()) == []


def test_persist_enabled_writes_valid_wav(
    pcm_audio: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With retention enabled, output is a mono PCM16 WAV at requested sample rate."""
    monkeypatch.setenv("JARVIS_VOICE_RETAIN_RAW", "1")
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T2",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is not None
    p = Path(ref)
    assert p.exists()
    # Verify it's a real WAV that wave.open() can read.
    with wave.open(str(p), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 16000


def test_persist_relative_path_under_artifacts_dir(
    pcm_audio: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned path lives under ``artifacts_dir`` (no escape via abs/symlink tricks)."""
    monkeypatch.setenv("JARVIS_VOICE_RETAIN_RAW", "1")
    ref = voice_artifact_store.persist(
        pcm_audio,
        turn_id="T3a",
        sample_rate_hz=16000,
        artifacts_dir=tmp_path,
    )
    assert ref is not None
    p = Path(ref)
    assert tmp_path in p.parents
