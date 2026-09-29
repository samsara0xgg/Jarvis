"""A short sound ends on its own pause, and never cuts the words that follow it.

Regression pins for two faults of the acoustic endpoint (2026-09-28): the
detector's pause flag stayed latched after a sound shorter than
``min_voiced_s``, so the next sentence was committed about 0.2 s in
(「嗯……帮我查天气」 became 「嗯……帮」 and 「我查天气」); and that short sound
never ended on its pause at all, so a cough kept an utterance in flight,
holding every answer (ADR 0074), until ``max_utterance_s``.

Shipped thresholds throughout: the ``record`` profile, 5-frame smoothing,
3 hits, 24 misses, ``min_voiced_s`` 0.3 and ``short_sound_grace_ms`` 400. Only
the Silero model is replaced, by one whose probability follows frame energy
over a -60 dBFS room floor.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.surface import voice_audio, voice_session

if TYPE_CHECKING:
    from collections.abc import Iterator

FRAME = voice_audio.SILERO_CHUNK_SAMPLES
FRAME_MS = 32
_RNG = np.random.default_rng(7)
ROOM = _RNG.normal(0, 30, FRAME).astype(np.int16).tobytes()  # about -60 dBFS
VOICE = np.full(FRAME, 8_000, dtype=np.int16).tobytes()


class _EnergySilero:
    """ONNX-shaped Silero stand-in: speech when the frame is louder than the room."""

    def run(self, _names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        rms = float(np.sqrt(np.mean(inputs["x"] ** 2)))
        probability = 0.9 if rms > 0.05 else 0.05  # between the room and a voice
        return [np.asarray([[probability]], dtype=np.float32), inputs["h"], inputs["c"]]


def _frames(script: list[tuple[str, int]]) -> Iterator[bytes]:
    for kind, milliseconds in script:
        for _ in range(milliseconds // FRAME_MS):
            yield VOICE if kind == "voice" else ROOM


def _conversation_commits(script: list[tuple[str, int]]) -> list[tuple[str, int, int]]:
    """Feed one conversation-mode arm; return ``(reason, commit_ms, audio_ms)`` per commit."""
    commits: list[tuple[str, int, int]] = []
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySilero()):
        assembler = voice_session.UtteranceAssembler(
            vad=voice_audio.SileroVad(mode="record"),
            config=replace(voice_session.RealtimeInputSessionConfig(), min_voiced_s=0.3),
            sample_rate_hz=16_000,
            frame_samples=FRAME,
            session_id="S",
        )
        assembler.prepare()
        for index, pcm in enumerate(_frames(script)):
            if not assembler.armed:  # conversation mode re-arms on every idle frame
                assembler.arm(
                    voice_session.WakeDetection(1, index * FRAME, index * FRAME_MS * 10**6, 1.0),
                    expires=False,
                )
            outcome = assembler.feed(
                voice_audio.CanonicalAudioFrame(
                    stream_epoch=1,
                    sequence=index,
                    sample_cursor=index * FRAME,
                    sample_rate_hz=16_000,
                    frame_count=FRAME,
                    adc_time_s=None,
                    captured_monotonic_ns=index * FRAME_MS * 10**6,
                    discontinuity_before=False,
                    pcm16_mono=pcm,
                ),
            )
            if isinstance(outcome, voice_session.CapturedUtterance):
                commits.append(
                    (
                        outcome.endpoint_reason,
                        (index + 1) * FRAME_MS,
                        len(outcome.audio_bytes) // 2 * 1000 // 16_000,
                    ),
                )
                assembler.prepare()
        assert not assembler.active, "an utterance is still in flight, holding every answer"
    return commits


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        pytest.param(
            [("room", 160), ("voice", 128), ("room", 5_000)],
            [("short_pause", 1_504)],
            id="a cough ends 1.2 s after it, not at max_utterance_s",
        ),
        pytest.param(
            [("room", 160), ("voice", 192), ("room", 2_000)],
            [("short_pause", 1_568)],
            id="a one-word 「好」 is committed on its own pause",
        ),
        pytest.param(
            [("room", 160), ("voice", 160), ("room", 992), ("voice", 1_280), ("room", 1_280)],
            [("acoustic_pause", 3_424)],
            id="「嗯」, a one-second hesitation, then the request is one utterance",
        ),
        pytest.param(
            [("room", 160), ("voice", 160), ("room", 1_600), ("voice", 1_280), ("room", 1_280)],
            [("short_pause", 1_536), ("acoustic_pause", 4_032)],
            id="after a longer hesitation the request is its own, whole utterance",
        ),
        pytest.param(
            [("room", 160), ("voice", 1_280), ("room", 1_280)],
            [("acoustic_pause", 2_272)],
            id="an ordinary sentence still ends 0.83 s after its last word",
        ),
    ],
)
def test_short_sound_endpoints(
    script: list[tuple[str, int]],
    expected: list[tuple[str, int]],
) -> None:
    """Each commit lands at the pause that ends it, and nothing stays in flight."""
    commits = _conversation_commits(script)
    assert [(reason, at_ms) for reason, at_ms, _ in commits] == expected


def test_the_request_after_a_hesitation_keeps_all_of_its_audio() -> None:
    """The committed audio spans the filler, the hesitation and every word after it."""
    script = [("room", 160), ("voice", 160), ("room", 992), ("voice", 1_280), ("room", 1_280)]
    ((_, _, audio_ms),) = _conversation_commits(script)
    # 160 ms filler + 992 ms hesitation + 1280 ms request + the 0.77 s pause, pre-roll included.
    assert audio_ms >= 160 + 992 + 1_280


class _ScriptedStream:
    """Blocking PortAudio shape that plays one script, then the room forever."""

    def __init__(self, script: list[tuple[str, int]]) -> None:
        self._frames = _frames(script)
        self.read_frames = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, blocksize: int) -> tuple[bytes, bool]:
        assert blocksize == FRAME
        self.read_frames += 1
        return next(self._frames, ROOM), False


def test_legacy_capture_does_not_cut_the_words_after_a_short_sound() -> None:
    """The legacy recorder (wake-listener fallback) ends at the request's own pause."""
    stream = _ScriptedStream(
        [("room", 160), ("voice", 160), ("room", 992), ("voice", 1_280), ("room", 1_280)],
    )
    with (
        patch.object(voice_audio, "_load_silero_session", return_value=_EnergySilero()),
        patch.object(voice_audio, "_open_input_stream", return_value=stream),
    ):
        audio = voice_audio.capture_utterance(
            vad=voice_audio.SileroVad(mode="record"),
            max_duration_s=10.0,
            min_voiced_s=0.3,
        )
    captured_ms = len(audio) // 2 * 1000 // 16_000
    # Before the fix it stopped 0.2 s into the request, at about 1.5 s.
    assert captured_ms >= 160 + 160 + 992 + 1_280
