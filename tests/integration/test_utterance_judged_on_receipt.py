"""ADR 0277: the brain judges a terminal's words as it receives them, in one round trip.

Three levels. The brain's end over a real ``/terminal/ws`` (what it judges, what it writes, what
it ends first); the terminal's end against a brain played by hand (what a reply means); and the
capture session of the soft barge-in rig sending to a brain a fixed latency away, where the time
from the final transcript to the send, and the calls made in between, are the number.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import numpy as np
import pytest

from jarvis.runtime import terminal as terminal_module
from jarvis.surface import voice_artifact_store, voice_asr, voice_pipeline
from jarvis.surface.terminal_listen import BrainListening, LinkedTurn, ListenHooks
from jarvis.surface.voice_pipeline import UtteranceJudgedError
from tests.support.barge_in import LONG, SHORT, BargeInRig
from tests.support.terminal_listen import (
    ListenBench,
    ask_op,
    connect_listening,
    listening_client,
    logged_utterances,
    ok_reply,
    utterance_frame,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jarvis.surface.terminal_speaker import VoiceLink

LATENCY_S = 0.1
"""One tailnet round trip, as the fake brain charges it for every call that waits."""


# --- the brain's end, over a real websocket --------------------------------------------------


def _judged(turn: str, said: str, *, conversation: bool = True, over_her: bool = False) -> Any:  # noqa: ANN401
    return utterance_frame(turn, said, judge={"conversation": conversation, "over_her": over_her})


def _send(
    tmp_path: Path, args: dict[str, Any], **hooks: Any,  # noqa: ANN401 - whichever hooks a test binds
) -> tuple[dict[str, Any], Path]:
    client, _hub, log = listening_client(tmp_path, **hooks)
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "utterance", args, "a" * 32)
        return ws.receive_json(), log


def test_a_turn_replaces_the_unspoken_answer_before_its_row_and_starts_jev_once(
    tmp_path: Path,
) -> None:
    """The supersede goes first (ADR 0053); the one Jev request starts as the words arrive."""
    seen: list[tuple[Any, ...]] = []
    log_path: list[Path] = []

    def rows() -> int:
        return len(logged_utterances(log_path[0]))

    def supersede(device: str, turn_id: str) -> None:
        seen.append(("supersede", device, turn_id, rows()))

    client, _hub, log = listening_client(
        tmp_path, supersede=supersede, recent_speech=lambda: "她刚说过的话",
        begin_line=lambda *line: seen.append(("begin", *line)),
        ask_words=lambda *line: seen.append(("ask", *line)),
    )
    log_path.append(log)
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "utterance", _judged("T1", "把灯打开。"), "a" * 32)
        reply = ws.receive_json()
    assert reply["ok"] is True
    assert reply["value"]["verdict"] == "turn"
    [(uid, node, _payload)] = logged_utterances(log)
    assert (uid, node) == (reply["value"]["event_uid"], "macbook")
    assert seen == [
        ("begin", "T1", "把灯打开。", "她刚说过的话", False, True),
        ("ask", "T1", "把灯打开。", "她刚说过的话", False, False),
        ("supersede", "macbook", "T1", 0),  # no row yet
    ]


@pytest.mark.parametrize(
    ("said", "conversation", "over_her", "verdict"),
    [
        ("嗯嗯。", True, False, "backchannel"),
        ("等我一下。", True, False, "wait"),
        ("嗯嗯。", False, True, "backchannel"),
        ("停。", False, True, "stop"),
    ],
)
def test_words_that_are_no_turn_write_nothing_and_supersede_nothing(
    tmp_path: Path, said: str, conversation: bool, over_her: bool, verdict: str,  # noqa: FBT001
) -> None:
    """The reply names the verdict; no row, no supersede, no cancel, no Jev request."""
    seen: list[str] = []
    reply, log = _send(
        tmp_path, _judged("T1", said, conversation=conversation, over_her=over_her),
        supersede=lambda *_: seen.append("supersede"), cancel_runs=lambda *_: seen.append("cancel"),
        begin_line=lambda *_: seen.append("begin"),
    )
    assert reply["value"] == {"verdict": verdict}
    assert logged_utterances(log) == []
    assert seen == []


def test_a_dismissal_ends_every_answer_before_it_replies(tmp_path: Path) -> None:
    """退下 is no turn; the answers being written end, then the unspoken ones, all in the reply."""
    seen: list[tuple[str, ...]] = []
    reply, log = _send(
        tmp_path, _judged("T1", "退下。"),
        cancel_runs=lambda device: seen.append(("cancel", device)),
        supersede=lambda device, turn: seen.append(("supersede", device, turn)),
    )
    assert reply["value"] == {"verdict": "dismissed"}
    assert logged_utterances(log) == []
    assert seen == [("cancel", "macbook"), ("supersede", "macbook", "T1")]


def test_a_quiet_phrase_sets_no_quiet_here_but_ends_the_answers_like_a_dismissal(
    tmp_path: Path,
) -> None:
    """The quiet level is the terminal's to set; the brain recognizes the phrase, then ends runs."""
    seen: list[str] = []
    reply, _log = _send(
        tmp_path, _judged("T1", "安静一点", conversation=False),
        set_quiet=lambda level: seen.append(f"set {level}"),
        cancel_runs=lambda *_: seen.append("cancel"), supersede=lambda *_: seen.append("supersede"),
    )
    assert reply["value"]["verdict"].startswith("quiet:")
    assert seen == ["cancel", "supersede"]


def test_a_quiet_phrase_is_only_recognized_where_it_can_be_set(tmp_path: Path) -> None:
    """No set_quiet on the brain: the phrase is words, as on a Mac with no quiet mode."""
    reply, _log = _send(tmp_path, _judged("T1", "安静一点", conversation=False))
    assert reply["value"]["verdict"] == "turn"


def test_words_another_device_recorded_first_are_dropped_before_anything_else(
    tmp_path: Path,
) -> None:
    """ADR 0219: asked first, by this terminal's name, costing no Jev request and no supersede."""
    seen: list[tuple[str, ...]] = []

    def elsewhere(device: str, turn_id: str, text: str) -> bool:
        seen.append(("elsewhere", device, turn_id, text))
        return True

    reply, log = _send(
        tmp_path, _judged("T1", "把灯打开。", conversation=False), heard_elsewhere=elsewhere,
        supersede=lambda *_: seen.append(("supersede",)),
        begin_line=lambda *_: seen.append(("begin",)),
        ask_words=lambda *_: seen.append(("ask",)),
    )
    assert reply["value"] == {"verdict": "elsewhere"}
    assert logged_utterances(log) == []
    assert seen == [("elsewhere", "macbook", "T1", "把灯打开。")]


def test_jev_confirms_a_dismissal_found_only_inside_a_sentence(tmp_path: Path) -> None:
    """ADR 0130: with Jev bound, a loose dismissal needs its yes; its no leaves a turn."""
    for choice, verdict in (("dismiss", "dismissed"), (None, "turn")):
        (tmp_path / str(choice)).mkdir()
        reply, _log = _send(
            tmp_path / str(choice), _judged("T1", "我让你退一下。"),
            ask_words=lambda *_line, answer=choice: answer,
        )
        assert reply["value"]["verdict"] == verdict


def test_a_judge_that_fails_leaves_the_words_a_turn_and_the_supersede_does_not_lose_them(
    tmp_path: Path,
) -> None:
    """Neither Jev nor the other device's log nor the supersede can cost the owner's words."""

    def boom(*_args: object) -> Any:  # noqa: ANN401
        msg = "down"
        raise RuntimeError(msg)

    reply, log = _send(
        tmp_path, _judged("T1", "把灯打开。"), ask_words=boom, heard_elsewhere=boom,
        recent_speech=boom, begin_line=boom, supersede=boom,
    )
    assert reply["value"]["verdict"] == "turn"
    assert len(logged_utterances(log)) == 1


def test_a_flag_that_is_not_true_or_false_is_refused_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    """The judge context is checked like every other argument."""
    reply, log = _send(
        tmp_path, utterance_frame("T1", judge={"conversation": "yes", "over_her": False}),
    )
    assert (reply["ok"], reply["code"]) == (False, "bad_args")
    assert logged_utterances(log) == []


# --- the terminal's end, against a brain played by hand -------------------------------------


@pytest.mark.parametrize(
    ("value", "verdict"),
    [
        ({"verdict": "dismissed"}, "dismissed"),
        ({"verdict": "elsewhere"}, "elsewhere"),
        ({"verdict": "turn", "event_uid": "a" * 32}, None),
        ({"event_uid": "a" * 32}, None),  # a brain that predates the judge writes a turn
    ],
)
def test_the_terminal_reads_a_verdict_or_the_event_from_the_reply(
    tmp_path: Path, value: dict[str, Any], verdict: str | None,
) -> None:
    """A verdict other than turn is the pipeline's to act on; an event, or none named, is a turn."""
    bench = ListenBench(tmp_path, lambda _frame: ok_reply(value))
    try:
        turn = LinkedTurn(bench.link)
        args = {"transcript": "hi", "turn_id": "T1", "judge": {"conversation": True,
                                                                "over_her": False}}
        if verdict is None:
            event = turn.emit_utterance(args, {"turn_id": "T1"})
            assert (event.event_uid, event.type) == ("a" * 32, "utterance.received")
        else:
            with pytest.raises(UtteranceJudgedError) as judged:
                turn.emit_utterance(args, {"turn_id": "T1"})
            assert judged.value.verdict == verdict
        assert bench.ops() == ["utterance"]  # the one call; the judge context rode in it
        assert bench.frames[0]["args"]["judge"] == {"conversation": True, "over_her": False}
    finally:
        bench.close()


# --- the capture session, against a brain a round trip away ----------------------------------


class _Brain:
    """A brain ``latency_s`` away from a terminal: each ask waits one round trip, a tell none.

    It serves the asks through the real :class:`BrainListening`, the judge included, and records
    every call with the time it was made and what the brain itself did while serving it.
    """

    def __init__(self, latency_s: float, **hooks: Any) -> None:  # noqa: ANN401 - the hooks a test binds
        self.latency_s = latency_s
        self.calls: list[tuple[float, str]] = []
        self.done: list[str] = []  # what the brain did, in order: hooks and rows
        self.rows: list[dict[str, Any]] = []
        self.listening = BrainListening(MagicMock(), MagicMock())
        self.listening.hooks = ListenHooks(
            supersede=lambda device, turn: self.done.append(f"supersede {device} {turn}"),
            cancel_runs=lambda device: self.done.append(f"cancel_runs {device}"),
            heard_elsewhere=hooks.pop("heard_elsewhere", None),
            ask_words=hooks.pop("ask_words", lambda *_line: None),
            recent_speech=lambda: "",
            **hooks,
        )

    def ops(self) -> list[str]:
        return [op for _at, op in self.calls]

    def at(self, op: str) -> float:
        return next(at for at, name in self.calls if name == op)

    def ask(self, op: str, args: Mapping[str, Any], *, timeout_s: float) -> Any:  # noqa: ANN401
        del timeout_s
        self.calls.append((time.monotonic(), op))
        time.sleep(self.latency_s)
        if op != "utterance":
            return self.listening._run(op, "macbook", args)  # noqa: SLF001
        judge = args.get("judge")
        verdict = "turn"
        if isinstance(judge, dict):
            verdict = self.listening._judge("macbook", args, judge)  # noqa: SLF001
        if verdict != "turn":
            return {"verdict": verdict}
        self.done.append("row")
        self.rows.append(dict(args))
        return {"verdict": "turn", "event_uid": uuid.uuid4().hex}

    def tell(self, op: str, args: Mapping[str, Any]) -> None:
        self.calls.append((time.monotonic(), op))
        self.listening._ops[op](args)  # noqa: SLF001


def _stamp_final_asr(rig: BargeInRig) -> dict[str, float]:
    """The monotonic time the final transcript is ready, for the delay to the send."""
    stamps: dict[str, float] = {}
    recognize = rig.asr.recognize

    def stamped(audio: bytes) -> voice_asr.TranscriptionResult:
        result = recognize(audio)
        stamps["asr"] = time.monotonic()
        return result

    rig.asr.recognize = stamped  # type: ignore[method-assign,assignment]
    return stamps


def _on_send_rig(tmp_path: Path, brain: _Brain, heard: str, **kwargs: Any) -> BargeInRig:  # noqa: ANN401
    """The wiring of a terminal: nothing is asked before the send, the brain judges (ADR 0277)."""
    turn = LinkedTurn(cast("VoiceLink", brain))
    return BargeInRig(
        tmp_path, heard, emit=turn.emit_utterance, judged_on_send=True, **kwargs,
    )


def _serial_rig(tmp_path: Path, brain: _Brain, heard: str, **kwargs: Any) -> BargeInRig:  # noqa: ANN401
    """The wiring before ADR 0277: each judgement a call to the brain, one after another."""

    def words(turn: str, text: str, recent: str, over_her: bool, confirm: bool) -> str | None:  # noqa: FBT001
        value = brain.ask("words", {"turn_id": turn, "text": text, "recent": recent,
                                    "over_her": over_her, "confirm": confirm}, timeout_s=3.0)
        return cast("str | None", value["choice"])

    def note(turn: str, verdict: str, text: str, over_her: bool, conversation: bool) -> None:  # noqa: FBT001
        brain.tell("note", {"turn_id": turn, "verdict": verdict, "text": text,
                            "over_her": over_her, "conversation": conversation})

    def begin(turn: str, text: str, recent: str, over_her: bool, conversation: bool) -> None:  # noqa: FBT001
        brain.tell("begin", {"turn_id": turn, "text": text, "recent": recent,
                             "over_her": over_her, "conversation": conversation})

    return BargeInRig(
        tmp_path, heard, emit=LinkedTurn(cast("VoiceLink", brain)).emit_utterance,
        recent_speech=lambda: brain.ask("recent", {}, timeout_s=1.0)["text"],
        ask_words=words, note_words=note, begin_line=begin,
        heard_elsewhere=lambda turn, text: brain.ask(
            "elsewhere", {"turn_id": turn, "text": text}, timeout_s=0.3,
        )["elsewhere"],
        supersede_unspoken=lambda turn: brain.ask("supersede", {"turn_id": turn}, timeout_s=5.0),
        **kwargs,
    )


def test_the_utterance_leaves_at_the_final_transcript_and_is_the_only_call_that_waits(
    tmp_path: Path,
) -> None:
    """A turn in conversation mode: one call, sent at once, judged and written in its round trip."""
    brain = _Brain(LATENCY_S)
    rig = _on_send_rig(tmp_path, brain, "把灯打开。", speaking=False)
    stamps = _stamp_final_asr(rig)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert brain.ops() == ["utterance"]  # every wait is a brain call, so nothing else waited
    assert stamps["asr"] <= brain.at("utterance")
    assert brain.done == ["supersede macbook " + brain.rows[0]["turn_id"], "row"]
    assert brain.rows[0]["transcript"] == "把灯打开。"
    assert ("accepted", None) in rig.phases
    assert "supersede" not in rig.output  # the session asked for none


def test_before_it_each_judgement_was_a_call_that_waited_and_the_send_came_after_all_of_them(
    tmp_path: Path,
) -> None:
    """The same turn on the wiring of ADR 0172: the cost the test above must not come back to."""
    brain = _Brain(LATENCY_S)
    rig = _serial_rig(tmp_path, brain, "把灯打开。", speaking=False)
    stamps = _stamp_final_asr(rig)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    waiting = [op for op in brain.ops() if op in {"elsewhere", "recent", "words", "supersede"}]
    assert len(waiting) >= 4
    assert brain.ops()[-1] == "utterance"
    assert brain.at("utterance") - stamps["asr"] >= 3 * LATENCY_S


def test_a_dismissal_is_judged_in_the_same_call_and_carried_out_without_another(
    tmp_path: Path,
) -> None:
    """ADR 0102 over her: she stops, the mode ends, the goodbye is asked; the brain ended runs."""
    brain = _Brain(LATENCY_S)
    rig = _on_send_rig(tmp_path, brain, "好, 退下吧。", speaking=True)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert brain.ops() == ["utterance"]
    assert brain.done[0].startswith("cancel_runs macbook")
    assert brain.rows == []
    assert "stop" in rig.output
    assert "cancel runs" not in rig.output  # the brain did, inside the call
    assert rig.conversation_changes == [(False, "dismissed")]
    assert rig.answers == ["dismissed"]
    assert ("empty", "dismissed") in rig.phases


def test_a_listening_sound_over_her_lets_her_go_on_and_is_no_turn(tmp_path: Path) -> None:
    """Soft barge-in (ADR 0216 for the phone, the Mac's own for this): the gain comes back."""
    brain = _Brain(LATENCY_S)
    rig = _on_send_rig(tmp_path, brain, "嗯嗯。")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert brain.rows == []
    assert brain.done == []
    assert ("empty", "backchannel") in rig.phases


def test_other_words_over_her_stop_her_and_are_a_turn_that_replaced_the_unspoken_answer(
    tmp_path: Path,
) -> None:
    """The stop comes with the verdict; the brain superseded before writing the row."""
    brain = _Brain(LATENCY_S)
    rig = _on_send_rig(tmp_path, brain, "给我讲个故事。")
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert "stop" in rig.output
    assert brain.done == ["supersede macbook " + brain.rows[0]["turn_id"], "row"]
    assert brain.ops() == ["utterance"]
    assert brain.rows[0]["judge"] == {"conversation": True, "over_her": True}


def test_words_another_device_recorded_first_let_her_go_on_and_are_no_turn(
    tmp_path: Path,
) -> None:
    """ADR 0219: the brain says elsewhere in the same call; she goes on from where she was."""
    brain = _Brain(LATENCY_S, heard_elsewhere=lambda *_: True)
    rig = _on_send_rig(tmp_path, brain, "把灯打开。")
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert brain.ops() == ["utterance"]
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert brain.rows == []
    assert ("empty", "elsewhere") in rig.phases


def test_a_voice_terminal_is_wired_to_send_first() -> None:
    """The production wiring: nothing the judge reaches is asked from this side."""
    ports = terminal_module._terminal_ports(LinkedTurn(MagicMock()))  # noqa: SLF001
    assert ports.judged_on_send
    for judge_port in (
        "ask_words", "note_words", "begin_line", "recent_speech", "heard_elsewhere",
        "supersede_unspoken",
    ):
        assert getattr(ports, judge_port) is None, judge_port


# --- the audio file is not on the way ------------------------------------------------------


class _Recognizer:
    def recognize(self, audio: bytes) -> voice_asr.TranscriptionResult:
        del audio
        return voice_asr.TranscriptionResult(
            text="把灯打开。", confidence=0.9, language_detected=None, emotion=None, event=None,
        )


def test_the_audio_is_written_after_the_utterance_is_sent_under_the_name_it_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event names the file; a slow encoder costs the send nothing (ADR 0277)."""
    order: list[str] = []
    sent: list[Mapping[str, object]] = []

    def slow_persist(
        pcm_audio: bytes, *, turn_id: str, sample_rate_hz: int, artifacts_dir: Path | None,
    ) -> str | None:
        del pcm_audio, sample_rate_hz
        assert artifacts_dir is not None
        time.sleep(0.3)
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        path = Path(str(voice_artifact_store.planned_ref(turn_id, artifacts_dir)))
        path.write_bytes(b"audio")
        order.append("persisted")
        return str(path)

    def emit(payload: Mapping[str, object], _correlation: Mapping[str, str]) -> Any:  # noqa: ANN401
        sent.append(dict(payload))
        order.append("sent")
        return MagicMock(event_uid="e" * 32)

    monkeypatch.setattr(voice_artifact_store, "persist", slow_persist)
    pipeline = voice_pipeline.VoicePipeline(
        emit=emit, recognizer=_Recognizer(),
        normalizer=voice_asr.AsrNormalizer(corrections=[], aliases={}, fuzzy_enabled=False),
        broadcaster=None, artifacts_dir=tmp_path / "audio",
    )
    started = time.monotonic()
    pipeline.run_turn(
        audio_bytes=np.full(16_000, 10_000, dtype="<i2").tobytes(), turn_id="T1",
        channel="inherent_wake", language="zh-CN", lock_already_held=False, wake_lead=False,
    )
    assert time.monotonic() - started < 0.25
    assert order == ["sent"]
    voice_artifact_store.flush()
    assert order == ["sent", "persisted"]
    ref = Path(str(sent[0]["audio_artifact_ref"]))
    assert (ref.parent, ref.stem) == (tmp_path / "audio", "T1")
    assert json.dumps(sent[0])  # the payload still crosses the wire as JSON
    assert ref.read_bytes() == b"audio"  # the file is there under the name the event carried


_FAKE_AFCONVERT = '#!/bin/sh\n# afconvert -f m4af -d aac -b 32000 <in> <out>\n/bin/cp "$7" "$8"\n'


@pytest.mark.parametrize("encoder", [False, True], ids=["no-afconvert", "afconvert"])
def test_the_name_the_utterance_carries_is_the_file_that_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, encoder: bool,
) -> None:
    """With the AAC encoder the audio is an .m4a, without it (Pi) a .wav; the ref is the file."""
    tools = tmp_path / "tools"
    tools.mkdir()
    if encoder:
        script = tools / "afconvert"
        script.write_text(_FAKE_AFCONVERT)
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools))  # whichever encoder this host has is not on it
    folder = tmp_path / "audio"
    ref = voice_artifact_store.planned_ref("T1", folder)
    voice_artifact_store.persist_later(
        np.full(1_600, 10_000, dtype="<i2").tobytes(),
        turn_id="T1", sample_rate_hz=16_000, artifacts_dir=folder,
    )
    voice_artifact_store.flush()
    assert ref == str(folder / ("T1.m4a" if encoder else "T1.wav"))
    assert sorted(p.name for p in folder.iterdir()) == [Path(str(ref)).name]
