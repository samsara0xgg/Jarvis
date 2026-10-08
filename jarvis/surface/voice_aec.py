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

The canceller always exists and the player always taps into it; each mic open
tells it which microphone it got and where Jarvis plays
(:meth:`EchoCanceller.set_input_device`). The reSpeaker's board cancels the echo of what it
plays itself, so that mic passes through untouched while the default output is the board too
(or unknown); any other pair is cancelled here (ADR 0191).

Diagnostics (``history_s > 0``): the last few seconds of mic-before, what
Jarvis played, and mic-after are kept in lockstep 10 ms chunks, and
:meth:`EchoCanceller.dump` writes them as one 3-channel 16 kHz WAV.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from collections import deque
from typing import TYPE_CHECKING

import numpy as np

from jarvis.surface.voice_tts import _RingBuffer  # the player's own SPSC ring

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# The XVF3800 board cancels the echo itself (it sends the cleaned mix on channel 0).
_SELF_CANCELLING_MIC = "respeaker"

# ~1.4 s at 48 kHz; the ingress worker drains it every 32 ms mic frame.
_PLAYBACK_RING_SAMPLES = 65_536


class EchoCanceller:
    """One WebRTC echo canceller shared by the player and the mic ingress."""

    def __init__(
        self, *, mic_rate_hz: int = 16_000, history_s: float = 0.0, follow_device: bool = True,
    ) -> None:
        """Load the WebRTC module now, so a missing wheel fails at startup.

        ``follow_device`` False cancels whatever microphone is open (a debug override).
        """
        from livekit import rtc  # noqa: PLC0415 - lazy: the wheel loads a native library

        self._rtc = rtc
        self._mic_rate_hz = mic_rate_hz
        self._mic_chunk = mic_rate_hz // 100  # WebRTC takes exactly 10 ms per call
        self._playback = _RingBuffer(_PLAYBACK_RING_SAMPLES)
        self._playback_rate_hz = 0
        self._playback_chunk = np.zeros(0, dtype=np.float32)
        self._follow_device = follow_device
        self._bypass = False
        self._stream_epoch: int | None = None
        # Diagnostics: (mic, played, cleaned) int16 bytes per 10 ms chunk.
        self._history: deque[tuple[bytes, bytes, bytes]] | None = (
            deque(maxlen=int(history_s * 100)) if history_s > 0 else None
        )
        self._history_lock = threading.Lock()
        self._restart()

    def set_input_device(self, name: str, output: str | None = None) -> None:
        """A microphone just opened: pass it through if it cancels its own echo, else cancel.

        The reSpeaker's reference is its own USB output: over another speaker (``output``, the
        default output's name) its mix still carries her voice, so it is cancelled here. An
        unknown output keeps the name test alone.
        """
        board_plays = output is None or _SELF_CANCELLING_MIC in output.lower()
        self._bypass = self._follow_device and _SELF_CANCELLING_MIC in name.lower() and board_plays
        LOGGER.info(
            "echo cancellation %s on microphone %r%s",
            "off (the microphone does its own)" if self._bypass else "on", name,
            "" if output is None else f" playing on {output!r}",
        )

    def add_playback(self, block: np.ndarray, sample_rate_hz: int) -> None:
        """Output callback: queue the float32 block the device is about to play."""
        if self._bypass:
            return
        self._playback_rate_hz = sample_rate_hz
        self._playback.write(block)

    def clean(self, pcm16: bytes, *, stream_epoch: int, discontinuity: bool) -> bytes:
        """Return ``pcm16`` with Jarvis's playback removed, delayed by 10 ms.

        The mic frame (512 samples) is not a multiple of WebRTC's 10 ms, so
        output runs one 10 ms chunk behind input; that fixed lag keeps every
        returned frame full. A microphone that cancels its own echo comes back
        as it came.
        """
        if self._bypass:
            return pcm16
        if stream_epoch != self._stream_epoch or discontinuity:
            self._stream_epoch = stream_epoch
            self._restart()
        self._mic_in.extend(pcm16)
        chunk_bytes = self._mic_chunk * 2
        while len(self._mic_in) >= chunk_bytes:
            self._feed_playback()
            mic = bytes(self._mic_in[:chunk_bytes])
            del self._mic_in[:chunk_bytes]
            frame = self._rtc.AudioFrame(mic, self._mic_rate_hz, 1, self._mic_chunk)
            self._apm.set_stream_delay_ms(0)  # a hint only; AEC3 finds the delay
            self._apm.process_stream(frame)
            cleaned = bytes(frame.data)
            self._mic_out.extend(cleaned)
            if self._history is not None:
                played = bytes(self._played_16k[:chunk_bytes]).ljust(chunk_bytes, b"\0")
                del self._played_16k[:chunk_bytes]
                with self._history_lock:
                    self._history.append((mic, played, cleaned))
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
            if self._history is not None:
                self._played_16k.extend(
                    np.interp(
                        np.linspace(0, samples - 1, self._mic_chunk),
                        np.arange(samples),
                        pcm,
                    ).astype("<i2").tobytes(),
                )
                # Bound the lag between the played and mic tracks to 1 s.
                del self._played_16k[: max(0, len(self._played_16k) - self._mic_rate_hz * 2)]

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
        self._played_16k = bytearray()

    def dump(self, directory: Path) -> Path | None:
        """Write the kept history as ``<time>.wav``: ch0 mic, ch1 played, ch2 cleaned."""
        if self._history is None:
            return None
        with self._history_lock:
            chunks = list(self._history)
        if not chunks:
            return None
        tracks = [np.frombuffer(b"".join(c[i] for c in chunks), dtype="<i2") for i in range(3)]
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / time.strftime("%Y%m%d-%H%M%S.wav")
        with wave.open(str(path), "wb") as out:
            out.setnchannels(3)
            out.setsampwidth(2)
            out.setframerate(self._mic_rate_hz)
            out.writeframes(np.stack(tracks, axis=1).tobytes())
        return path
