"""ADR 0400: a voice terminal's words are heard by Qwen through the brain.

Against the real code with Qwen's connection and SenseVoice replaced: the ``hear`` frames over
the real terminal socket come back as Qwen's words and say whether the brain hears; the
terminal's recognizer streams the utterance as it is captured and the commit takes Qwen's words
for all of it; without Qwen's words SenseVoice's stand and the utterance says why.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest

from jarvis.state.device_tokens import pair_device
from jarvis.surface.terminal_hearing import (
    FRAME_BYTES,
    BrainHearing,
    CloudFinalRecognizer,
    QwenEars,
    RemoteHearing,
)
from tests.support.hearing import FakeQwen, HearingBench, SenseVoiceStandIn
from tests.support.terminal import TERMINAL_WS, bearer, brain_client, hello_frame

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.surface.voice_asr import TranscriptionResult

SID = "c" * 32
A, B, C = (letter * FRAME_BYTES for letter in (b"a", b"b", b"c"))


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
