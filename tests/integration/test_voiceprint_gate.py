"""ADR 0286: a terminal sends a voiceprint, and the brain turns away a long utterance not his.

Four levels, all hermetic. The brain's end over a real ``/terminal/ws`` and a real ``/phone/ws``
with a seeded profile and synthetic embeddings of a chosen cosine to it (the verdict, what is
written, what is learned, what is logged); the capture session of a Mac running alone, which
judges before emitting; the pipeline that computes the voiceprint beside the final ASR; and the
seeding CLI. The speaker model itself is exercised only where its file is on this machine.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import sqlite3
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from jarvis.cli import main as cli_main
from jarvis.deployment.models import default_voiceprint_model_path
from jarvis.runtime import _voiceprint_gate
from jarvis.shared import Event
from jarvis.state import voiceprint as voiceprint_module
from jarvis.state.voiceprint import DIMENSIONS, VoiceprintBook
from jarvis.surface import voice_asr, voice_pipeline
from jarvis.surface.terminal_events import phone_turn_id
from jarvis.surface.voice_print import VoiceprintExtractor, load_extractor
from tests.support.barge_in import LONG
from tests.support.barge_in import BargeInRig as _MacRig
from tests.support.phone import TURN_WORDS, open_ws, say_words
from tests.support.phone import JudgeRig as _PhoneRig
from tests.support.terminal_listen import (
    ask_op,
    connect_listening,
    listening_client,
    logged_utterances,
    utterance_frame,
)

if TYPE_CHECKING:
    from collections.abc import Callable

MIC = "reSpeaker XVF3800 4-Mic Array"
OTHER_MIC = "Allen's AirPods Pro"


def _axis(index: int) -> np.ndarray:
    vector = np.zeros(DIMENSIONS, dtype=np.float32)
    vector[index] = 1.0
    return vector


def _tone(seconds: float) -> bytes:
    """Loud enough that the pipeline takes it for speech."""
    t = np.arange(int(16_000 * seconds)) / 16_000
    return bytes((np.sin(2 * np.pi * 220 * t) * 8_000).astype("<i2").tobytes())


def _at(cosine: float, *, away: int = 1) -> np.ndarray:
    """A unit vector whose cosine with his profile (axis 0) is ``cosine``."""
    return _axis(0) * np.float32(cosine) + _axis(away) * np.float32(math.sqrt(1 - cosine**2))


def _wire(
    cosine: float, *, seconds: float = 2.5, mic_age_s: float = 300.0, mic: str = MIC,
    away: int = 1,
) -> dict[str, Any]:
    return {
        "embedding": [round(float(one), 5) for one in _at(cosine, away=away)],
        "seconds": seconds, "mic": mic, "mic_age_s": mic_age_s,
    }


def _book(tmp_path: Path, *, gate: bool = True, seed: bool = True) -> VoiceprintBook:
    """His profile on the reSpeaker of ``macbook``, as the offline study seeds it."""
    book = VoiceprintBook(tmp_path / "memory.db", gate=lambda: gate)
    if seed:
        book.seed("macbook", MIC, 300, _axis(0).tolist())
    return book


def _rows(book: VoiceprintBook) -> dict[tuple[str, str], int]:
    return {(one.device, one.mic): one.rows for one in book.status()}


def _terminal_judge(
    tmp_path: Path, book: VoiceprintBook, voiceprint: dict[str, Any] | None, *,
    said: str = "把灯打开。", turn: str = "T1", **hooks: Any,  # noqa: ANN401 - whichever hooks
) -> tuple[dict[str, Any], Path]:
    """One judged utterance of ``macbook`` over the real link: the reply and the brain's log."""
    client, _hub, log = listening_client(tmp_path, voiceprints=book, **hooks)
    extra: dict[str, object] = {"judge": {"conversation": False, "over_her": False}}
    if voiceprint is not None:
        extra["voiceprint"] = voiceprint
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "utterance", utterance_frame(turn, said, **extra), "a" * 32)
        return ws.receive_json(), log


def _payload(log: Path) -> dict[str, Any]:
    [(_uid, _node, raw)] = logged_utterances(log)
    return dict(json.loads(raw))


# --- the brain's end: a terminal's utterance --------------------------------------------------


def test_a_long_utterance_far_from_his_profile_is_not_owner_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """Asked first: no elsewhere check, no Jev request, no supersede; the words are dropped."""
    seen: list[str] = []

    def elsewhere(*_args: object) -> bool:
        seen.append("elsewhere")
        return False

    reply, log = _terminal_judge(
        tmp_path, _book(tmp_path), _wire(0.1), heard_elsewhere=elsewhere,
        begin_line=lambda *_a: seen.append("begin"), ask_words=lambda *_a: seen.append("ask"),
        supersede=lambda *_a: seen.append("supersede"),
        cancel_runs=lambda *_a: seen.append("cancel"),
    )
    assert reply["ok"] is True
    assert reply["value"] == {"verdict": "not_owner"}
    assert logged_utterances(log) == []
    assert seen == []


# (what differs from the case above, and why it is not enough to turn him away)
NOT_ENOUGH: dict[str, dict[str, Any]] = {
    "speech under two seconds": {"seconds": 1.5},
    "a microphone open under three minutes": {"mic_age_s": 60.0},
    "a score between 0.30 and 0.50": {"cosine": 0.4},
    "a microphone with no profile": {"mic": OTHER_MIC},
}


@pytest.mark.parametrize("name", [*NOT_ENOUGH, "the gate off", "no voiceprint at all",
                                   "a voiceprint of the wrong shape"])
def test_anything_short_of_all_the_conditions_is_judged_as_it_always_was(
    tmp_path: Path, name: str,
) -> None:
    """A normal turn: the row is written, and keeps the score, the microphone and the seconds."""
    changes = dict(NOT_ENOUGH.get(name, {}))
    cosine = float(changes.pop("cosine", 0.1))
    voiceprint: dict[str, Any] | None = _wire(cosine, **changes)
    if name == "no voiceprint at all":
        voiceprint = None
    elif name == "a voiceprint of the wrong shape":
        voiceprint = {**_wire(cosine), "embedding": [0.5] * 12}
    book = _book(tmp_path, gate=name != "the gate off")
    reply, log = _terminal_judge(tmp_path, book, voiceprint)
    assert reply["value"]["verdict"] == "turn"
    payload = _payload(log)
    assert payload["transcript"] == "把灯打开。"
    assert "embedding" not in json.dumps(payload)
    if voiceprint is None or name == "a voiceprint of the wrong shape":
        assert "voiceprint" not in payload
    elif name == "a microphone with no profile":
        # no profile of its own: scored against his best other one, never turned away
        assert payload["voiceprint"] == {"score": 0.1, "mic": OTHER_MIC, "seconds": 2.5}
    else:
        kept = {"score": round(cosine, 3), "mic": MIC, "seconds": changes.get("seconds", 2.5)}
        assert payload["voiceprint"] == kept


def test_a_turn_keeps_its_score_and_every_scored_utterance_is_logged_with_its_verdict(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """INFO on the brain: device, microphone, seconds, score and verdict; never the embedding."""
    caplog.set_level(logging.INFO, logger="jarvis.state.voiceprint")
    book = _book(tmp_path)
    away, _ = _terminal_judge(tmp_path, book, _wire(0.1, away=2), turn="T1")
    (tmp_path / "again").mkdir()
    kept, _ = _terminal_judge(tmp_path / "again", book, _wire(0.6), turn="T2")
    assert (away["value"]["verdict"], kept["value"]["verdict"]) == ("not_owner", "turn")
    lines = [r.getMessage() for r in caplog.records if r.name == "jarvis.state.voiceprint"]
    assert len(lines) == 2
    assert "device=macbook" in lines[0]
    assert f"mic='{MIC}'" in lines[0]
    assert "seconds=2.5" in lines[0]
    assert "score=0.100" in lines[0]
    assert "verdict=not_owner" in lines[0]
    assert "score=0.600" in lines[1]
    assert "verdict=turn" in lines[1]
    assert "0.99" not in " ".join(lines)  # no embedding


def test_a_turn_he_owns_joins_his_profile_and_nothing_else_does(tmp_path: Path) -> None:
    """Learned: a turn of 2 s or more, on a microphone open 3 minutes, scoring 0.50 or more."""
    book = _book(tmp_path)
    assert _rows(book) == {("macbook", MIC): 1}  # the seed
    cases = [
        ("a clear turn", _wire(0.6), 2),
        ("too short", _wire(0.6, seconds=1.5), 2),
        ("a young microphone", _wire(0.6, mic_age_s=60.0), 2),
        ("unsure", _wire(0.4), 2),
        ("a stranger", _wire(0.1), 2),
        ("another clear turn", _wire(0.9), 3),
    ]
    for index, (name, voiceprint, rows) in enumerate(cases):
        folder = tmp_path / f"case{index}"
        folder.mkdir()
        _terminal_judge(folder, book, voiceprint, turn=f"T{index}")
        assert _rows(book)[("macbook", MIC)] == rows, name


def test_a_resent_utterance_is_learned_once(tmp_path: Path) -> None:
    """The row is keyed by its turn: the terminal that never saw the answer sends it again."""
    book = _book(tmp_path)
    for index in range(2):
        folder = tmp_path / f"send{index}"
        folder.mkdir()
        _terminal_judge(folder, book, _wire(0.7), turn="T1")
    assert _rows(book) == {("macbook", MIC): 2}


def test_words_that_are_no_turn_teach_no_profile(tmp_path: Path) -> None:
    """A dismissal is his voice, but not a turn: it is logged, and nothing is learned from it."""
    book = _book(tmp_path)
    client, _hub, _log = listening_client(tmp_path, voiceprints=book)
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "utterance", utterance_frame("T9", "退下。", voiceprint=_wire(0.8),
                                         judge={"conversation": True, "over_her": False}))
        assert ws.receive_json()["value"] == {"verdict": "dismissed"}
    assert _rows(book) == {("macbook", MIC): 1}  # the seed alone


def test_a_scoring_that_fails_leaves_the_words_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile cannot cost him his words."""
    book = _book(tmp_path)

    def boom(*_args: object) -> Any:  # noqa: ANN401
        msg = "down"
        raise RuntimeError(msg)

    monkeypatch.setattr(book, "score", boom)
    reply, log = _terminal_judge(tmp_path, book, _wire(0.1))
    assert reply["value"]["verdict"] == "turn"
    assert "voiceprint" not in _payload(log)


# --- profiles ----------------------------------------------------------------------------------


def test_a_new_microphone_starts_from_his_best_other_profile_and_is_ready_after_two_days(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.35 against the other profile starts it; 30 rows over two days make it ready."""
    book = _book(tmp_path)
    today = {"now": datetime(2026, 10, 14, 9, 0, tzinfo=timezone(timedelta(hours=-4)))}
    monkeypatch.setattr(voiceprint_module, "local_now", lambda: today["now"])

    def hear(cosine: float, index: int, *, away: int = 1) -> str:
        folder = tmp_path / f"h{index}"
        folder.mkdir()
        reply, _ = _terminal_judge(
            folder, book, _wire(cosine, mic=OTHER_MIC, away=away), turn=f"T{index}",
        )
        return str(reply["value"]["verdict"])

    assert hear(0.30, 0) == "turn"  # too far from his other profile to start this one
    assert hear(0.36, 1) == "turn"
    assert _rows(book)[("macbook", OTHER_MIC)] == 1
    for index in range(2, 30):
        hear(0.5, index)
    airpods = next(one for one in book.status() if one.mic == OTHER_MIC)
    assert (airpods.rows, airpods.days, airpods.ready) == (29, 1, False)
    today["now"] += timedelta(days=1)
    hear(0.5, 30)
    airpods = next(one for one in book.status() if one.mic == OTHER_MIC)
    assert (airpods.rows, airpods.days, airpods.ready) == (30, 2, True)
    # Ready, it turns away what its own profile calls a stranger, and learns above 0.50 only.
    assert hear(0.1, 31, away=2) == "not_owner"  # far from what it heard, not just from axis 0


def test_the_profile_is_the_weighted_mean_of_the_newest_rows_up_to_weight_300(
    tmp_path: Path,
) -> None:
    """A seed weighs its count, a learned row 1; the oldest rows fade out of the window."""
    book = VoiceprintBook(tmp_path / "memory.db")
    book.seed("macbook", MIC, 500, _axis(0).tolist())  # capped at 300
    probe = voiceprint_module.Voiceprint(_axis(1), 3.0, MIC, 600.0)
    assert (book.score("macbook", probe).score or 0.0) == pytest.approx(0.0, abs=1e-6)
    for index in range(300):  # 300 rows of axis 1 push the whole seed out of the window
        book.finish(
            book.score("macbook", voiceprint_module.Voiceprint(_at(0.6, away=1), 3.0, MIC, 600.0)),
            "turn", f"T{index}",
        )
    # every learned row leans 0.6 on axis 0 and 0.8 on axis 1; the seed no longer counts
    score = book.score("macbook", probe).score
    assert score == pytest.approx(0.8, abs=1e-4)


def test_a_seed_counts_as_ready_and_the_cli_reads_no_embeddings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """``voiceprint seed`` imports the study's centroids; ``status`` prints rows, days, ready."""
    study = tmp_path / "study.json"
    study.write_text(
        json.dumps({"mics": {
            "reSpeaker": {"n": 140, "centroid": _axis(0).tolist()},
            "AirPods": {"n": 40, "centroid": _axis(2).tolist()},
        }}),
        encoding="utf-8",
    )
    root = ["--runtime-root", str(tmp_path / "root")]
    names = ["--mic", f"reSpeaker={MIC}", "--mic", f"AirPods={OTHER_MIC}"]
    assert cli_main(["voiceprint", "seed", str(study), "--device", "macbook", *names, *root]) == 0
    seeded = capsys.readouterr().out
    assert f"macbook\t{MIC}\tseeded n=140" in seeded
    assert cli_main(["voiceprint", "status", *root]) == 0
    status = capsys.readouterr().out.splitlines()
    assert [line.split("\t")[:5] for line in status] == [
        ["macbook", OTHER_MIC, "rows=1", "days=1", "ready=yes"],
        ["macbook", MIC, "rows=1", "days=1", "ready=yes"],
    ]
    assert all(line.count("\t") == 5 and "last_added=20" in line for line in status)  # no vector


def test_a_seed_file_that_does_not_match_its_names_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Every key named, every name used, every centroid good, or no row at all."""
    study = tmp_path / "study.json"
    good = {"n": 10, "centroid": _axis(0).tolist()}
    study.write_text(
        json.dumps({"mics": {"a": good, "b": {"n": 10, "centroid": [1.0, 2.0]}}}), encoding="utf-8",
    )
    root = ["--runtime-root", str(tmp_path / "root")]
    for names in (["--mic", "a=A"], ["--mic", "a=A", "--mic", "b=B"]):
        assert cli_main(
            ["voiceprint", "seed", str(study), "--device", "macbook", *names, *root],
        ) == 1
        capsys.readouterr()
    assert cli_main(["voiceprint", "status", *root]) == 0
    assert capsys.readouterr().out == ""


def test_the_gate_is_a_brain_setting_that_is_on_unless_it_is_false() -> None:
    """``voiceprint.gate`` in the shipped config; a value that is not true or false is refused."""
    assert _voiceprint_gate({}) is True
    assert _voiceprint_gate({"voiceprint": {"gate": True}}) is True
    assert _voiceprint_gate({"voiceprint": {"gate": False}}) is False
    with pytest.raises(TypeError):
        _voiceprint_gate({"voiceprint": {"gate": "off"}})


# --- the phone ---------------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [{}, {"over_her": True}, {"conversation": True}])
def test_a_phone_say_that_is_not_his_voice_is_answered_not_owner_like_elsewhere(
    tmp_path: Path, flags: dict[str, bool],
) -> None:
    """Flagged or not: ``verdict: not_owner``, ``turn_id: null``, no row, no barge, no Jev."""
    rig = _PhoneRig(tmp_path)
    book = VoiceprintBook(tmp_path / "memory.db")
    book.seed("iphone", MIC, 300, _axis(0).tolist())
    rig.build(voiceprints=book, elsewhere=lambda *_a: False)
    with rig.connect() as ws:
        open_ws(ws)
        reply = say_words(ws, "u1", TURN_WORDS, voiceprint=_wire(0.1), **flags)
    assert reply == {
        "type": "said", "utterance_id": "u1", "turn_id": None, "verdict": "not_owner",
    }
    assert rig.turns() == []
    assert rig.calls == []
    assert rig.asked == []
    assert rig.elsewhere_asked == []  # asked first: the other devices' log is not read


def test_a_phone_say_that_is_his_voice_is_a_turn_that_keeps_the_score_and_teaches_the_profile(
    tmp_path: Path,
) -> None:
    """No embedding on the row; the profile grows once, however often the say is sent."""
    rig = _PhoneRig(tmp_path)
    book = VoiceprintBook(tmp_path / "memory.db")
    book.seed("iphone", MIC, 300, _axis(0).tolist())
    rig.build(voiceprints=book)
    with rig.connect() as ws:
        open_ws(ws)
        first = say_words(ws, "u1", TURN_WORDS, voiceprint=_wire(0.7))
        again = say_words(ws, "u1", TURN_WORDS, voiceprint=_wire(0.7))
        typed = say_words(ws, "t1", TURN_WORDS, spoken=False, voiceprint=_wire(0.1))
        junk = say_words(ws, "u2", TURN_WORDS, voiceprint={"embedding": "no", "mic": MIC})
    assert first["turn_id"] == again["turn_id"] == phone_turn_id("iphone", "u1")
    assert (typed["verdict"], junk["verdict"]) == ("turn", "turn")
    with sqlite3.connect(rig.log) as raw:
        rows = {
            uid: json.loads(payload) for uid, payload in raw.execute(
                "SELECT json_extract(payload_json, '$.utterance_id'), payload_json FROM events "
                "WHERE type = 'utterance.received' ORDER BY id",
            )
        }
    assert rows["u1"]["voiceprint"] == {"score": 0.7, "mic": MIC, "seconds": 2.5}
    assert "voiceprint" not in rows["u2"]
    assert "embedding" not in json.dumps(rows)
    assert _rows(book) == {("iphone", MIC): 2}  # the seed and the one clear turn


# --- a Mac running alone -----------------------------------------------------------------------


class _Embedder:
    """The speaker model's stand-in: the voiceprint it hands over, and when it was asked."""

    def __init__(self, wire: dict[str, Any]) -> None:
        self.wire = wire
        self.asked: list[dict[str, Any]] = []
        self.order: list[str] = []

    def begin(
        self, pcm16: bytes, sample_rate_hz: int, *, seconds: float, mic: str, mic_age_s: float,
    ) -> Callable[[], dict[str, Any] | None]:
        del seconds, mic_age_s
        self.order.append("embedding started")
        self.asked.append({"bytes": len(pcm16), "rate": sample_rate_hz, "mic": mic})
        wire = {**self.wire, "mic": mic}
        return lambda: wire


def _mac(
    tmp_path: Path, wire: dict[str, Any], *, gate: bool = True,
) -> tuple[_MacRig, VoiceprintBook, _Embedder, str]:
    """A Mac that judges its own words, with his profile seeded on the microphone it opens.

    The last value is that microphone's name.
    """
    book = VoiceprintBook(tmp_path / "memory.db", gate=lambda: gate)
    embedder = _Embedder(wire)
    rig = _MacRig(
        tmp_path, "把灯打开。", speaking=False, voiceprints=book,
        embedder=embedder,  # type: ignore[arg-type]
    )
    mic = rig.ingress.active_input()
    assert mic is not None
    rig.ingress._active_opened_s -= 300.0  # noqa: SLF001 - the microphone has been open a while
    book.seed("mac", mic[0], 300, _axis(0).tolist())
    return rig, book, embedder, mic[0]


def test_a_mac_running_alone_turns_away_a_long_utterance_that_is_not_his(tmp_path: Path) -> None:
    """The same verdict, before emitting: no row, and the surface sees the turn absorbed."""
    rig, book, embedder, mic = _mac(tmp_path, _wire(0.1))
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.turns() == []
    assert ("empty", "not_owner") in rig.phases
    assert len(embedder.asked) == 1
    assert embedder.asked[0]["mic"] == mic
    assert _rows(book)[("mac", mic)] == 1


def test_a_mac_running_alone_keeps_the_score_of_his_turn_and_learns_from_it(
    tmp_path: Path,
) -> None:
    """The committed row carries ``voiceprint: {score, mic, seconds}`` and no embedding."""
    rig, book, _embedder, mic = _mac(tmp_path, _wire(0.8))
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.turns() == ["把灯打开。"]
    with contextlib.closing(sqlite3.connect(rig.db)) as raw:
        [(payload,)] = raw.execute(
            "SELECT payload_json FROM events WHERE type = 'utterance.received'",
        ).fetchall()
    kept = json.loads(payload)["voiceprint"]
    assert set(kept) == {"score", "mic", "seconds"}
    assert kept["score"] == 0.8
    assert "embedding" not in payload
    assert _rows(book)[("mac", mic)] == 2


def test_a_mac_with_the_gate_off_still_scores_but_turns_nothing_away(tmp_path: Path) -> None:
    """Off is off: the stranger's words are a turn."""
    rig, _book, _embedder, _mic = _mac(tmp_path, _wire(0.1), gate=False)
    try:
        rig.say(LONG)
    finally:
        rig.close()
    assert rig.turns() == ["把灯打开。"]


# --- the terminal's pipeline: the voiceprint is made beside the final ASR ----------------------


class _Asr:
    """Final ASR that notes when it was asked."""

    def __init__(self, steps: list[str]) -> None:
        self.steps = steps

    def recognize(self, audio_bytes: bytes) -> voice_asr.TranscriptionResult:
        del audio_bytes
        self.steps.append("recognize")
        return voice_asr.TranscriptionResult(
            text="把灯打开。", confidence=0.9, language_detected=None, emotion=None, event=None,
        )


def _terminal_pipeline(
    embedder: _Embedder, sent: list[dict[str, object]],
) -> voice_pipeline.VoicePipeline:
    """A terminal's pipeline: the utterance goes to ``sent`` instead of the brain."""

    def emit(payload: Any, correlation: Any) -> Event:  # noqa: ANN401
        sent.append(dict(payload))
        return Event("e" * 32, "utterance.received", 1, 0, dict(payload), None, dict(correlation))

    return voice_pipeline.VoicePipeline(
        recognizer=_Asr(embedder.order),
        broadcaster=None, artifacts_dir=None, emit=emit,
        voiceprints=embedder,  # type: ignore[arg-type]
    )


def test_the_embedding_starts_before_the_final_asr_and_goes_with_the_utterance() -> None:
    """On a terminal the utterance carries the embedding, the seconds, the mic and its age."""
    embedder = _Embedder(_wire(0.5))
    sent: list[dict[str, object]] = []
    pcm = _tone(3.0)
    _terminal_pipeline(embedder, sent).run_turn(
        audio_bytes=pcm, turn_id="T1", channel="inherent_wake", language="zh-CN",
        speech_s=2.6, mic=(MIC, 412.0),
        before_emit=lambda _text, _voiceprint: {"conversation": False, "over_her": False},
    )
    assert embedder.order == ["embedding started", "recognize"]
    [payload] = sent
    assert payload["voiceprint"]["mic"] == MIC  # type: ignore[index]
    assert payload["judge"] == {"conversation": False, "over_her": False}
    assert embedder.asked == [{"bytes": len(pcm), "rate": 16_000, "mic": MIC}]


def test_an_utterance_with_no_microphone_goes_without_a_voiceprint() -> None:
    """The push-to-talk path names no microphone, so nothing is embedded."""
    embedder = _Embedder(_wire(0.5))
    sent: list[dict[str, object]] = []
    _terminal_pipeline(embedder, sent).run_turn(
        audio_bytes=_tone(1.0), turn_id="T1", channel="inherent_ptt", language="zh-CN",
    )
    assert "voiceprint" not in sent[0]
    assert embedder.asked == []


def test_a_missing_speaker_model_means_no_extractor_and_never_an_error(tmp_path: Path) -> None:
    """No file: no extractor. A file that is no model: no voiceprint, not a failed turn."""
    assert load_extractor(tmp_path / "models" / "absent.onnx") is None
    broken = tmp_path / "models" / "broken.onnx"
    broken.parent.mkdir()
    broken.write_bytes(b"not a model")
    extractor = load_extractor(broken)
    assert extractor is not None
    assert extractor.begin(b"\0\0" * 16_000, 16_000, seconds=1.0, mic=MIC, mic_age_s=1.0)() is None


# --- the model itself, where its file is on this machine ---------------------------------------


def _model_path() -> Path | None:
    root = Path(os.environ.get("JARVIS_RUNTIME_ROOT", "~/.jarvis")).expanduser()
    path = default_voiceprint_model_path(root)
    return path if path.is_file() else None


@pytest.fixture
def extractor() -> VoiceprintExtractor:
    """The real speaker model, or a skip when its file is not on this machine."""
    path = _model_path()
    if path is None:
        pytest.skip("the speaker model is not on this machine")
    one = load_extractor(path)
    assert one is not None
    return one


def _speech_like(seconds: float, rate: int, seed: int) -> bytes:
    """A voiced, wobbling harmonic stack with a little noise: a different voice per seed."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * rate)) / rate
    wave = sum(
        np.sin(2 * np.pi * f * t * (1 + 0.1 * np.sin(2 * np.pi * 3 * t))) / (index + 1)
        for index, f in enumerate((120 + 30 * seed, 240, 360, 480))
    ) + 0.05 * rng.standard_normal(t.size)
    return bytes((np.clip(wave / 3, -1, 1) * 32767).astype("<i2").tobytes())


def test_the_speaker_model_gives_a_unit_embedding_of_192_numbers_at_any_sample_rate(
    extractor: VoiceprintExtractor,
) -> None:
    """The wire object: rounded to five decimals, L2-normalised, resampled to 16 kHz inside."""
    wave16 = _speech_like(3.0, 16_000, seed=1)
    wire = extractor.begin(wave16, 16_000, seconds=2.8, mic=MIC, mic_age_s=200.04)()
    assert wire is not None
    vector = np.asarray(wire["embedding"])
    assert vector.shape == (DIMENSIONS,)
    assert float(np.linalg.norm(vector)) == pytest.approx(1.0, abs=1e-3)
    assert all(round(one, 5) == one for one in wire["embedding"])
    assert (wire["seconds"], wire["mic"], wire["mic_age_s"]) == (2.8, MIC, 200.0)
    wave48 = _speech_like(3.0, 48_000, seed=1)
    other = extractor.begin(wave48, 48_000, seconds=2.8, mic=MIC, mic_age_s=1.0)()
    assert other is not None
    assert float(np.dot(vector, np.asarray(other["embedding"]))) > 0.95
    stranger = extractor.begin(_speech_like(3.0, 16_000, seed=7), 16_000, seconds=3.0, mic=MIC,
                               mic_age_s=1.0)()
    assert stranger is not None
    assert float(np.dot(vector, np.asarray(stranger["embedding"]))) < 0.999
    assert extractor.begin(b"", 16_000, seconds=0.0, mic=MIC, mic_age_s=1.0)() is None
