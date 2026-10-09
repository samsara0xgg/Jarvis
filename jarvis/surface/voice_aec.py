"""Software echo cancellation: take Jarvis's own voice out of the microphone.

Over laptop speakers the microphone hears every word Jarvis says, so a wake
word, a conversation-mode barge-in, or a turn captured during playback would
hear Jarvis instead of Allen. WebRTC's AEC3 (through ``livekit``) subtracts
it: the player hands over each block it gives the output device, stamped with
the host time its first sample leaves the speaker, and the ingress worker
cleans each mic frame, stamped with the host time its first sample was
captured. AEC3 estimates the speaker-to-mic delay itself but needs the two
streams to keep their spacing, which arrival order does not give: a Python
thread that stalls on the GIL delivers the far end late, AEC3 underruns, its
delay alignment shifts and her voice leaks for a second (ADR 0193). So the
two are paired by host time: each 10 ms mic chunk is fed with the 10 ms of far
end that sounds ``_LEAD_NS`` after it, cut from the stamped blocks; if those
have not arrived yet and the player is alive, the mic waits for them.

Threads: :meth:`EchoCanceller.add_playback` runs on the player's reader or
PortAudio output callback and only appends a stamped block under a lock the
mic side holds for a slice at most; every WebRTC call runs on the ingress
worker inside :meth:`EchoCanceller.clean`.

The canceller always exists and the player always taps into it; each mic open
tells it which microphone it got and where Jarvis plays
(:meth:`EchoCanceller.set_input_device`). The reSpeaker's board cancels the echo of what it
plays itself, so that mic passes through untouched while the default output is the board too
(or unknown); any other pair is cancelled here (ADR 0191).

Diagnostics (``history_s > 0``): the last few seconds of mic-before, the far
end fed with each mic chunk, and mic-after are kept in lockstep 10 ms chunks,
and :meth:`EchoCanceller.dump` writes them as one 3-channel 16 kHz WAV, so a
dump replays offline exactly. :meth:`EchoCanceller.summary` counts the waits,
gaps and restarts since the last restart.
"""

from __future__ import annotations

import logging
import threading
import time
import wave
from collections import deque
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from pathlib import Path

LOGGER = logging.getLogger(__name__)

# The XVF3800 board cancels the echo itself (it sends the cleaned mix on channel 0).
_SELF_CANCELLING_MIC = "respeaker"

_CHUNK_NS = 10_000_000  # WebRTC takes exactly 10 ms per call
# The far end is fed this long before its echo is due, so it reaches AEC3 first even when the
# player's latency stamp is a little late. AEC3 finds a lead on its own, but not a long one:
# 100 ms cancels as well as 40 ms in the synthetic room, 150 ms does not (ADR 0193).
_LEAD_NS = 60_000_000
# A stamp within this of where counting samples puts it is jitter or clock drift, not a gap.
_RESYNC_NS = 2_000_000
# The player is alive while a block arrived this recently; only then is a missing far end late
# rather than absent, and only then does the mic wait (at most _WAIT_S) for it.
_ALIVE_NS = 500_000_000
_WAIT_S = 0.3
_RENDER_MAX_BLOCKS = 2048  # ~20 s of 512-frame blocks: a bound for when no mic is reading


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
        # The far end: (host time of the first sample, float32 samples), oldest first, under
        # the condition's lock. The end is the host time just past the newest sample.
        self._render: deque[tuple[int, np.ndarray]] = deque(maxlen=_RENDER_MAX_BLOCKS)
        self._render_cond = threading.Condition()
        self._render_rate_hz = 0
        self._render_end_ns = 0
        self._render_seen_ns = 0  # time.monotonic_ns() when the newest block arrived
        self._follow_device = follow_device
        self._bypass = False
        self._stream_epoch: int | None = None
        self._restarts = 0
        # Host time of the first sample waiting in _mic_in; None until a stream's first frame.
        self._mic_t0_ns: int | None = None
        self._max_wait_s: float  # set by _restart, with the other counts
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

    def add_playback(self, block: np.ndarray, sample_rate_hz: int, presentation_ns: int) -> None:
        """Player side: keep the float32 block the device is about to play.

        ``presentation_ns`` is the ``time.monotonic_ns`` time its first sample leaves the
        speaker. Never waits for the mic side: the lock is held for a slice at most.
        """
        if self._bypass:
            return
        with self._render_cond:
            # Blocks that follow each other stay sample-exact; only a real jump moves the stamp.
            end = self._render_end_ns
            start = end if abs(presentation_ns - end) <= _RESYNC_NS else presentation_ns
            self._render.append((start, np.array(block, dtype=np.float32)))
            self._render_rate_hz = sample_rate_hz
            self._render_end_ns = start + len(block) * 1_000_000_000 // sample_rate_hz
            self._render_seen_ns = time.monotonic_ns()
            self._render_cond.notify()

    def clean(
        self,
        pcm16: bytes,
        *,
        stream_epoch: int,
        discontinuity: bool,
        adc_time_s: float | None = None,
    ) -> bytes:
        """Return ``pcm16`` with Jarvis's playback removed, delayed by 10 ms.

        The mic frame (512 samples) is not a multiple of WebRTC's 10 ms, so
        output runs one 10 ms chunk behind input; that fixed lag keeps every
        returned frame full. A microphone that cancels its own echo comes back
        as it came.

        ``adc_time_s`` is when the frame's first sample was captured (PortAudio's clock, the
        host's monotonic one). Without it, the frame's arrival stands in for the first frame
        and the stream is then followed by counting samples. Only a new ``stream_epoch`` builds
        a fresh WebRTC state: after a gap (``discontinuity`` or a jump in ``adc_time_s``) the
        mic and the far end are spliced at the same instant instead.
        """
        if self._bypass:
            return pcm16
        if stream_epoch != self._stream_epoch:
            self._stream_epoch = stream_epoch
            self._restarts += 1
            self._restart()
        self._place_mic(len(pcm16) // 2, adc_time_s, discontinuity=discontinuity)
        self._mic_in.extend(pcm16)
        chunk_bytes = self._mic_chunk * 2
        while len(self._mic_in) >= chunk_bytes:
            assert self._mic_t0_ns is not None  # noqa: S101 - _place_mic set it
            played = self._feed_render(self._mic_t0_ns)
            self._mic_t0_ns += _CHUNK_NS
            mic = bytes(self._mic_in[:chunk_bytes])
            del self._mic_in[:chunk_bytes]
            frame = self._rtc.AudioFrame(mic, self._mic_rate_hz, 1, self._mic_chunk)
            self._apm.set_stream_delay_ms(0)  # a hint only; AEC3 finds the delay
            self._apm.process_stream(frame)
            cleaned = bytes(frame.data)
            self._mic_out.extend(cleaned)
            if self._history is not None:
                with self._history_lock:
                    self._history.append((mic, played, cleaned))
        out = bytes(self._mic_out[: len(pcm16)])
        del self._mic_out[: len(pcm16)]
        return out

    def _place_mic(self, samples: int, adc_time_s: float | None, *, discontinuity: bool) -> None:
        """Pin ``_mic_t0_ns``, the host time of the first sample in ``_mic_in``, to this frame."""
        held_ns = len(self._mic_in) // 2 * 1_000_000_000 // self._mic_rate_hz
        if adc_time_s is None:
            start_ns = time.monotonic_ns() - samples * 1_000_000_000 // self._mic_rate_hz
        else:
            start_ns = round(adc_time_s * 1e9)
        if self._mic_t0_ns is None:
            self._mic_t0_ns = start_ns - held_ns
        elif discontinuity or (
            adc_time_s is not None and abs(start_ns - (self._mic_t0_ns + held_ns)) > _RESYNC_NS
        ):
            # ponytail: the < 10 ms held from before the gap shares a chunk with the first after
            self._gaps += 1
            self._mic_t0_ns = start_ns - held_ns

    def _feed_render(self, mic_ns: int) -> bytes:
        """Feed AEC3 the far end for the mic chunk starting at ``mic_ns``; return it at 16 kHz.

        That is the 10 ms ending ``_LEAD_NS`` after the chunk, zeros where nothing was played.
        If it has not arrived and the player is alive, wait for it.
        """
        window_end = mic_ns + _LEAD_NS
        with self._render_cond:
            if self._render_end_ns >= window_end:
                self._may_wait = True
            elif self._may_wait and time.monotonic_ns() - self._render_seen_ns < _ALIVE_NS:
                began = time.monotonic()
                arrived = self._render_cond.wait_for(
                    lambda: self._render_end_ns >= window_end, _WAIT_S,
                )
                self._waits += 1
                self._max_wait_s = max(self._max_wait_s, time.monotonic() - began)
                if not arrived:
                    # Do not stall every later chunk too: wait again once it has caught up.
                    self._timeouts += 1
                    self._may_wait = False
            rate = self._render_rate_hz
            if rate == 0:
                return bytes(self._mic_chunk * 2)
            samples = rate // 100
            window = np.zeros(samples, dtype=np.float32)
            window_start = window_end - _CHUNK_NS
            while self._render and (
                self._render[0][0] + len(self._render[0][1]) * 1_000_000_000 // rate
                <= window_start
            ):
                self._render.popleft()
            for start, block in self._render:
                if start >= window_end:
                    break
                at = round((window_start - start) * rate / 1e9)  # window[0] is block[at]
                lo = max(0, -at)
                hi = min(samples, len(block) - at)
                if hi > lo:
                    window[lo:hi] = block[at + lo : at + hi]
        pcm = np.clip(np.rint(window * 32767.0), -32768, 32767).astype("<i2")
        self._apm.process_reverse_stream(self._rtc.AudioFrame(pcm.tobytes(), rate, 1, samples))
        if self._history is None:
            return b""
        return bytes(
            np.interp(np.linspace(0, samples - 1, self._mic_chunk), np.arange(samples), pcm)
            .astype("<i2")
            .tobytes(),
        )

    def _restart(self) -> None:
        """New mic stream: fresh WebRTC state and capture clock, 10 ms of lead.

        The far end needs no reset: it is addressed by time, so what is stale is never cut.
        """
        self._apm = self._rtc.AudioProcessingModule(
            echo_cancellation=True, high_pass_filter=True,
        )
        self._mic_in = bytearray()
        self._mic_out = bytearray(self._mic_chunk * 2)
        self._mic_t0_ns = None
        # Since this restart: waits for a late far end, the longest, those that gave up, gaps.
        self._waits = self._timeouts = self._gaps = 0
        self._max_wait_s = 0.0
        self._may_wait = True

    def summary(self) -> str:
        """The waits, gaps and restarts since the last restart, for the diagnostics log."""
        return (
            f"far-end waits {self._waits} (longest {self._max_wait_s * 1000:.0f} ms, "
            f"{self._timeouts} gave up), capture gaps {self._gaps}, "
            f"restarts {self._restarts}"
        )

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
