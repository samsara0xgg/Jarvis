"""L5 voice TTS pipeline — MiniMax streaming WS + PortAudio playback.

ADR-0005 §4.2 / §5.3 / §10 (F6-F7 fallback chain).

Layer rules: imports only stdlib, third-party (`websockets`, `sounddevice`,
`numpy`), `jarvis.shared`, and `jarvis.state.event_log`. Does NOT name
`jarvis.decision`, `jarvis.execution`, `jarvis.deployment`,
`jarvis.runtime`, or `jarvis.cli`.

This file lands in 4 commits per ADR-0005 §14:
  Task 13: _preprocess_for_speech
  Task 14: AudioStreamPlayer
  Task 15: MiniMaxWSClient + MiniMaxUnavailableError (this one)
  Task 16: TTSPipeline + macos_say_fallback
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from jarvis.surface import voice_ducking

LOGGER = logging.getLogger(__name__)


# --- Text preprocessor (ported inline from legacy core/tts_preprocessor.py) ---

# Emoji + symbol Unicode ranges that should be stripped from TTS input.
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map symbols
    "\U0001f700-\U0001f77f"
    "\U0001f780-\U0001f7ff"
    "\U0001f800-\U0001f8ff"
    "\U0001f900-\U0001f9ff"
    "\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff"
    "\U00002600-\U000027bf"  # dingbats
    "\U0001f1e6-\U0001f1ff"  # regional indicator (flags)
    "]+",
    flags=re.UNICODE,
)

_MARKDOWN_BOLD_RE = re.compile(r"\*\*(.*?)\*\*")
_MARKDOWN_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)")
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")


def _preprocess_for_speech(text: str) -> str:
    """Strip TTS-hostile chars (emoji, markdown markers) before synthesis.

    Verbatim behavior port of legacy `core/tts_preprocessor.py`. Does NOT
    apply content safety; the Pre-emit Gate has already vetted the text.
    """
    if not text:
        return ""
    out = _EMOJI_RE.sub("", text)
    out = _MARKDOWN_BOLD_RE.sub(r"\1", out)
    out = _MARKDOWN_LINK_RE.sub(r"\1", out)
    out = _MARKDOWN_ITALIC_RE.sub(r"\1", out)
    return out.strip()


# --- Voice / document tag extraction (spec §3.6.6 structured response) -----
#
# Render-layer emits structured responses with ``<voice>...</voice>`` (spoken)
# and ``<document>...</document>`` (displayed) regions in one chunk stream.
# Without filtering, the pipeline synthesises tag characters literally
# (garbled "less-than voice greater-than" speech) and the document body bleeds
# into TTS. Bug 3 fix from post-ADR-0005 smoke.

_VOICE_REGION_RE = re.compile(r"<voice>(.*?)</voice>", re.DOTALL)
_UNCLOSED_VOICE_RE = re.compile(r"<voice>(.*)\Z", re.DOTALL)
_DOCUMENT_REGION_RE = re.compile(r"<document>.*?</document>", re.DOTALL)


def _extract_voice_content(text: str) -> str:
    """Return the concatenated content inside ``<voice>...</voice>`` regions.

    - When the joined chunk stream contains zero ``<voice>`` tags, returns
      ``text`` unchanged (legacy compat for plain-text unit fixtures);
      isolated ``<document>`` content yields ``""`` so the pipeline does
      not speak displayed-only content.
    - When the stream contains one or more closed ``<voice>...</voice>``
      regions, returns their joined inner text.
    - When the stream contains an *unclosed* ``<voice>`` (truncated mid-
      region), returns everything from ``<voice>`` to end. The TTSPipeline
      flushes on close OR ``end_turn``; the latter is the path that hits
      this case.
    Nested ``<document>`` regions inside the matched voice content are
    stripped before return so a misordered surface emission can't leak
    document chars into TTS.
    """
    if "<voice>" not in text:
        if "<document>" in text:
            return ""
        return text
    parts = _VOICE_REGION_RE.findall(text)
    if not parts:
        m = _UNCLOSED_VOICE_RE.search(text)
        if m is None:
            return ""
        parts = [m.group(1)]
    joined = "".join(parts)
    joined = _DOCUMENT_REGION_RE.sub("", joined)
    return joined.strip()


# --- Audio stream playback (ported from legacy core/audio_stream_player.py) ---
#
# Persistent-stream PCM player — replaces per-sentence subprocess playback. A
# single long-lived ``sounddevice.OutputStream`` plus a lockless SPSC float32
# ring buffer carries audio; gain ducking is applied sample-accurately inside
# the PortAudio callback so user-speech ducking has no inter-sentence gap.
#
# Two intentional deviations from the legacy port (see ADR-0005 §14 Task 14):
#   1. ``sounddevice`` is lazy-imported in ``_open_output_stream`` so tests can
#      construct an :class:`AudioStreamPlayer` without PortAudio installed.
#   2. The OutputStream itself is opened lazily on the first ``write()`` call
#      (``lazy_open=True`` default). This lets unit tests probe the bytes /
#      gain API surface without ever touching the real audio device.


class _RingBuffer:
    """Single-producer single-consumer lockless ring of float32 samples.

    Capacity rounds up to the next power of 2 so wrap-around is a bit-AND.
    Underrun policy: short reads zero-pad — silence is the right output when
    we've got nothing. Ported verbatim from the legacy
    ``core/audio_stream_player.RingBuffer``.
    """

    def __init__(self, size_samples: int) -> None:
        n = 1
        while n < size_samples:
            n <<= 1
        self._size = n
        self._mask = n - 1
        self._buf = np.zeros(n, dtype=np.float32)
        self._write_idx = 0
        self._read_idx = 0

    def available_read(self) -> int:
        return self._write_idx - self._read_idx

    def available_write(self) -> int:
        return self._size - (self._write_idx - self._read_idx)

    def read_into(self, out: np.ndarray, n: int) -> int:
        avail = self.available_read()
        actual = min(n, avail)
        if actual > 0:
            ri = self._read_idx & self._mask
            end = ri + actual
            if end <= self._size:
                out[:actual] = self._buf[ri:end]
            else:
                first = self._size - ri
                out[:first] = self._buf[ri:]
                out[first:actual] = self._buf[: actual - first]
            self._read_idx += actual
        if actual < n:
            out[actual:n] = 0.0
        return actual

    def write(self, data: np.ndarray) -> int:
        n = min(len(data), self.available_write())
        if n == 0:
            return 0
        wi = self._write_idx & self._mask
        end = wi + n
        if end <= self._size:
            self._buf[wi:end] = data[:n]
        else:
            first = self._size - wi
            self._buf[wi:] = data[:first]
            self._buf[: n - first] = data[first:n]
        self._write_idx += n
        return n

    def reset(self) -> None:
        self._write_idx = 0
        self._read_idx = 0


class _GainRamp:
    """Linear gain ramp applied inside the PortAudio callback.

    Scratch buffers are preallocated to avoid numpy allocation on the audio
    hot path. Ported verbatim from the legacy
    ``core/audio_stream_player.GainRamp``.
    """

    def __init__(self, max_block_size: int = 4096) -> None:
        self._current: float = 1.0
        self._target: float = 1.0
        self._remaining: int = 0
        self._scratch = np.empty(max_block_size, dtype=np.float32)
        self._arange = np.arange(max_block_size, dtype=np.float32)

    @property
    def current(self) -> float:
        return self._current

    def set_target(self, target: float, ramp_samples: int) -> None:
        self._target = float(target)
        self._remaining = max(0, int(ramp_samples))
        if self._remaining == 0:
            self._current = self._target

    def apply(self, pcm_block: np.ndarray) -> None:
        n = len(pcm_block)
        if self._remaining == 0:
            if self._current != 1.0:
                pcm_block *= self._current
            return

        step = min(n, self._remaining)
        frac_end = step / self._remaining
        next_gain = self._current + (self._target - self._current) * frac_end

        if step == 1:
            scratch = self._scratch[:1]
            scratch[0] = next_gain
        else:
            slope = (next_gain - self._current) / (step - 1)
            scratch = self._scratch[:step]
            np.multiply(self._arange[:step], slope, out=scratch)
            scratch += self._current
        pcm_block[:step] *= scratch

        if step < n:
            pcm_block[step:] *= next_gain

        self._current = next_gain
        self._remaining -= step
        if self._remaining == 0:
            self._current = self._target


def _open_output_stream(  # noqa: PLR0913 — passthrough to sd.OutputStream
    *,
    sample_rate_hz: int,
    channels: int,
    blocksize: int,
    latency: str | float,
    device: Any | None,  # noqa: ANN401
    callback: Callable[..., None],
) -> Any:  # noqa: ANN401
    """Open a PortAudio output stream.

    Module-level so tests can :func:`unittest.mock.patch.object` it without
    needing PortAudio installed. ``sounddevice`` is imported lazily so the
    surrounding module stays importable in environments where it isn't
    available (CI, headless test runners).
    """
    import sounddevice as sd  # noqa: PLC0415

    return sd.OutputStream(
        samplerate=sample_rate_hz,
        channels=channels,
        dtype="float32",
        blocksize=blocksize,
        latency=latency,
        device=device,
        callback=callback,
    )


class AudioStreamPlayer:
    """Persistent-stream PCM player with sample-accurate duckable gain.

    Public surface required by ADR-0005 §4.2:

    * ``write(pcm: bytes)`` — feed float32 mono PCM bytes
    * ``bytes_pending()`` — queued bytes not yet played
    * ``flush()`` — drop everything queued (abort)
    * ``duck(target_gain, ramp_ms)`` — ramp gain down for user speech
    * ``current_gain()`` — instantaneous output gain
    * ``close()`` — close the OutputStream

    The OutputStream is opened lazily (``lazy_open=True`` default): construction
    does NOT touch PortAudio, and ``write()`` only feeds the ring buffer.
    Production callers invoke :meth:`start` once (or pass ``lazy_open=False`` at
    construction) before they expect audio to actually leave the speaker. This
    lets unit tests exercise the bytes / gain API surface without sounddevice
    installed.
    """

    _BYTES_PER_SAMPLE = 4  # float32 mono

    def __init__(  # noqa: PLR0913 — keyword-only audio + lifecycle config
        self,
        *,
        sample_rate_hz: int = 48000,
        channels: int = 1,
        ring_seconds: float = 2.0,
        blocksize: int = 0,
        latency: str | float = "low",
        device: Any | None = None,  # noqa: ANN401
        on_first_chunk: Callable[[], None] | None = None,
        lazy_open: bool = True,
    ) -> None:
        """Construct an idle player; does not open the OutputStream by default."""
        if channels != 1:
            msg = "only mono supported for now"
            raise NotImplementedError(msg)
        self._sample_rate_hz = int(sample_rate_hz)
        self._channels = channels
        self._ring = _RingBuffer(int(sample_rate_hz * ring_seconds))
        self._gain = _GainRamp(max_block_size=4096)
        self._blocksize = int(blocksize)
        self._latency = latency
        self._device = device

        self._stream: Any | None = None
        self._underflow_count = 0
        self._callback_calls = 0
        self._drained = threading.Event()
        self._drained.set()
        self._abort = threading.Event()
        self._played_samples: int = 0
        self._on_first_chunk: Callable[[], None] | None = on_first_chunk
        self._first_chunk_fired: bool = False

        if not lazy_open:
            self.start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open the PortAudio OutputStream if not already running."""
        if self._stream is not None:
            return
        self._stream = _open_output_stream(
            sample_rate_hz=self._sample_rate_hz,
            channels=self._channels,
            blocksize=self._blocksize,
            latency=self._latency,
            device=self._device,
            callback=self._callback,
        )
        self._stream.start()
        LOGGER.info(
            "AudioStreamPlayer started: %dHz ch=%d blocksize=%s latency=%s",
            self._sample_rate_hz,
            self._channels,
            self._blocksize,
            self._latency,
        )

    def stop(self) -> None:
        """Stop and close the OutputStream. Safe to call repeatedly."""
        if self._stream is None:
            return
        try:
            self._stream.stop()
            self._stream.close()
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("stream close error (ignored): %s", exc)
        self._stream = None
        self._ring.reset()
        self._played_samples = 0
        self._drained.set()
        LOGGER.info(
            "AudioStreamPlayer stopped; lifetime callbacks=%d underflows=%d",
            self._callback_calls,
            self._underflow_count,
        )

    def close(self) -> None:
        """Alias for :meth:`stop` — matches ADR-0005 §4.2 surface."""
        self.stop()

    def restart(self) -> None:
        """Close + reopen — used by watchdog when device change detected."""
        LOGGER.warning("AudioStreamPlayer restart (likely device change)")
        self.stop()
        self.start()

    # ------------------------------------------------------------------
    # Write API
    # ------------------------------------------------------------------

    def write(
        self,
        pcm: bytes,
        *,
        wait_if_full: bool = True,
        timeout_s: float = 10.0,
    ) -> None:
        """Feed mono float32 PCM ``bytes`` into the ring.

        Does NOT open the OutputStream — callers must :meth:`start` once
        (or construct with ``lazy_open=False``) before they expect audio
        to actually leave the speaker. The ring still accepts samples
        even when the stream is closed; this is what lets unit tests
        exercise the bytes / gain API without touching PortAudio.

        Blocks until every byte is committed to the ring, unless
        ``wait_if_full=False`` or ``timeout_s`` elapses. Clears any stale
        abort signal at entry; re-checks it on each ring-full retry so a
        mid-write abort exits promptly.
        """
        samples = np.frombuffer(pcm, dtype=np.float32)
        self._drained.clear()
        self._abort.clear()
        deadline = time.monotonic() + timeout_s
        offset = 0
        while offset < len(samples):
            if self._abort.is_set():
                return
            written = self._ring.write(samples[offset:])
            offset += written
            if offset >= len(samples):
                break
            if not wait_if_full:
                LOGGER.warning("ring full, dropping %d samples", len(samples) - offset)
                return
            if time.monotonic() > deadline:
                LOGGER.warning("write timeout, dropping %d samples", len(samples) - offset)
                return
            time.sleep(0.01)

    def bytes_pending(self) -> int:
        """Queued bytes not yet read by the PortAudio callback."""
        return self._ring.available_read() * self._BYTES_PER_SAMPLE

    def flush(self) -> None:
        """Drop every queued sample and signal in-flight writes to bail."""
        self._ring.reset()
        self._drained.set()
        self._abort.set()

    def drain(self, timeout_s: float = 30.0) -> bool:
        """Block until the ring is empty (or timeout/abort). Returns True if drained."""
        deadline = time.monotonic() + timeout_s
        while self._ring.available_read() > 0:
            if self._abort.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        self._drained.set()
        return True

    # ------------------------------------------------------------------
    # Gain / ducking
    # ------------------------------------------------------------------

    def set_gain(self, target: float, ramp_ms: float = 30.0) -> None:
        """Smoothly ramp current gain to ``target`` over ``ramp_ms``."""
        ramp_samples = int(self._sample_rate_hz * ramp_ms / 1000.0)
        self._gain.set_target(target, ramp_samples)

    def duck(self, target_gain: float = 0.3, ramp_ms: int = 30) -> None:
        """Ramp gain down to ``target_gain`` over ``ramp_ms`` (user-speech ducking)."""
        self.set_gain(target_gain, float(ramp_ms))

    def unduck(self, ramp_ms: float = 10.0) -> None:
        """Restore gain to 1.0. Use when user stops speaking."""
        self.set_gain(1.0, ramp_ms)

    def current_gain(self) -> float:
        """Instantaneous output gain (post-ramp tick)."""
        return self._gain.current

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @property
    def underflow_count(self) -> int:
        """Lifetime PortAudio output-underflow callbacks (watchdog signal)."""
        return self._underflow_count

    @property
    def callback_calls(self) -> int:
        """Lifetime PortAudio callback invocations (liveness signal)."""
        return self._callback_calls

    @property
    def is_running(self) -> bool:
        """True iff the OutputStream is open and PortAudio reports it active."""
        return self._stream is not None and self._stream.active

    @property
    def played_samples(self) -> int:
        """Monotonic count of real samples written to the output. Resets on stop()."""
        return self._played_samples

    def reset_first_chunk(self) -> None:
        """Re-arm the first-chunk callback for the next TTS turn."""
        self._first_chunk_fired = False

    # ------------------------------------------------------------------
    # Callback — runs on PortAudio thread, keep it tight
    # ------------------------------------------------------------------

    def _callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,  # noqa: ANN401, ARG002
        status: Any,  # noqa: ANN401
    ) -> None:
        """PortAudio calls this when it needs ``frames`` samples.

        No allocation on the hot path: ``outdata`` is preallocated by
        PortAudio; we read from the ring (which zero-pads on underrun), then
        apply gain in place.
        """
        self._callback_calls += 1
        if status and getattr(status, "output_underflow", False):
            self._underflow_count += 1

        view = outdata[:, 0] if outdata.ndim > 1 else outdata
        actual = self._ring.read_into(view, frames)
        self._played_samples += actual

        if actual > 0 and not self._first_chunk_fired and self._on_first_chunk is not None:
            # never crash the audio thread due to caller bugs
            with contextlib.suppress(Exception):
                self._on_first_chunk()
            self._first_chunk_fired = True

        self._gain.apply(view)


# --- MiniMax T2A WebSocket client (ported from legacy core/tts_minimax_ws.py) ---
#
# Protocol (https://platform.minimax.io/docs/guides/speech-t2a-websocket):
#
#     connect → connected_success → task_start → task_started
#         → task_continue → audio chunks (hex pcm) → is_final
#         → task_finish → close
#
# Audio frames carry hex-encoded int16 LE PCM at ``sample_rate_in`` (32 kHz by
# default). The client decodes hex → int16 → float32 mono and returns the
# concatenated bytes — the same float32 PCM bytes the
# :class:`AudioStreamPlayer` consumes.
#
# Two intentional deviations from the legacy port (ADR-0005 §14 Task 15):
#   1. ``websockets.connect`` is wrapped by a module-level ``_ws_connect`` seam
#      so tests can :func:`unittest.mock.patch.object` it without standing up a
#      real server.
#   2. Primary / fallback endpoint logic lives in :meth:`synthesize` itself —
#      one ``OSError`` on the primary triggers a fresh connect to the fallback.
#      If both fail, :class:`MiniMaxUnavailableError` bubbles to Task 16's
#      fallback chain (F7 macos-say).


class MiniMaxUnavailableError(RuntimeError):
    """Both primary and fallback MiniMax endpoints are unreachable."""


class _MiniMaxProtocolError(RuntimeError):
    """Server returned a non-zero status_code in ``base_resp``."""


async def _ws_connect(url: str, *, additional_headers: dict[str, str]) -> Any:  # noqa: ANN401
    """Open a websocket connection.

    Module-level so tests can :func:`unittest.mock.patch.object` it without
    standing up a real server. ``websockets`` is imported lazily so the module
    stays importable even when the dependency is absent in some CI runners.
    """
    import websockets  # noqa: PLC0415

    return await websockets.connect(url, additional_headers=additional_headers)


def _base_to_ws_url(base_url: str) -> str:
    """Rewrite ``https://host`` → ``wss://host/ws/v1/t2a_v2`` (legacy convention)."""
    cleaned = base_url.rstrip("/")
    cleaned = cleaned.replace("https://", "wss://").replace("http://", "ws://")
    return cleaned + "/ws/v1/t2a_v2"


class MiniMaxWSClient:
    """One-shot MiniMax TTS WebSocket client with primary/fallback endpoint.

    Public surface required by ADR-0005 §4.2:

    * :meth:`synthesize` — text → concatenated float32 mono PCM bytes
    * :meth:`synthesize_stream` — async iterator yielding PCM chunks as bytes

    Defaults match the legacy ``core/tts_minimax_ws.py`` constants. The
    ``sample_rate_in`` / ``sample_rate_out`` pair stays equal (32 kHz) by
    default so no ``soxr`` resampling is needed; callers that want 48 kHz
    output must install ``soxr`` and pass ``sample_rate_out=48000``.
    """

    _CONNECT_TIMEOUT = 3.0
    _TASK_START_TIMEOUT = 3.0
    _FIRST_CHUNK_TIMEOUT = 8.0
    _BETWEEN_CHUNK_TIMEOUT = 5.0

    def __init__(  # noqa: PLR0913 — keyword-only audio + endpoint config
        self,
        *,
        api_key: str,
        voice: str = "Chinese (Mandarin)_ExplorativeGirl",
        primary_endpoint: str = "https://api-uw.minimax.io",
        fallback_endpoint: str = "https://api.minimax.chat",
        model: str = "speech-2.8-turbo",
        volume: int = 5,
        sample_rate_in: int = 32000,
        sample_rate_out: int = 32000,
        connect_timeout_s: float = 3.0,
    ) -> None:
        """Configure endpoints, voice and audio shape; does not connect yet."""
        self._api_key = api_key
        self._voice = voice
        self._primary_endpoint = primary_endpoint
        self._fallback_endpoint = fallback_endpoint
        self._model = model
        self._volume = int(volume)
        self._sr_in = int(sample_rate_in)
        self._sr_out = int(sample_rate_out)
        self._connect_timeout = float(connect_timeout_s)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def synthesize(self, text: str) -> bytes:
        """Return float32 mono PCM bytes for ``text``.

        Tries the primary endpoint first; on ``OSError`` / ``TimeoutError`` /
        websockets error, falls back to the secondary endpoint. If both fail,
        raises :class:`MiniMaxUnavailableError`.
        """
        chunks = [chunk async for chunk in self.synthesize_stream(text)]
        return b"".join(chunks)

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield float32 PCM chunks as bytes as they arrive from MiniMax."""
        last_exc: BaseException | None = None
        for endpoint in (self._primary_endpoint, self._fallback_endpoint):
            try:
                async for chunk in self._stream_one_endpoint(endpoint, text):
                    yield chunk
            except (OSError, TimeoutError, _MiniMaxProtocolError) as exc:
                LOGGER.warning("MiniMax endpoint %s failed: %s", endpoint, exc)
                last_exc = exc
                continue
            except Exception as exc:
                # websockets raises subclasses of Exception (not OSError); we
                # treat any WS-layer failure as a connect/protocol failure for
                # fallback, and re-raise anything outside that namespace.
                if not type(exc).__module__.startswith("websockets"):
                    raise
                LOGGER.warning(
                    "MiniMax endpoint %s failed (%s): %s",
                    endpoint,
                    type(exc).__name__,
                    exc,
                )
                last_exc = exc
                continue
            else:
                return

        msg = "both primary and fallback MiniMax endpoints failed"
        raise MiniMaxUnavailableError(msg) from last_exc

    # ------------------------------------------------------------------
    # Internals — one session against a single endpoint
    # ------------------------------------------------------------------

    async def _stream_one_endpoint(
        self, endpoint: str, text: str,
    ) -> AsyncIterator[bytes]:
        ws_url = _base_to_ws_url(endpoint)
        headers = {"Authorization": f"Bearer {self._api_key}"}

        conn = await asyncio.wait_for(
            _ws_connect(ws_url, additional_headers=headers),
            timeout=self._connect_timeout,
        )
        try:
            await self._handshake(conn, text)
            resampler = self._make_resampler()
            async for chunk_bytes in self._stream_audio(conn, resampler):
                yield chunk_bytes
            # task_finish — best-effort, server may already be closing
            with contextlib.suppress(Exception):
                await conn.send(json.dumps({"event": "task_finish"}))
        finally:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _handshake(self, conn: Any, text: str) -> None:  # noqa: ANN401
        """Drive ``connected_success → task_start → task_started → task_continue``."""
        # 1. connected_success
        await asyncio.wait_for(conn.recv(), timeout=self._connect_timeout)

        # 2. task_start
        task_start = {
            "event": "task_start",
            "model": self._model,
            "voice_setting": {
                "voice_id": self._voice,
                "speed": 1.0,
                "vol": self._volume,
                "pitch": 0,
            },
            "audio_setting": {
                "format": "pcm",
                "sample_rate": self._sr_in,
                "bitrate": 128000,
                "channel": 1,
            },
        }
        await conn.send(json.dumps(task_start))
        ts = await asyncio.wait_for(conn.recv(), timeout=self._TASK_START_TIMEOUT)
        ts_obj = json.loads(ts)
        status = ts_obj.get("base_resp", {}).get("status_code", 0)
        if status != 0:
            msg = f"task_start rejected: {ts_obj.get('base_resp')}"
            raise _MiniMaxProtocolError(msg)

        # 3. task_continue
        await conn.send(json.dumps({"event": "task_continue", "text": text}))

    async def _stream_audio(
        self, conn: Any, resampler: Any | None,  # noqa: ANN401
    ) -> AsyncIterator[bytes]:
        """Loop ``conn.recv`` until ``is_final``; yield float32 PCM byte chunks."""
        carry: bytes = b""
        first = True
        while True:
            timeout = self._FIRST_CHUNK_TIMEOUT if first else self._BETWEEN_CHUNK_TIMEOUT
            msg_raw = await asyncio.wait_for(conn.recv(), timeout=timeout)
            obj = json.loads(msg_raw)

            audio_hex = obj.get("data", {}).get("audio", "") or ""
            if audio_hex:
                pcm_f32, carry = _decode_audio_hex(audio_hex, carry)
                if resampler is not None and pcm_f32.size:
                    pcm_f32 = resampler.resample_chunk(pcm_f32)
                if pcm_f32.size:
                    first = False
                    yield pcm_f32.astype(np.float32).tobytes()

            if obj.get("is_final"):
                if resampler is not None:
                    tail = resampler.resample_chunk(
                        np.zeros(0, dtype=np.float32), last=True,
                    )
                    if tail.size:
                        yield tail.astype(np.float32).tobytes()
                return

    def _make_resampler(self) -> Any | None:  # noqa: ANN401
        """Return a ``soxr.ResampleStream`` when sr_in != sr_out, else ``None``.

        ``soxr`` is imported lazily so the surrounding module stays importable
        in environments without it; the failure mode is a clear runtime error
        only when resampling is actually required.
        """
        if self._sr_in == self._sr_out:
            return None
        try:
            import soxr  # noqa: PLC0415
        except ImportError as exc:
            msg = (
                f"sample_rate_in={self._sr_in} != sample_rate_out={self._sr_out} "
                "requires the optional 'soxr' dependency"
            )
            raise RuntimeError(msg) from exc
        return soxr.ResampleStream(
            self._sr_in, self._sr_out, 1, dtype="float32", quality="HQ",
        )


def _decode_audio_hex(audio_hex: str, carry: bytes) -> tuple[np.ndarray, bytes]:
    """Decode a hex-encoded int16-LE PCM frame to float32 mono PCM in [-1, 1].

    Carries a trailing odd byte forward into the next frame so int16 reshape
    never sees an unaligned buffer. Ported from
    ``core/tts_minimax_ws.MinimaxWSClient.feed``.
    """
    if len(audio_hex) % 2:
        audio_hex = audio_hex[:-1]
    raw = carry + bytes.fromhex(audio_hex)
    aligned_len = (len(raw) // 2) * 2
    new_carry = raw[aligned_len:]
    raw = raw[:aligned_len]
    if not raw:
        return np.zeros(0, dtype=np.float32), new_carry
    pcm_i16 = np.frombuffer(raw, dtype=np.int16).copy()
    pcm_f32 = pcm_i16.astype(np.float32) / 32768.0
    return pcm_f32, new_carry


# --- TTS Pipeline (gate-mode routing + fallback chain) -----------------------
#
# ADR-0005 §5.3 / §10 F6-F7. The pipeline is event-driven: `_tts_watcher`
# (runtime layer) dispatches `surface.response_*` events into `begin_turn`,
# `handle_chunk`, `handle_emitted`. The pipeline owns gate-mode routing
# (spec §3.6.6: `sentence` plays each chunk immediately; `full_text` and
# `structured` buffer until the response is emitted) and the MiniMax →
# `macos_say` fallback chain (§3.6.11).
#
# This file's prior layers are pure ports (preprocessor, AudioStreamPlayer,
# MiniMaxWSClient). The pipeline is the orchestrator that L4/L5 events talk to.

GateMode = Literal["sentence", "full_text", "structured"]


class TTSPipeline:
    """Event-driven TTS playback per ADR-0005 §5.3.

    Lifecycle (one turn):

    * ``begin_turn(turn_id, gate_mode)`` — called on ``surface.response_open``;
      records the routing mode and clears any partial buffer from the prior
      turn.
    * ``handle_chunk(turn_id, text)`` — called on ``surface.response_chunk``.
      In ``sentence`` mode the chunk is synthesized and queued for playback
      immediately; in ``full_text`` / ``structured`` mode the chunk is
      appended to a per-turn buffer with no synthesis.
    * ``handle_emitted(turn_id)`` — called on ``surface.response_emitted``.
      In buffered modes the joined buffer is now synthesized in one shot,
      then ``end_turn`` runs.
    * ``end_turn(turn_id)`` — broadcasts the optional ``spoken`` voice phase
      and clears per-turn state. Safe to call on its own (sentence mode
      uses this directly from the watcher when the response is emitted).

    Sample rate alignment: callers should construct ``provider`` and
    ``player`` at the same rate (default 32 kHz). When they differ, the
    provider's ``soxr`` path resamples internally — see
    :class:`MiniMaxWSClient` for the contract.
    """

    def __init__(
        self,
        *,
        provider: MiniMaxWSClient,
        player: AudioStreamPlayer,
        fallback: Callable[[str], None],
        broadcaster: object | None = None,
        ducker: voice_ducking.SystemAudioDucker | None = None,
    ) -> None:
        """Wire the synthesis provider, audio player and macOS-say fallback.

        Args:
            provider: WebSocket-backed TTS source (MiniMax).
            player: PCM sink with a queue (drives ``is_speaking`` + ducking).
            fallback: Called with cleaned text when the provider is down;
                use :func:`macos_say_fallback` in production.
            broadcaster: Optional object exposing
                ``broadcast_voice_sync(phase, *, turn_id)`` — the pipeline
                emits ``"spoken"`` at end-of-turn for UI feedback.
            ducker: Optional :class:`voice_ducking.SystemAudioDucker`.
                When wired, system output is muted (refcounted) around
                each synth+play call so the assistant's OWN playback is
                not picked up by an open mic — ADR §5.3. Sharing one
                ducker instance between the wake listener and the TTS
                pipeline ensures the refcount nests correctly when both
                paths want output muted simultaneously.
        """
        self._provider = provider
        self._player = player
        self._fallback = fallback
        self._broadcaster = broadcaster
        self._ducker = ducker
        self._turn_id: str | None = None
        self._gate_mode: GateMode = "sentence"
        self._buffer: list[str] = []

    def begin_turn(self, turn_id: str, *, gate_mode: GateMode | None) -> None:
        """Called on ``surface.response_open``. Resets buffer + routing mode."""
        self._turn_id = turn_id
        self._gate_mode = gate_mode or "sentence"
        self._buffer.clear()

    def handle_chunk(self, turn_id: str, text: str) -> None:
        """Called on ``surface.response_chunk``.

        Sentence mode: every chunk is accumulated; a ``</voice>`` close
        tag in the chunk flushes the buffer through synth. Multiple
        ``<voice>...</voice>`` regions in one turn each flush at their
        own close. This batches the WS-connect overhead — one MiniMax
        round trip per voice region instead of one per chunk — and
        eliminates literal tag-text from being synthesised.
        Full_text / structured: accumulate as before, flush on
        ``handle_emitted``.
        """
        if turn_id != self._turn_id:
            LOGGER.warning(
                "TTSPipeline: chunk for unknown turn_id=%s (current=%s)",
                turn_id,
                self._turn_id,
            )
            return
        self._buffer.append(text)
        if self._gate_mode == "sentence" and "</voice>" in text:
            self._flush_buffer()

    def handle_emitted(self, turn_id: str) -> None:
        """Called on ``surface.response_emitted``. Flushes any pending buffer."""
        if turn_id != self._turn_id:
            return
        self.end_turn(turn_id)

    def end_turn(self, turn_id: str) -> None:
        """Flush any remaining buffer, broadcast ``spoken``, clear state.

        Defensive flush covers truncated streams (``</voice>`` never
        arrived) and the tests that drive the pipeline directly without
        a full ``handle_emitted`` event.
        """
        self._flush_buffer()
        broadcast = getattr(self._broadcaster, "broadcast_voice_sync", None)
        if callable(broadcast):
            try:
                broadcast("spoken", turn_id=turn_id)
            except Exception as exc:  # noqa: BLE001 — broadcast must not crash TTS
                LOGGER.warning("broadcast_voice_sync(spoken) failed: %r", exc)
        self._turn_id = None
        self._buffer.clear()

    def _flush_buffer(self) -> None:
        """Synth whatever is in the buffer (if any), then clear it."""
        if not self._buffer:
            return
        joined = "".join(self._buffer)
        self._buffer.clear()
        if joined:
            self._speak(joined)

    def is_speaking(self) -> bool:
        """True iff the player still has queued bytes (drives wake suppression)."""
        return self._player.bytes_pending() > 0

    def _speak(self, text: str) -> None:
        """Synthesize ``text`` and push the PCM bytes to the player.

        ADR §5.3: while the synth + write hits the speakers, mute system
        output (refcounted) so any other macOS audio source does not
        layer on top. Ducker failures are logged at DEBUG and never
        break TTS — the synth path itself must keep working.

        On :class:`MiniMaxUnavailableError` (both endpoints down) or any
        unexpected synth/playback exception, route the cleaned text to
        the macOS ``say`` fallback. F7 is the terminal leaf — failures
        beyond that are logged only (assistant response goes silent).
        """
        # Extract <voice>...</voice> regions BEFORE preprocessing so the
        # downstream MiniMax call never sees literal tag chars (which
        # synthesise as "less-than voice greater-than" gibberish) and so
        # any <document>...</document> region is silently dropped from
        # synthesis. See ``_extract_voice_content`` for fallback rules
        # when the text has no tags (legacy plain-text path).
        voice_only = _extract_voice_content(text)
        cleaned = _preprocess_for_speech(voice_only)
        if not cleaned:
            return
        # NOTE: do NOT wrap synth+write in SystemAudioDucker. That ducker
        # zeroes the macOS master output volume — which silences the TTS
        # output stream itself for the duration of write() (write blocks
        # while the ring drains, up to its 10 s timeout). Legacy used a
        # PCM-level gain duck inside the player for barge-in; the
        # OS-level master-volume duck only belongs on the wake-capture
        # path (mute speakers while the mic is open).
        try:
            pcm = asyncio.run(self._provider.synthesize(cleaned))
            self._player.write(pcm)
        except MiniMaxUnavailableError:
            LOGGER.warning(
                "MiniMax unavailable; falling back to macos_say for: %r",
                cleaned,
            )
            self._fallback(cleaned)
        except Exception:
            # TTS path must never crash the daemon; F7 fallback.
            LOGGER.exception(
                "TTS synth failed for turn_id=%s", self._turn_id,
            )
            self._fallback(cleaned)


def macos_say_fallback(text: str, *, voice: str = "Tingting") -> None:
    """ADR-0005 §10 F7 fallback: macOS ``say`` subprocess.

    Log-only on failure; the assistant response is silent but the daemon
    stays up. ADR-0007 will eventually replace this leaf with a
    ``surface.failed`` event.
    """
    try:
        subprocess.run(  # noqa: S603 — `say` is the macOS API contract.
            ["say", "-v", voice, text],  # noqa: S607 — PATH lookup is the contract.
            check=False,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        LOGGER.warning(
            "macos_say_fallback failed: %r — response remains silent", exc,
        )


__all__ = [
    "AudioStreamPlayer",
    "MiniMaxUnavailableError",
    "MiniMaxWSClient",
    "TTSPipeline",
    "_preprocess_for_speech",
    "macos_say_fallback",
]
