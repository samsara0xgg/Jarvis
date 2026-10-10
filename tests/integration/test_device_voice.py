"""ADR 0219: a device's voice acts only on its own turns, and words heard twice make one turn.

Acceptance checks on the real code over a real event log and the real response-run registry; the
model side is not involved at all (no key, no network).

Shown: (a) barge-in, supersede and exit cancel only the runs of turns the speaking device
opened, on the host's own session and on a terminal's hooks, and a turn with no opening row is
the host's alone; (b) ``heard_elsewhere`` answers from the log by the rule of the ADR; (c) the
shared judge returns ``elsewhere`` before it calls anything else; (d) the Mac's session treats
it as absorbed, without a supersede.
"""

# ruff: noqa: RUF001 - the zh transcripts carry fullwidth punctuation on purpose.

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from jarvis.decision.response_run import legacy_full_text_policy, start_response_run
from jarvis.runtime import device_voice, inherent_loop
from jarvis.shared.realtime import new_response_id
from jarvis.state.event_log import MAC_NODE, emit_event, open_event_log
from jarvis.surface import word_judge
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.terminal_link import TerminalHub
from jarvis.surface.voice_controls import VoiceControls
from tests.integration.test_soft_barge_in import LONG, SHORT, _Rig
from tests.integration.test_wave4a_response_run import _make_runtime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from jarvis.runtime import JarvisRuntime

PHONE = "iphone-2"
TERMINAL = "macbook"
WAKE_CHANNEL = "inherent_wake"
# The three turns whose runs are open: the Mac's, the phone's, and one with no opening row.
TURNS = {"mac": "T-mac", "iphone": "T-iphone", "none": "T-none"}


# --- scoping -------------------------------------------------------------------------------


def _heard(
    runtime: JarvisRuntime, turn_id: str, node: str,
) -> Any:  # noqa: ANN401 - the emitted Event
    """The row that opens ``turn_id``: words heard by voice on ``node``, a moment ago."""
    return emit_event(
        runtime.conn,
        type="utterance.received",
        payload={"transcript": "说了一句话", "turn_id": turn_id, "channel": WAKE_CHANNEL},
        correlation={"turn_id": turn_id},
        ingestion_node=node,
    )


def _open_run(runtime: JarvisRuntime, turn_id: str, trigger_uid: str) -> None:
    """Register an open final run for ``turn_id``, the way a turn waiting on an action is."""
    factory, registry = runtime.llm_session_factory, runtime.response_runs
    assert factory is not None
    assert registry is not None
    response_id = new_response_id()
    run = start_response_run(
        runtime.conn,
        turn_id=turn_id,
        trigger_event_uid=trigger_uid,
        request_client=factory.create(factory.snapshot(None), response_id=response_id),
        policy=legacy_full_text_policy(
            evidence_snapshot_hash="a" * 64, preset_snapshot_hash="b" * 64,
        ),
        response_id=response_id,
        committed_event_bus=runtime.committed_event_bus,
    )
    run.mark("waiting_action")
    registry.register(run)


def _scene(tmp_path: Path, open_for: tuple[str, ...]) -> JarvisRuntime:
    """A runtime with an open run for each named turn, and two waiting turns with no run.

    ``mac`` and ``iphone`` open with a voice row under their node; ``none`` has only a trigger
    that names another turn, so it has no opening row (a reconciliation turn). ``W-mac`` and
    ``W-iphone`` are recent sentences whose run has not opened yet.
    """
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    nodes = {"mac": MAC_NODE, "iphone": PHONE}
    for name in open_for:
        turn_id = TURNS[name]
        if name == "none":
            trigger = emit_event(
                runtime.conn, type="surface.user_intent",
                payload={"transcript": "x", "turn_id": "T-trigger", "channel": "cli_stdin"},
                correlation={"turn_id": "T-trigger"},
            )
        else:
            trigger = _heard(runtime, turn_id, nodes[name])
        _open_run(runtime, turn_id, trigger.event_uid)
    _heard(runtime, "W-mac", MAC_NODE)
    _heard(runtime, "W-iphone", PHONE)
    return runtime


def _cancelled(runtime: JarvisRuntime) -> list[tuple[str, str]]:
    return sorted(
        (str(row[0]), str(row[1])) for row in runtime.conn.execute(
            "SELECT json_extract(payload_json, '$.turn_id'), "
            "json_extract(payload_json, '$.reason') FROM events WHERE type = 'response.cancelled'",
        )
    )


def _hooks(runtime: JarvisRuntime) -> Any:  # noqa: ANN401 - ListenHooks
    """The hooks the runtime binds for every terminal; the terminal is told per call."""
    listening = inherent_loop._build_brain_listening(  # noqa: SLF001
        runtime, TerminalHub(events=BrainEvents(runtime.conn)),
        controls=VoiceControls(), set_conversation=lambda _on, _why: None,
        set_quiet=lambda _level: None,
    )
    assert listening is not None
    listening.drop_unspoken = lambda turn_ids: turn_ids  # type: ignore[method-assign]
    return listening.hooks


def _trigger(
    runtime: JarvisRuntime, via: str, device: str, trigger: str,
) -> str | None:
    """Fire ``trigger`` as ``device``'s voice does: the host's own session, or a terminal's."""
    if via == "local":
        streaming = cast("Any", SimpleNamespace(drop_unspoken=lambda turn_ids: turn_ids))
        ports = inherent_loop._local_listen_ports(runtime, streaming)  # noqa: SLF001
        assert ports.interrupt is not None
        assert ports.supersede_unspoken is not None
        assert ports.cancel_voice_runs is not None
        if trigger == "interrupt":
            return ports.interrupt("test")
        if trigger == "supersede":
            ports.supersede_unspoken("T-next")
        else:
            ports.cancel_voice_runs()
        return None
    hooks = _hooks(runtime)
    if trigger == "interrupt":
        return str(hooks.interrupt(device, "test"))
    if trigger == "supersede":
        hooks.supersede(device, "T-next")
    else:
        hooks.cancel_runs(device)
    return None


REASON = {"interrupt": "barge_in", "supersede": "superseded", "cancel_runs": "user_stop"}
# (how the trigger is wired, the device that speaks, the trigger, runs open, turns cancelled)
SCOPING = {
    "mac barge-in leaves the phone's answer alone": (
        "local", MAC_NODE, "interrupt", ("mac", "iphone"), ["T-mac"],
    ),
    "mac barge-in with only the phone's answer open cancels nothing": (
        "local", MAC_NODE, "interrupt", ("iphone",), [],
    ),
    "mac barge-in cancels a turn with no opening row, which is the host's": (
        "local", MAC_NODE, "interrupt", ("none",), ["T-none"],
    ),
    "mac barge-in with its own and a background run open is still ambiguous": (
        "local", MAC_NODE, "interrupt", ("mac", "none"), [],
    ),
    "a terminal's barge-in cancels its own answer among all three": (
        "terminal", PHONE, "interrupt", ("mac", "iphone", "none"), ["T-iphone"],
    ),
    "a terminal's barge-in with the others' answers open cancels nothing": (
        "terminal", PHONE, "interrupt", ("mac", "none"), [],
    ),
    "a terminal named like the Mac's own rows never touches a background turn": (
        "terminal", MAC_NODE, "interrupt", ("none",), [],
    ),
    "mac supersede drops only the Mac's earlier answer": (
        "local", MAC_NODE, "supersede", ("mac", "iphone", "none"), ["T-mac"],
    ),
    "a terminal's supersede drops only its own earlier answer": (
        "terminal", PHONE, "supersede", ("mac", "iphone", "none"), ["T-iphone"],
    ),
    "a terminal named mac supersedes the Mac's rows and not a background turn": (
        "terminal", MAC_NODE, "supersede", ("mac", "iphone", "none"), ["T-mac"],
    ),
    "mac exit ends only the Mac's answers": (
        "local", MAC_NODE, "cancel_runs", ("mac", "iphone", "none"), ["T-mac"],
    ),
    "a terminal's exit ends only its own answers": (
        "terminal", PHONE, "cancel_runs", ("mac", "iphone", "none"), ["T-iphone"],
    ),
    "a terminal named mac exits only the Mac's rows": (
        "terminal", MAC_NODE, "cancel_runs", ("mac", "iphone", "none"), ["T-mac"],
    ),
}


AMBIGUOUS = {"mac barge-in with its own and a background run open is still ambiguous"}


@pytest.mark.parametrize("name", list(SCOPING))
def test_a_devices_voice_cancels_only_the_runs_of_its_own_turns(
    tmp_path: Path, name: str,
) -> None:
    """Each trigger, from each device, cancels the runs of that device's turns and no others."""
    via, device, trigger, open_for, expected = SCOPING[name]
    runtime = _scene(tmp_path, open_for)
    outcome = _trigger(runtime, via, device, trigger)
    assert _cancelled(runtime) == [(turn, REASON[trigger]) for turn in expected]
    if trigger == "interrupt":
        # Two runs of the speaking device's own are the one case barge-in has always refused.
        assert outcome == (
            "cancelled" if expected
            else "ambiguous_open_runs" if name in AMBIGUOUS else "no_open_run"
        )
    if trigger == "supersede":
        # The sentences whose run has not opened yet are marked for the same device alone.
        registry = runtime.response_runs
        assert registry is not None
        assert (registry.turn_superseded("W-mac"), registry.turn_superseded("W-iphone")) == (
            device == MAC_NODE, device == PHONE,
        )
    runtime.conn.close()


def test_the_hold_on_completion_stays_global(tmp_path: Path) -> None:
    """ADR 0053's hold is on every run whoever speaks: a terminal's hold reaches the registry."""
    runtime = _make_runtime(tmp_path, lifecycle=True, cancel=True)
    registry = runtime.response_runs
    assert registry is not None
    _hooks(runtime).hold_runs(True)  # noqa: FBT003
    assert registry.wait_completion_allowed(0.05) is False
    _hooks(runtime).hold_runs(False)  # noqa: FBT003
    assert registry.wait_completion_allowed(0.05) is True
    runtime.conn.close()


# --- heard_elsewhere -----------------------------------------------------------------------

OLD_S = 20.0


def _utterance_row(  # noqa: PLR0913 - the row's own fields
    path: Path, node: str, text: str, *, age_s: float, event_type: str = "utterance.received",
    channel: str = WAKE_CHANNEL,
) -> None:
    conn = open_event_log(path)
    try:
        emit_event(
            conn,
            type=event_type,
            payload={"transcript": text, "turn_id": f"T-{node}-{age_s}", "channel": channel},
            correlation={"turn_id": f"T-{node}-{age_s}"},
            ts_epoch_ms=int((time.time() - age_s) * 1000),
            ingestion_node=node,
        )
    finally:
        conn.close()


# (the words a device recorded, the node that recorded them, how long ago,
#  the words the asking device heard, the asking device, expected)
ELSEWHERE = {
    "the same device's own row is not another device": (
        "把客厅的灯打开", "iphone", 2.0, "把客厅的灯打开", "iphone", False,
    ),
    "another device, identical words": (
        "把客厅的灯打开", "mac", 2.0, "把客厅的灯打开", "iphone", True,
    ),
    "the Mac asking about the phone's words": (
        "把客厅的灯打开", "iphone", 2.0, "把客厅的灯打开", "mac", True,
    ),
    "zh with other punctuation": (
        "把客厅的灯打开。", "mac", 2.0, "把，客厅的灯，打开", "iphone", True,
    ),
    "case and spaces in English": (
        "Turn on the Living Room light", "mac", 2.0, "turn on the living room LIGHT.",
        "iphone", True,
    ),
    "the other heard the wake phrase too": (
        "嘿，贾维斯，今天天气怎么样", "mac", 3.0, "今天天气怎么样", "iphone", True,
    ),
    "the other's words contain these": (
        "明天上午十点提醒我开会", "mac", 3.0, "明天上午十点", "iphone", True,
    ),
    "these contain the other's words": (
        "明天上午十点", "mac", 3.0, "明天上午十点提醒我开会", "iphone", True,
    ),
    "two recognizers disagree on a word": (
        "把客厅的灯打开", "mac", 2.0, "把客厅得灯打开", "iphone", True,
    ),
    "older than the window": (
        "把客厅的灯打开", "mac", OLD_S, "把客厅的灯打开", "iphone", False,
    ),
    "unrelated words": ("把客厅的灯打开", "mac", 2.0, "今天天气怎么样", "iphone", False),
    "one character": ("好", "mac", 2.0, "好", "iphone", False),
    "one character left after punctuation": ("好。", "mac", 2.0, "，好", "iphone", False),
    "only the wake phrase": ("嘿，贾维斯，", "mac", 2.0, "Hey Jarvis,", "iphone", False),
}


@pytest.mark.parametrize("name", list(ELSEWHERE))
def test_words_are_heard_elsewhere_by_the_rule_of_the_adr(tmp_path: Path, name: str) -> None:
    """Another device's recent utterance with matching words, and nothing else, is a yes."""
    recorded, node, age_s, heard, asking, expected = ELSEWHERE[name]
    path = tmp_path / "events.db"
    open_event_log(path).close()
    _utterance_row(path, node, recorded, age_s=age_s)
    assert device_voice.heard_elsewhere(path, asking, heard) is expected


def test_typed_words_and_empty_logs_are_never_heard_elsewhere(tmp_path: Path) -> None:
    """Only a voice utterance (an ``utterance.received``) counts; typing on the Mac does not."""
    path = tmp_path / "events.db"
    open_event_log(path).close()
    assert device_voice.heard_elsewhere(path, "iphone", "把客厅的灯打开") is False
    _utterance_row(
        path, "mac", "把客厅的灯打开", age_s=1.0, event_type="surface.user_intent",
        channel="cli_stdin",
    )
    assert device_voice.heard_elsewhere(path, "iphone", "把客厅的灯打开") is False


def test_the_window_and_the_ratio_are_the_adrs() -> None:
    """The two named constants are the ADR's numbers."""
    assert device_voice.ELSEWHERE_WINDOW_S == 8.0
    assert device_voice.ELSEWHERE_SIMILARITY == 0.6


# --- the shared judge ------------------------------------------------------------------------


def test_elsewhere_is_the_judges_first_word_and_costs_no_request() -> None:
    """Before the quiet command, the regexes, ``begin``, ``note`` and Jev."""
    calls: list[str] = []

    def spy(name: str, result: object = None) -> Callable[..., Any]:
        def call(*_args: object) -> Any:  # noqa: ANN401 - whichever hook it stands in for
            calls.append(name)
            return result
        return call

    def heard_elsewhere(turn_id: str, _text: str) -> bool:
        calls.append(f"elsewhere {turn_id}")
        return True

    hooks = word_judge.WordHooks(
        ask=spy("ask"), note=spy("note"), begin=spy("begin"), recent=spy("recent", ""),
        quiet=True,
        heard_elsewhere=heard_elsewhere,
    )
    for text in ("勿扰", "嗯", "退下", "把灯打开", "别说了"):
        for over_her, conversation in ((True, False), (False, True), (True, True)):
            assert word_judge.words_verdict(
                hooks, "T1", text, conversation=conversation, over_her=over_her,
            ) == "elsewhere"
    assert set(calls) == {"elsewhere T1"}


def test_a_hook_that_says_no_or_fails_leaves_the_judge_as_it_was(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No answer, a no, and an exception all mean the words were heard here alone."""
    def boom(_turn_id: str, _text: str) -> bool:
        raise RuntimeError

    for hook in (None, lambda _t, _x: False, boom):
        hooks = word_judge.WordHooks(quiet=True, heard_elsewhere=hook)
        assert word_judge.words_verdict(
            hooks, "T1", "勿扰", conversation=False, over_her=False,
        ) == "quiet:dnd"
        assert word_judge.words_verdict(
            hooks, "T1", "把灯打开", conversation=False, over_her=False,
        ) == "turn"
    assert "heard_elsewhere failed" in caplog.text


# --- the Mac's session -----------------------------------------------------------------------


@pytest.mark.parametrize("voiced", [SHORT, LONG])
def test_the_mac_absorbs_words_the_phone_heard_first_and_she_goes_on(
    tmp_path: Path, voiced: int,
) -> None:
    """A barge-in is settled as go-on (a held one resumes), nothing is superseded, no turn."""
    asked: list[tuple[str, str]] = []

    def heard_elsewhere(turn_id: str, text: str) -> bool:
        asked.append((turn_id, text))
        return True

    rig = _Rig(tmp_path, "把客厅的灯打开。", heard_elsewhere=heard_elsewhere)
    try:
        rig.say(voiced)
    finally:
        rig.close()
    assert rig.output == (
        ["gain 0.2", "gain 1.0"] if voiced == SHORT
        else ["gain 0.2", "pause", "go on", "gain 1.0"]
    )
    assert "supersede" not in rig.output
    assert "stop" not in rig.output
    assert rig.speaking
    assert rig.turns() == []
    assert rig.answers == []
    assert ("empty", "elsewhere") in rig.phases
    assert [text for _turn, text in asked] == ["把客厅的灯打开。"]


def test_words_not_heard_elsewhere_are_a_turn_and_supersede_as_before(tmp_path: Path) -> None:
    """The check answering no changes nothing."""
    rig = _Rig(tmp_path, "把客厅的灯打开。", speaking=False, heard_elsewhere=lambda _t, _x: False)
    try:
        rig.say(SHORT)
    finally:
        rig.close()
    assert "supersede" in rig.output
    assert len(rig.turns()) == 1
