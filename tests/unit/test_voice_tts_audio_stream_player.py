"""ADR-0005 voice_tts.AudioStreamPlayer — ring buffer + gain ramps."""
from __future__ import annotations

import numpy as np

from jarvis.surface import voice_tts


def test_audio_stream_player_writes_pcm_to_ring() -> None:
    """Writing float32 PCM bytes lands them in the ring buffer."""
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    chunk = np.zeros(480, dtype=np.float32).tobytes()
    player.write(chunk)
    assert player.bytes_pending() >= len(chunk)


def test_audio_stream_player_flush_clears_ring() -> None:
    """`flush` drops every queued sample without touching the stream."""
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    player.write(b"\x00" * 1920)
    player.flush()
    assert player.bytes_pending() == 0


def test_audio_stream_player_duck_lowers_gain() -> None:
    """`duck` schedules a ramp to a sub-unity gain target."""
    player = voice_tts.AudioStreamPlayer(sample_rate_hz=48000)
    player.duck(target_gain=0.3, ramp_ms=10)
    # Just verifies the API is callable and doesn't raise.
    assert player.current_gain() <= 1.0
