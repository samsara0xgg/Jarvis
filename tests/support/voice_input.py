"""Fakes for the voice input path: backend, wake engine, Silero, pipeline, ingress."""

from __future__ import annotations

import time
import wave
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from jarvis.surface import voice_audio, voice_backend

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def wait_until_true(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.001)


def pcm_frame(value: int, *, frames: int = voice_audio.SILERO_CHUNK_SAMPLES) -> bytes:
    return np.full(frames, value, dtype="<i2").tobytes()


class FakeBackend:
    """Callback-capable backend with exact owner/fault/epoch accounting."""

    def __init__(
        self,
        *,
        stop_status: voice_backend.BackendStopStatus = voice_backend.BackendStopStatus.CLOSED,
    ) -> None:
        self.format = voice_backend.AudioInputFormat(16_000, 1, 512)
        self.stop_status = stop_status
        self.sinks: dict[int, voice_backend.InputFrameSink] = {}
        self.attempts: dict[int, str] = {}
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.device_uid = "fake-input-A"
        self.pending_fault: voice_backend.BackendFault | None = None
        self.start_count = 0
        self.stop_count = 0
        self.active_owner_count = 0
        self.max_active_owner_count = 0
        self.ownership_version = 0
        self._failing_starts: list[voice_backend.BackendStartStatus] = []

    def fail_starts(self, *statuses: voice_backend.BackendStartStatus) -> None:
        """Make the next starts end in these statuses, in order, then open for real.

        ``OPEN_UNCERTAIN`` is a foreign open that may own the device: the owner stays
        claimed until ``stop`` (which ``stop_status`` decides); the others claim nothing.
        """
        self._failing_starts.extend(statuses)

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: voice_backend.InputFrameSink,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStartResult:
        del timeout_s
        failing = self._failing_starts.pop(0) if self._failing_starts else None
        if failing is not None and failing is not voice_backend.BackendStartStatus.OPEN_UNCERTAIN:
            self.start_count += 1
            return voice_backend.BackendStartResult(
                status=failing,
                stream_epoch=stream_epoch,
                profile=None,
                reason=f"injected_{failing.value}",
                attempt_id=attempt_id,
            )
        assert self.active_epoch is None, "a second physical owner was opened"
        self.start_count += 1
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.ownership_version += 1
        self.active_owner_count += 1
        self.max_active_owner_count = max(
            self.max_active_owner_count,
            self.active_owner_count,
        )
        self.sinks[stream_epoch] = frame_sink
        self.attempts[stream_epoch] = attempt_id
        profile = voice_backend.InputDeviceProfile(
            device_uid=self.device_uid,
            device_name=self.device_uid,
            backend="fake",
            input_format=self.format,
        )
        if failing is not None:
            return voice_backend.BackendStartResult(
                status=failing,
                stream_epoch=stream_epoch,
                profile=None,
                reason=f"injected_{failing.value}",
                attempt_id=attempt_id,
            )
        return voice_backend.BackendStartResult(
            status=voice_backend.BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=profile,
            attempt_id=attempt_id,
        )

    def stop(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStopResult:
        del timeout_s
        self.stop_count += 1
        if (
            self.active_epoch == stream_epoch
            and self.active_attempt_id == attempt_id
            and self.stop_status is voice_backend.BackendStopStatus.CLOSED
        ):
            self.active_epoch = None
            self.active_attempt_id = None
            self.active_owner_count -= 1
            self.ownership_version += 1
        return voice_backend.BackendStopResult(
            status=self.stop_status,
            stream_epoch=stream_epoch,
            reason=(
                "injected_stuck_backend"
                if self.stop_status is voice_backend.BackendStopStatus.CLOSE_UNCERTAIN
                else None
            ),
            attempt_id=attempt_id,
        )

    def emit(
        self,
        *,
        epoch: int,
        value: int,
        discontinuity: bool = False,
    ) -> None:
        callback_owned = bytearray(pcm_frame(value))
        self.sinks[epoch](
            stream_epoch=epoch,
            attempt_id=self.attempts[epoch],
            callback_buffer=callback_owned,
            frame_count=512,
            adc_time_s=time.monotonic(),
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=discontinuity,
        )
        # If ingress retained a borrowed callback buffer, every subscriber
        # would now observe this mutation instead of ``value``.
        callback_owned[:] = b"\x7f" * len(callback_owned)

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        fault = self.pending_fault
        if fault is not None and fault.stream_epoch == stream_epoch:
            self.pending_fault = None
            return fault
        return None

    def current_device_uid(self) -> str | None:
        return self.device_uid

    def current_output_route(self) -> voice_backend.OutputRoute | None:
        return None

    def input_format(self) -> voice_backend.AudioInputFormat:
        return self.format

    def ownership_snapshot(self) -> voice_backend.BackendOwnershipSnapshot:
        state = (
            voice_backend.BackendLifecycleState.CLOSED
            if self.active_epoch is None
            else voice_backend.BackendLifecycleState.OPEN
            if self.stop_status is voice_backend.BackendStopStatus.CLOSED
            else voice_backend.BackendLifecycleState.UNCERTAIN
        )
        return voice_backend.BackendOwnershipSnapshot(
            state=state,
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.ownership_version,
            physical_owner_possible=self.active_epoch is not None,
            helper_thread_alive=False,
            reason=(
                "injected_stuck_backend"
                if self.active_epoch is not None
                and state is voice_backend.BackendLifecycleState.UNCERTAIN
                else None
            ),
        )


class EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy; state passes through."""

    def __init__(self) -> None:
        self.run_count = 0

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.run_count += 1
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


class StatefulEnergySession(EnergySession):
    """``EnergySession`` that moves the recurrent state each call and keeps what it was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.input_h_values: list[float] = []
        self.input_c_values: list[float] = []

    def run(
        self,
        _output_names: object,
        inputs: dict[str, np.ndarray],
    ) -> list[np.ndarray]:
        self.input_h_values.append(float(inputs["h"].flat[0]))
        self.input_c_values.append(float(inputs["c"].flat[0]))
        probability, h, c = super().run(_output_names, inputs)
        return [probability, h + 1.0, c + 10.0]


class FakeWakeEngine:
    """Detect selected wake windows and retain reset/close proof."""

    model_name = "hey_jarvis_v0.1"

    def __init__(self, detections: set[int] | None = None) -> None:
        self._detections = detections
        self.calls = 0
        self.resets = 0
        self.closed = False

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        call = self.calls
        self.calls += 1
        detected = self._detections is None or call in self._detections
        return {self.model_name: 0.9 if detected else 0.0}

    def reset(self) -> None:
        self.resets += 1

    def close(self) -> None:
        self.closed = True


class RecordingPipeline:
    """Whole-WAV adapter fixture; never owns a hardware stream."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> Any:  # noqa: ANN401
        self.calls.append(dict(kwargs))
        return MagicMock()


def make_ingress(
    backend: FakeBackend,
    *,
    native_capacity: int = 64,
    subscriber_capacity: int = 16,
    wake_input_channel: int | None = None,
    echo_canceller: Any = None,  # noqa: ANN401
) -> voice_audio.AudioIngress:
    return voice_audio.AudioIngress(
        backend=backend,
        echo_canceller=echo_canceller,
        config=replace(
            voice_audio.AudioIngressConfig(),
            wake_input_channel=wake_input_channel,
            native_ring_capacity=native_capacity,
            default_subscriber_capacity=subscriber_capacity,
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            reopen_initial_backoff_s=0.001,
            reopen_max_backoff_s=0.002,
            shutdown_timeout_s=1.0,
        ),
    )


def write_wav(path: Path, pcm: np.ndarray, *, rate: int = 16_000, channels: int = 1) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.astype("<i2").tobytes())
    return path


class EnergySilero:
    """ONNX-shaped Silero stand-in: speech when the frame is louder than the room."""

    def run(self, _names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        rms = float(np.sqrt(np.mean(inputs["x"] ** 2)))
        probability = 0.9 if rms > 0.05 else 0.05  # between the room and a voice
        return [np.asarray([[probability]], dtype=np.float32), inputs["h"], inputs["c"]]
