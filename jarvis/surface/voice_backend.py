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

import ctypes
import ctypes.util
import enum
import functools
import logging
import struct
import threading
import time
import wave
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from jarvis.shared.realtime_trace import record_realtime_trace

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

LOGGER = logging.getLogger(__name__)


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
    STALE_ATTEMPT = "stale_attempt"


class BackendLifecycleState(enum.Enum):
    """Single physical-owner FSM state published by the backend ledger."""

    CLOSED = "closed"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    UNCERTAIN = "uncertain"


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


class RouteKind(enum.Enum):
    """ADR-0006 D9 output route class; ``hardware_aec`` is never produced here."""

    HEADPHONES = "headphones"
    SPEAKER = "speaker"
    HARDWARE_AEC = "hardware_aec"
    UNKNOWN = "unknown"


BargeMode = Literal["ptt", "keyword_two_stage", "natural"]


@dataclass(frozen=True)
class OutputRoute:
    """Default-output identity as observed from CoreAudio, never a name match."""

    uid: str
    name: str
    transport_type: str
    data_source: str | None
    sample_rate_hz: int | None


@dataclass(frozen=True)
class DeviceProfileKey:
    """ADR-0006 D9 exact profile identity; ``aec_mode`` is fixed to ``none``."""

    input_uid: str
    output_uid: str | None
    backend: str
    route_kind: RouteKind
    input_sample_rate: int
    output_sample_rate: int | None
    aec_mode: str = "none"

    def as_text(self) -> str:
        """Return the seven fields joined for traces."""
        return "|".join(
            str(part)
            for part in (
                self.input_uid,
                self.output_uid,
                self.backend,
                self.route_kind.value,
                self.aec_mode,
                self.input_sample_rate,
                self.output_sample_rate,
            )
        )


@dataclass(frozen=True)
class DeviceProfileSnapshot:
    """Profile resolved for one stream epoch with its allowed barge-in ceiling."""

    key: DeviceProfileKey
    stream_epoch: int
    allowed_barge_mode: BargeMode
    validation_record_hash: str | None = None


def route_kind_for(
    *,
    output_transport: str | None,
    output_data_source: str | None,
) -> RouteKind:
    """Classify the output route by CoreAudio transport; names are never evidence."""
    if output_transport != "bltn":
        # Bluetooth/USB/DisplayPort/virtual/aggregate: a USB DAC speaker and a
        # USB headset share one transport, so nothing external earns headphones.
        return RouteKind.UNKNOWN
    if output_data_source == "hdpn":
        return RouteKind.HEADPHONES
    return RouteKind.SPEAKER


def resolve_device_profile(
    *,
    input_profile: InputDeviceProfile,
    output: OutputRoute | None,
    stream_epoch: int,
    detection_mode: str,
    accepted_natural_profiles: Sequence[DeviceProfileKey],
) -> DeviceProfileSnapshot:
    """Resolve the exact profile key and its barge ceiling; natural needs a listing."""
    key = DeviceProfileKey(
        input_uid=input_profile.device_uid,
        output_uid=output.uid if output is not None else None,
        backend=input_profile.backend,
        route_kind=route_kind_for(
            output_transport=output.transport_type if output is not None else None,
            output_data_source=output.data_source if output is not None else None,
        ),
        input_sample_rate=input_profile.input_format.sample_rate_hz,
        output_sample_rate=output.sample_rate_hz if output is not None else None,
    )
    for accepted in accepted_natural_profiles:
        if accepted.route_kind is RouteKind.HEADPHONES and replace(
            accepted,
            route_kind=key.route_kind,
        ) == key:
            return DeviceProfileSnapshot(
                key=replace(key, route_kind=RouteKind.HEADPHONES),
                stream_epoch=stream_epoch,
                allowed_barge_mode="natural",
            )
    mode: BargeMode = "keyword_two_stage" if detection_mode == "keyword_two_stage" else "ptt"
    return DeviceProfileSnapshot(key=key, stream_epoch=stream_epoch, allowed_barge_mode=mode)


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
    attempt_id: str | None = None

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
    attempt_id: str | None = None

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
    attempt_id: str | None = None


@dataclass(frozen=True)
class BackendOwnershipSnapshot:
    """Monotonic physical ownership truth, including unresolved helper debt."""

    state: BackendLifecycleState
    stream_epoch: int | None
    attempt_id: str | None
    version: int
    physical_owner_possible: bool
    helper_thread_alive: bool
    reason: str | None = None


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
        attempt_id: str,
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
        attempt_id: str,
        frame_sink: InputFrameSink,
        render_source: RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> BackendStartResult:
        """Open one epoch and start callbacks."""
        ...

    def stop(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        timeout_s: float | None = None,
    ) -> BackendStopResult:
        """Stop exactly the requested epoch with a hard implementation bound."""
        ...

    def poll_fault(self, *, stream_epoch: int) -> BackendFault | None:
        """Return a pending physical fault without blocking the ADC callback."""
        ...

    def current_device_uid(self) -> str | None:
        """Return the currently selected default-input identity."""
        ...

    def current_output_route(self) -> OutputRoute | None:
        """Return the default-output identity, or ``None`` when unobservable."""
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

    def ownership_snapshot(self) -> BackendOwnershipSnapshot:
        """Return the backend's non-lossy physical ownership ledger."""
        ...


class _DefaultInputOwnerRegistry:
    """Process-wide assertion that the default microphone has one owner."""

    _lock = threading.Lock()
    _owner_token: object | None = None

    @classmethod
    def claim(cls, token: object) -> bool:
        with cls._lock:
            # Claims are deliberately non-reentrant, including for the same
            # token. Backend FSM state must prove CLOSED before any new open;
            # a lingering registry claim is ownership debt, never permission.
            if cls._owner_token is not None:
                return False
            cls._owner_token = token
            return True

    @classmethod
    def release(cls, token: object) -> None:
        with cls._lock:
            if cls._owner_token is token:
                cls._owner_token = None


_CA_SYSTEM_OBJECT = 1
_CA_GLOBAL_SCOPE = struct.unpack(">I", b"glob")[0]
_CA_OUTPUT_SCOPE = struct.unpack(">I", b"outp")[0]
_CF_UTF8 = 0x08000100


class _CoreAudioPropertyAddress(ctypes.Structure):
    _fields_ = (
        ("selector", ctypes.c_uint32),
        ("scope", ctypes.c_uint32),
        ("element", ctypes.c_uint32),
    )


@functools.cache
def _coreaudio_libraries() -> tuple[Any, Any] | None:
    """Load CoreAudio/CoreFoundation once; ``None`` off macOS."""
    core_audio = ctypes.util.find_library("CoreAudio")
    core_foundation = ctypes.util.find_library("CoreFoundation")
    if core_audio is None or core_foundation is None:
        return None
    ca = ctypes.cdll.LoadLibrary(core_audio)
    cf = ctypes.cdll.LoadLibrary(core_foundation)
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(_CoreAudioPropertyAddress),
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    ]
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    return ca, cf


def _coreaudio_property(
    ca: Any,  # noqa: ANN401 - ctypes CDLL
    obj: int,
    selector: bytes,
    scope: int,
    out: Any,  # noqa: ANN401 - ctypes value
) -> bool:
    address = _CoreAudioPropertyAddress(struct.unpack(">I", selector)[0], scope, 0)
    size = ctypes.c_uint32(ctypes.sizeof(out))
    status = ca.AudioObjectGetPropertyData(
        obj,
        ctypes.byref(address),
        0,
        None,
        ctypes.byref(size),
        ctypes.byref(out),
    )
    return bool(status == 0)


def _coreaudio_string(ca: Any, cf: Any, obj: int, selector: bytes) -> str | None:  # noqa: ANN401
    ref = ctypes.c_void_p(0)
    if not _coreaudio_property(ca, obj, selector, _CA_GLOBAL_SCOPE, ref) or not ref.value:
        return None
    buffer = ctypes.create_string_buffer(512)
    try:
        if not cf.CFStringGetCString(ref, buffer, len(buffer), _CF_UTF8):
            return None
        return buffer.value.decode("utf-8", errors="replace")
    finally:
        cf.CFRelease(ref)


def _coreaudio_default_output_route() -> OutputRoute | None:
    """Read the default output's uid/name/transport/data-source/rate, or ``None``."""
    libraries = _coreaudio_libraries()
    if libraries is None:
        return None
    ca, cf = libraries
    device = ctypes.c_uint32(0)
    if not _coreaudio_property(ca, _CA_SYSTEM_OBJECT, b"dOut", _CA_GLOBAL_SCOPE, device):
        return None
    uid = _coreaudio_string(ca, cf, device.value, b"uid ")
    transport = ctypes.c_uint32(0)
    if uid is None or not _coreaudio_property(
        ca, device.value, b"tran", _CA_GLOBAL_SCOPE, transport
    ):
        return None
    source = ctypes.c_uint32(0)
    has_source = _coreaudio_property(ca, device.value, b"ssrc", _CA_OUTPUT_SCOPE, source)
    rate = ctypes.c_double(0.0)
    has_rate = _coreaudio_property(ca, device.value, b"nsrt", _CA_GLOBAL_SCOPE, rate)
    return OutputRoute(
        uid=uid,
        name=_coreaudio_string(ca, cf, device.value, b"lnam") or uid,
        transport_type=struct.pack(">I", transport.value).decode("latin-1"),
        data_source=(
            struct.pack(">I", source.value).decode("latin-1") if has_source else None
        ),
        sample_rate_hz=int(rate.value) if has_rate and rate.value > 0 else None,
    )


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

    stream_epoch: int
    attempt_id: str
    done: threading.Event
    helper: threading.Thread | None = None
    cancel_requested: bool = False
    stream: Any | None = None
    profile: InputDeviceProfile | None = None
    error: BaseException | None = None
    cleanup_error: BaseException | None = None


@dataclass
class _StopAttempt:
    """Cross-thread result box for one bounded PortAudio close."""

    stream_epoch: int
    attempt_id: str
    close_attempt_id: int
    done: threading.Event
    helper: threading.Thread | None = None
    error: BaseException | None = None
    abort_error: BaseException | None = None


@dataclass
class _RouteQueryAttempt:
    """Single bounded default-route query; late completion is reusable."""

    done: threading.Event
    helper: threading.Thread | None = None
    device_uid: str | None = None


class SoundDeviceDuplexBackend:
    """One callback-driven PortAudio input owner with a non-lossy attempt FSM.

    Foreign open/close calls cannot be cancelled safely. Each physical claim
    therefore remains in one stable ownership attempt until an exact helper
    proves it closed. Timeouts retain that debt as ``UNCERTAIN``; helper thread
    exit alone is never treated as proof that the device closed.
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
        self._state = BackendLifecycleState.CLOSED
        self._state_reason: str | None = None
        self._version = 0
        self._stream: Any | None = None
        self._profile: InputDeviceProfile | None = None
        self._stream_epoch: int | None = None
        self._attempt_id: str | None = None
        self._open_attempt: _StartAttempt | None = None
        self._close_attempt: _StopAttempt | None = None
        self._close_attempt_sequence = 0
        self._last_closed_epoch: int | None = None
        self._last_closed_attempt_id: str | None = None
        self._callback_fault: tuple[int, str, str] | None = None
        self._finished_attempt: tuple[int, str] | None = None
        self._route_query_attempt: _RouteQueryAttempt | None = None
        self._callback_count = 0
        self._callback_deadline_misses = 0

    def _transition_locked(
        self,
        state: BackendLifecycleState,
        *,
        reason: str | None = None,
    ) -> None:
        self._state = state
        self._state_reason = reason
        self._version += 1

    def _publish_closed_locked(self, *, attempt_id: str, stream_epoch: int) -> bool:
        """CAS one exact ownership attempt to CLOSED and release its claim."""
        if self._attempt_id != attempt_id or self._stream_epoch != stream_epoch:
            return False
        self._last_closed_epoch = stream_epoch
        self._last_closed_attempt_id = attempt_id
        self._stream = None
        self._profile = None
        self._stream_epoch = None
        self._attempt_id = None
        self._open_attempt = None
        self._close_attempt = None
        self._callback_fault = None
        self._finished_attempt = None
        self._transition_locked(BackendLifecycleState.CLOSED)
        _DefaultInputOwnerRegistry.release(self._owner_token)
        return True

    def ownership_snapshot(self) -> BackendOwnershipSnapshot:
        """Return current physical ownership truth without consuming it."""
        with self._lock:
            helper = (
                self._close_attempt.helper
                if self._close_attempt is not None
                else self._open_attempt.helper
                if self._open_attempt is not None
                else None
            )
            return BackendOwnershipSnapshot(
                state=self._state,
                stream_epoch=self._stream_epoch,
                attempt_id=self._attempt_id,
                version=self._version,
                physical_owner_possible=self._state is not BackendLifecycleState.CLOSED,
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                reason=self._state_reason,
            )

    def _open_commit_allowed_locked(self, attempt: _StartAttempt) -> bool:
        """Hide cross-thread state narrowing behind one exact CAS predicate."""
        return (
            self._open_attempt is attempt
            and self._state is BackendLifecycleState.OPENING
            and not attempt.cancel_requested
        )

    def current_output_route(self) -> OutputRoute | None:
        """Observe the default output through the L5 CoreAudio shim."""
        try:
            return _coreaudio_default_output_route()
        except Exception:  # noqa: BLE001 - foreign framework boundary fails to unknown
            LOGGER.debug("default output route query failed", exc_info=True)
            return None

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
        attempt_id: str,
        frame_sink: InputFrameSink,
        render_source: RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> BackendStartResult:
        """Open and start the sole default-input callback stream."""
        if render_source is not None:
            return BackendStartResult(
                status=BackendStartStatus.FAILED_CLOSED,
                stream_epoch=stream_epoch,
                profile=None,
                reason="sounddevice_wave3_render_source_not_owned",
                attempt_id=attempt_id,
            )
        with self._lock:
            if self._state is BackendLifecycleState.UNCERTAIN:
                return BackendStartResult(
                    status=BackendStartStatus.OPEN_UNCERTAIN,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason=self._state_reason or "prior_device_state_uncertain",
                    attempt_id=self._attempt_id,
                )
            if self._state is not BackendLifecycleState.CLOSED:
                return BackendStartResult(
                    status=BackendStartStatus.OWNER_BUSY,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason=f"backend_{self._state.value}",
                    attempt_id=self._attempt_id,
                )
            if not _DefaultInputOwnerRegistry.claim(self._owner_token):
                return BackendStartResult(
                    status=BackendStartStatus.OWNER_BUSY,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason="default_input_already_owned",
                    attempt_id=attempt_id,
                )
            attempt = _StartAttempt(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                done=threading.Event(),
            )
            self._stream_epoch = stream_epoch
            self._attempt_id = attempt_id
            self._open_attempt = attempt
            self._close_attempt = None
            self._stream = None
            self._profile = None
            self._transition_locked(BackendLifecycleState.OPENING)

        record_realtime_trace(
            "audio_input_stream_open_started",
            stream_epoch=stream_epoch,
            attempt_id=attempt_id,
            backend="sounddevice",
            measurement_boundary="software_owner_control",
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
            # ADC realtime boundary: bounded scalar bookkeeping and one
            # private copy sink. It performs small Python allocations; it
            # never waits, logs, performs I/O, awaits, or calls user code.
            callback_started_ns = time.monotonic_ns()
            self._callback_count += 1
            try:
                raw_adc_time = getattr(time_info, "inputBufferAdcTime", None)
                adc_time_s = float(raw_adc_time) if raw_adc_time is not None else None
                discontinuity = bool(status and getattr(status, "input_overflow", False))
                frame_sink(
                    stream_epoch=stream_epoch,
                    attempt_id=attempt_id,
                    callback_buffer=callback_buffer,
                    frame_count=frame_count,
                    adc_time_s=adc_time_s,
                    captured_monotonic_ns=callback_started_ns,
                    discontinuity_before=discontinuity,
                )
            except Exception:  # noqa: BLE001 - callback must fail closed without logging
                self._callback_fault = (
                    stream_epoch,
                    attempt_id,
                    "callback_sink_error",
                )
            if time.monotonic_ns() - callback_started_ns > expected_callback_ns:
                self._callback_deadline_misses += 1

        def _finished_callback() -> None:
            # PortAudio thread: publish a scalar for poll_fault(); no logging.
            self._finished_attempt = (stream_epoch, attempt_id)

        def _open() -> None:  # noqa: C901 - exact foreign-open cleanup FSM
            stream: Any | None = None
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
                with self._lock:
                    exact = (
                        self._attempt_id == attempt_id
                        and self._stream_epoch == stream_epoch
                        and self._open_attempt is attempt
                    )
                    if exact:
                        attempt.stream = stream
                        attempt.profile = profile
                        self._stream = stream
                        self._profile = profile
                    cancelled = not exact or attempt.cancel_requested
                if cancelled:
                    try:
                        stream.abort()
                        stream.close()
                    except Exception as close_exc:  # noqa: BLE001
                        attempt.cleanup_error = close_exc
                        with self._lock:
                            if exact:
                                self._transition_locked(
                                    BackendLifecycleState.UNCERTAIN,
                                    reason=_foreign_error_reason(close_exc),
                                )
                    else:
                        with self._lock:
                            self._publish_closed_locked(
                                attempt_id=attempt_id,
                                stream_epoch=stream_epoch,
                            )
            except Exception as exc:  # noqa: BLE001 - foreign open surfaced as typed failure
                attempt.error = exc
                if stream is not None:
                    try:
                        stream.abort()
                        stream.close()
                    except Exception as close_exc:  # noqa: BLE001 - cleanup ownership boundary
                        attempt.cleanup_error = close_exc
                with self._lock:
                    exact = (
                        self._attempt_id == attempt_id
                        and self._stream_epoch == stream_epoch
                        and self._open_attempt is attempt
                    )
                    if exact and attempt.cleanup_error is not None:
                        if stream is not None:
                            self._stream = stream
                        self._transition_locked(
                            BackendLifecycleState.UNCERTAIN,
                            reason=_foreign_error_reason(attempt.cleanup_error),
                        )
                    elif exact:
                        self._publish_closed_locked(
                            attempt_id=attempt_id,
                            stream_epoch=stream_epoch,
                        )
            finally:
                attempt.done.set()

        helper = threading.Thread(
            target=_open,
            name=f"jarvis-audio-input-open-e{stream_epoch}",
            daemon=True,
        )
        attempt.helper = helper
        try:
            helper.start()
        except RuntimeError as exc:
            attempt.error = exc
            with self._lock:
                self._publish_closed_locked(
                    attempt_id=attempt_id,
                    stream_epoch=stream_epoch,
                )
            attempt.done.set()
        open_timeout_s = (
            self._open_timeout_s
            if timeout_s is None
            else min(self._open_timeout_s, max(0.0, timeout_s))
        )
        timed_out = not attempt.done.wait(timeout=open_timeout_s)
        if timed_out:
            with self._lock:
                timed_out = not attempt.done.is_set()
                if timed_out and self._open_attempt is attempt:
                    attempt.cancel_requested = True
                    self._transition_locked(
                        BackendLifecycleState.UNCERTAIN,
                        reason="open_timeout",
                    )
        if timed_out:
            record_realtime_trace(
                "audio_input_stream_open_failed",
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                backend="sounddevice",
                outcome="bounded_timeout_state_uncertain",
                helper_thread_alive=helper.is_alive(),
            )
            return BackendStartResult(
                status=BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=stream_epoch,
                profile=None,
                reason="open_timeout",
                attempt_id=attempt_id,
            )
        if attempt.cleanup_error is not None:
            reason = (
                _foreign_error_reason(attempt.cleanup_error)
                if attempt.cleanup_error is not None
                else "open_cleanup_state_uncertain"
            )
            record_realtime_trace(
                "audio_input_stream_open_failed",
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                backend="sounddevice",
                outcome="cleanup_state_uncertain",
                reason=reason,
            )
            return BackendStartResult(
                status=BackendStartStatus.OPEN_UNCERTAIN,
                stream_epoch=stream_epoch,
                profile=None,
                reason=reason,
                attempt_id=attempt_id,
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
                attempt_id=attempt_id,
                backend="sounddevice",
                outcome="failed_closed",
                reason=reason,
            )
            return BackendStartResult(
                status=BackendStartStatus.FAILED_CLOSED,
                stream_epoch=stream_epoch,
                profile=None,
                reason=reason,
                attempt_id=attempt_id,
            )
        with self._lock:
            if not self._open_commit_allowed_locked(attempt):
                return BackendStartResult(
                    status=BackendStartStatus.OPEN_UNCERTAIN,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason=self._state_reason or "open_commit_revoked",
                    attempt_id=attempt_id,
                )
            self._finished_attempt = None
            self._callback_fault = None
            self._transition_locked(BackendLifecycleState.OPEN)
        record_realtime_trace(
            "audio_input_stream_opened",
            stream_epoch=stream_epoch,
            attempt_id=attempt_id,
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
            attempt_id=attempt_id,
        )

    def _close_result_from_snapshot(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        reason: str | None = None,
    ) -> BackendStopResult:
        with self._lock:
            helper = (
                self._close_attempt.helper
                if self._close_attempt is not None
                else self._open_attempt.helper
                if self._open_attempt is not None
                else None
            )
            if self._state is BackendLifecycleState.CLOSED:
                return BackendStopResult(
                    status=BackendStopStatus.CLOSED,
                    stream_epoch=stream_epoch,
                    reason=reason,
                    attempt_id=attempt_id,
                )
            return BackendStopResult(
                status=BackendStopStatus.CLOSE_UNCERTAIN,
                stream_epoch=self._stream_epoch or stream_epoch,
                reason=reason or self._state_reason or f"backend_{self._state.value}",
                helper_thread_alive=bool(helper is not None and helper.is_alive()),
                attempt_id=self._attempt_id or attempt_id,
            )

    def stop(  # noqa: C901, PLR0912, PLR0915 - exact bounded close/join FSM
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        timeout_s: float | None = None,
    ) -> BackendStopResult:
        """Close or join the exact physical debt; never infer from worker exit."""
        wait_event: threading.Event
        launch_close = False
        with self._lock:
            if self._state is BackendLifecycleState.CLOSED:
                return BackendStopResult(
                    status=BackendStopStatus.ALREADY_CLOSED,
                    stream_epoch=stream_epoch,
                    reason="backend_closed",
                    attempt_id=attempt_id,
                )
            actual_epoch = self._stream_epoch
            actual_attempt_id = self._attempt_id
            if actual_epoch is None or actual_attempt_id is None:
                return BackendStopResult(
                    status=BackendStopStatus.CLOSE_UNCERTAIN,
                    stream_epoch=stream_epoch,
                    reason="ownership_ledger_incomplete",
                    attempt_id=attempt_id,
                )
            if (actual_epoch, actual_attempt_id) != (stream_epoch, attempt_id):
                record_realtime_trace(
                    "audio_input_stream_close_ignored",
                    requested_stream_epoch=stream_epoch,
                    requested_attempt_id=attempt_id,
                    owned_stream_epoch=actual_epoch,
                    owned_attempt_id=actual_attempt_id,
                    reason="stale_attempt_identity",
                )
                return BackendStopResult(
                    status=BackendStopStatus.STALE_ATTEMPT,
                    stream_epoch=stream_epoch,
                    reason=(
                        f"requested_{stream_epoch}:{attempt_id}_does_not_match_"
                        f"owned_{actual_epoch}:{actual_attempt_id}"
                    ),
                    attempt_id=attempt_id,
                )
            if self._close_attempt is not None and not self._close_attempt.done.is_set():
                wait_event = self._close_attempt.done
                close_attempt = self._close_attempt
            elif self._state is BackendLifecycleState.OPENING or (
                self._state is BackendLifecycleState.UNCERTAIN
                and self._open_attempt is not None
                and not self._open_attempt.done.is_set()
            ):
                open_attempt = self._open_attempt
                if open_attempt is None:
                    return BackendStopResult(
                        status=BackendStopStatus.CLOSE_UNCERTAIN,
                        stream_epoch=actual_epoch,
                        reason="opening_attempt_ledger_missing",
                        attempt_id=actual_attempt_id,
                    )
                open_attempt.cancel_requested = True
                if self._state is BackendLifecycleState.OPENING:
                    self._transition_locked(
                        BackendLifecycleState.UNCERTAIN,
                        reason="close_requested_during_open",
                    )
                wait_event = open_attempt.done
                close_attempt = None
            else:
                stream = self._stream
                if stream is None:
                    return BackendStopResult(
                        status=BackendStopStatus.CLOSE_UNCERTAIN,
                        stream_epoch=actual_epoch,
                        reason=self._state_reason or "owned_stream_handle_missing",
                        attempt_id=actual_attempt_id,
                    )
                owned_stream = stream
                self._close_attempt_sequence += 1
                close_attempt = _StopAttempt(
                    stream_epoch=actual_epoch,
                    attempt_id=actual_attempt_id,
                    close_attempt_id=self._close_attempt_sequence,
                    done=threading.Event(),
                )
                self._close_attempt = close_attempt
                self._transition_locked(BackendLifecycleState.CLOSING)
                wait_event = close_attempt.done
                launch_close = True

        if close_attempt is not None and launch_close:
            def _close() -> None:
                try:
                    owned_stream.abort()
                except Exception as exc:  # noqa: BLE001
                    close_attempt.abort_error = exc
                    record_realtime_trace(
                        "audio_input_stream_abort_failed",
                        stream_epoch=close_attempt.stream_epoch,
                        attempt_id=close_attempt.attempt_id,
                        close_attempt_id=close_attempt.close_attempt_id,
                        reason=_foreign_error_reason(exc),
                        close_will_still_be_attempted=True,
                    )
                try:
                    owned_stream.close()
                except Exception as exc:  # noqa: BLE001
                    close_attempt.error = exc
                    with self._lock:
                        if self._close_attempt is close_attempt:
                            self._transition_locked(
                                BackendLifecycleState.UNCERTAIN,
                                reason=_foreign_error_reason(exc),
                            )
                else:
                    with self._lock:
                        self._publish_closed_locked(
                            attempt_id=close_attempt.attempt_id,
                            stream_epoch=close_attempt.stream_epoch,
                        )
                finally:
                    close_attempt.done.set()

            helper = threading.Thread(
                target=_close,
                name=(
                    f"jarvis-audio-input-close-e{close_attempt.stream_epoch}"
                    f"-a{close_attempt.close_attempt_id}"
                ),
                daemon=True,
            )
            close_attempt.helper = helper
            try:
                helper.start()
            except RuntimeError as exc:
                close_attempt.error = exc
                with self._lock:
                    if self._close_attempt is close_attempt:
                        self._transition_locked(
                            BackendLifecycleState.UNCERTAIN,
                            reason=_foreign_error_reason(exc),
                        )
                close_attempt.done.set()

        close_timeout_s = (
            self._close_timeout_s
            if timeout_s is None
            else min(self._close_timeout_s, max(0.0, timeout_s))
        )
        if not wait_event.wait(timeout=close_timeout_s):
            with self._lock:
                if self._state in {
                    BackendLifecycleState.OPENING,
                    BackendLifecycleState.CLOSING,
                }:
                    self._transition_locked(
                        BackendLifecycleState.UNCERTAIN,
                        reason="close_timeout",
                    )
            result = self._close_result_from_snapshot(
                stream_epoch=actual_epoch,
                attempt_id=actual_attempt_id,
                reason="close_timeout",
            )
        else:
            result = self._close_result_from_snapshot(
                stream_epoch=actual_epoch,
                attempt_id=actual_attempt_id,
            )
        record_realtime_trace(
            "audio_input_stream_closed",
            stream_epoch=result.stream_epoch,
            attempt_id=result.attempt_id,
            backend="sounddevice",
            outcome=result.status.value,
            reason=result.reason,
            helper_thread_alive=result.helper_thread_alive,
        )
        return result

    def poll_fault(self, *, stream_epoch: int) -> BackendFault | None:
        """Surface callback, finished-stream, and inactive-stream faults."""
        with self._lock:
            attempt_id = self._attempt_id
            if (
                self._callback_fault is not None
                and self._callback_fault[:2] == (stream_epoch, attempt_id)
            ):
                code = self._callback_fault[2]
                self._callback_fault = None
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code=code,
                    detail=code,
                    recoverable=True,
                    attempt_id=attempt_id,
                )
            if self._finished_attempt == (stream_epoch, attempt_id):
                self._finished_attempt = None
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_finished",
                    detail="PortAudio finished callback fired",
                    recoverable=True,
                    attempt_id=attempt_id,
                )
            stream = self._stream
            if (
                self._state is not BackendLifecycleState.OPEN
                or self._stream_epoch != stream_epoch
                or stream is None
            ):
                return None
            try:
                active = bool(stream.active)
            except Exception as exc:  # noqa: BLE001 - foreign stream property boundary
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_state_error",
                    detail=_foreign_error_reason(exc),
                    recoverable=True,
                    attempt_id=attempt_id,
                )
            if not active:
                return BackendFault(
                    stream_epoch=stream_epoch,
                    code="stream_inactive",
                    detail="PortAudio stream reports inactive",
                    recoverable=True,
                    attempt_id=attempt_id,
                )
        return None

    def current_device_uid(self) -> str | None:
        """Resolve default input through one bounded, non-accumulating helper."""
        with self._lock:
            attempt = self._route_query_attempt
            if attempt is not None and attempt.done.is_set():
                self._route_query_attempt = None
                return attempt.device_uid
            if attempt is None:
                attempt = _RouteQueryAttempt(done=threading.Event())
                self._route_query_attempt = attempt

                def _query() -> None:
                    try:
                        attempt.device_uid = _default_input_device_profile(
                            self._input_format,
                        ).device_uid
                    except Exception:  # noqa: BLE001 - provider boundary
                        attempt.device_uid = None
                    finally:
                        attempt.done.set()

                helper = threading.Thread(
                    target=_query,
                    name="jarvis-audio-input-route-query",
                    daemon=True,
                )
                attempt.helper = helper
                try:
                    helper.start()
                except RuntimeError:
                    self._route_query_attempt = None
                    return None
        if not attempt.done.wait(timeout=min(0.05, self._open_timeout_s)):
            return None
        with self._lock:
            if self._route_query_attempt is attempt:
                self._route_query_attempt = None
            return attempt.device_uid

    @property
    def callback_count(self) -> int:
        """Return lifetime ADC callback calls for bounded live-smoke reporting."""
        return self._callback_count

    @property
    def callback_deadline_misses(self) -> int:
        """Return callbacks whose software copy exceeded one audio period."""
        return self._callback_deadline_misses


_REPLAY_SAMPLE_RATE_HZ = 16_000


class FileReplayBackend:
    """Replay one 16 kHz mono PCM16 WAV through the real ingress at real-time pace.

    Seed of the ADR-0006 §10.3 Tier 2 corpus runner. It never touches the
    default microphone or its owner registry, and after EOF it keeps
    emitting silence like a still-open device until stopped (or until
    ``tail_silence_s`` elapses when given).
    """

    def __init__(
        self,
        path: Path,
        *,
        frame_samples: int = 512,
        tail_silence_s: float | None = None,
    ) -> None:
        """Validate the WAV layout now so a bad fixture fails before any start."""
        with wave.open(str(path), "rb") as reader:
            if reader.getframerate() != _REPLAY_SAMPLE_RATE_HZ:
                msg = f"replay WAV sample rate must be 16000 Hz: {reader.getframerate()}"
                raise ValueError(msg)
            if reader.getnchannels() != 1:
                msg = f"replay WAV must be mono: channels={reader.getnchannels()}"
                raise ValueError(msg)
            if reader.getsampwidth() != 2:  # noqa: PLR2004 - PCM16 byte width
                msg = f"replay WAV must be 16-bit PCM: sample width={reader.getsampwidth()}"
                raise ValueError(msg)
            self._pcm = reader.readframes(reader.getnframes())
        self._path = path
        self._frame_samples = frame_samples
        self._tail_silence_s = tail_silence_s
        self._format = AudioInputFormat(
            sample_rate_hz=_REPLAY_SAMPLE_RATE_HZ,
            channels=1,
            callback_frame_samples=frame_samples,
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._epoch: int | None = None
        self._attempt_id: str | None = None
        self._version = 0
        self.frames_emitted = 0
        self.eof_reached = False

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: InputFrameSink,
        render_source: RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> BackendStartResult:
        """Start the paced replay thread for one epoch."""
        del render_source, timeout_s
        with self._lock:
            if self._thread is not None:
                return BackendStartResult(
                    status=BackendStartStatus.OWNER_BUSY,
                    stream_epoch=stream_epoch,
                    profile=None,
                    reason="replay_already_running",
                    attempt_id=attempt_id,
                )
            self._epoch = stream_epoch
            self._attempt_id = attempt_id
            self._version += 1
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(stream_epoch, attempt_id, frame_sink),
                name="jarvis-file-replay",
                daemon=False,
            )
            self._thread.start()
        return BackendStartResult(
            status=BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=InputDeviceProfile(
                device_uid=f"file-replay:{self._path.name}",
                device_name=self._path.name,
                backend="file_replay",
                input_format=self._format,
            ),
            attempt_id=attempt_id,
        )

    def _run(self, stream_epoch: int, attempt_id: str, frame_sink: InputFrameSink) -> None:
        frame_bytes = self._frame_samples * 2
        period_s = self._frame_samples / _REPLAY_SAMPLE_RATE_HZ
        silence = bytes(frame_bytes)
        deadline = time.monotonic()
        offset = 0
        tail_deadline: float | None = None
        while not self._stop.is_set():
            if offset < len(self._pcm):
                frame = self._pcm[offset : offset + frame_bytes]
                offset += frame_bytes
                if len(frame) < frame_bytes:
                    frame = frame + bytes(frame_bytes - len(frame))
            else:
                if not self.eof_reached:
                    self.eof_reached = True
                    if self._tail_silence_s is not None:
                        tail_deadline = time.monotonic() + self._tail_silence_s
                if tail_deadline is not None and time.monotonic() >= tail_deadline:
                    return
                frame = silence
            deadline += period_s
            time.sleep(max(0.0, deadline - time.monotonic()))
            if self._stop.is_set():
                return
            frame_sink(
                stream_epoch=stream_epoch,
                attempt_id=attempt_id,
                callback_buffer=frame,
                frame_count=self._frame_samples,
                adc_time_s=None,
                captured_monotonic_ns=time.monotonic_ns(),
                discontinuity_before=False,
            )
            self.frames_emitted += 1

    def stop(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        timeout_s: float | None = None,
    ) -> BackendStopResult:
        """Stop exactly the running epoch and join the replay thread."""
        with self._lock:
            thread = self._thread
            if thread is None:
                return BackendStopResult(
                    status=BackendStopStatus.ALREADY_CLOSED,
                    stream_epoch=stream_epoch,
                    attempt_id=attempt_id,
                )
            if stream_epoch != self._epoch or attempt_id != self._attempt_id:
                return BackendStopResult(
                    status=BackendStopStatus.STALE_ATTEMPT,
                    stream_epoch=stream_epoch,
                    reason="epoch_or_attempt_mismatch",
                    attempt_id=attempt_id,
                )
            self._stop.set()
        thread.join(timeout=timeout_s)
        alive = thread.is_alive()
        with self._lock:
            if not alive:
                self._thread = None
                self._epoch = None
                self._attempt_id = None
                self._version += 1
        return BackendStopResult(
            status=(BackendStopStatus.CLOSE_UNCERTAIN if alive else BackendStopStatus.CLOSED),
            stream_epoch=stream_epoch,
            reason="replay_thread_join_timeout" if alive else None,
            helper_thread_alive=alive,
            attempt_id=attempt_id,
        )

    def poll_fault(self, *, stream_epoch: int) -> BackendFault | None:
        """A file never faults."""
        del stream_epoch
        return None

    def current_device_uid(self) -> str | None:
        """Identify the replayed file, never a physical device."""
        return f"file-replay:{self._path.name}"

    def current_output_route(self) -> OutputRoute | None:
        """Replay observes no output route, which resolves ``unknown``."""
        return None

    def input_format(self) -> AudioInputFormat:
        """Return the fixed canonical replay layout."""
        return self._format

    def output_format(self) -> None:
        """Replay owns no output."""
        return

    def capabilities(self) -> BackendCapabilities:
        """Replay owns nothing physical and has no reliable clocks."""
        return BackendCapabilities(
            owns_default_input=False,
            owns_render_clock=False,
            aec=False,
            natural_barge_in=False,
            reliable_adc_time=False,
            reliable_dac_time=False,
        )

    def ownership_snapshot(self) -> BackendOwnershipSnapshot:
        """Report the replay thread as the only owner that can exist."""
        with self._lock:
            thread = self._thread
            return BackendOwnershipSnapshot(
                state=(
                    BackendLifecycleState.OPEN
                    if thread is not None
                    else BackendLifecycleState.CLOSED
                ),
                stream_epoch=self._epoch,
                attempt_id=self._attempt_id,
                version=self._version,
                physical_owner_possible=False,
                helper_thread_alive=thread is not None and thread.is_alive(),
            )


__all__ = [
    "AudioDuplexBackend",
    "AudioInputFormat",
    "BackendCapabilities",
    "BackendFault",
    "BackendLifecycleState",
    "BackendOwnershipSnapshot",
    "BackendStartResult",
    "BackendStartStatus",
    "BackendStopResult",
    "BackendStopStatus",
    "BargeMode",
    "DeviceProfileKey",
    "DeviceProfileSnapshot",
    "FileReplayBackend",
    "InputClockMapping",
    "InputDeviceProfile",
    "InputFrameSink",
    "OutputRoute",
    "RenderSource",
    "RouteKind",
    "SoundDeviceDuplexBackend",
    "resolve_device_profile",
    "route_kind_for",
]
