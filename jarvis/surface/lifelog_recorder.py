"""This machine's share of his recorded day (ADR 0256): the microphone it already holds, in pieces.

A terminal that listens holds one audio ingress. With ``lifelog_capture.enabled`` it also takes a
diagnostic lane of that ingress and cuts what it hears into pieces at each ten-minute mark of the
wall clock, at a gap in the stream, and whenever it pauses (the microphone muted, or the switch
off). A piece is written as a WAV under ``.recording/`` in the outbox, encoded there with macOS's
``afconvert`` (Opus in CAF, else AAC in MPEG-4), moved into the outbox whole, and sent to the
brain's archive (ADR 0238) with this device's token. The outbox keeps a piece until the brain's
SHA-256 of it matches this side's, so the Mac holds only what has not reached the brain yet.

The lane is the canonical one every listener reads (16 kHz, mono, channel 0), as heard: no echo
cancellation, no noise suppression.

Layer rules (L5): stdlib, ``httpx``, the L5 voice ingress, and the archive's L2 names.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import subprocess
import threading
import time
import uuid
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import httpx

from jarvis.state.lifelog import MAX_PIECE_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.surface.voice_audio import CanonicalAudioFrame

LOGGER = logging.getLogger(__name__)

OUTBOX_DIRNAME: Final = "lifelog-outbox"
"""Under the terminal's runtime root."""
RECORDING_DIRNAME: Final = ".recording"
HELD_DIRNAME: Final = "held"
"""Pieces the brain refused for a reason that sending again will not change; kept, not deleted."""
PIECE_MS: Final = 10 * 60 * 1000
LANE_CAPACITY: Final = 512
"""About 16 s of 32 ms frames: the lane only waits on a disk write, never on an encode."""
UPLOAD_PATH: Final = "/inherent/lifelog/audio/"
RETRY_S: Final = 60.0
FULL_RETRY_S: Final = 3600.0
ENCODE_TIMEOUT_S: Final = 120.0
UPLOAD_TIMEOUT_S: Final = 120.0

_READY: Final = re.compile(
    r"(?P<start>\d{13})_(?P<ms>\d+)ms_(?P<id>[0-9a-f]{32})\.(?P<ext>caf|m4a)",
)
_RECORDING: Final = re.compile(r"(?P<start>\d{13})_(?P<id>[0-9a-f]{32})\.wav")
_WAV_HEADER_BYTES: Final = 44
_FULL: Final = 507
_FORMATS: Final = (("caff", "opus", "caf", "24000"), ("m4af", "aac", "m4a", "32000"))
_REFUSED_FOR_GOOD: Final = frozenset({400, 409, 411, 413, 415, 422})


class Lane(Protocol):
    """What the recorder reads: one subscription of the ingress."""

    @property
    def closed(self) -> bool:
        """Whether the lane is gone."""
        ...

    def read(self, *, timeout_s: float = 0.0) -> CanonicalAudioFrame | None:
        """One frame, or ``None`` after ``timeout_s``."""
        ...

    def close(self) -> None:
        """Unsubscribe."""
        ...



def encode_piece(wav: Path, stem: Path) -> Path | None:
    """``wav`` as Opus in CAF at ``stem.caf``, else AAC at ``stem.m4a``; ``None`` if neither."""
    for fmt, codec, ext, bitrate in _FORMATS:
        out = stem.with_suffix(f".{ext}")
        argv = ["afconvert", "-f", fmt, "-d", codec, "-b", bitrate, str(wav), str(out)]
        try:
            # S603: fixed argv, no shell.
            subprocess.run(argv, check=True, capture_output=True, timeout=ENCODE_TIMEOUT_S)  # noqa: S603
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            out.unlink(missing_ok=True)
            continue
        if out.stat().st_size > 0:
            return out
        out.unlink(missing_ok=True)
    return None


@dataclass
class _Piece:
    """The piece being written: its WAV, where it started and how many samples it holds."""

    path: Path
    writer: wave.Wave_write
    piece_id: str
    started_ms: int
    ends_at_ms: int
    rate_hz: int
    samples: int = 0
    last_cursor: int | None = None
    epoch: int | None = None

    @property
    def duration_ms(self) -> int:
        return self.samples * 1000 // self.rate_hz


def _wall_ms_of(frame: CanonicalAudioFrame) -> int:
    """The wall-clock moment of ``frame``'s first sample, from its monotonic capture time."""
    lag_ns = max(0, time.monotonic_ns() - frame.captured_monotonic_ns)
    return (time.time_ns() - lag_ns) // 1_000_000


class LifelogRecorder:
    """Cuts one lane into pieces and sends them to the brain; one per listening terminal."""

    def __init__(  # noqa: PLR0913 — what it reads, where it keeps, where it sends, when it stops.
        self,
        lane: Lane,
        outbox: Path,
        *,
        brain_url: str,
        token: str,
        paused: Callable[[], bool],
        encode: Callable[[Path, Path], Path | None] = encode_piece,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Nothing runs until :meth:`start`."""
        self._lane = lane
        self._outbox = outbox
        self._recording = outbox / RECORDING_DIRNAME
        self._held = outbox / HELD_DIRNAME
        self._paused = paused
        self._encode = encode
        self._url = brain_url.rstrip("/") + UPLOAD_PATH
        self._http = httpx.Client(
            timeout=UPLOAD_TIMEOUT_S, transport=transport,
            headers={"Authorization": f"Bearer {token}"},
        )
        self._piece: _Piece | None = None
        self._closed = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Finish what an earlier run left, then read the lane and send on two threads."""
        for path in (self._outbox, self._recording, self._held):
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.recover()
        loops = ((self._read, "jarvis-lifelog-lane"), (self._send, "jarvis-lifelog-send"))
        for target, name in loops:
            thread = threading.Thread(target=target, name=name, daemon=True)
            thread.start()
            self._threads.append(thread)
        LOGGER.info("lifelog: this terminal records its microphone into the brain's archive")

    def close(self) -> None:
        """Stop reading, close the piece in hand, and stop sending (unsent pieces stay)."""
        self._closed.set()
        self._wake.set()
        self._lane.close()
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._finish()
        self._http.close()

    # --- cutting -------------------------------------------------------------------------

    def feed(self, frame: CanonicalAudioFrame) -> None:
        """Add one frame to the piece in hand, opening and closing pieces where they break."""
        if self._paused():
            self._finish()
            return
        piece = self._piece
        mark: int | None = None
        if piece is not None:
            broken = (
                frame.discontinuity_before
                or frame.stream_epoch != piece.epoch
                or frame.sample_rate_hz != piece.rate_hz
                or (piece.last_cursor is not None and frame.sample_cursor != piece.last_cursor)
            )
            if broken or piece.started_ms + piece.duration_ms >= piece.ends_at_ms:
                # A piece the stream runs on from ends at the next mark after this one, even
                # when the wall clock puts its first frame a few milliseconds short of the mark.
                mark = None if broken else piece.ends_at_ms
                self._finish()
                piece = None
        if piece is None:
            piece = self._open(_wall_ms_of(frame), frame.sample_rate_hz, after_mark=mark)
            piece.epoch = frame.stream_epoch
        piece.writer.writeframesraw(frame.pcm16_mono)
        piece.samples += frame.frame_count
        piece.last_cursor = frame.sample_cursor + frame.frame_count

    def _open(self, started_ms: int, rate_hz: int, *, after_mark: int | None = None) -> _Piece:
        piece_id = uuid.uuid4().hex
        path = self._recording / f"{started_ms}_{piece_id}.wav"
        writer = wave.open(str(path), "wb")  # noqa: SIM115 — closed in _finish
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate_hz)
        ends_at = (started_ms // PIECE_MS + 1) * PIECE_MS
        if after_mark is not None:
            ends_at = max(ends_at, after_mark + PIECE_MS)
        self._piece = _Piece(path, writer, piece_id, started_ms, ends_at, rate_hz)
        return self._piece

    def _finish(self) -> None:
        """Close the piece in hand and encode it into the outbox."""
        piece, self._piece = self._piece, None
        if piece is None:
            return
        piece.writer.close()
        if piece.samples == 0:
            piece.path.unlink(missing_ok=True)
            return
        self._ready(piece.path, piece.started_ms, piece.duration_ms, piece.piece_id)

    def _ready(self, wav: Path, started_ms: int, duration_ms: int, piece_id: str) -> None:
        # Encoded beside its WAV and moved into the outbox whole: the sender never sees a
        # piece the encoder is still writing.
        stem = self._recording / f"{started_ms}_{max(1, duration_ms)}ms_{piece_id}"
        out = self._encode(wav, stem)
        if out is None:
            LOGGER.warning("lifelog: could not encode %s; the WAV stays in the outbox", wav.name)
            return
        out.replace(self._outbox / out.name)
        wav.unlink(missing_ok=True)
        self._wake.set()

    def recover(self) -> None:
        """Encode the WAVs a stopped run left half-written, from the samples on disk."""
        for wav in sorted(self._recording.glob("*.wav")):
            match = _RECORDING.fullmatch(wav.name)
            if match is None or (self._piece is not None and wav == self._piece.path):
                continue
            data = wav.read_bytes()[_WAV_HEADER_BYTES:]
            data = data[: len(data) // 2 * 2]
            if not data:
                wav.unlink(missing_ok=True)
                continue
            rate = _wav_rate(wav)
            with wave.open(str(wav), "wb") as fixed:
                fixed.setnchannels(1)
                fixed.setsampwidth(2)
                fixed.setframerate(rate)
                fixed.writeframes(data)
            duration = (len(data) // 2) * 1000 // rate
            self._ready(wav, int(match["start"]), duration, match["id"])

    def _read(self) -> None:
        while not self._closed.is_set() and not self._lane.closed:
            frame = self._lane.read(timeout_s=0.25)
            if frame is None:
                if self._paused():
                    self._finish()
                continue
            try:
                self.feed(frame)
            except OSError:
                LOGGER.exception("lifelog: writing the piece failed; starting a new one")
                with contextlib.suppress(OSError, wave.Error):
                    if self._piece is not None:
                        self._piece.writer.close()
                self._piece = None

    # --- sending -------------------------------------------------------------------------

    def send_ready(self) -> float:
        """Send every piece in the outbox once; the seconds to wait before the next pass."""
        wait = RETRY_S
        for path in sorted(self._outbox.iterdir()):
            match = _READY.fullmatch(path.name)
            if match is None or self._closed.is_set():
                continue
            wait = max(wait, self._send_one(path, match))
            if wait >= FULL_RETRY_S:
                break
        return wait

    def _send_one(self, path: Path, match: re.Match[str]) -> float:
        data = path.read_bytes()
        if len(data) > MAX_PIECE_BYTES:
            self._hold(path, "larger than the brain takes")
            return RETRY_S
        try:
            answer = self._http.put(
                self._url + match["id"],
                params={"started_ms": match["start"], "duration_ms": match["ms"]},
                content=data,
            )
        except httpx.HTTPError as exc:
            LOGGER.info(
                "lifelog: the brain is not reachable (%s); sending later", type(exc).__name__,
            )
            return RETRY_S
        if answer.status_code in (200, 201):
            digest = answer.json().get("sha256") if answer.content else None
            if digest == hashlib.sha256(data).hexdigest():
                path.unlink(missing_ok=True)
            else:
                LOGGER.warning("lifelog: the brain's digest of %s does not match", path.name)
            return RETRY_S
        if answer.status_code == _FULL:
            LOGGER.warning("lifelog: the brain's disk is full; pieces wait here")
            return FULL_RETRY_S
        if answer.status_code in _REFUSED_FOR_GOOD:
            self._hold(path, f"refused {answer.status_code}")
            return RETRY_S
        LOGGER.warning("lifelog: the brain answered %d; sending later", answer.status_code)
        return RETRY_S

    def _hold(self, path: Path, why: str) -> None:
        LOGGER.warning("lifelog: %s %s; kept in %s", path.name, why, HELD_DIRNAME)
        path.replace(self._held / path.name)

    def _send(self) -> None:
        while not self._closed.is_set():
            wait = RETRY_S
            try:
                wait = self.send_ready()
            except OSError:
                LOGGER.exception("lifelog: reading the outbox failed")
            self._wake.wait(timeout=wait)
            self._wake.clear()


def _wav_rate(wav: Path) -> int:
    """The sample rate in a WAV header, even one whose lengths were never written."""
    with wav.open("rb") as raw:
        header = raw.read(_WAV_HEADER_BYTES)
    rate = int.from_bytes(header[24:28], "little") if len(header) >= 28 else 0  # noqa: PLR2004
    return rate if rate > 0 else 16_000

