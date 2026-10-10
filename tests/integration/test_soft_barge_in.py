"""Soft barge-in: speech over Jarvis first lowers her, then its words decide.

Conversation mode stopped Jarvis at the first frame of any speech over her
(ADR 0041), so a cough, a 「嗯」, a door or the TV cut her answer off and
cancelled the rest of it. Now she yields: lowered at onset, held where she is
once the speech holds ``barge_in_confirm_voiced_s`` of voice, and the final
transcript decides. Nothing, or a listening sound, lets her go on from there
with the gain back and is no turn; a stop request stops her and is no turn;
other words stop her and are a turn.

A real DuplexVoiceSession on the scripted ingress, with the shipped VAD profile
and Silero replaced by a stand-in whose probability follows frame energy, feeds
a real VoicePipeline whose recognizer returns scripted text, over a real Event
Log. Only the output side is recorded: her gains, holds and stops, in order.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import numpy as np
import pytest
import yaml

from jarvis.shared.realtime_trace import realtime_trace_snapshot, reset_realtime_trace
from jarvis.state.event_log import open_event_log
from jarvis.surface import (
    voice_asr,
    voice_audio,
    voice_backend,
    voice_pipeline,
    voice_session,
    voice_tts,
)
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
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.surface.voice_cues import VoiceCues

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
        self.heard: list[bytes] = []
        self.tags: dict[str, tuple[str | None, str | None]] = {}  # text -> (emotion, event)

    def recognize(self, audio_bytes: bytes) -> voice_asr.TranscriptionResult:
        self.heard.append(audio_bytes)
        text = self._texts.pop(0)
        emotion, event = self.tags.get(text, (None, None))
        return voice_asr.TranscriptionResult(
            text=text,
            confidence=0.9,
            language_detected=None,
            emotion=emotion,
            event=event,
        )


class _Rig:
    """Jarvis talking in conversation mode while Allen makes one sound."""

    def __init__(  # noqa: PLR0913 - each switch a test flips
        self,
        tmp_path: Path,
        heard: str,
        *,
        speaking: bool = True,
        confirm_voiced_s: float = 0.4,
        idle_exit_s: float = 10.0,
        wait_s: float = 60.0,
        recent_speech: Callable[[], str] | None = None,
        ask_words: Callable[[str, str, str, bool, bool], str | None] | None = None,
        note_words: Callable[[str, str, str, bool, bool], None] | None = None,
        begin_line: Callable[[str, str, str, bool, bool], None] | None = None,
        backend: _FakeBackend | None = None,
        wake_input_channel: int | None = None,
        output_active: Callable[[], bool] | None = None,
        stop_speaking: Callable[[], object] | None = None,
        answer_words: Callable[[str, str, str], None] | None = None,
        cancel_voice_runs: Callable[[], None] | None = None,
        cues: VoiceCues | None = None,
        heard_elsewhere: Callable[[str, str], bool] | None = None,
    ) -> None:
        self.output: list[str] = []
        self.phases: list[tuple[str, object]] = []
        self.conversation = True
        self.conversation_changes: list[tuple[bool, str]] = []
        self.answers: list[str] = []
        self.speaking = speaking
        self.db = tmp_path / "events.db"
        open_event_log(self.db).close()
        self.backend = backend or _FakeBackend()
        self.ingress = _ingress(self.backend, wake_input_channel=wake_input_channel)
        self.asr = _ScriptedAsr([heard])
        pipeline = voice_pipeline.VoicePipeline(
            conn_factory=lambda: open_event_log(self.db),
            recognizer=self.asr,
            normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
            broadcaster=self,
            artifacts_dir=None,
            cues=cues,
        )
        with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySilero()):
            self.session = voice_session.DuplexVoiceSession(
                ingress=self.ingress,
                wake_engine=_FakeWakeEngine(detections=set()),
                vad=voice_audio.SileroVad(mode="record"),
                pipeline=pipeline,
                broadcaster=self,
                output_active=output_active or (lambda: self.speaking),
                wake_threshold=0.5,
                config=replace(
                    voice_session.RealtimeInputSessionConfig(),
                    min_voiced_s=0.3,
                    pre_roll_ms=64,
                    worker_poll_s=0.001,
                    shutdown_timeout_s=1.0,
                    barge_in_confirm_voiced_s=confirm_voiced_s,
                    conversation_idle_exit_s=idle_exit_s,
                    conversation_wait_s=wait_s,
                ),
                mic_muted=lambda: False,
                conversation=lambda: self.conversation,
                set_conversation=self._set_conversation,
                answer_words=answer_words
                or (lambda _turn_id, reason, _text: self.answers.append(reason)),
                stop_speaking=stop_speaking or self._stop,
                supersede_unspoken=lambda _turn_id: self.output.append("supersede"),
                cancel_voice_runs=cancel_voice_runs or (lambda: self.output.append("cancel runs")),
                yield_speaking=lambda gain: self.output.append(f"gain {gain}"),
                pause_speaking=lambda paused: self.output.append("pause" if paused else "go on"),
                recent_speech=recent_speech,
                ask_words=ask_words,
                note_words=note_words,
                begin_line=begin_line,
                cues=cues,
                heard_elsewhere=heard_elsewhere,
            )
            assert self.session.start().started

    def _set_conversation(self, on: bool, reason: str) -> None:  # noqa: FBT001 - session callback shape
        self.conversation_changes.append((on, reason))
        self.conversation = on

    def _stop(self) -> None:
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

    def room(self, seconds: float) -> None:
        """Nobody talking for ``seconds``."""
        epoch = self.ingress.stream_epoch
        assert epoch is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.backend.emit(epoch=epoch, value=ROOM)
            time.sleep(0.002)

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


@pytest.mark.parametrize("speaking", [True, False])
def test_a_dismissal_stops_her_ends_the_mode_and_is_no_turn(
    tmp_path: Path, speaking: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """ADR 0102: 「好, 退下吧」 in conversation mode, over her or not."""
    rig = _Rig(tmp_path, "好, 退下吧。", speaking=speaking)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert ("stop" in rig.output) is speaking
    assert "cancel runs" in rig.output  # every answer still on its way, any age
    assert rig.conversation_changes == [(False, "dismissed")]
    assert rig.answers == ["dismissed"]
    assert rig.turns() == []
    assert ("empty", "dismissed") in rig.phases


@pytest.mark.parametrize("speaking", [True, False])
def test_leaving_from_the_surface_does_what_a_dismissal_does_without_the_words(
    tmp_path: Path, speaking: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """ADR 0138: ``dismiss()`` ends every answer being written and stops her, silently."""
    rig = _Rig(tmp_path, "", speaking=speaking)
    try:
        rig.session.dismiss()
    finally:
        rig.close()
    assert rig.output == (["cancel runs", "stop"] if speaking else ["cancel runs"])
    assert rig.answers == []
    # The surface turned the mode off itself; nothing is flipped a second time.
    assert rig.conversation_changes == []


@pytest.mark.parametrize(("heard", "reason"), [("The.", "unclear"), ("嗯。", "backchannel")])
def test_a_lone_word_in_the_mode_is_dropped_and_keeps_nothing_open(
    tmp_path: Path, heard: str, reason: str,
) -> None:
    """ADR 0102: with her silent, a hum or a lone word is no turn and the quiet clock runs on."""
    rig = _Rig(tmp_path, heard, speaking=False, idle_exit_s=0.05)
    try:
        rig.say(SHORT)
        rig.room(0.2)
    finally:
        rig.close()
    assert rig.turns() == []
    assert ("empty", reason) in rig.phases
    assert rig.answers == []
    assert rig.conversation_changes == [(False, "idle")]


def test_wait_for_me_holds_the_mode_then_quiet_ends_it(tmp_path: Path) -> None:
    """ADR 0102: 「等我一下」 is no turn and keeps the mode open its wait, not forever."""
    rig = _Rig(tmp_path, "等我一下。", speaking=False, idle_exit_s=0.05, wait_s=0.6)
    try:
        rig.say(SHORT)
        rig.room(0.1)
        held = list(rig.conversation_changes)
        rig.room(0.8)
    finally:
        rig.close()
    assert held == []
    assert rig.answers == ["wait"]
    assert rig.conversation_changes == [(False, "idle")]
    assert rig.turns() == []
    assert ("empty", "wait") in rig.phases


def test_a_turn_holds_the_mode_until_her_answer_then_quiet_ends_it(tmp_path: Path) -> None:
    """ADR 0102: an accepted turn waits for her answer; the clock starts again after she speaks."""
    rig = _Rig(tmp_path, "给我讲个故事。", speaking=False, idle_exit_s=0.1)
    try:
        rig.say(SHORT)
        rig.room(0.3)
        waiting = list(rig.conversation_changes)
        rig.speaking = True
        rig.room(0.05)
        rig.speaking = False
        rig.room(0.3)
    finally:
        rig.close()
    assert waiting == []
    assert rig.turns() == ["给我讲个故事。"]
    assert rig.conversation_changes == [(False, "idle")]


def test_dismissals_are_whole_phrases_or_short_orders() -> None:
    """A dismissal is the whole sentence, or 退下 / 退一下 / a leading 退出 in a short one."""
    said = (
        "退下。", "你可以退下了", "没事了。", "就这样吧!",
        "Hey, Jarvis, 退下。", "Bye bye.", "That's all.",
        "退出退出退下, 暂停停一下等。", "我让你退一下。", "退出。", "没事了没事了。",
    )
    for heard in said:
        assert voice_asr.is_dismissal(heard), heard
    not_said = (
        "退下以后呢?", "你会退下吗", "跟妈妈说再见", "就这样做", "结束了吗?", "By the way", "停。",
        "怎么退出 vim", "他从董事会退下来以后去做了投资, 后来怎么样了",
    )
    for heard in not_said:
        assert not voice_asr.is_dismissal(heard), heard
    waits = (
        "等我一下。", "你等我一下", "稍等一下", "Hold on.", "Give me a second.",
        "等我一下等我一下。",
    )
    for heard in waits:
        assert voice_asr.is_wait_request(heard), heard
        assert not voice_asr.is_stop_request(heard), heard
    for heard in ("等一下。", "等我回来再说", "Wait."):
        assert not voice_asr.is_wait_request(heard), heard


def test_runs_of_stop_wait_and_listening_sounds_are_judged_whole() -> None:
    """A run of them is one verdict (stop, wait, sound, in that order); other words, a turn."""
    stops = (
        "停下来停下来停下来停。", "你别说话你别说话停。", "嗯哼停。", "等一下等一下。",
        "停停下来。", "闭嘴闭嘴。", "别说了别说了。", "嗯, 停, 停下来。", "好了好了。",
        "Stop stop.", "嗯等我一下, 停。",
    )
    for heard in stops:
        assert voice_asr.is_stop_request(heard), heard
        assert not voice_asr.is_backchannel(heard), heard
    waits = ("嗯哼等我一下。", "嗯, 等我一下等我一下。", "稍等一下, 嗯。")
    for heard in waits:
        assert voice_asr.is_wait_request(heard), heard
        assert not voice_asr.is_stop_request(heard), heard
    sounds = ("嗯h。", "呃hm。", "嗯哼。", "嗯嗯哼哼。", "Mm-hmm.")
    for heard in sounds:
        assert voice_asr.is_backchannel(heard), heard
        assert not voice_asr.is_stop_request(heard), heard
        assert not voice_asr.is_wait_request(heard), heard
    turns = (
        "停一下, 帮我查天气。", "等一下我要问你个问题。", "嗯哼, 然后呢。",
        "停下来停下来, 为什么?", "等我一下, 我去拿手机。", "好。", "对。", "Yeah.", "嗯ok。",
        "你别说话了吗?",
    )
    for heard in turns:
        assert not voice_asr.is_backchannel(heard), heard
        assert not voice_asr.is_stop_request(heard), heard
        assert not voice_asr.is_wait_request(heard), heard
    for heard in ("退下退下。", "没事了没事了。", "Bye bye bye."):
        assert voice_asr.is_dismissal(heard), heard


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
        ("Pause.", "stop_request"),
        # "That's enough" in the 2026-09-29 live tests, each taken for a turn.
        ("OK可以了。", "stop_request"),
        ("可以啦。", "stop_request"),
        # Said in a run, as the 2026-09-30 live tests heard it: still no turn.
        ("停下来停下来停。", "stop_request"),
        # 「pause」 over her comes back as some other lone English word
        # (2026-09-29 live tests; a synthesized one); any such word stops her.
        ("Pulse.", "stop_request"),
        ("Cause.", "stop_request"),
        ("Po", "stop_request"),
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


@pytest.mark.parametrize(
    ("heard", "reason"),
    [
        ("五。", "unclear"),
        ("And.", "backchannel"),
        # A drawn-out 「嗯」 in the 2026-09-29 live test, which stopped her as a turn.
        ("うん。", "backchannel"),
        ("응.", "backchannel"),
    ],
)
def test_one_word_that_says_nothing_keeps_her_talking(
    tmp_path: Path,
    heard: str,
    reason: str,
) -> None:
    """What final ASR made of a 「嗯」 over her in the live test: no stop, and no turn."""
    rig = _Rig(tmp_path, heard)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", reason) in rig.phases


def test_a_lone_answer_word_over_her_is_still_a_turn(tmp_path: Path) -> None:
    """A lone 「对」 may be answering a waiting card (ADR 0062): she stops, and it is a turn."""
    rig = _Rig(tmp_path, "对。")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["对。"]


def test_every_one_word_card_answer_is_still_a_turn_over_her() -> None:
    """A lone word the confirmation grammar answers a card with is no unclear sound or stop."""
    grammar = (repo_root() / "config" / "confirm_grammar.yaml").read_text(encoding="utf-8")
    patterns = [str(row["pattern"]) for row in yaml.safe_load(grammar)]
    words = {*re.findall(r"[\u4e00-\u9fff]|[a-z]+", " ".join(patterns)), "don't", "dont"}
    answers = {word for word in words if any(re.fullmatch(p, word) for p in patterns)}
    assert {"好", "发", "别", "yes", "send", "cancel", "don't"} <= answers
    assert not [
        word for word in answers
        if voice_asr.is_unclear_sound(word + ".") or voice_asr.is_stop_request(word + ".")
    ]
    # 「可以了」 over her is enough; the card's 「可以」 is still yes.
    assert not voice_asr.is_stop_request("可以。")
    # A lone English word asked as a question is a turn (「What?」 repeats her).
    assert not voice_asr.is_stop_request("What?")


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
    """She is held while he is still talking, stopped once his words are in, and they are answered.

    Her gain comes back only after the stop.
    """
    rig = _Rig(tmp_path, "明天的会改到三点")
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "pause", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["明天的会改到三点"]


def test_a_stop_request_long_enough_to_hold_her_gets_no_answer(tmp_path: Path) -> None:
    """「别说了」 said slowly held her; it stops her, and it is still no question."""
    rig = _Rig(tmp_path, "别说了")
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "pause", "stop", "gain 1.0"]
    assert rig.turns() == []
    assert ("empty", "stop_request") in rig.phases


@pytest.mark.parametrize(("heard", "reason"), [("嗯。", "backchannel"), ("", "no_speech")])
def test_a_long_hum_or_cough_holds_her_then_she_goes_on(
    tmp_path: Path,
    heard: str,
    reason: str,
) -> None:
    """A drawn-out 「嗯——」 or a cough held her; it says nothing, so she goes on from there.

    In the 2026-09-29 live test such a 「嗯——」 stopped her for good.
    """
    rig = _Rig(tmp_path, heard)
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "pause", "go on", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", reason) in rig.phases


_SAID = "Your demo is scheduled for Friday, October 9. It isn't on your calendar."


@pytest.mark.parametrize("voiced", [SHORT, LONG])
def test_her_own_words_over_her_are_echo_she_goes_on_and_it_is_no_turn(
    tmp_path: Path, voiced: int,
) -> None:
    """2026-10-02: the board's echo canceller did not converge; her voice in the mic stopped her."""
    rig = _Rig(tmp_path, "Your demo is.", recent_speech=lambda: _SAID)
    try:
        rig.say(voiced)
    finally:
        rig.close()
    assert rig.output == (
        ["gain 0.2", "gain 1.0"] if voiced == SHORT else ["gain 0.2", "pause", "go on", "gain 1.0"]
    )
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", "echo") in rig.phases


def _raises() -> str:
    message = "event log locked"
    raise OSError(message)


@pytest.mark.parametrize("recent_speech", [lambda: _SAID, lambda: "", _raises, None])
def test_words_that_are_not_her_own_still_stop_her_and_are_a_turn(
    tmp_path: Path, recent_speech: Callable[[], str] | None,
) -> None:
    """No match, nothing said lately, a failing lookup or none bound: as before."""
    rig = _Rig(tmp_path, "明天的会改到三点", recent_speech=recent_speech)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["明天的会改到三点"]


def test_a_failing_lookup_leaves_her_own_words_a_turn_as_before(tmp_path: Path) -> None:
    """Without her recent words there is no echo to tell: the words are a turn."""
    rig = _Rig(tmp_path, "Your demo is.", recent_speech=_raises)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0", "supersede"]
    assert rig.turns() == ["Your demo is."]


def test_a_stop_word_she_said_herself_still_stops_her(tmp_path: Path) -> None:
    """Echo is judged after stop requests: 「Wait.」 stops her even when she just said it."""
    rig = _Rig(tmp_path, "Wait.", recent_speech=lambda: "Okay, wait a moment, I'll check.")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "stop", "gain 1.0"]
    assert rig.turns() == []
    assert ("empty", "stop_request") in rig.phases


# (what final ASR heard over her, what she had said in the 30 s before), from the
# event log: the first five are 2026-10-02 echoes, the rest Allen's own barge-ins.
_ECHOES = [
    (
        "Your demo is.",
        "Your demo is scheduled for Friday, October 9. It isn't on your calendar.",
    ),
    (
        "I don't age.",
        "I don't age like a person, but I'm here and ready to help.",
    ),
    (
        "Go ahead and read.",
        "I'm an AI assistant. Okay, go ahead and restart it. I'll be here.",
    ),
    (
        "不询问猫头鹰。",
        "小鹿。 它们一路询问猫头鹰,松鼠和河狸,终于找到回家的路。 从此,小鹿成了好朋友。",
    ),
    (
        "You're aging.",
        "Sure, go ahead. How's your day going? Yes, your agent workspace was another main focus.",
    ),
]
_GENUINE = [
    (
        "那太阳呢？",  # noqa: RUF001 - as final ASR wrote it
        "- Its gravity is about one-sixth of Earth's. The Moon is about 384,400 km away.",
    ),
    (
        "一个月前的今天发生了什么？",  # noqa: RUF001 - as final ASR wrote it
        "大约一周前,我看到你在推进 Jarvis 的自动日报能力:日报技能已经进了 main。",
    ),
    (
        "他的活动。",
        "我能确认的是今天上午的进展:你推进了 Timesink 的 Focus 页面,设置侧栏和趋势指标。",
    ),
    (
        "呃,可 you.",
        "Tandoori Trails is one option; enter your address to see its delivery availability.",
    ),
    (
        "Wait,呃.",
        "What I can access depends on the permissions you've granted. You're welcome, Allen.",
    ),
    (
        "哈哈哈,没事没事。",
        "你刚才说了句 The words I sick too,我以为你是在说自己也生病了,所以才那样回应。",
    ),
    (
        "继续讲。",
        "好,我给你讲个大约一百字的长城故事。 好,先等一下。",
    ),
    (
        "讲故事。",
        "It's 8:03 PM. 从前,有只小狐狸捡到一颗星星。 它爬上山顶,把星星送回夜空。",
    ),
]


@pytest.mark.parametrize(("heard", "said"), _ECHOES)
def test_her_echo_in_the_mic_is_known_from_the_event_log_rows(heard: str, said: str) -> None:
    """Each 2026-10-02 echo is a copy of her recent words."""
    assert voice_asr.is_own_echo(heard, said)


@pytest.mark.parametrize(("heard", "said"), _GENUINE)
def test_allens_own_words_over_her_are_not_her_echo(heard: str, said: str) -> None:
    """His barge-ins on other days are not a copy of her recent words."""
    assert not voice_asr.is_own_echo(heard, said)


class _StereoBackend(_FakeBackend):
    """Two channels: channel 0 is ``value``, channel 1 the beam, ``value`` plus ``BEAM``."""

    def __init__(self) -> None:
        super().__init__()
        self.format = voice_backend.AudioInputFormat(16_000, 2, 512)

    def emit(self, *, epoch: int, value: int, discontinuity: bool = False) -> None:
        frame = np.empty((512, 2), dtype="<i2")
        frame[:, 0], frame[:, 1] = value, value + BEAM
        self.sinks[epoch](
            stream_epoch=epoch,
            attempt_id=self.attempts[epoch],
            callback_buffer=bytearray(frame.tobytes()),
            frame_count=512,
            adc_time_s=time.monotonic(),
            captured_monotonic_ns=time.monotonic_ns(),
            discontinuity_before=discontinuity,
            channels=2,
        )


BEAM = 3  # what channel 1 carries over channel 0, per sample


@pytest.mark.parametrize("speaking", [True, False])
def test_words_are_heard_from_a_clean_beam_channel_over_her_or_not(
    tmp_path: Path, speaking: bool,  # noqa: FBT001 - pytest parameter
) -> None:
    """ADR 0133: a clean beam is what final ASR hears, over her or not."""
    rig = _Rig(
        tmp_path, "你是谁?", speaking=speaking,
        backend=_StereoBackend(), wake_input_channel=1,
    )
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    (heard,) = rig.asr.heard
    samples = set(np.frombuffer(heard, dtype="<i2").tolist())
    assert samples == {ROOM + BEAM, VOICE + BEAM}


def test_a_device_with_no_wake_channel_hears_channel_zero(tmp_path: Path) -> None:
    """ADR 0133: a mono microphone, or the setting off, hears channel 0."""
    rig = _Rig(tmp_path, "你是谁?")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    (heard,) = rig.asr.heard
    assert set(np.frombuffer(heard, dtype="<i2").tolist()) == {ROOM, VOICE}


def _assembler_run(
    *, beam: bool, background: int = 100, lose_beam_at: int | None = None,
) -> tuple[list[voice_session.CapturedUtterance], list[bytes], list[bytes | None]]:
    """One conversation-mode utterance: its commit, what ASR was prepared with, each frame's beam.

    Channel 0 is flat per frame; the beam of frame ``i`` is ``background + i`` (``VOICE + i``
    where he speaks), so each frame is its own and no beam sample equals a channel-0 one.
    """
    commits: list[voice_session.CapturedUtterance] = []
    prepared: list[bytes] = []
    beams: list[bytes | None] = []
    with patch.object(voice_audio, "_load_silero_session", return_value=_EnergySilero()):
        assembler = voice_session.UtteranceAssembler(
            vad=voice_audio.SileroVad(mode="record"),
            config=replace(voice_session.RealtimeInputSessionConfig(), min_voiced_s=0.3),
            sample_rate_hz=16_000,
            frame_samples=512,
            session_id="S",
            prepare_final=lambda _id, audio, _speech_s: prepared.append(audio),
        )
        assembler.prepare()
        for index, value in enumerate([ROOM] * 8 + [VOICE] * 30 + [ROOM] * 60):
            if not assembler.armed:
                assembler.arm(
                    voice_session.WakeDetection(1, index * 512, index, 1.0), expires=False,
                )
            wake = (
                np.full(512, (VOICE if value == VOICE else background) + index, dtype="<i2")
                .tobytes()
                if beam and index != lose_beam_at else None
            )
            beams.append(wake)
            outcome = assembler.feed(
                voice_audio.CanonicalAudioFrame(
                    stream_epoch=1,
                    sequence=index,
                    sample_cursor=index * 512,
                    sample_rate_hz=16_000,
                    frame_count=512,
                    adc_time_s=None,
                    captured_monotonic_ns=index,
                    discontinuity_before=False,
                    pcm16_mono=np.full(512, value, dtype="<i2").tobytes(),
                    wake_pcm16=wake,
                ),
            )
            if isinstance(outcome, voice_session.CapturedUtterance):
                commits.append(outcome)
                break
    return commits, prepared, beams


def test_a_clean_beam_is_what_final_asr_hears_for_exactly_the_utterances_frames(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pre-roll to last frame, by cursor; the prepared pass is a prefix of the commit's audio."""
    with caplog.at_level("INFO", logger="jarvis.surface.voice_session"):
        (utterance,), prepared, beams = _assembler_run(beam=True)
    first = utterance.start_sample_cursor // 512
    last = utterance.end_sample_cursor // 512
    expected = b"".join(item for item in beams[first:last] if item is not None)
    assert first < 8  # the pre-roll is in it
    assert utterance.audio_bytes == expected
    assert prepared
    assert all(
        utterance.audio_bytes.startswith(audio) and np.frombuffer(audio, "<i2")[0] >= 100
        for audio in prepared
    )
    lines = [r.message for r in caplog.records if "final ASR heard" in r.message]
    assert len(lines) == 1
    assert lines[0].startswith("final ASR heard channel 1 (beam snr ")


def test_a_loud_beam_background_gives_the_utterance_channel_zero(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Speech 8000 over a background near 3000 is snr under 5: commit and prepared are channel 0."""
    with caplog.at_level("INFO", logger="jarvis.surface.voice_session"):
        (utterance,), prepared, _ = _assembler_run(beam=True, background=3_000)
    assert set(np.frombuffer(utterance.audio_bytes, dtype="<i2").tolist()) <= {ROOM, VOICE}
    assert prepared
    assert all(
        utterance.audio_bytes.startswith(audio)
        and set(np.frombuffer(audio, dtype="<i2").tolist()) <= {ROOM, VOICE}
        for audio in prepared
    )
    assert any(
        r.message.startswith("final ASR heard channel 0 (beam snr ") for r in caplog.records
    )


def test_one_frame_missing_the_beam_gives_the_utterance_channel_zero_and_one_log_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A gap in the beam never splices two channels into one utterance."""
    with caplog.at_level("INFO", logger="jarvis.surface.voice_session"):
        (utterance,), prepared, _ = _assembler_run(beam=True, lose_beam_at=20)
    assert set(np.frombuffer(utterance.audio_bytes, dtype="<i2").tolist()) <= {ROOM, VOICE}
    assert prepared
    assert sum("wake channel missing" in record.message for record in caplog.records) == 1


def test_a_device_without_a_beam_gives_the_utterance_channel_zero() -> None:
    """No wake channel at all is channel 0 too."""
    (utterance,), _, _ = _assembler_run(beam=False)
    assert set(np.frombuffer(utterance.audio_bytes, dtype="<i2").tolist()) <= {ROOM, VOICE}


def test_zero_confirm_time_stops_her_at_onset(tmp_path: Path) -> None:
    """``barge_in_confirm_voiced_s: 0`` is ADR 0041 as it shipped: no yield, a turn."""
    rig = _Rig(tmp_path, "你是谁?", confirm_voiced_s=0.0)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["stop", "supersede"]
    assert rig.turns() == ["你是谁?"]


def test_a_sound_while_she_is_silent_is_an_ordinary_utterance(tmp_path: Path) -> None:
    """Nothing to yield: no gain change, and his words are a turn (a lone hum is ADR 0102's)."""
    rig = _Rig(tmp_path, "你是谁?", speaking=False)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["supersede"]
    assert rig.turns() == ["你是谁?"]


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


def _block(player: voice_tts.AudioStreamPlayer, frames: int) -> np.ndarray:
    out = np.zeros((frames, 1), dtype=np.float32)
    player._callback(out, frames, None, None)  # noqa: SLF001
    return out[:, 0]


def test_a_held_answer_keeps_its_place_and_fades_back_in() -> None:
    """Held, nothing of the answer is consumed; let go, it goes on from the held sample."""
    player = _player()
    lease = player.activate_generation(
        session_id="S", response_id="R1", response_group_id="G", turn_id="T",
    )
    assert isinstance(lease, GenerationLease)
    generation = lease.playback_generation_id
    player.begin_generation_segment(
        expected_playback_generation_id=generation, sequence=0, text="words", segment_hash="h",
    )
    words = np.linspace(0.1, 0.9, 512, dtype=np.float32)
    player.write_generation(
        words.tobytes(), expected_playback_generation_id=generation, segment_sequence=0,
    )
    assert np.array_equal(_block(player, 64), words[:64])
    player.pause_generation(paused=True)
    _block(player, 64)
    _block(player, 64)  # the decay to silence
    assert not _block(player, 64).any()
    player.pause_generation(paused=False)
    back = np.concatenate([_block(player, 64) for _ in range(3)])
    assert back[0] == 0.0  # faded in, not stepped onto the waveform
    assert np.array_equal(back[128:], words[192:256])


def test_a_later_answer_never_starts_held() -> None:
    """A hold names the answer it held: the next one plays at once, whole."""
    player = _player(ring_seconds=0.02)
    first, _ = _speak(player, "R1")
    player.pause_generation(paused=True)
    _block(player, 8)
    player.interrupt_generation(expected_playback_generation_id=first)
    player.settle_interrupted_generation(expected_playback_generation_id=first)
    player.retire_generation(first)
    _, block = _speak(player, "R2")
    assert np.all(block == 1.0)


def test_words_under_her_barge_in_yield_still_count_as_heard() -> None:
    """Live 2026-10-02: Allen heard 「四」 at the yield gain; a fade to silence is not heard."""
    from jarvis.surface.voice_tts import _GainRamp  # noqa: PLC0415

    ramp = _GainRamp()
    ramp.set_target(0.2, 480)  # the yield
    assert ramp.apply(np.ones(1024, dtype=np.float32)) == "normal"
    assert ramp.apply(np.ones(1024, dtype=np.float32)) == "normal"
    ramp.set_target(0.0, 480)  # the hold's fade-out
    assert ramp.apply(np.ones(1024, dtype=np.float32)) == "attenuated"
