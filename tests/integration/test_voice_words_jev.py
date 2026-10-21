"""ADR 0130 — Jev settles the short words the regexes cannot, against a fake route.

A real DuplexVoiceSession in conversation mode (the rig of the soft barge-in tests) hears one
scripted line, over her or while she is silent. The route is a ``SurrogateRoute`` whose ``post``
answers from a script instead of the network, so what is asserted is what the session did (her
gain, stop, the mode, the turns) and what left for Jev (the text, how often, the use tag).
"""

from __future__ import annotations

import json
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

import pytest

from jarvis.decision.surrogate_route import JevLog, Reply, SurrogateRoute
from jarvis.decision.voice_words import VoiceWords, words_state
from jarvis.runtime.bootstrap.jev import _voice_words
from jarvis.surface import voice_session
from tests.support.barge_in import LONG, SHORT, BargeInRig
from tests.support.clock import FakeClock
from tests.support.runtime_config import runtime_config
from tests.support.voice_words import LISTENING, LONG_LINE

if TYPE_CHECKING:
    from pathlib import Path

    from jarvis.shared.clock import Clock

SHORT_ORDER = "我让你退一下。"  # a dismissal only inside a sentence


class _FakeRoute(SurrogateRoute):
    """Answers ``words`` questions from ``choice``/``confidence`` and records each request."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.sent: list[tuple[str, str, str | None]] = []
        self.choice, self.confidence, self.hang = "none", 0.99, False

    def post(
        self, state: str, questions: dict[str, Any], use: str, ref: str | None = None,
    ) -> Future[Reply] | None:
        del questions
        self.sent.append((state, use, ref))
        future: Future[Reply] = Future()
        if not self.hang:
            body = {"answers": {"words": {"choice": self.choice, "confidence": self.confidence}}}
            future.set_result(Reply(body, None, 0.0))
        return future


class _Words:
    """A VoiceWords over the fake route, with the dataset in ``log_path``."""

    def __init__(
        self, tmp_path: Path, *, timeout_ms: int = 800, clock: Clock | None = None,
    ) -> None:
        """``clock`` is a stopped fake unless the test needs its deadline to pass for real."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.log_path = tmp_path / "decisions.jsonl"
        self.route = _FakeRoute(
            model="m", min_confidence=1.0, timeout_ms=timeout_ms, clock=clock or FakeClock(),
            log=JevLog(self.log_path),
        )
        self.words = VoiceWords(self.route, 0.95, 24)

    def say(self, choice: str, confidence: float = 0.97) -> None:
        self.route.choice, self.route.confidence = choice, confidence

    def rig(self, tmp_path: Path, heard: str, **kwargs: Any) -> BargeInRig:  # noqa: ANN401 - the rig's own
        return BargeInRig(
            tmp_path, heard, ask_words=self.words.ask, note_words=self.words.note, **kwargs,
        )

    def lines(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text("utf-8").splitlines()]


def _run(rig: BargeInRig, frames: int = SHORT) -> None:
    try:
        rig.say(frames)
    finally:
        rig.close()


def test_a_dismissal_inside_a_sentence_needs_jev_to_confirm(tmp_path: Path) -> None:
    """Not confirmed (none, or below the bar): a turn, the mode stays; confirmed: dismissed."""
    for choice, confidence in (("none", 0.99), ("dismiss", 0.9)):
        words = _Words(tmp_path / choice)
        words.say(choice, confidence)
        rig = words.rig(tmp_path / choice, SHORT_ORDER, speaking=False)
        _run(rig)
        assert rig.turns() == [SHORT_ORDER]
        assert rig.conversation_changes == []
        assert rig.answers == []
        assert [use for _state, use, _ref in words.route.sent] == ["voice_words"]
    words = _Words(tmp_path / "yes")
    words.say("dismiss")
    rig = words.rig(tmp_path / "yes", SHORT_ORDER, speaking=False)
    _run(rig)
    assert rig.turns() == []
    assert rig.conversation_changes == [(False, "dismissed")]
    assert rig.answers == ["dismissed"]
    state = words.route.sent[0][0]
    assert state.splitlines() == [
        "[The assistant was silent; hands-free mode]", f"User: {SHORT_ORDER}",
    ]


def test_a_whole_dismissal_is_instant_and_never_asked(tmp_path: Path) -> None:
    """A whole-line dismissal needs no Jev: no call, dismissed at once."""
    words = _Words(tmp_path)
    rig = words.rig(tmp_path, "退下。", speaking=False)
    _run(rig)
    assert words.route.sent == []
    assert rig.conversation_changes == [(False, "dismissed")]
    assert rig.turns() == []


def test_a_confident_keep_going_lets_her_go_on_and_a_weaker_one_does_not(tmp_path: Path) -> None:
    """Keep_going at 0.97 over her lets her go on; at 0.9 the line is a turn."""
    sure = _Words(tmp_path / "sure")
    sure.say("keep_going", 0.97)
    rig = sure.rig(tmp_path / "sure", LISTENING, recent_speech=lambda: "你今天有三项日程")
    _run(rig)
    assert rig.output == ["gain 0.2", "gain 1.0"]
    assert rig.speaking
    assert rig.turns() == []
    assert ("empty", "backchannel") in rig.phases
    assert sure.route.sent[0][0].splitlines() == [
        "Assistant: 你今天有三项日程",
        "[The assistant was speaking when this was heard]",
        f"User: {LISTENING}",
    ]
    unsure = _Words(tmp_path / "unsure")
    unsure.say("keep_going", 0.9)
    rig = unsure.rig(tmp_path / "unsure", LISTENING)
    _run(rig)
    assert "stop" in rig.output
    assert rig.turns() == [LISTENING]


@pytest.mark.parametrize(
    ("choice", "conversation", "over_her", "verdict"),
    [
        ("keep_going", True, True, "backchannel"),
        ("keep_going", True, False, "backchannel"),
        ("dismiss", True, True, "dismissed"),
        ("dismiss", True, False, "dismissed"),
        ("wait", True, False, "wait"),
        ("stop", True, True, "stop"),
        ("stop", True, False, "turn"),
        # Dismiss and wait mean nothing outside conversation mode; over her they act as stop.
        ("dismiss", False, True, "stop"),
        ("wait", False, True, "stop"),
        ("stop", False, True, "stop"),
        (None, True, True, "turn"),
    ],
)
def test_jev_choice_acts_as_the_matching_regex_verdict(
    choice: str | None, conversation: bool, over_her: bool, verdict: str,  # noqa: FBT001
) -> None:
    """Each accepted choice becomes the verdict the regex path gives for that word."""
    assert voice_session._jev_verdict(  # noqa: SLF001
        choice, conversation=conversation, over_her=over_her,
    ) == verdict


def test_wait_in_the_mode_holds_it_and_answers(tmp_path: Path) -> None:
    """Jev says wait in the mode: held, answered, no turn."""
    words = _Words(tmp_path)
    words.say("wait")
    rig = words.rig(tmp_path, "Give it a sec please.", speaking=False)
    _run(rig)
    assert rig.answers == ["wait"]
    assert rig.turns() == []
    assert ("empty", "wait") in rig.phases


def test_without_jev_the_verdicts_are_todays(tmp_path: Path) -> None:
    """No callable: a dismissal inside a sentence still dismisses; an unknown line is a turn."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    rig = BargeInRig(tmp_path / "a", SHORT_ORDER, speaking=False)
    _run(rig)
    assert rig.conversation_changes == [(False, "dismissed")]
    assert rig.turns() == []
    rig = BargeInRig(tmp_path / "b", LISTENING)
    _run(rig)
    assert "stop" in rig.output
    assert rig.turns() == [LISTENING]


def test_no_answer_in_time_is_a_turn(tmp_path: Path) -> None:
    """No answer within timeout_ms: the line is a turn."""
    clock = FakeClock()
    words = _Words(tmp_path, timeout_ms=50, clock=clock)
    words.route.hang = True
    rig = words.rig(tmp_path, LISTENING)
    clock.pass_deadline_of(0.05, lambda: _run(rig))
    assert len(words.route.sent) == 1
    assert rig.turns() == [LISTENING]


def test_a_long_line_is_never_sent(tmp_path: Path) -> None:
    """A line over max_chars never leaves the Mac."""
    words = _Words(tmp_path)
    words.say("keep_going")
    rig = words.rig(tmp_path, LONG_LINE)
    _run(rig, LONG)
    assert words.route.sent == []
    assert rig.turns() == [LONG_LINE]


def test_a_line_the_regexes_absorbed_is_noted_not_sent(tmp_path: Path) -> None:
    """A line absorbed by the regexes alone is noted in the dataset, with its text."""
    words = _Words(tmp_path)
    rig = words.rig(tmp_path, "嗯嗯。")
    _run(rig)
    assert words.route.sent == []
    [line] = words.lines()
    assert (line["kind"], line["use"]) == ("regex", "voice_words")
    assert (line["verdict"], line["text"]) == ("backchannel", "嗯嗯。")
    assert line["over_her"] is True
    assert line["conversation"] is True


def test_the_speaker_tag_is_the_line_prefix() -> None:
    """The judged line is the last, prefixed with its speaker."""
    assert words_state("hello", "", over_her=False).endswith("\nUser: hello")


def test_off_or_absent_is_none_and_on_builds_the_reader() -> None:
    """Off or absent is None; a bad value is refused at load (``test_runtime_config``)."""
    good = {"model": "m", "at": 0.95, "timeout_ms": 800, "max_chars": 24}
    clock = FakeClock()

    def built(config: dict[str, Any]) -> VoiceWords | None:
        return _voice_words(runtime_config(config).jev.voice_words, clock)

    assert built({"realtime": {"jev_words": {**good, "enabled": False}}}) is None
    assert built({}) is None
    assert built({"realtime": {"jev_words": {**good, "enabled": True}}}) is not None
