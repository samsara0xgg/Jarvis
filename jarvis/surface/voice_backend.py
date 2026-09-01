"""Typed PortAudio ownership for ADR-0006's single audio ingress.

The PortAudio callback in this module has one deliberately small job: hand the
callback-owned buffer to the private ingress sink before returning.  It never
logs, waits, opens a database, calls an application callback, or transfers a
borrowed buffer beyond callback lifetime.  ``AudioIngress`` copies the bytes
into its preallocated native SPSC ring inside that private sink.

Wave 3 keeps the accepted Wave-2 output actor intact.  ``AudioDuplexBackend``
therefore exposes the render-source seam required by a future paired
VoiceProcessingIO backend, while ``SoundDeviceDuplexBackend`` owns only the
default input stream in this wave.  It never creates a second output stream.
"""

from __future__ import annotations

import enum
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import Callable


class BackendStartStatus(enum.Enum):
    """Auditable result of one physical input-open attempt."""

    STARTED = "started"
    OWNER_BUSY = "owner_busy"
    FAILED_CLOSED = "failed_closed"
    OPEN_UNCERTAIN = "open_uncertain"


class BackendStopStatus(enum.Enum):
    """Auditable result of stopping one physical input epoch."""

    CLOSED = "closed"
    ALREADY_CLOSED = "already_closed"
    CLOSE_UNCERTAIN = "close_uncertain"


@dataclass(frozen=True)
class AudioInputFormat:
    """Native PCM layout delivered by a backend callback."""

    sample_rate_hz: int
    channels: int
    callback_frame_samples: int
    sample_dtype: str = "int16_le"
    interleaving: str = "interleaved"

    @property
    def bytes_per_callback(self) -> int:
        """Maximum byte count for the configured fixed-size callback."""
        return self.callback_frame_samples * self.channels * 2


@dataclass(frozen=True)
class InputDeviceProfile:
    """Profile identity that must never flow across a stream epoch."""

    device_uid: str
    device_name: str
    backend: str
    input_format: AudioInputFormat
    device_index: int | None = None


@dataclass(frozen=True)
class BackendCapabilities:
    """Media capabilities observed for one backend implementation."""

    owns_default_input: bool
    owns_render_clock: bool
    aec: bool
    natural_barge_in: bool
    reliable_adc_time: bool
    reliable_dac_time: bool


@dataclass(frozen=True)
class InputClockMapping:
    """One input-cursor anchor into the monotonic process clock."""

    stream_epoch: int
    input_sample_cursor: int
    adc_time_s: float | None
    monotonic_ns: int
    input_clock_domain: str


@dataclass(frozen=True)
class BackendStartResult:
    """Typed start result; uncertain opens never authorize a fallback owner."""

    status: BackendStartStatus
    stream_epoch: int
    profile: InputDeviceProfile | None
    reason: str | None = None

    @property
    def started(self) -> bool:
        """Return whether this attempt definitively owns a running stream."""
        return self.status is BackendStartStatus.STARTED


@dataclass(frozen=True)
class BackendStopResult:
    """Typed stop result; only ``CLOSED`` releases the global owner claim."""

    status: BackendStopStatus
    stream_epoch: int
    reason: str | None = None
    helper_thread_alive: bool = False

    @property
    def definitively_closed(self) -> bool:
        """Return whether callers may safely consider the device unowned."""
        return self.status in {
            BackendStopStatus.CLOSED,
            BackendStopStatus.ALREADY_CLOSED,
        }


@dataclass(frozen=True)
class BackendFault:
    """One physical backend fault observed outside the ADC callback."""

    stream_epoch: int
    code: str
    detail: str
    recoverable: bool


class RenderSource(Protocol):
    """Future paired-backend render seam; unused by SoundDevice in Wave 3."""

    def render_into(self, output_buffer: Any, frame_count: int) -> int:  # noqa: ANN401
        """Fill a backend-owned output buffer and return real sample count."""
        ...


class InputFrameSink(Protocol):
    """Private callback-to-ingress copy boundary."""

    def __call__(  # noqa: PLR0913 - fixed backend callback metadata contract
        self,
        *,
        stream_epoch: int,
        callback_buffer: Any,  # noqa: ANN401
        frame_count: int,
        adc_time_s: float | None,
        captured_monotonic_ns: int,
        discontinuity_before: bool,
    ) -> None:
        """Copy one callback-owned buffer before the call returns."""
        ...


class AudioDuplexBackend(Protocol):
    """Replaceable owner for local input and, in later waves, render clocks."""

    def start(
        self,
        *,
        stream_epoch: int,
        frame_sink: InputFrameSink,
        render_source: RenderSource | None = None,
    ) -> BackendStartResult:
        """Open one epoch and start callbacks."""
        ...

    def stop(self, *, stream_epoch: int) -> BackendStopResult:
        """Stop exactly the requested epoch with a hard implementation bound."""
        ...

    def poll_fault(self, *, stream_epoch: int) -> BackendFault | None:
        """Return a pending physical fault without blocking the ADC callback."""
        ...

    def current_device_uid(self) -> str | None:
        """Return the currently selected default-input identity."""
        ...

    def input_format(self) -> AudioInputFormat:
        """Return the configured native input layout."""
        ...

    def output_format(self) -> None:
        """Wave-3 SoundDevice output remains owned by the Wave-2 player."""
        ...

    def capabilities(self) -> BackendCapabilities:
        """Return implementation capabilities, independent of route policy."""
        ...


class _DefaultInputOwnerRegistry:
    """Process-wide assertion that the default microphone has one owner."""

    _lock = threading.Lock()
    _owner_token: object | None = None

    @classmethod
    def claim(cls, token: object) -> bool:
        with cls._lock:
            if cls._owner_token is not None and cls._owner_token is not token:
                return False
            cls._owner_token = token
            return True

    @classmethod
    def release(cls, token: object) -> None:
        with cls._lock:
            if cls._owner_token is token:
                cls._owner_token = None


def _default_input_device_profile(input_format: AudioInputFormat) -> InputDeviceProfile:
    """Resolve sounddevice's default input outside the realtime callback."""
    import sounddevice as sd  # noqa: PLC0415

    default_device = sd.default.device
    try:
        # sounddevice 0.5.x exposes a private ``_InputOutputPair``: it is
        # indexable but intentionally not a tuple/list subclass.
        input_index = int(default_device[0])
    except (IndexError, TypeError):
        input_index = int(default_device)
    raw = sd.query_devices(input_index, "input")
    name = str(raw.get("name", f"input-{input_index}"))
    return InputDeviceProfile(
        device_uid=f"sounddevice:{input_index}:{name}",
        device_name=name,
        backend="sounddevice",
        input_format=input_format,
        device_index=input_index,
    )


def _foreign_error_reason(error: BaseException) -> str:
    """Return bounded operator detail for a typed foreign-call outcome."""
    detail = str(error).strip().replace("\n", " ")
    return f"{type(error).__name__}:{detail[:240]}" if detail else type(error).__name__


def _assert_default_profile_unchanged(
    initial: InputDeviceProfile,
    current: InputDeviceProfile,
) -> None:
    """Reject a route switch between default resolution and stream start."""
    if current.device_uid != initial.device_uid:
        msg = (
            "default input changed during open: "
            f"{initial.device_uid}->{current.device_uid}"
        )
        raise RuntimeError(msg)


def _open_sounddevice_input_stream(
    *,
    input_format: AudioInputFormat,
    device_index: int | None,
    callback: Callable[..., None],
    finished_callback: Callable[[], None],
) -> Any:  # noqa: ANN401
    """Construct the sole production ``RawInputStream`` callback path."""
    import sounddevice as sd  # noqa: PLC0415

    return sd.RawInputStream(
        device=device_index,
        samplerate=input_format.sample_rate_hz,
        channels=input_format.channels,
        dtype="int16",
        blocksize=input_format.callback_frame_samples,
        callback=callback,
        finished_callback=finished_callback,
    )


@dataclass
class _StartAttempt:
    """Cross-thread result box for one bounded PortAudio open."""

    done: threading.Event
    cancelled: threading.Event
    handoff_lock: threading.Lock
    stream: Any | None = None
    profile: InputDeviceProfile | None = None
    error: BaseException | None = None
    state_uncertain: bool = False


@dataclass
class _StopAttempt:
    """Cross-thread result box for one bounded PortAudio close."""

    done: threading.Event
    error: BaseException | None = None


class SoundDeviceDuplexBackend:
    """One callback-driven PortAudio input owner with typed bounded lifecycle.

    PortAudio open/close are foreign calls and cannot be force-cancelled safely
    from Python.  They run in named daemon helpers solely to impose caller
    bounds. A timed-out operation is reported as ``*_UNCERTAIN`` and retains
    the process-wide owner claim until a late helper proves the stream closed,
    so the daemon cannot open a second input and pretend the first one closed.
    A late successful open observes its cancel flag and immediately closes the
    stream instead of publishing callbacks.
    """

    def __init__(
        self,
        *,
        input_format: AudioInputFormat,
        open_timeout_s: float,
        close_timeout_s: float,
    ) -> None:
        """Configure one backend; no hardware is opened until ``start``."""
        if open_timeout_s <= 0 or close_timeout_s <= 0:
            msg = "sounddevice backend timeouts must be positive"
            raise ValueError(msg)
        self._input_format = input_format
        self._open_timeout_s = open_timeout_s
        self._close_timeout_s = close_timeout_s
        self._owner_token = object()
        self._lock = threading.Lock()
        self._stream: Any | None = None
        self._profile: InputDeviceProfile | None = None
        self._active_epoch: int | None = None
        self._state_uncertain = False
        self._callback_fault_epoch: int | None = None
        self._callback_fault_code: str | None = None
        self._finished_epoch: int | None = None
        self._callback_count = 0
        self._callback_deadline_misses = 0

    def input_format(self) -> AudioInputFormat:
        """Return the fixed native callback format."""
        return self._input_format

    def output_format(self) -> None:
        """Return no owned output format in Wave 3."""

    def capabilities(self) -> BackendCapabilities:
        """SoundDevice v1 has ADC timing but no shared render/AEC clock."""
        return BackendCapabilities(
            owns_default_input=True,
            owns_render_clock=False,
            aec=False,
            natural_barge_in=False,
            reliable_adc_time=True,
            reliable_dac_time=False,
        )

    def start(  # noqa: C901, PLR0911, PLR0915 - one linear bounded foreign-open state machine
        self,
        *,
        stream_epoch: int,
        frame_sink: InputFrameSink,
        render_source: RenderSource | None = None,
    ) -> BackendStartResult:
        """Open and start the sole default-input callback stream."""
        if render_source is not None:
            return BackendStartResult(
                status=BackendStartStatus.FAILED_CLOSED,
                stream_epoch=stream_epoch,
                profile=None,
                reason="sounddevice_wave3_render_source_not_owned",
            )
        with self._lock:
            if self._state_uncertain:
                return BackendStartResult(
                    status=BackendStartStatus.OPEN_UNCERTAIN,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason="prior_device_state_uncertain",
                )
            if self._stream is not None or not _DefaultInputOwnerRegistry.claim(
                self._owner_token,
            ):
                return BackendStartResult(
                    status=BackendStartStatus.OWNER_BUSY,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason="default_input_already_owned",
                )

        record_realtime_trace(
            "audio_input_stream_open_started",
            stream_epoch=stream_epoch,
            backend="sounddevice",
            measurement_boundary="software_owner_control",
        )
        attempt = _StartAttempt(
            done=threading.Event(),
            cancelled=threading.Event(),
            handoff_lock=threading.Lock(),
        )
        expected_callback_ns = int(
            self._input_format.callback_frame_samples
            / self._input_format.sample_rate_hz
            * 1_000_000_000
        )

        def _callback(
            callback_buffer: Any,  # noqa: ANN401
            frame_count: int,
            time_info: Any,  # noqa: ANN401
            status: Any,  # noqa: ANN401
        ) -> None:
            # ADC realtime boundary: scalar bookkeeping + one private copy
            # sink only.  No logger/trace/database/event/await/user callback.
            callback_started_ns = time.monotonic_ns()
            self._callback_count += 1
            try:
                raw_adc_time = getattr(time_info, "inputBufferAdcTime", None)
                adc_time_s = float(raw_adc_time) if raw_adc_time is not None else None
                discontinuity = bool(status and getattr(status, "input_overflow", False))
                frame_sink(
                    stream_epoch=stream_epoch,
                    callback_buffer=callback_buffer,
                    frame_count=frame_count,
                    adc_time_s=adc_time_s,
                    captured_monotonic_ns=callback_started_ns,
                    discontinuity_before=discontinuity,
                )
            except Exception:  # noqa: BLE001 - callback must fail closed without logging
                self._callback_fault_epoch = stream_epoch
                self._callback_fault_code = "callback_sink_error"
            if time.monotonic_ns() - callback_started_ns > expected_callback_ns:
                self._callback_deadline_misses += 1

        def _finished_callback() -> None:
            # PortAudio thread: publish a scalar for poll_fault(); no logging.
            self._finished_epoch = stream_epoch

        def _open() -> None:
            stream: Any | None = None
            result_published = False
            try:
                profile = _default_input_device_profile(self._input_format)
                stream = _open_sounddevice_input_stream(
                    input_format=self._input_format,
                    device_index=profile.device_index,
                    callback=_callback,
                    finished_callback=_finished_callback,
                )
                # RawInputStream is pinned to the resolved index. Re-read the
                # default before callbacks begin so an open-time route switch
                # cannot attach the old device to the new device's profile.
                current_profile = _default_input_device_profile(self._input_format)
                _assert_default_profile_unchanged(profile, current_profile)
                stream.start()
                with attempt.handoff_lock:
                    cancelled = attempt.cancelled.is_set()
                    if not cancelled:
                        attempt.stream = stream
                        attempt.profile = profile
                        # Result publication and completion share the handoff
                        # lock with the timeout path. A boundary-time success
                        # therefore cannot strand an open stream in the box.
                        attempt.done.set()
                        result_published = True
                if cancelled:
                    try:
                        stream.abort()
                    finally:
                        stream.close()
                    _DefaultInputOwnerRegistry.release(self._owner_token)
            except Exception as exc:  # noqa: BLE001 - foreign open surfaced as typed failure
                attempt.error = exc
                if stream is None:
                    _DefaultInputOwnerRegistry.release(self._owner_token)
                else:
                    try:
                        stream.abort()
                        stream.close()
                    except Exception as close_exc:  # noqa: BLE001 - cleanup ownership boundary
                        attempt.error = close_exc
                        attempt.state_uncertain = True
                    else:
                        _DefaultInputOwnerRegistry.release(self._owner_token)
            finally:
                if not result_published:
                    with attempt.handoff_lock:
                        attempt.done.set()

        helper = threading.Thread(
            target=_open,
            name=f"jarvis-audio-input-open-e{stream_epoch}",
            daemon=True,
        )
        helper.start()
        timed_out = not attempt.done.wait(timeout=self._open_timeout_s)
        if timed_out:
            with attempt.handoff_lock:
                # The helper may have completed on the timeout boundary before
                # this lock was acquired. In that case consume its definitive
                # result instead of falsely returning OPEN_UNCERTAIN.
                timed_out = not attempt.done.is_set()
                if timed_out:
                    attempt.cancelled.set()
        if timed_out:
            with self._lock:
                self._state_uncertain = True
            record_realtime_trace(
                "audio_input_stream_open_failed",
                stream_epoch=stream_epoch,
                backend="sounddevice",
                outcome="bounded_timeout_state_uncertain",
                helper_thread_alive=helper.is_alive(),
            )
            return BackendStartResult(
                status=BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=stream_epoch,
                profile=None,
                reason="open_timeout",
            )
        if attempt.state_uncertain:
            with self._lock:
                self._state_uncertain = True
            reason = (
                _foreign_error_reason(attempt.error)
                if attempt.error is not None
                else "open_cleanup_state_uncertain"
            )
            record_realtime_trace(
                "audio_input_stream_open_failed",
                stream_epoch=stream_epoch,
                backend="sounddevice",
                outcome="cleanup_state_uncertain",
                reason=reason,
            )
            return BackendStartResult(
                status=BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=stream_epoch,
                profile=None,
                reason=reason,
            )
        if attempt.error is not None or attempt.stream is None or attempt.profile is None:
            reason = (
                _foreign_error_reason(attempt.error)
                if attempt.error is not None
                else "stream_missing_after_open"
            )
            record_realtime_trace(
                "audio_input_stream_open_failed",
                stream_epoch=stream_epoch,
                backend="sounddevice",
                outcome="failed_closed",
                reason=reason,
            )
            return BackendStartResult(
                status=BackendStartStatus.FAILED_CLOSED,
                stream_epoch=stream_epoch,
                profile=None,
                reason=reason,
            )
        with self._lock:
            self._stream = attempt.stream
            self._profile = attempt.profile
            self._active_epoch = stream_epoch
            self._finished_epoch = None
            self._callback_fault_epoch = None
            self._callback_fault_code = None
        record_realtime_trace(
            "audio_input_stream_opened",
            stream_epoch=stream_epoch,
            backend="sounddevice",
            device_uid=attempt.profile.device_uid,
            sample_rate_hz=self._input_format.sample_rate_hz,
            channels=self._input_format.channels,
            callback_frame_samples=self._input_format.callback_frame_samples,
            measurement_boundary="portaudio_stream_started_not_first_callback",
        )
        return BackendStartResult(
            status=BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=attempt.profile,
        )

    def stop(self, *, stream_epoch: int) -> BackendStopResult:
        """Abort and close one epoch without ever claiming an uncertain close."""
        with self._lock:
            stream = self._stream
            active_epoch = self._active_epoch
            if stream is None and self._state_uncertain:
                return BackendStopResult(
                    status=BackendStopStatus.CLOSE_UNCERTAIN,
                    stream_epoch=stream_epoch,
                    reason="prior_foreign_call_state_uncertain",
                    helper_thread_alive=True,
                )
            if stream is None or active_epoch != stream_epoch:
                return BackendStopResult(
                    status=BackendStopStatus.ALREADY_CLOSED,
                    stream_epoch=stream_epoch,
                    reason="epoch_not_active",
                )
            # Reject a second stop while the foreign close is in flight.
            self._stream = None
            self._active_epoch = None
        attempt = _StopAttempt(done=threading.Event())

        def _close() -> None:
            try:
                stream.abort()
                stream.close()
                # Release only after the foreign close actually returns. This
                # also prevents a timeout-boundary success from permanently
                # poisoning the process-wide owner registry.
                _DefaultInputOwnerRegistry.release(self._owner_token)
            except Exception as exc:  # noqa: BLE001 - foreign close surfaced as typed result
                attempt.error = exc
            finally:
                attempt.done.set()

        helper = threading.Thread(
            target=_close,
            name=f"jarvis-audio-input-close-e{stream_epoch}",
            daemon=True,
        )
        helper.start()
        if not attempt.done.wait(timeout=self._close_timeout_s):
            with self._lock:
                self._state_uncertain = True
            result = BackendStopResult(
                status=BackendStopStatus.CLOSE_UNCERTAIN,
                stream_epoch=stream_epoch,
                reason="close_timeout",
                helper_thread_alive=helper.is_alive(),
            )
        elif attempt.error is not None:
            with self._lock:
                self._state_uncertain = True
            result = BackendStopResult(
                status=BackendStopStatus.CLOSE_UNCERTAIN,
                stream_epoch=stream_epoch,
                reason=_foreign_error_reason(attempt.error),
                helper_thread_alive=helper.is_alive(),
            )
        else:
            result = BackendStopResult(
                status=BackendStopStatus.CLOSED,
                stream_epoch=stream_epoch,
            )
        record_realtime_trace(
            "audio_input_stream_closed",
            stream_epoch=stream_epoch,
            backend="sounddevice",
            outcome=result.status.value,
            reason=result.reason,
            helper_thread_alive=result.helper_thread_alive,
        )
        return result

    def poll_fault(self, *, stream_epoch: int) -> BackendFault | None:
        """Surface callback, finished-stream, and inactive-stream faults."""
        with self._lock:
            if self._callback_fault_epoch == stream_epoch:
                code = self._callback_fault_code or "callback_error"
                self._callback_fault_epoch = None
                self._callback_fault_code = None
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code=code,
                    detail=code,
                    recoverable=True,
                )
            if self._finished_epoch == stream_epoch:
                self._finished_epoch = None
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_finished",
                    detail="PortAudio finished callback fired",
                    recoverable=True,
                )
            stream = self._stream
            if self._active_epoch != stream_epoch or stream is None:
                return None
            try:
                active = bool(stream.active)
            except Exception as exc:  # noqa: BLE001 - foreign stream property boundary
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_state_error",
                    detail=_foreign_error_reason(exc),
                    recoverable=True,
                )
            if not active:
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_inactive",
                    detail="PortAudio stream reports inactive",
                    recoverable=True,
                )
        return None

    def current_device_uid(self) -> str | None:
        """Resolve the current default input for route-change detection."""
        try:
            return _default_input_device_profile(self._input_format).device_uid
        except Exception:  # noqa: BLE001 - default-device provider boundary
            return None

    @property
    def callback_count(self) -> int:
        """Return lifetime ADC callback calls for bounded live-smoke reporting."""
        return self._callback_count

    @property
    def callback_deadline_misses(self) -> int:
        """Return callbacks whose software copy exceeded one audio period."""
        return self._callback_deadline_misses


__all__ = [
    "AudioDuplexBackend",
    "AudioInputFormat",
    "BackendCapabilities",
    "BackendFault",
    "BackendStartResult",
    "BackendStartStatus",
    "BackendStopResult",
    "BackendStopStatus",
    "InputClockMapping",
    "InputDeviceProfile",
    "InputFrameSink",
    "RenderSource",
    "SoundDeviceDuplexBackend",
]
