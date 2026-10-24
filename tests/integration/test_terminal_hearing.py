"""ADR 0400: a voice terminal's words are heard by Qwen through the brain.

Against the real code with Qwen's connection and SenseVoice replaced: the ``hear`` frames over
the real terminal socket come back as Qwen's words and say whether the brain hears; the
terminal's recognizer streams the utterance as it is captured and the commit takes Qwen's words
for all of it; without Qwen's words SenseVoice's stand and the utterance says why.
"""

from __future__ import annotations

# ruff: noqa: SLF001 - the VAD's thresholds are set where the hybrid's caption test sets them
import base64
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import open_event_log
from jarvis.surface import voice_audio, voice_pipeline, voice_session
from jarvis.surface.terminal_hearing import (
    FRAME_BYTES,
    BrainHearing,
    CloudFinalRecognizer,
    QwenEars,
    RemoteHearing,
)
from tests.support.hearing import FakeQwen, HearingBench, SenseVoiceStandIn
from tests.support.terminal import TERMINAL_WS, bearer, brain_client, hello_frame
from tests.support.voice_input import (
    FakeBackend,
    FakeWakeEngine,
    StatefulEnergySession,
    make_ingress,
    wait_until_true,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.surface.voice_asr import TranscriptionResult

SID = "c" * 32
A, B, C = (letter * FRAME_BYTES for letter in (b"a", b"b", b"c"))
_FRAME_BYTES = voice_audio.SILERO_CHUNK_SAMPLES * 2
_HANG_GUARD_S = 15.0
"""How long a wait on the session's threads lasts before the test calls it hung; not a budget."""


@pytest.mark.parametrize("hears", [True, False])
def test_ready_says_whether_the_brain_hears(tmp_path: Path, *, hears: bool) -> None:
    """A voice terminal's ``ready`` carries ``hear``: true only when the brain has Qwen."""
    client, hub, _log = brain_client(tmp_path)
    if hears:
        hub.hearing = BrainHearing(QwenEars("key", connect=FakeQwen().connect))
    with client.websocket_connect(TERMINAL_WS, headers=bearer(pair_device(tmp_path, "mac"))) as ws:
        ws.send_text(hello_frame(voice=True))
        assert ws.receive_json()["hear"] is hears


def test_a_stream_over_the_real_socket_comes_back_as_qwens_words(tmp_path: Path) -> None:
    """open, audio, finish in; the words so far and the final words out, under one ``sid``."""
    qwen = FakeQwen()
    client, hub, _log = brain_client(tmp_path)
    hub.hearing = BrainHearing(QwenEars("key", connect=qwen.connect))
    with client.websocket_connect(TERMINAL_WS, headers=bearer(pair_device(tmp_path, "mac"))) as ws:
        ws.send_text(hello_frame(voice=True))
        ws.receive_json()
        for frame in (
            {"op": "open", "terms": ["Jarvis", "pi", "Jarvis", " "]},
            {"op": "audio", "pcm": base64.b64encode(b"135E").decode()},
            {"op": "finish"},
        ):
            ws.send_text(json.dumps({"type": "hear", "sid": SID, **frame}))
        assert ws.receive_json() == {"type": "hear", "sid": SID, "op": "text", "text": "135E"}
        assert ws.receive_json() == {"type": "hear", "sid": SID, "op": "done", "text": "135E"}
    [task] = qwen.tasks
    assert task.parameters["vocabulary"] == {"Jarvis": 4}  # a two-letter term is left out
    assert task.audio == b"135E"


def _heard(
    tmp_path: Path, qwen: FakeQwen, pieces: tuple[bytes, ...], *, hears: bool = True,
) -> TranscriptionResult:
    bench = HearingBench(tmp_path, qwen, hears=hears)
    try:
        recognizer = CloudFinalRecognizer(
            sensevoice=SenseVoiceStandIn(),  # type: ignore[arg-type]
            hearing=RemoteHearing(bench.link),
            terms=lambda: ["Jarvis"],
        )
        for piece in pieces:
            recognizer.stream("U1", piece)
        return recognizer.recognize_prepared("U1", b"".join(pieces), 0.3)
    finally:
        bench.close()


def test_the_commit_takes_qwens_words_for_the_whole_utterance(tmp_path: Path) -> None:
    """Capture frames of any size reach one Qwen task in order, and its words are the turn's."""
    qwen = FakeQwen()
    heard = _heard(tmp_path, qwen, (A, B[:100], B[100:], C[:7]))
    assert (heard.text, heard.asr_engine) == ((A + B + C[:7]).decode() + ".", "qwen")
    [task] = qwen.tasks
    assert (bytes(task.audio), task.finished) == (A + B + C[:7], True)


def test_the_captions_are_qwens_words_from_the_first_it_brings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """ADR 0402. Conversation mode, real session, pipeline and link: Qwen's words are the captions.

    SenseVoice's snapshots say 森; Qwen's words so far say how many frames it has heard. From
    Qwen's first words on, the captions are Qwen's, growing, and SenseVoice's never come back.
    """
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS, "record", voice_audio.VadThresholds(0.4, -45.0, 1, 1, 40),
    )
    bench = HearingBench(
        tmp_path, FakeQwen(running=True, words=lambda audio: f"千问{len(audio) // _FRAME_BYTES}"),
    )
    pipeline = voice_pipeline.VoicePipeline(
        conn_factory=lambda: open_event_log(tmp_path / "events.db"),
        recognizer=CloudFinalRecognizer(
            sensevoice=SenseVoiceStandIn(),  # type: ignore[arg-type]
            hearing=RemoteHearing(bench.link),
            terms=list,
        ),
        broadcaster=None,
        artifacts_dir=None,
    )
    broadcaster = MagicMock()
    backend = FakeBackend()
    ingress = make_ingress(backend)
    with patch.object(voice_audio, "_load_silero_session", return_value=StatefulEnergySession()):
        session = voice_session.DuplexVoiceSession(
            ingress=ingress,
            wake_engine=FakeWakeEngine(detections=set()),
            vad=voice_audio.SileroVad(mode="record"),
            pipeline=pipeline,
            broadcaster=broadcaster,
            output_active=lambda: False,
            wake_threshold=0.5,
            config=replace(
                voice_session.RealtimeInputSessionConfig(),
                pre_roll_ms=64,
                min_voiced_s=0.032,
                max_utterance_s=10.0,
                worker_poll_s=0.001,
                shutdown_timeout_s=1.0,
                partial_asr=voice_session.PartialAsrConfig(captions=True, interval_ms=32),
            ),
            conversation=lambda: True,
        )
        assert session.start().started
    epoch = ingress.stream_epoch
    assert epoch is not None

    def captions() -> list[str]:
        return [
            call.kwargs["text"] for call in broadcaster.broadcast_voice_sync.call_args_list
            if call.args[0] == "partial"
        ]

    def frames_heard() -> list[int]:
        return [int(text.removeprefix("千问")) for text in captions() if text.startswith("千问")]

    def speak_until(enough: Callable[[list[int]], bool]) -> Callable[[], bool]:
        def spoke() -> bool:  # the captions move on the microphone's frames, one per poll
            backend.emit(epoch=epoch, value=10_000)
            return enough(frames_heard())

        return spoke

    try:
        for _ in range(4):
            backend.emit(epoch=epoch, value=0)  # armed and idle: the pre-roll
        wait_until_true(speak_until(bool), timeout_s=_HANG_GUARD_S)
        first = frames_heard()[0]
        wait_until_true(speak_until(lambda heard: heard[-1] >= first + 8), timeout_s=_HANG_GUARD_S)
    finally:
        assert session.close().definitively_closed
        bench.close()

    shown = captions()
    qwens = shown[next(i for i, text in enumerate(shown) if text.startswith("千问")):]
    assert all(text.startswith("千问") for text in qwens)
    assert frames_heard() == sorted(frames_heard())


@pytest.mark.parametrize(
    ("hears", "fails", "pieces", "engine"),
    [
        (False, False, (A,), "qwen_unavailable"),
        (True, True, (A,), "qwen_error"),
        (True, False, (b"",), "qwen_empty"),
    ],
    ids=["the brain does not hear", "qwen fails", "qwen writes nothing"],
)
def test_without_qwens_words_sensevoices_stand_and_say_why(
    tmp_path: Path, *, hears: bool, fails: bool, pieces: tuple[bytes, ...], engine: str,
) -> None:
    """SenseVoice's words and score stand, and ``asr_engine`` names why Qwen's did not."""
    heard = _heard(tmp_path, FakeQwen(fails=fails), pieces, hears=hears)
    assert (heard.text, heard.asr_engine, heard.sensevoice_confidence) == ("森", engine, 0.9)
