"""L5 a phone's speaker as the host's native player (ADR 0209).

The media actor drives a :class:`~jarvis.surface.voice_native_out.FramedAudioStreamPlayer`
exactly as it drives the helper of ADR 0129; this one sends the same frames over a
WebSocket instead of a pipe, and the phone renders them. Leases, the ledger and the
heard-prefix accounting stay on the host and account from the phone's REPORT frames
unchanged.

**Wire.** One WebSocket binary message is ``u8 type`` plus the payload, little-endian, with no
length prefix (the message is the frame). The layouts are ADR 0129's
(``native/voice_out/main.swift``), with one new audio frame:

    host -> phone   2 ACTIVE   i64 generation (-1: none)
                    3 DISCARD  u64 seq: drop every sample sent before this frame
                    4 GAIN     f32 target, u32 ramp_samples (latest wins)
                    5 HOLD     i64 generation (-1: none)
                    6 PCM16    i64 generation, i64 start_cursor, i16[] samples
    phone -> host   0x81 READY        u32 sample_rate, u32 buffer_frames,
                                      i64 device_latency_ns, i64 clock_ns
                    0x82 REPORT       i64 generation, i64 start, i64 end, u8 audibility,
                                      i64 callback_ns, i64 presentation_delay_ns, u8 first
                    0x83 STATUS       i64 read_idx, f64 gain, u64 callbacks, u64 overloads,
                                      u64 starvation_gaps, u32 tail_ramp_samples,
                                      u64 played_samples
                    0x84 DISCARD_ACK  u64 seq

PCM16 samples are at the rate of the host's ``ready`` frame (32 kHz, MiniMax's own rate, so
nothing is resampled). Type 1 (float32 PCM) is never sent to a phone.

**Clock.** The phone stamps READY's ``clock_ns``, every REPORT's ``callback_ns`` and every
``ping`` with its own monotonic clock. The ledger compares reports with ``time.monotonic_ns()``
on the host, so a report is mapped by an offset ``host - phone`` before it is written. Each of
READY and ``ping`` gives a sample ``host_receive_ns - phone_ns``, which is the true offset plus
the one-way delay of that frame; the offset used is the smallest sample of the last
:data:`_CLOCK_WINDOW_S` seconds, so its error is the least uplink delay seen. That errs late:
a sound is judged heard no earlier than it was, and the heard prefix never claims words the
phone did not play. The window follows clock drift.

Layer rules: stdlib, ``numpy`` and ``jarvis.surface`` only.
"""

from __future__ import annotations

import collections
import logging
import struct
import threading
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from jarvis.surface.voice_native_out import (
    _READY,
    FramedAudioStreamPlayer,
)
from jarvis.surface.voice_tts import PlayerStartResult, PlayerStopResult

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger("jarvis.surface.phone_player")

PHONE_SAMPLE_RATE_HZ = 32_000
"""The rate a phone plays at: MiniMax's own, so the host does not resample."""
PCM16 = 6
"""Host -> phone audio frame type: ``i64 generation, i64 start_cursor, i16[] samples``."""
MAX_PHONE_FRAME_BYTES = 64
"""Largest binary frame the host accepts from a phone (the biggest layout is 51 bytes)."""
_PHONE_PCM_FRAME_SAMPLES = 6_400  # 200 ms at 32 kHz: a frame small enough to cancel quickly
_CLOCK_WINDOW_S = 60.0
_MAX_SKEW_NS = 24 * 3600 * 1_000_000_000
_READY_FMT = struct.Struct("<IIqq")


class PhoneFrameError(ValueError):
    """A binary frame from the phone that does not parse as the protocol says."""


class PhoneAudioStreamPlayer(FramedAudioStreamPlayer):
    """The host's player whose realtime half is a phone, behind ``send_binary``.

    ``send_binary`` queues one whole frame for the socket and returns False once it is gone;
    it may be called from the media actor's thread. The player is open from the phone's
    READY until :meth:`stop` or :meth:`connection_lost`.
    """

    max_pcm_frame_samples = _PHONE_PCM_FRAME_SAMPLES

    def __init__(
        self,
        *,
        send_binary: Callable[[bytes], bool],
        sample_rate_hz: int = PHONE_SAMPLE_RATE_HZ,
        **kwargs: Any,  # noqa: ANN401 - AudioStreamPlayer's own keyword surface
    ) -> None:
        """Build an idle player; it opens when the phone's READY arrives."""
        super().__init__(sample_rate_hz=sample_rate_hz, **kwargs)
        self._send_binary = send_binary
        self._state_lock = threading.Lock()
        self._got_ready = False
        self._gone = False
        self._clock_samples: collections.deque[tuple[int, int]] = collections.deque()
        self._clock_offset_ns: int | None = None

    # ------------------------------------------------------------------
    # Socket
    # ------------------------------------------------------------------

    def send_frame(self, frame_type: int, *parts: bytes) -> bool:
        """Send one frame; False before the phone's READY and after the connection is gone."""
        if not self._got_ready or self._gone:
            return False
        return self._send_binary(bytes([frame_type]) + b"".join(parts))

    def pcm_frame(self, samples: np.ndarray) -> tuple[int, bytes]:
        """16-bit little-endian samples: the host never sends float32 to a phone."""
        scaled = np.rint(np.asarray(samples, dtype=np.float32) * 32768.0)
        return PCM16, np.clip(scaled, -32768, 32767).astype("<i2").tobytes()

    def on_binary(self, data: bytes) -> None:
        """Handle one binary frame from the phone.

        Raises:
            PhoneFrameError: the frame is not one the protocol defines, or is malformed.
        """
        if not data or len(data) > MAX_PHONE_FRAME_BYTES:
            msg = "empty or oversized binary frame"
            raise PhoneFrameError(msg)
        try:
            self._on_frame(data[0], memoryview(data)[1:])
        except (struct.error, IndexError) as exc:
            msg = f"malformed binary frame of type {data[0]:#x}"
            raise PhoneFrameError(msg) from exc

    def _on_other_frame(self, frame_type: int, body: memoryview) -> None:
        if frame_type != _READY:
            msg = f"unknown binary frame type {frame_type:#x}"
            raise PhoneFrameError(msg)
        received_ns = time.monotonic_ns()
        rate, _frames, latency_ns, clock_ns = _READY_FMT.unpack(body)
        if rate != self._sample_rate_hz:
            msg = f"the phone plays at {rate} Hz, the host sends {self._sample_rate_hz}"
            raise PhoneFrameError(msg)
        if self._got_ready:
            return  # one READY per connection; a repeat changes nothing
        self._estimated_output_latency_ns = self._clamped_latency_ns(latency_ns)
        self.observe_clock(clock_ns, received_ns)
        with self._state_lock:
            self._got_ready = True
            self._lifecycle_state = "open"

    # ------------------------------------------------------------------
    # Clock
    # ------------------------------------------------------------------

    def observe_clock(self, phone_ns: int, host_ns: int) -> None:
        """One frame stamped ``phone_ns`` on the phone's clock arrived at ``host_ns`` here."""
        with self._state_lock:
            window = self._clock_samples
            window.append((host_ns, host_ns - phone_ns))
            while window[0][0] < host_ns - int(_CLOCK_WINDOW_S * 1e9):
                window.popleft()
            self._clock_offset_ns = min(sample for _, sample in window)

    @property
    def clock_offset_ns(self) -> int | None:
        """``host - phone`` in nanoseconds, from the smallest sample of the last minute."""
        return self._clock_offset_ns

    def _host_callback_ns(self, callback_ns: int) -> int:
        offset = self._clock_offset_ns
        return callback_ns + (0 if offset is None else offset)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> PlayerStartResult:
        """Open once the phone has sent READY; there is nothing else to start."""
        with self._state_lock:
            if self._gone:
                return PlayerStartResult("failed_closed", 0, "connection_gone")
            if not self._got_ready:
                return PlayerStartResult("failed_closed", 0, "no_ready")
            return PlayerStartResult("already_started", 0, "ready")

    def stop(self, *, timeout_s: float | None = None) -> PlayerStopResult:
        """Discard what is queued and close; the socket itself is the route's."""
        del timeout_s
        self._terminalize_software_playback()
        self.connection_lost()
        return PlayerStopResult("closed", 0, "phone_player_stopped")

    def connection_lost(self) -> None:
        """The socket is gone: nothing can be played, and no discard will be acknowledged."""
        with self._state_lock:
            self._gone = True
            self._lifecycle_state = "closed"

    @property
    def is_running(self) -> bool:
        """True from the phone's READY until the connection is gone."""
        return self._got_ready and not self._gone
