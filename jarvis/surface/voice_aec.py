"""Software echo cancellation: take Jarvis's own voice out of the microphone.

Over laptop speakers the microphone hears every word Jarvis says, so a wake
word, a conversation-mode barge-in, or a turn captured during playback would
hear Jarvis instead of Allen. WebRTC's AEC3 (through ``livekit``) subtracts
it: the player hands over each block it gives the output device, and the
ingress worker feeds those blocks as the far end before it cleans each mic
frame. AEC3 estimates the speaker-to-mic delay itself, so the two streams only
have to arrive in order, never pre-aligned.

Threads: :meth:`EchoCanceller.add_playback` runs on the PortAudio output
callback and only writes a preallocated ring; every WebRTC call runs on the
ingress worker inside :meth:`EchoCanceller.clean`.
"""

from __future__ import annotations

import numpy as np

from jarvis.surface.voice_tts import _RingBuffer  # the player's own SPSC ring

# ~1.4 s at 48 kHz; the ingress worker drains it every 32 ms mic frame.
_PLAYBACK_RING_SAMPLES = 65_536


class EchoCanceller:
    """One WebRTC echo canceller shared by the player and the mic ingress."""

    def __init__(self, *, mic_rate_hz: int = 16_000) -> None:
        """Load the WebRTC module now, so a missing wheel fails at startup."""
        from livekit import rtc  # noqa: PLC0415 - lazy: the wheel loads a native library

        self._rtc = rtc
        self._mic_rate_hz = mic_rate_hz
        self._mic_chunk = mic_rate_hz // 100  # WebRTC takes exactly 10 ms per call
        self._playback = _RingBuffer(_PLAYBACK_RING_SAMPLES)
        self._playback_rate_hz = 0
        self._playback_chunk = np.zeros(0, dtype=np.float32)
        self._stream_epoch: int | None = None
        self._restart()

    def add_playback(self, block: np.ndarray, sample_rate_hz: int) -> None:
        """Output callback: queue the float32 block the device is about to play."""
        self._playback_rate_hz = sample_rate_hz
        self._playback.write(block)

    def clean(self, pcm16: bytes, *, stream_epoch: int, discontinuity: bool) -> bytes:
        """Return ``pcm16`` with Jarvis's playback removed, delayed by 10 ms.

        The mic frame (512 samples) is not a multiple of WebRTC's 10 ms, so
        output runs one 10 ms chunk behind input; that fixed lag keeps every
        returned frame full.
        """
        if stream_epoch != self._stream_epoch or discontinuity:
            self._stream_epoch = stream_epoch
            self._restart()
        self._mic_in.extend(pcm16)
        chunk_bytes = self._mic_chunk * 2
        while len(self._mic_in) >= chunk_bytes:
            self._feed_playback()
            frame = self._rtc.AudioFrame(
                bytes(self._mic_in[:chunk_bytes]), self._mic_rate_hz, 1, self._mic_chunk,
            )
            del self._mic_in[:chunk_bytes]
            self._apm.set_stream_delay_ms(0)  # a hint only; AEC3 finds the delay
            self._apm.process_stream(frame)
            self._mic_out.extend(bytes(frame.data))
        out = bytes(self._mic_out[: len(pcm16)])
        del self._mic_out[: len(pcm16)]
        return out

    def _feed_playback(self) -> None:
        rate = self._playback_rate_hz
        if rate == 0:
            return
        samples = rate // 100
        if self._playback_chunk.size != samples:
            self._playback_chunk = np.zeros(samples, dtype=np.float32)
        while self._playback.available_read() >= samples:
            self._playback.read_into(self._playback_chunk, samples)
            pcm = np.clip(np.rint(self._playback_chunk * 32767.0), -32768, 32767).astype("<i2")
            self._apm.process_reverse_stream(
                self._rtc.AudioFrame(pcm.tobytes(), rate, 1, samples),
            )

    def _restart(self) -> None:
        """New mic stream: fresh WebRTC state, no stale playback, 10 ms of lead."""
        self._apm = self._rtc.AudioProcessingModule(
            echo_cancellation=True, high_pass_filter=True,
        )
        drain = np.zeros(4096, dtype=np.float32)
        while self._playback.available_read() > 0:
            self._playback.read_into(drain, drain.size)
        self._mic_in = bytearray()
        self._mic_out = bytearray(self._mic_chunk * 2)
