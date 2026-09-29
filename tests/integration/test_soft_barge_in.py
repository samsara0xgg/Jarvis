"""Soft barge-in: speech over Jarvis first lowers her, then its words decide.

Conversation mode stopped Jarvis at the first frame of any speech over her
(ADR 0041), so a cough, a 「嗯」, a door or the TV cut her answer off and
cancelled the rest of it. Now she yields: lowered at onset, stopped once the
speech holds ``barge_in_confirm_voiced_s`` of voice, and for a shorter sound the
final transcript decides. Nothing, or a listening sound, gives her the gain back
and is no turn; a stop request stops her and is no turn; other words stop her
and are a turn.

A real DuplexVoiceSession on the scripted ingress, with the shipped VAD profile
and Silero replaced by a stand-in whose probability follows frame energy, feeds
a real VoicePipeline whose recognizer returns scripted text, over a real Event
Log. Only the output side is recorded: her gains and her stops, in order.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest
import yaml

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_asr, voice_audio, voice_pipeline, voice_session, voice_tts
from jarvis.surface.voice_ledger import GenerationLease
from tests.canary._helpers import repo_root
from tests.integration.test_incremental_tts import _pipeline
from tests.integration.test_voice_short_sounds import _EnergySilero
from tests.integration.test_wave2_streaming_media import _FakeProvider, _player
from tests.integration.test_wave3_single_audio_ingress import (
    _FakeBackend,
    _FakeWakeEngine,
    _ingress,
    _wait_until,
)

if TYPE_CHECKING:
    from pathlib import Path

VOICE, ROOM = 8_000, 30  # constant frames at about -12 and -60 dBFS
# With the shipped profile (5-frame smoothing, 3 hits) speech starts on the
# fifth voice frame and the last two voice frames' smoothing counts too, so a
# burst of N frames holds N - 2 voiced frames: 6 is a short sound, 25 is not.
SHORT, LONG = 6, 25
# Replayed through Silero, the 2026-09-28 live test's 「嗯」 held 0.61 s of voice:
# 21 frames hold 19 voiced ones, 0.61 s.
HUM = 21


class _ScriptedAsr:
    """Final ASR that hears the next scripted transcript."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts

    def recognize(self, audio_bytes: bytes) -> voice_asr.TranscriptionResult:
        del audio_bytes
        return voice_asr.TranscriptionResult(
            text=self._texts.pop(0),
            confidence=0.9,
            language_detected=None,
            emotion=None,
        )


class _Rig:
    """Jarvis talking in conversation mode while Allen makes one sound."""

    def __init__(
        self,
        tmp_path: Path,
        heard: str,
        *,
        speaking: bool = True,
        confirm_voiced_s: float = 0.4,
        stop_gate: threading.Event | None = None,
    ) -> None:
        self.output: list[str] = []
        self.stop_gate = stop_gate
        self.phases: list[tuple[str, object]] = []
        self.speaking = speaking
        self.db = tmp_path / "events.db"
        open_event_log(self.db).close()
        self.backend = _FakeBackend()
        self.ingress = _ingress(self.backend)
        pipeline = voice_pipeline.VoicePipeline(
            conn_factory=lambda: open_event_log(self.db),
            recognizer=_ScriptedAsr([heard]),
            normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
            broadcaster=self,
            artifacts_dir=None,
        )
        with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySilero()):
            self.session = voice_session.DuplexVoiceSession(
                ingress=self.ingress,
                wake_engine=_FakeWakeEngine(detections=set()),
                vad=voice_audio.SileroVad(mode="record"),
                pipeline=pipeline,
                broadcaster=self,
                output_active=lambda: self.speaking,
                wake_threshold=0.5,
                config=replace(
                    voice_session.RealtimeInputSessionConfig(),
                    min_voiced_s=0.3,
                    pre_roll_ms=64,
                    worker_poll_s=0.001,
                    shutdown_timeout_s=1.0,
                    barge_in_confirm_voiced_s=confirm_voiced_s,
                ),
                mic_muted=lambda: False,
                conversation=lambda: True,
                stop_speaking=self._stop,
                supersede_unspoken=lambda _turn_id: self.output.append("supersede"),
                yield_speaking=lambda gain: self.output.append(f"gain {gain}"),
            )
            assert self.session.start().started

    def _stop(self) -> None:
        if self.stop_gate is not None:
            assert self.stop_gate.wait(5.0)
        self.output.append("stop")
        self.speaking = False

    def broadcast_voice_sync(self, phase: str, *, turn_id: str, **payload: object) -> None:
        del turn_id
        self.phases.append((phase, payload.get("reason")))

    def say(self, voice_frames: int, *, judged: str = "phase") -> None:
        """Room, then ``voice_frames`` of voice, then room until his words are judged.

        Judged is the surface's ``accepted`` or ``empty``; a wake phrase alone
        gets neither (the session listens on), so ``judged="gain"`` waits for
        her gain to come back instead.
        """
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        for value in [ROOM] * 8 + [VOICE] * voice_frames + [ROOM] * 60:
            self.backend.emit(epoch=epoch, value=value)
            time.sleep(0.002)
        if judged == "gain":
            _wait_until(lambda: self.output[-1:] == ["gain 1.0"])
        else:
            _wait_until(lambda: any(phase in {"accepted", "empty"} for phase, _ in self.phases))

    def turns(self) -> list[str]:
        with contextlib.closing(open_event_log(self.db)) as conn:
            return [
                str(row[0])
                for row in conn.execute(
                    "SELECT json_extract(payload_json, '$.transcript') FROM events "
                    "WHERE type = 'utterance.received'",
                )
            ]

    def close(self) -> None:
        assert self.session.close().definitively_closed


def _endpoints() -> list[object]:
    return [
        point.attributes.get("endpoint_reason")
        for point in realtime_trace_snapshot()
        if point.name == "endpoint_candidate"
    ]


def test_a_cough_over_her_lowers_her_then_gives_the_gain_back(tmp_path: Path) -> None:
    """Nothing in it: never stopped, no turn, ended on the short barge-in pause."""
    reset_realtime_trace()
    rig = _Rig(tmp_path, "")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.turns() == []
    assert ("empty", "no_speech") in rig.phases
    assert _endpoints() == ["barge_pause"]


def test_a_listening_sound_keeps_her_talking_and_is_no_turn(tmp_path: Path) -> None:
    """「嗯嗯」 over her: the gain comes back and nothing is answered."""
    rig = _Rig(tmp_path, "嗯嗯。")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", "backchannel") in rig.phases


@pytest.mark.parametrize(
    ("heard", "reason"),
    [
        ("停。", "stop_request"),
        ("停", "no_speech"),
        # How final ASR heard 「停」 over her in the 2026-09-28 live test.
        ("停立。", "stop_request"),
        ("顶。", "stop_request"),
        ("等一下。", "stop_request"),
        ("Wait.", "stop_request"),
    ],
)
def test_a_stop_request_stops_her_and_is_no_turn(
    tmp_path: Path,
    heard: str,
    reason: str,
) -> None:
    """「停」 as heard over her, or too short to be a turn: stopped before the gain comes back."""
    rig = _Rig(tmp_path, heard)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0"]
    assert rig.turns() == []
    assert ("empty", reason) in rig.phases


@pytest.mark.parametrize("heard", ["五。", "And."])
def test_one_word_that_says_nothing_keeps_her_talking(tmp_path: Path, heard: str) -> None:
    """What final ASR made of a 「嗯」 over her in the live test: no stop, and no turn."""
    rig = _Rig(tmp_path, heard)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", "unclear") in rig.phases


def test_a_lone_answer_word_over_her_is_still_a_turn(tmp_path: Path) -> None:
    """A lone 「对」 may be answering a waiting card (ADR 0062): she stops, and it is a turn."""
    rig = _Rig(tmp_path, "对。")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["对。"]


def test_the_live_tests_hum_no_longer_stops_her(tmp_path: Path) -> None:
    """At the shipped confirm time a 0.61 s 「嗯」 is judged by its words, not its length."""
    shipped = yaml.safe_load((repo_root() / "config" / "jarvis.yaml").read_text(encoding="utf-8"))
    confirm = voice_session.realtime_input_session_config_from_mapping(
        shipped["realtime"]["single_audio_ingress"],
    ).barge_in_confirm_voiced_s
    rig = _Rig(tmp_path, "嗯。", confirm_voiced_s=confirm)
    try:
        rig.say(HUM)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []


def test_her_name_alone_over_her_stops_her_and_listens_on(tmp_path: Path) -> None:
    """「Hey Jarvis」 and a pause: she stops, and his question is still to come."""
    rig = _Rig(tmp_path, "Hey, Jarvis.")
    try:
        rig.say(SHORT, judged="gain")
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0"]
    assert rig.turns() == []


def test_words_that_hold_enough_voice_stop_her_and_are_a_turn(tmp_path: Path) -> None:
    """She stops while he is still talking, and his words are answered."""
    rig = _Rig(tmp_path, "明天的会改到三点")
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["明天的会改到三点"]


def test_her_gain_comes_back_only_after_a_late_stop(tmp_path: Path) -> None:
    """A stop still on its way when his words are judged keeps her quieter until it lands."""
    gate = threading.Event()
    rig = _Rig(tmp_path, "明天的会改到三点", stop_gate=gate)
    try:
        rig.say(LONG)
        judged = list(rig.output)
        gate.set()
        _wait_until(lambda: rig.output[-1:] == ["gain 1.0"])
    finally:
        rig.close()
    assert judged == ["gain 0.2", "supersede"]
    assert rig.output == ["gain 0.2", "supersede", "stop", "gain 1.0"]


def test_a_stop_request_long_enough_to_stop_her_gets_no_answer(tmp_path: Path) -> None:
    """「别说了」 said slowly already stopped her; it is still no question."""
    rig = _Rig(tmp_path, "别说了")
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0"]
    assert rig.turns() == []
    assert ("empty", "stop_request") in rig.phases


def test_zero_confirm_time_stops_her_at_onset(tmp_path: Path) -> None:
    """``barge_in_confirm_voiced_s: 0`` is ADR 0041 as it shipped: no yield, a turn."""
    rig = _Rig(tmp_path, "嗯嗯。", confirm_voiced_s=0.0)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["stop", "supersede"]
    assert rig.turns() == ["嗯嗯。"]


def test_a_sound_while_she_is_silent_is_an_ordinary_utterance(tmp_path: Path) -> None:
    """Nothing to yield: no gain change, and his words are judged as ever."""
    rig = _Rig(tmp_path, "嗯嗯。", speaking=False)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["supersede"]
    assert rig.turns() == ["嗯嗯。"]


def test_a_yield_never_lifts_the_speech_mute(tmp_path: Path) -> None:
    """The player's gain is the mute's times the yield's, whatever order they come in."""
    pipeline, player = _pipeline(
        tmp_path / "events.db",
        _FakeProvider(candidate_count=1),
        speak_from_segments=True,
    )
    gains: list[float] = []
    player.set_gain = lambda gain, _ramp_ms=30.0: gains.append(gain)  # type: ignore[method-assign,assignment]
    try:
        pipeline.set_output_gain(0.0)  # Allen mutes her
        pipeline.set_yield_gain(0.2)  # then talks over her
        pipeline.set_yield_gain(1.0)  # his cough is judged
        pipeline.set_output_gain(1.0)  # he unmutes her
        pipeline.set_yield_gain(0.2)
        pipeline.set_output_gain(0.0)
    finally:
        assert pipeline.close()
    assert gains == [0.0, 0.0, 0.0, 1.0, 0.2, 0.0]


def _speak(player: voice_tts.AudioStreamPlayer, response_id: str) -> tuple[int, np.ndarray]:
    """One generation holding eight samples at full scale; its first block as heard."""
    lease = player.activate_generation(
        session_id="S", response_id=response_id, response_group_id="G", turn_id="T",
    )
    assert isinstance(lease, GenerationLease)
    generation = lease.playback_generation_id
    player.begin_generation_segment(
        expected_playback_generation_id=generation, sequence=0, text="words", segment_hash="h",
    )
    player.write_generation(
        np.ones(8, dtype=np.float32).tobytes(),
        expected_playback_generation_id=generation,
        segment_sequence=0,
    )
    block = np.zeros((8, 1), dtype=np.float32)
    player._callback(block, 8, None, None)  # noqa: SLF001
    return generation, block[:, 0]


@pytest.mark.parametrize(("after_stop", "heard"), [(1.0, 1.0), (0.0, 0.0)])
def test_the_answer_after_a_stop_starts_at_the_gain_set_while_she_was_silent(
    after_stop: float,
    heard: float,
) -> None:
    """The yield's restore once she is stopped (or a mute) holds from the next first sample.

    Left to ramp in with the next answer, it began that answer at the yield's
    0.2 (so it was never counted as heard in full), or let a mute's first
    block through.
    """
    player = _player(ring_seconds=0.02)
    player.set_gain(0.2, 0.0)  # Allen talks over her
    first, _ = _speak(player, "R1")
    player.interrupt_generation(expected_playback_generation_id=first)
    player.settle_interrupted_generation(expected_playback_generation_id=first)
    player.retire_generation(first)
    player.set_gain(after_stop, 30.0)
    player._callback(np.zeros((8, 1), dtype=np.float32), 8, None, None)  # noqa: SLF001
    _, block = _speak(player, "R2")
    assert np.all(block == heard)
