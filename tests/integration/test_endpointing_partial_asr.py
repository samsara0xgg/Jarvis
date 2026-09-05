"""ADR-0006 D7 scripted-partial harness: stable prefix, hold, bounds, degrade.

Every case drives ``UtteranceAssembler`` frame by frame with a scripted
partial decoder and runs the ``PartialAsrLane`` synchronously, so hold and
bound arithmetic is checked in audio time (32 ms frames) with no sleeps.
Only the late-revision case starts the real ``DuplexVoiceSession`` threads.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import numpy as np
import pytest

from jarvis.shared.realtime_trace import (
    RealtimeTracePoint,
    realtime_trace_snapshot,
    reset_realtime_trace,
)
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_backend, voice_pipeline, voice_session

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_FRAME = voice_audio.SILERO_CHUNK_SAMPLES
_SPEECH = 10_000
_SILENCE = 0


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition did not become true before bounded deadline")
        time.sleep(0.001)


def _pcm(value: int) -> bytes:
    return np.full(_FRAME, value, dtype="<i2").tobytes()


def _frame(index: int, value: int) -> voice_audio.CanonicalAudioFrame:
    return voice_audio.CanonicalAudioFrame(
        stream_epoch=1,
        sequence=index,
        sample_cursor=index * _FRAME,
        sample_rate_hz=16_000,
        frame_count=_FRAME,
        adc_time_s=None,
        captured_monotonic_ns=index,
        discontinuity_before=False,
        pcm16_mono=_pcm(value),
    )


class _EnergySession:
    """ONNX-shaped Silero fixture whose probability follows sample energy."""

    def run(self, _output_names: object, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
        probability = 0.9 if np.any(inputs["x"]) else 0.0
        return [
            np.asarray([[probability]], dtype=np.float32),
            inputs["h"].copy(),
            inputs["c"].copy(),
        ]


class _ScriptedDecoder:
    """Return scripted partial hypotheses in order; the last one repeats."""

    def __init__(self, texts: list[str], *, delay_s: float = 0.0) -> None:
        self._texts = list(texts)
        self._delay_s = delay_s
        self.calls = 0

    def partial_text(self, audio_bytes: bytes) -> str:
        del audio_bytes
        if self._delay_s:
            time.sleep(self._delay_s)
        index = min(self.calls, len(self._texts) - 1)
        self.calls += 1
        return self._texts[index]


def _partial(**overrides: Any) -> voice_session.PartialAsrConfig:  # noqa: ANN401
    values: dict[str, Any] = {
        "enabled": True,
        "interval_ms": 32,
        "candidate_ms": 64,
        "max_hold_ms": 160,
        "post_roll_ms": 32,
    }
    values.update(overrides)
    return voice_session.PartialAsrConfig(**values)


class _Harness:
    """One armed assembler around a synchronous partial lane."""

    def __init__(
        self,
        decoder: _ScriptedDecoder,
        *,
        partial: voice_session.PartialAsrConfig,
        required_misses: int = 10,
    ) -> None:
        reset_realtime_trace()
        self._patch = patch.object(
            voice_audio,
            "_load_silero_session",
            return_value=_EnergySession(),
        )
        self._patch.start()
        vad = voice_audio.SileroVad(mode="record")
        vad._t = voice_audio.VadThresholds(0.4, -45.0, 1, 1, required_misses)  # noqa: SLF001
        self.lane = voice_session.PartialAsrLane(decoder)
        self.assembler = voice_session.UtteranceAssembler(
            vad=vad,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                min_voiced_s=0.032,
                partial_asr=partial,
            ),
            sample_rate_hz=16_000,
            frame_samples=_FRAME,
            session_id="S-partial",
            lane=self.lane,
        )
        self.assembler.prepare()
        self.assembler.arm(voice_session.WakeDetection(1, 0, 0, 0.9))
        self.index = 0

    def close(self) -> None:
        self._patch.stop()

    def feed(self, value: int, *, decode: bool = True) -> voice_session.CapturedUtterance | None:
        outcome = self.assembler.feed(_frame(self.index, value))
        self.index += 1
        if decode:
            self.lane.run_once(timeout_s=0)
        assert not isinstance(
            outcome,
            voice_session.UtteranceCaptureFailure | voice_session.WakeArmExpired,
        )
        return outcome

    def feed_many(self, values: list[int]) -> list[voice_session.CapturedUtterance | None]:
        return [self.feed(value) for value in values]


def _traces(name: str) -> list[RealtimeTracePoint]:
    return [point for point in realtime_trace_snapshot() if point.name == name]


def _decisions() -> list[tuple[object, object, object]]:
    return [
        (p.attributes["verdict"], p.attributes["reason"], p.attributes["held_ms"])
        for p in _traces("endpoint_decision")
    ]


def test_stable_prefix_advances_only_after_two_identical_revisions() -> None:
    """The stable prefix is the code-point prefix shared by consecutive revisions."""
    harness = _Harness(
        _ScriptedDecoder(["今天天气", "今天天气不错", "今天天气很好", "今天天气很好。"]),
        partial=_partial(),
    )
    try:
        harness.feed(_SPEECH)
        observed = []
        for _ in range(4):
            assert harness.feed(_SPEECH) is None
            observed.append(harness.assembler.stable_prefix)
    finally:
        harness.close()
    assert observed == ["", "今天天气", "今天天气", "今天天气很好"]
    assert [p.attributes["stable_prefix_len"] for p in _traces("asr_partial")] == [0, 4, 4, 6]
    assert [p.attributes["revision"] for p in _traces("asr_partial")] == [1, 2, 3, 4]
    assert harness.assembler.endpoint_phase is voice_session.EndpointPhase.SPEECH_ACTIVE


def test_complete_stable_prefix_commits_before_max_hold() -> None:
    """A terminal-punctuated stable prefix finalizes on the hold-open frame."""
    harness = _Harness(_ScriptedDecoder(["把灯打开。"]), partial=_partial())
    try:
        outcomes = harness.feed_many([_SPEECH, _SPEECH, _SPEECH, _SILENCE, _SILENCE])
    finally:
        harness.close()
    utterance = outcomes[-1]
    assert outcomes[:-1] == [None] * 4
    assert isinstance(utterance, voice_session.CapturedUtterance)
    assert utterance.endpoint_reason == "stable_prefix_complete"
    assert _decisions() == [
        ("hold", "acoustic_pause_candidate", 0.0),
        ("commit", "stable_prefix_complete", 0.0),
    ]
    assert float(str(_decisions()[-1][2])) < 160
    # post-roll: three speech frames + one silence frame, not the whole hold.
    assert len(utterance.audio_bytes) == 4 * _FRAME * 2
    assert utterance.end_sample_cursor == 4 * _FRAME
    assert harness.assembler.endpoint_phase is voice_session.EndpointPhase.FINALIZING_ASR


def test_incomplete_stable_prefix_holds_until_max_hold_then_commits() -> None:
    """A dangling connective keeps holding for exactly max_hold_ms after the candidate."""
    harness = _Harness(_ScriptedDecoder(["把灯打开然后"]), partial=_partial())
    try:
        outcomes = harness.feed_many([_SPEECH] * 3)
        silence_frames = 0
        utterance = None
        while utterance is None:
            utterance = harness.feed(_SILENCE)
            silence_frames += 1
    finally:
        harness.close()
    assert outcomes == [None] * 3
    assert utterance.endpoint_reason == "max_hold"
    assert silence_frames == 2 + 5, "candidate (2 frames) + max_hold (5 frames)"
    assert _decisions() == [
        ("hold", "acoustic_pause_candidate", 0.0),
        ("commit", "max_hold", 160.0),
    ]


def test_speech_resume_during_hold_returns_to_speech_active_without_commit() -> None:
    """Speech inside the hold reopens speech_active and commits nothing."""
    harness = _Harness(_ScriptedDecoder(["把灯打开然后"]), partial=_partial())
    try:
        outcomes = harness.feed_many([_SPEECH] * 3 + [_SILENCE] * 3 + [_SPEECH] * 2)
        phase = harness.assembler.endpoint_phase
    finally:
        harness.close()
    assert outcomes == [None] * 8
    assert phase is voice_session.EndpointPhase.SPEECH_ACTIVE
    assert _decisions() == [
        ("hold", "acoustic_pause_candidate", 0.0),
        ("resume", "speech_resumed", 32.0),
    ]


def test_slow_partial_decode_degrades_to_acoustic_endpointing() -> None:
    """One over-budget decode disables partials for the rest of the utterance."""
    harness = _Harness(
        _ScriptedDecoder(["把灯打开。"], delay_s=0.05),
        partial=_partial(interval_ms=32),
        required_misses=4,
    )
    try:
        assert harness.feed(_SPEECH) is None
        assert harness.feed(_SPEECH) is None
        degraded = _traces("partial_asr_degraded")
        partials_before = len(_traces("asr_partial"))
        outcomes = harness.feed_many([_SPEECH, _SILENCE, _SILENCE, _SILENCE, _SILENCE])
    finally:
        harness.close()
    assert len(degraded) == 1
    assert degraded[0].attributes["reason"] == "partial_decode_over_budget"
    assert float(degraded[0].attributes["decode_ms"] or 0) > 32
    assert partials_before == 1
    assert len(_traces("asr_partial")) == 1, "no partial accepted after degrade"
    utterance = outcomes[-1]
    assert outcomes[:-1] == [None] * 4
    assert isinstance(utterance, voice_session.CapturedUtterance)
    assert utterance.endpoint_reason == "acoustic_pause"
    assert _decisions() == [
        ("hold", "acoustic_pause_candidate", 0.0),
        ("commit", "acoustic_pause", 64.0),
    ]


def test_three_consecutive_snapshot_drops_degrade() -> None:
    """A lane that cannot keep pace coalesces; three drops in a row degrade."""
    harness = _Harness(_ScriptedDecoder(["把灯打开。"]), partial=_partial())
    try:
        for _ in range(4):
            assert harness.feed(_SPEECH, decode=False) is None
    finally:
        harness.close()
    degraded = _traces("partial_asr_degraded")
    assert len(degraded) == 1
    assert degraded[0].attributes["reason"] == "coalescing_queue_drops"
    assert degraded[0].attributes["consecutive_drops"] == 3
    assert harness.lane.drops == 3
    assert harness.lane.take_revision() is None, "cancel dropped the pending snapshot"


def test_disabled_partial_asr_keeps_acoustic_pause_and_emits_no_new_traces() -> None:
    """``enabled: false`` is the pre-D7 rule: VAD empty() commits ``acoustic_pause``."""
    harness = _Harness(
        _ScriptedDecoder(["把灯打开。"]),
        partial=voice_session.PartialAsrConfig(),
        required_misses=2,
    )
    try:
        outcomes = harness.feed_many([_SPEECH] * 3 + [_SILENCE] * 2)
    finally:
        harness.close()
    utterance = outcomes[-1]
    assert isinstance(utterance, voice_session.CapturedUtterance)
    assert utterance.endpoint_reason == "acoustic_pause"
    assert len(utterance.audio_bytes) == 5 * _FRAME * 2
    assert harness.lane.decodes == 0
    assert _traces("endpoint_decision") == []
    assert _traces("asr_partial") == []


# ---------------------------------------------------------------------------
# Late revision after commit: full session, real VoicePipeline and Event Log.
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Callback-capable backend with one owner and no physical device."""

    def __init__(self) -> None:
        self.format = voice_backend.AudioInputFormat(16_000, 1, _FRAME)
        self.sinks: dict[int, voice_backend.InputFrameSink] = {}
        self.attempts: dict[int, str] = {}
        self.active_epoch: int | None = None
        self.active_attempt_id: str | None = None
        self.version = 0

    def start(
        self,
        *,
        stream_epoch: int,
        attempt_id: str,
        frame_sink: voice_backend.InputFrameSink,
        render_source: voice_backend.RenderSource | None = None,
        timeout_s: float | None = None,
    ) -> voice_backend.BackendStartResult:
        del render_source, timeout_s
        assert self.active_epoch is None
        self.active_epoch = stream_epoch
        self.active_attempt_id = attempt_id
        self.version += 1
        self.sinks[stream_epoch] = frame_sink
        self.attempts[stream_epoch] = attempt_id
        return voice_backend.BackendStartResult(
            status=voice_backend.BackendStartStatus.STARTED,
            stream_epoch=stream_epoch,
            profile=voice_backend.InputDeviceProfile(
                device_uid="fake-input",
                device_name="fake-input",
                backend="fake",
                input_format=self.format,
            ),
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
        if self.active_epoch == stream_epoch and self.active_attempt_id == attempt_id:
            self.active_epoch = None
            self.active_attempt_id = None
            self.version += 1
        return voice_backend.BackendStopResult(
            status=voice_backend.BackendStopStatus.CLOSED,
            stream_epoch=stream_epoch,
            attempt_id=attempt_id,
        )

    def emit(self, *, epoch: int, value: int) -> None:
        self.sinks[epoch](
            stream_epoch=epoch,
            attempt_id=self.attempts[epoch],
            callback_buffer=bytearray(_pcm(value)),
            frame_count=_FRAME,
            adc_time_s=time.monotonic(),
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=False,
        )

    def poll_fault(self, *, stream_epoch: int) -> voice_backend.BackendFault | None:
        del stream_epoch
        return None

    def current_device_uid(self) -> str | None:
        return "fake-input"

    def input_format(self) -> voice_backend.AudioInputFormat:
        return self.format

    def output_format(self) -> None:
        return None

    def capabilities(self) -> voice_backend.BackendCapabilities:
        return voice_backend.BackendCapabilities(
            owns_default_input=True,
            owns_render_clock=False,
            aec=False,
            natural_barge_in=False,
            reliable_adc_time=True,
            reliable_dac_time=False,
        )

    def ownership_snapshot(self) -> voice_backend.BackendOwnershipSnapshot:
        closed = self.active_epoch is None
        return voice_backend.BackendOwnershipSnapshot(
            state=(
                voice_backend.BackendLifecycleState.CLOSED
                if closed
                else voice_backend.BackendLifecycleState.OPEN
            ),
            stream_epoch=self.active_epoch,
            attempt_id=self.active_attempt_id,
            version=self.version,
            physical_owner_possible=not closed,
            helper_thread_alive=False,
        )


class _FakeWakeEngine:
    model_name = "hey_jarvis_v0.1"

    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    def predict(self, _frame_bytes: bytes) -> dict[str, float]:
        call = self.calls
        self.calls += 1
        return {self.model_name: 0.9 if call == 0 else 0.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _BlockingRecognizer:
    """Final text is fixed; the partial decode blocks until the test releases it."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.partial_calls = 0

    def recognize(self, audio_pcm: bytes) -> voice_asr.TranscriptionResult:
        del audio_pcm
        return voice_asr.TranscriptionResult(
            text="最终文本。",
            confidence=0.9,
            language_detected="zh",
            emotion=None,
        )

    def partial_text(self, audio_bytes: bytes) -> str:
        del audio_bytes
        self.partial_calls += 1
        self.release.wait(timeout=5.0)
        return "部分文本"


def test_late_partial_revision_is_discarded_and_only_final_text_is_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decode still in flight at commit never reaches the assembler or the Event Log."""
    reset_realtime_trace()
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,  # noqa: SLF001
        "record",
        voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    db_path = tmp_path / "events.db"
    recognizer = _BlockingRecognizer()
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(db_path),
        recognizer=recognizer,
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None,
        artifacts_dir=tmp_path,
    )
    backend = _FakeBackend()
    ingress = voice_audio.AudioIngress(
        backend=backend,
        config=replace(
            voice_audio.AudioIngressConfig(),
            worker_poll_s=0.0005,
            fault_poll_s=0.001,
            route_poll_s=60.0,
            shutdown_timeout_s=1.0,
        ),
    )
    wake_engine = _FakeWakeEngine()
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=wake_engine,
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=None,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=2.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
                partial_asr=_partial(interval_ms=320, candidate_ms=64, max_hold_ms=96),
            ),
        )
        assert session.start().started
        epoch = ingress.stream_epoch
        assert epoch == 1
        for value in [0, 0, 0, 0, 10_000, 11_000, 12_000, 0, 0, 0, 0, 0]:
            backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)

        def _committed_rows() -> list[dict[str, Any]]:
            with open_event_log(db_path) as conn:
                rows = conn.execute(
                    "SELECT payload_json FROM events WHERE type = 'utterance.received'",
                ).fetchall()
            return [json.loads(row[0]) for row in rows]

        _wait_until(lambda: len(_committed_rows()) == 1)
        payload = _committed_rows()[0]
        assert payload["transcript"] == "最终文本。"
        assert payload["endpoint_reason"] == "max_hold"
        assert recognizer.partial_calls == 1, "exactly one partial decode was in flight"
        _wait_until(
            lambda: session._assembler.endpoint_phase  # noqa: SLF001
            is voice_session.EndpointPhase.COMMITTED,
        )
        recognizer.release.set()
        _wait_until(lambda: session.metrics().partial_late_revisions_discarded == 1)
        close = session.close()
    assert close.definitively_closed
    assert _traces("asr_partial") == [], "the late revision never became a partial"
    committed = _traces("utterance_committed")
    assert len(committed) == 1
    assert committed[0].attributes["utterance_id"] == payload["utterance_id"]
    assert session.metrics().partial_decodes == 1


# ---------------------------------------------------------------------------
# Data-driven completeness table and config parsing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("把灯打开。", True),
        ("今天天气不错", True),
        ("我们还去公园吗？", True),  # noqa: RUF001
        ("明天下雨的话", False),
        ("我想问一下，", False),  # noqa: RUF001
        ("然后", False),
        ("turn on the light.", True),
        ("Turn on the light", True),
        ("I want to", False),
        ("I want to go and", False),
        ("", False),
        ("   ", False),
    ],
)
def test_completeness_table(text: str, *, expected: bool) -> None:
    """The endpoint completeness rule is local, deterministic, and data-checked."""
    assert voice_asr.looks_complete(voice_asr.normalize_partial_text(text)) is expected


def test_partial_asr_config_defaults_off_and_parses_block() -> None:
    """Absent block is off; explicit values parse; bad values name their key."""
    parse = voice_session.realtime_input_session_config_from_mapping
    assert parse({}).partial_asr == voice_session.PartialAsrConfig()
    assert parse({}).partial_asr.enabled is False
    parsed = parse(
        {
            "partial_asr": {
                "enabled": True,
                "interval_ms": 200,
                "candidate_ms": 192,
                "max_hold_ms": 800,
                "post_roll_ms": 160,
            },
        },
    ).partial_asr
    assert parsed == voice_session.PartialAsrConfig(
        enabled=True,
        interval_ms=200,
        candidate_ms=192,
        max_hold_ms=800,
        post_roll_ms=160,
    )
    with pytest.raises(ValueError, match=r"partial_asr\.enabled"):
        parse({"partial_asr": {"enabled": "yes"}})
    with pytest.raises(ValueError, match=r"partial_asr\.interval_ms"):
        parse({"partial_asr": {"interval_ms": 0}})
    with pytest.raises(ValueError, match="partial_asr must be a mapping"):
        parse({"partial_asr": True})
