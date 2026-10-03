"""L5 native voice output: the realtime half of the streaming player in a Swift helper.

ADR 0129.  ``AudioStreamPlayer._callback`` needs the GIL every CoreAudio IO
cycle, so any daemon thread holding it longer than one cycle makes coreaudiod
skip a cycle, and Allen hears a pop.  ``native/voice_out/main.swift`` renders
the same ring, gain ramps and declicks in its own process; this module is the
daemon's side of the pipe.  Leases, the ``PlaybackLedger``, heard-prefix
accounting and the media actor stay in :class:`AudioStreamPlayer`, which this
class inherits: only what touches the realtime half is overridden.

The wire protocol is documented at the top of ``main.swift``; the constants
below mirror it.

Layer rules: stdlib, ``numpy`` and ``jarvis.surface.voice_tts`` only.
"""

from __future__ import annotations

import collections
import contextlib
import logging
import os
import shutil
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import numpy as np

from jarvis.surface.voice_ledger import GenerationLease
from jarvis.surface.voice_tts import (
    AudioStreamPlayer,
    PlayerStartResult,
    PlayerStopResult,
    _GenerationRingBuffer,
)

if TYPE_CHECKING:
    from jarvis.surface.voice_ledger import (
        AudibilityClass,
        ForegroundBusy,
        OutputTimelineSnapshot,
        StalePlaybackGeneration,
    )

LOGGER = logging.getLogger(__name__)

_HELPER_DIR = Path(__file__).resolve().parents[2] / "native" / "voice_out"
_BUILD_TIMEOUT_S = 300.0

# Python -> helper.
_PCM, _ACTIVE, _DISCARD, _GAIN, _HOLD = 1, 2, 3, 4, 5
# helper -> Python.
_READY, _REPORT, _STATUS, _DISCARD_ACK, _RENDERED = 0x81, 0x82, 0x83, 0x84, 0x85
# The helper's frame ceiling is 65536 samples; stay well inside it.
_MAX_PCM_FRAME_SAMPLES = 32768
# The wrong clock is off by seconds (CLOCK_MONOTONIC counts sleep, UPTIME_RAW
# does not); pipe and wake-up latency are well under this.
_CLOCK_SKEW_WARN_NS = 1_000_000
_CLOCK_SKEW_FAIL_NS = 100_000_000
_State = Literal["closed", "opening", "open", "closing", "uncertain"]
_AUDIBILITY: tuple[AudibilityClass, ...] = ("normal", "attenuated", "muted", "unknown")

_REPORT_FMT = struct.Struct("<qqqBqqB")
_STATUS_FMT = struct.Struct("<qdQQQIQ")
_READY_FMT = struct.Struct("<IIqq")
_RENDERED_HEAD = struct.Struct("<Q")


def ensure_helper_binary(source_dir: Path = _HELPER_DIR) -> Path:
    """Return the helper binary, compiling it first when missing or older than its source."""
    source = source_dir / "main.swift"
    binary = source_dir / ".build" / "jarvis-voice-out"
    if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    swiftc = shutil.which("swiftc") or "/usr/bin/swiftc"
    binary.parent.mkdir(parents=True, exist_ok=True)
    scratch = binary.with_name(f"{binary.name}.{os.getpid()}.tmp")
    try:
        subprocess.run(  # noqa: S603 - fixed argv, repo-owned source
            [swiftc, "-O", "-o", str(scratch), str(source)],
            check=True,
            capture_output=True,
            timeout=_BUILD_TIMEOUT_S,
        )
        scratch.replace(binary)
    except subprocess.CalledProcessError as exc:
        LOGGER.warning("voice-out build failed: %s", exc.stderr.decode(errors="replace")[-400:])
        raise
    finally:
        scratch.unlink(missing_ok=True)
    LOGGER.info("built %s", binary)
    return binary


class _MirroredRing(_GenerationRingBuffer):
    """The helper's sample ring as the media actor sees it: counters, with samples on the pipe.

    ``sent`` is the producer index in the helper's own numbering.  ``read_idx``
    comes back in STATUS frames and already includes applied discards, so
    capacity is only reclaimed once the helper's render thread has moved past
    the samples, exactly as ``_GenerationRingBuffer.available_write`` does.
    """

    def __init__(self, player: NativeAudioStreamPlayer, size_samples: int) -> None:
        super().__init__(size_samples)
        self._player = player
        self.sent = 0
        self.read_idx = 0
        self.discard_before = 0
        self.discard_seq = 0
        self.acked_seq = 0

    def reset(self) -> None:
        self.sent = self.read_idx = self.discard_before = 0
        self.discard_seq = self.acked_seq = 0

    def available_read(self) -> int:
        return self.sent - max(self.read_idx, self.discard_before)

    def available_write(self) -> int:
        return self._size - (self.sent - self.read_idx)

    def write(
        self,
        data: np.ndarray,
        *,
        generation: int,
        output_start_cursor: int,
    ) -> int:
        """Send as much as fits the mirrored capacity; 0 means full (never a drop)."""
        n = min(len(data), self.available_write(), _MAX_PCM_FRAME_SAMPLES)
        if n <= 0:
            return 0
        head = struct.pack("<qq", generation, output_start_cursor)
        if not self._player.send_frame(_PCM, head, data[:n].astype("<f4", copy=False).tobytes()):
            return 0
        self.sent += n
        return n

    def request_discard(self) -> int:
        """ACTIVE(-1) then DISCARD(seq): the helper drops everything sent before the frame."""
        self._player.send_frame(_ACTIVE, struct.pack("<q", -1))
        self.discard_seq += 1
        self.discard_before = self.sent
        self._player.send_frame(_DISCARD, struct.pack("<Q", self.discard_seq))
        return self.discard_before


class NativeAudioStreamPlayer(AudioStreamPlayer):
    """``AudioStreamPlayer`` whose realtime callback runs in ``jarvis-voice-out``.

    Needs ``generation_safe=True``.  ``playback_tap`` (the echo canceller's far
    end) is fed from the helper's RENDERED frames: the final mono block it hands
    the device, silence included, in order.
    """

    _READY_TIMEOUT_S = 5.0
    _ACK_WAIT_S = 0.05

    def __init__(
        self,
        *,
        buffer_frames: int = 512,
        extra_args: tuple[str, ...] = (),
        **kwargs: Any,  # noqa: ANN401 - AudioStreamPlayer's own keyword surface
    ) -> None:
        """Build an idle player; ``extra_args`` go to the helper (``--null-device`` in tests)."""
        if not kwargs.get("generation_safe", False):
            msg = "the native player is generation-safe only"
            raise NotImplementedError(msg)
        lazy_open = kwargs.pop("lazy_open", True)
        super().__init__(lazy_open=True, **kwargs)
        ring = self._generation_ring
        assert ring is not None  # noqa: S101 - generation_safe was checked above
        self._mirror = _MirroredRing(self, ring._size)  # noqa: SLF001
        self._generation_ring = self._mirror
        self._buffer_frames = int(buffer_frames)
        self._extra_args = extra_args
        self._proc: subprocess.Popen[bytes] | None = None
        self._helper_died = False
        self._pipe_lock = threading.Lock()
        self._ready = threading.Event()
        self._got_ready = False
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=8)
        self._stderr_thread = threading.Thread()
        self._native_gain = 1.0
        self.clock_skew_ns = 0
        self.rendered_dropped_samples = 0
        self._tap_failed = False
        if not lazy_open:
            started = self.start()
            if not started.started:
                msg = f"output helper start failed: {started.reason}"
                raise RuntimeError(msg)

    # ------------------------------------------------------------------
    # Pipe
    # ------------------------------------------------------------------

    def send_frame(self, frame_type: int, *parts: bytes) -> bool:
        """Write one frame to the helper; False once it is gone."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        length = 1 + sum(map(len, parts))
        with self._pipe_lock:
            try:
                proc.stdin.write(struct.pack("<IB", length, frame_type))
                for part in parts:
                    proc.stdin.write(part)
                proc.stdin.flush()
            except (BrokenPipeError, ValueError, OSError):
                return False
        return True

    def _read_loop(self, proc: subprocess.Popen[bytes]) -> None:
        stdout = proc.stdout
        assert stdout is not None  # noqa: S101 - Popen(stdout=PIPE)
        try:
            while True:
                header = stdout.read(4)
                if len(header) < 4:  # noqa: PLR2004
                    break
                (length,) = struct.unpack("<I", header)
                body = stdout.read(length)
                if len(body) < length:
                    break
                self._on_frame(body[0], memoryview(body)[1:])
        except Exception:  # a bad frame ends the helper, not the daemon
            LOGGER.exception("voice-out helper stream failed")
        finally:
            self._on_helper_exit(proc)

    def _on_frame(self, frame_type: int, body: memoryview) -> None:
        if frame_type == _REPORT:
            gen, start, end, audibility, callback_ns, delay_ns, first = _REPORT_FMT.unpack(body)
            self._callback_reports.write(
                generation=gen,
                output_start_cursor=start,
                output_end_cursor=end,
                audibility_class=_AUDIBILITY[audibility],
                callback_monotonic_ns=callback_ns,
                presentation_delay_ns=self._clamped_latency_ns(delay_ns),
                first_for_generation=bool(first),
            )
        elif frame_type == _STATUS:
            (
                self._mirror.read_idx,
                self._native_gain,
                self._callback_calls,
                self._underflow_count,
                self._starvation_gaps,
                self._tail_ramp_samples,
                self._played_samples,
            ) = _STATUS_FMT.unpack(body)
        elif frame_type == _DISCARD_ACK:
            (seq,) = struct.unpack("<Q", body)
            self._mirror.acked_seq = max(self._mirror.acked_seq, seq)
        elif frame_type == _RENDERED:
            self._on_rendered(body)
        elif frame_type == _READY:
            received_ns = time.monotonic_ns()
            _rate, frames, latency_ns, clock_ns = _READY_FMT.unpack(body)
            self._buffer_frames = frames
            self._estimated_output_latency_ns = self._clamped_latency_ns(latency_ns)
            self.clock_skew_ns = received_ns - clock_ns
            self._got_ready = True
            self._ready.set()

    def _on_rendered(self, body: memoryview) -> None:
        """Hand one rendered block to the playback tap; a broken tap never ends her voice."""
        (self.rendered_dropped_samples,) = _RENDERED_HEAD.unpack_from(body)
        tap = self._playback_tap
        if tap is None:
            return
        try:
            tap(np.frombuffer(body[_RENDERED_HEAD.size :], dtype="<f4"), self._sample_rate_hz)
        except Exception:  # the far end must not kill the helper stream
            if not self._tap_failed:
                self._tap_failed = True
                LOGGER.exception("playback tap failed; later failures are not logged")

    def _clamped_latency_ns(self, latency_ns: int) -> int:
        """A real report is clamped to the ceiling; a non-positive one carries no measurement."""
        if latency_ns <= 0:
            return self._default_output_latency_ns
        return min(latency_ns, int(self._MAX_PLAUSIBLE_OUTPUT_LATENCY_S * 1_000_000_000))

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None  # noqa: S101 - Popen(stderr=PIPE)
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                self._stderr_tail.append(line)
                LOGGER.warning("voice-out helper: %s", line)

    def _on_helper_exit(self, proc: subprocess.Popen[bytes]) -> None:
        with self._lifecycle_lock:
            if self._proc is not proc:
                return
            unexpected = self._lifecycle_state == "open"
            if unexpected:
                self._lifecycle_state = "closed"
                self._helper_died = True
        self._ready.set()
        if unexpected:
            LOGGER.warning(
                "voice-out helper exited unexpectedly (rc=%s); restart() or the next start() "
                "brings it back",
                proc.poll(),
            )
            self._terminalize_software_playback()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _helper_argv(self, binary: Path) -> list[str]:
        argv = [
            str(binary),
            "--rate", str(self._sample_rate_hz),
            "--ring-samples", str(self._mirror._size),  # noqa: SLF001
            "--buffer-frames", str(self._buffer_frames),
        ]  # fmt: skip
        if self._playback_tap is not None:
            argv.append("--rendered")
        argv += self._extra_args
        if self._device is not None:
            argv += ["--device", str(self._device)]
        return argv

    def start(self) -> PlayerStartResult:
        """Build the helper if needed, spawn it and wait for READY."""
        with self._lifecycle_lock:
            if self._lifecycle_state == "open":
                return PlayerStartResult(
                    "already_started", self._ownership_attempt_id, "already_open",
                )
            if self._lifecycle_state != "closed":
                return PlayerStartResult(
                    "uncertain",
                    self._ownership_attempt_id,
                    f"ownership_debt_{self._lifecycle_state}",
                )
            self._lifecycle_attempt_id += 1
            attempt_id = self._ownership_attempt_id = self._lifecycle_attempt_id
            self._lifecycle_state = "opening"
        try:
            binary = ensure_helper_binary()
            self._mirror.reset()
            self._ready.clear()
            self._got_ready = False
            self._stderr_tail.clear()
            proc = subprocess.Popen(  # noqa: S603 - fixed argv, repo-built binary
                self._helper_argv(binary),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._set_state("closed")
            reason = f"helper_spawn:{type(exc).__name__}"
            return PlayerStartResult("failed_closed", attempt_id, reason)
        self._proc = proc
        reader = threading.Thread(
            target=self._read_loop, args=(proc,), daemon=True, name="jarvis-voice-out-in",
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(proc,), daemon=True, name="jarvis-voice-out-err",
        )
        reader.start()
        self._stderr_thread.start()
        if not self._ready.wait(self._READY_TIMEOUT_S) or not self._got_ready:
            return self._fail_start(proc, attempt_id)
        if abs(self.clock_skew_ns) > _CLOCK_SKEW_FAIL_NS:
            self._stderr_tail.append(f"clock_mismatch_ns={self.clock_skew_ns}")
            return self._fail_start(proc, attempt_id)
        if abs(self.clock_skew_ns) > _CLOCK_SKEW_WARN_NS:
            LOGGER.warning("voice-out clock differs from monotonic by %d ns", self.clock_skew_ns)
        self._set_state("open")
        LOGGER.info(
            "NativeAudioStreamPlayer started: %dHz buffer=%d frames latency=%d ns",
            self._sample_rate_hz,
            self._buffer_frames,
            self._estimated_output_latency_ns,
        )
        return PlayerStartResult("started", attempt_id, "helper_started")

    def _fail_start(self, proc: subprocess.Popen[bytes], attempt_id: int) -> PlayerStartResult:
        self._kill(proc, timeout_s=1.0)
        self._stderr_thread.join(timeout=0.5)
        reason = "; ".join([f"rc={proc.returncode}", *self._stderr_tail])
        self._proc = None
        self._set_state("closed" if proc.poll() is not None else "uncertain")
        return PlayerStartResult("failed_closed", attempt_id, f"helper:{reason}")

    def _set_state(self, state: _State) -> None:
        with self._lifecycle_lock:
            self._lifecycle_state = state

    @staticmethod
    def _kill(proc: subprocess.Popen[bytes], *, timeout_s: float) -> None:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=timeout_s)

    def stop(self, *, timeout_s: float | None = None) -> PlayerStopResult:
        """Discard what is queued, close the helper's stdin, then kill it if it will not exit."""
        self._terminalize_software_playback()
        timeout = self._DEFAULT_LIFECYCLE_TIMEOUT_S if timeout_s is None else max(0.0, timeout_s)
        with self._lifecycle_lock:
            state = self._lifecycle_state
            if state == "closed":
                return PlayerStopResult(
                    "already_closed", self._ownership_attempt_id, "already_closed",
                )
            if state in {"opening", "closing"}:
                return PlayerStopResult(
                    "uncertain",
                    self._ownership_attempt_id,
                    f"{state}_in_flight",
                    helper_thread_alive=True,
                )
            self._lifecycle_attempt_id += 1
            attempt_id = self._lifecycle_attempt_id
            self._lifecycle_state = "closing"
            proc = self._proc
        if proc is not None:
            with self._pipe_lock, contextlib.suppress(OSError, ValueError):
                assert proc.stdin is not None  # noqa: S101 - Popen(stdin=PIPE)
                proc.stdin.close()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill(proc, timeout_s=1.0)
            if proc.poll() is None:
                self._set_state("uncertain")
                return PlayerStopResult(
                    "uncertain", attempt_id, "close_timeout", helper_thread_alive=True,
                )
        with self._lifecycle_lock:
            self._proc = None
            self._lifecycle_state = "closed"
        LOGGER.info(
            "NativeAudioStreamPlayer stopped; lifetime callbacks=%d overloads=%d",
            self._callback_calls,
            self._underflow_count,
        )
        return PlayerStopResult("closed", attempt_id, "helper_exited")

    # ------------------------------------------------------------------
    # Realtime half, forwarded
    # ------------------------------------------------------------------

    def activate_generation(
        self,
        *,
        session_id: str,
        response_id: str,
        response_group_id: str,
        turn_id: str,
    ) -> GenerationLease | ForegroundBusy:
        """Mint the lease, then tell the helper which generation may play.

        A helper that died since the last answer is started again first: the
        media actor starts the player once, at boot, so without this she would
        stay silent until the daemon restarts.
        """
        if self._helper_died:
            self._helper_died = False
            restarted = self.start()
            LOGGER.warning("voice-out helper restarted for the next answer: %s", restarted.status)
        lease = super().activate_generation(
            session_id=session_id,
            response_id=response_id,
            response_group_id=response_group_id,
            turn_id=turn_id,
        )
        if isinstance(lease, GenerationLease):
            self.send_frame(_ACTIVE, struct.pack("<q", lease.playback_generation_id))
        return lease

    def complete_generation(
        self,
        *,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration:
        """Close a naturally presented generation; the helper then plays nothing of it."""
        snapshot = super().complete_generation(
            expected_playback_generation_id=expected_playback_generation_id,
        )
        if self._active_lease is None:
            self.send_frame(_ACTIVE, struct.pack("<q", -1))
        return snapshot

    def settle_interrupted_generation(
        self,
        *,
        expected_playback_generation_id: int,
    ) -> OutputTimelineSnapshot | StalePlaybackGeneration | None:
        """Freeze once the helper acked the discard (every report before it is then in).

        The ack comes one render callback after the DISCARD frame (about 11 ms),
        so this waits for it, bounded, instead of returning ``None`` at once: the
        media actor yields to its other tasks on ``None``, and a response task
        that reads the tombstone in that window ends its own turn as a failure
        before the interrupt's terminal commits.  ``None`` is the bounded answer
        for an ack that is slower than that; a dead helper never acks, so it
        freezes at once.
        """
        mirror = self._mirror
        if expected_playback_generation_id in self._tombstoned_generations:
            deadline = time.monotonic() + self._ACK_WAIT_S
            while mirror.acked_seq < mirror.discard_seq and self.is_running:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.0005)
        return super().settle_interrupted_generation(
            expected_playback_generation_id=expected_playback_generation_id,
        )

    def set_gain(self, target: float, ramp_ms: float = 30.0) -> None:
        """Latest-wins gain ramp, consumed by the helper's render thread."""
        ramp_samples = max(0, int(self._sample_rate_hz * ramp_ms / 1000.0))
        self.send_frame(_GAIN, struct.pack("<fI", target, ramp_samples))

    def pause_generation(self, *, paused: bool) -> None:
        """Hold the answer playing now in place, or let it go on."""
        super().pause_generation(paused=paused)
        self.send_frame(_HOLD, struct.pack("<q", self._paused_generation))

    def current_gain(self) -> float:
        """Gain at the helper's last status frame."""
        return self._native_gain

    @property
    def is_running(self) -> bool:
        """True while the helper process is alive and the stream is open."""
        proc = self._proc
        return proc is not None and self._lifecycle_state == "open" and proc.poll() is None
