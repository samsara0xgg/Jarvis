"""ADR 0172 step 5b: a voice terminal listens, and the brain holds the turn.

Acceptance checks, each against the real code and fakes only for the model side (the word judge,
the speech provider, the recognizer, the microphone):

- a call with a deadline falls back as it does on one machine when the brain is silent, refuses
  or is gone, and a fire-and-forget frame to a gone brain is dropped; an ask in flight fails at
  once when the link drops;
- the terminal's conversation mode follows the brain's, in step in both directions;
- the brain writes an utterance once, under the device's name, however often it is sent, refuses
  what is not one, and answers asks only after the tells that preceded them;
- the answer goes to the terminal that heard the utterance, not the one connected last;
- the brain's drop and stop commands reach the terminal that holds the playback, and the
  terminal's executor runs them against its media actor;
- a microphone that cannot be opened leaves a terminal that still speaks, and is not retried;
- a listening terminal runs a daemon's device watch and power observer beside its capture
  session, hooked to one power coordinator, and ends them with it; one that cannot listen runs
  neither; the reSpeaker's saved look is kept on the board while any terminal runs, and a pick
  on the Settings page reaches it;
- one end-to-end run over a real websocket: a replayed WAV, heard by a real capture session with a
  fake recognizer, becomes an ``utterance.received`` in the brain's log under the terminal's
  name, the brain's answer is played by the terminal, and the end-of-speech to first-audible
  number is read from the trace.
"""

# ruff: noqa: FBT003 - the capture session's callbacks take their one bit positionally.

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.runtime import terminal as terminal_module
from jarvis.runtime.daemon import devices, voice_power
from jarvis.runtime.dictation import load_vocab
from jarvis.runtime.settings import Settings
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl, reset_realtime_trace
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface import (
    respeaker_board,
    terminal_listen,
    voice_artifact_store,
    voice_backend,
)
from jarvis.surface.terminal_listen import (
    DROP_UNSPOKEN,
    STOP_OUTPUT,
    BrainListening,
    LinkedControls,
    LinkedTurn,
    ListenHooks,
    RemotePolish,
    with_voice_commands,
)
from jarvis.surface.terminal_speaker import BrainCallError
from tests.support.stderr_log import stderr_in, stderr_sent_nowhere
from tests.support.terminal import (
    TERMINAL_WS,
    VoiceBrain,
    VoiceTerminal,
    ack_frame,
    append_answer,
    bearer,
    brain_client,
    hello_frame,
    logged_types,
    playback_frame,
    wait_for,
    ws_rows,
)
from tests.support.terminal_listen import (
    LISTEN_CONFIG,
    ListenBench,
    ask_op,
    connect_listening,
    heard_session,
    listening_client,
    logged_utterances,
    ok_reply,
    utterance_frame,
)
from tests.support.voice_input import write_wav
from tools.realtime_trace_report import load_trace, summarize_trace

# --- the terminal's end, against a brain we play by hand --------------------------------------


@pytest.fixture
def fast_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-call deadlines, short: what they guard is the fallback, not the wait."""
    for name in (
        "UTTERANCE_TIMEOUT_S", "WORKING_TIMEOUT_S", "INTERRUPT_TIMEOUT_S", "RUNS_TIMEOUT_S",
    ):
        monkeypatch.setattr(terminal_listen, name, 0.1)


def test_the_brains_answers_are_what_the_terminals_callbacks_return(tmp_path: Path) -> None:
    """Asks return what the brain said; tells carry no id and arrive in the order told."""

    def brain(frame: dict[str, Any]) -> dict[str, Any] | None:
        replies: dict[str, object] = {
            "working": {"working": True},
            "interrupt": {"outcome": "cancelled"}, "cancel_runs": {},
            "utterance": {"verdict": "turn", "event_uid": "a" * 32},
        }
        return ok_reply(replies[frame["op"]])

    bench = ListenBench(tmp_path, brain)
    try:
        turn = LinkedTurn(bench.link)
        assert turn.turn_working() is True
        assert turn.interrupt("conversation_speech") == "cancelled"
        turn.cancel_runs()
        event = turn.emit_utterance({"transcript": "hi", "turn_id": "T1"}, {"turn_id": "T1"})
        assert (event.event_uid, event.type, event.correlation) == (
            "a" * 32, "utterance.received", {"turn_id": "T1"},
        )
        turn.hold_runs(True)
        turn.answer_words("T1", "wait", "Okay.")
        wait_for(lambda: len(bench.frames) == 6, "frames did not reach the brain")
    finally:
        bench.close()
    told = [frame for frame in bench.frames if "id" not in frame]
    assert [frame["op"] for frame in told] == ["hold", "say"]
    asked = [frame for frame in bench.frames if "id" in frame]
    assert [frame["op"] for frame in asked] == [
        "working", "interrupt", "cancel_runs", "utterance",
    ]
    assert all(frame["type"] == "ask" for frame in bench.frames)


@pytest.mark.parametrize("brain_is", ["silent", "refusing", "gone"])
@pytest.mark.usefixtures("fast_deadlines")
def test_a_call_with_a_deadline_keeps_its_fallback_when_the_brain_cannot_answer(
    tmp_path: Path, brain_is: str,
) -> None:
    """Nothing in flight, a stop that stops, and an utterance the brain never took is an error."""
    refuse: dict[str, Any] = {"ok": False, "code": "not_listening", "message": "no"}
    bench = ListenBench(tmp_path, (lambda _frame: refuse) if brain_is == "refusing" else None)
    try:
        if brain_is == "gone":
            bench.link.down()
        turn = LinkedTurn(bench.link)
        started = time.monotonic()
        assert turn.turn_working() is False
        assert turn.interrupt("conversation_speech") == "brain_unreachable"
        turn.cancel_runs()  # does not raise: the local stop already happened
        assert time.monotonic() - started < 3.0
        with pytest.raises(BrainCallError):
            turn.emit_utterance({"transcript": "hi", "turn_id": "T1"}, {"turn_id": "T1"})
        turn.hold_runs(True)  # fire and forget, never an error
        time.sleep(0.2)
        sent = bench.ops()
    finally:
        bench.close()
    if brain_is == "gone":
        assert sent == []  # a tell to a gone brain is dropped, not queued for later
    else:
        assert sent[:4] == ["working", "interrupt", "cancel_runs", "utterance"]


def test_an_ask_in_flight_fails_at_once_when_the_link_drops(tmp_path: Path) -> None:
    """A call with a long deadline does not wait it out once the link is known to be down."""
    bench = ListenBench(tmp_path)  # the brain never answers
    outcome: list[BaseException | object] = []

    def asker() -> None:
        try:
            outcome.append(bench.link.ask("working", {}, timeout_s=30.0))
        except BrainCallError as exc:
            outcome.append(exc)

    try:
        thread = threading.Thread(target=asker)
        thread.start()
        wait_for(lambda: bool(bench.frames), "the ask never left")
        started = time.monotonic()
        bench.link.down()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert time.monotonic() - started < 2.0
    finally:
        bench.close()
    assert len(outcome) == 1
    assert isinstance(outcome[0], BrainCallError)


def test_an_ask_from_the_loop_that_carries_the_link_is_refused_not_deadlocked(
    tmp_path: Path,
) -> None:
    """The link's loop is the one that would deliver the reply; waiting on it would hang."""
    bench = ListenBench(tmp_path)
    try:

        async def on_the_loop() -> str:
            try:
                bench.link.ask("working", {}, timeout_s=30.0)
            except BrainCallError as exc:
                return str(exc)
            return ""

        said = asyncio.run_coroutine_threadsafe(on_the_loop(), bench.loop).result(timeout=5)
    finally:
        bench.close()
    assert "must not run on the loop" in said
    assert bench.frames == []


def test_conversation_mode_follows_the_brain_both_ways(tmp_path: Path) -> None:
    """The brain owns it: the terminal's changes reach it and a surface's switch reaches back."""
    state = {"conversation": False, "quiet": "off"}
    taken: list[tuple[str, dict[str, Any]]] = []

    def brain(frame: dict[str, Any]) -> dict[str, Any] | None:
        op, args = frame["op"], frame["args"]
        if op == "controls":
            return ok_reply(dict(state))
        taken.append((op, args))
        if op == "conversation":
            state["conversation"] = args["on"]
        if op == "quiet":
            state["quiet"] = args["level"]
        return ok_reply({})

    bench = ListenBench(tmp_path, brain)
    left: list[bool] = []
    controls = LinkedControls(bench.link, poll_s=0.02)
    controls.on_surface_exit = lambda: left.append(True)
    controls.start()
    try:
        controls.set_conversation(True, "wake")
        wait_for(lambda: ("conversation", {"on": True, "reason": "wake"}) in taken, "not taken")
        assert controls.conversation() is True
        time.sleep(0.15)  # polls come and go; the brain agrees, so nothing flips back
        assert controls.conversation() is True
        controls.set_quiet("quiet")
        wait_for(lambda: state["quiet"] == "quiet", "quiet never reached the brain")
        state["conversation"] = False  # the owner switched it off from a surface
        wait_for(lambda: controls.conversation() is False, "the terminal never followed")
        wait_for(lambda: left == [True], "the session was not told to leave conversation")
        state["conversation"] = True  # and on again: mirrored, no exit callback
        wait_for(controls.conversation, "the terminal never followed it back on")
    finally:
        controls.stop()
        bench.close()
    assert left == [True]


def test_a_terminals_polish_is_asked_of_the_brain_with_its_words_and_its_own_vocabulary(
    tmp_path: Path,
) -> None:
    """The word list is read from this machine on every call; the brain's answer is the text."""
    vocab = tmp_path / "vocab.yaml"
    vocab.write_text("user:\n- Typlus\nauto:\n- 星核\n", encoding="utf-8")
    context = {"app": "Ghostty", "window": "claude", "selected": "", "before": "先看"}

    def brain(frame: dict[str, Any]) -> dict[str, Any] | None:
        assert frame["op"] == "polish"
        return ok_reply({"text": "POLISHED " + frame["args"]["raw"]})

    bench = ListenBench(tmp_path, brain)
    try:
        polish = RemotePolish(bench.link, functools.partial(load_vocab, vocab))
        polish.warm()  # nothing to open: the brain's own client holds the connection
        assert polish("嗯 把 typlus 改了", context, "zh") == "POLISHED 嗯 把 typlus 改了"
        vocab.write_text("user:\n- NewTerm\n", encoding="utf-8")
        polish("again", context, "en")
    finally:
        bench.close()
    assert [frame["args"] for frame in bench.frames] == [
        {"raw": "嗯 把 typlus 改了", "context": context, "language": "zh",
         "vocab": ["Typlus", "星核"]},
        {"raw": "again", "context": context, "language": "en", "vocab": ["NewTerm"]},
    ]


@pytest.mark.parametrize("brain_is", ["silent", "refusing", "gone", "textless"])
def test_a_polish_the_brain_cannot_give_raises_so_dictation_shows_the_raw_words(
    tmp_path: Path, brain_is: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dictation turns that error into `{error, raw}`; this is only that it raises, and why."""
    monkeypatch.setattr(terminal_listen, "POLISH_TIMEOUT_S", 0.2)
    refuse: dict[str, Any] = {"ok": False, "code": "not_supported", "message": "no polish here"}
    answers: dict[str, Any] = {
        "refusing": lambda _frame: refuse, "textless": lambda _frame: ok_reply({"other": 1}),
    }
    bench = ListenBench(tmp_path, answers.get(brain_is))
    try:
        if brain_is == "gone":
            bench.link.down()
        polish = RemotePolish(bench.link, list)
        with pytest.raises(BrainCallError):
            polish("raw", {"app": "", "window": "", "selected": "", "before": ""}, "zh")
    finally:
        bench.close()


# --- the brain's end of /terminal/ws ----------------------------------------------------------


def test_an_utterance_is_written_once_under_the_devices_name_however_often_it_is_sent(
    tmp_path: Path,
) -> None:
    """Idempotent by utterance id; the payload is the pipeline's fields, the audio a reference."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, _hub, log = listening_client(tmp_path)
    ref = "/Users/allen/.jarvis/memory/audio/U-T1.wav"
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        bad = utterance_frame("T1", audio_artifact_ref=ref, unknown_key="x")
        ask_op(ws, "utterance", bad, "a" * 32)
        first = ws.receive_json()
        ask_op(ws, "utterance", utterance_frame("T1", audio_artifact_ref=ref), "b" * 32)  # a retry
        second = ws.receive_json()
        ask_op(ws, "utterance", utterance_frame("T2"), "c" * 32)
        third = ws.receive_json()
    assert (first["type"], first["id"], first["ok"]) == ("reply", "a" * 32, True)
    assert second["value"] == first["value"]  # the same event, not a second one
    assert third["value"]["event_uid"] != first["value"]["event_uid"]
    rows = logged_utterances(log)
    assert [(uid, node) for uid, node, _ in rows] == [
        (first["value"]["event_uid"], "macbook"),
        (third["value"]["event_uid"], "macbook"),
    ]
    payload = json.loads(rows[0][2])
    assert payload == {
        "transcript": "turn on the light",
        "turn_id": "T1",
        "channel": "inherent_wake",
        "confidence": 0.9,
        "language": "zh",
        "audio_artifact_ref": ref,
        "utterance_id": "U-T1",
    }
    with sqlite3.connect(log) as conn:
        correlation = conn.execute(
            "SELECT correlation_json FROM events WHERE type = 'utterance.received' ORDER BY id",
        ).fetchone()[0]
    assert json.loads(correlation) == {"turn_id": "T1"}


def test_the_brain_refuses_what_is_not_a_well_formed_ask_and_the_link_lives_on(
    tmp_path: Path,
) -> None:
    """Each refusal is a reply with a code; nothing is written; the next good ask is answered."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, _hub, log = listening_client(tmp_path)
    bad_utterances = [
        {**utterance_frame("T1"), "transcript": ""},
        {**utterance_frame("T1"), "channel": "text"},
        {**utterance_frame("../T1")},
        {**utterance_frame("T1"), "utterance_id": 7},
        {**utterance_frame("T1"), "language": "x" * 100},
        {**utterance_frame("T1"), "confidence": "high"},
    ]
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        replies = []
        for index, args in enumerate(bad_utterances):
            ask_op(ws, "utterance", args, f"{index:032d}")
            replies.append(ws.receive_json())
        ask_op(ws, "no_such_op", {}, "e" * 32)
        replies.append(ws.receive_json())
        ask_op(ws, "quiet", {"level": "deafening"}, "f" * 32)
        replies.append(ws.receive_json())
        ask_op(ws, "interrupt", {"source": 7}, "1" * 32)
        replies.append(ws.receive_json())
        ask_op(ws, "working", {}, "2" * 32)
        alive = ws.receive_json()
    assert [(reply["ok"], reply.get("code")) for reply in replies] == [
        (False, "bad_args"),
        (False, "bad_args"),
        (False, "bad_args"),
        (False, "bad_args"),
        (False, "bad_args"),
        (False, "bad_args"),
        (False, "unknown_op"),
        (False, "bad_args"),
        (False, "bad_args"),
    ]
    assert alive == {"type": "reply", "id": "2" * 32, "ok": True, "value": {"working": False}}
    assert logged_utterances(log) == []


def test_the_brain_polishes_a_terminals_dictation_or_says_it_does_not(tmp_path: Path) -> None:
    """The hook gets the words, where they land, the language and the terminal's word list."""
    from contextlib import ExitStack  # noqa: PLC0415

    seen: list[tuple[str, dict[str, str], str, list[str]]] = []

    def polish(raw: str, context: Any, language: str, vocab: Any) -> str:  # noqa: ANN401
        seen.append((raw, dict(context), language, list(vocab)))
        return "Done."

    client, _hub, _log = listening_client(tmp_path, polish=polish)
    good: dict[str, Any] = {
        "raw": "um done",
        "context": {"app": "Ghostty"},
        "language": "en",
        "vocab": ["Typlus"],
    }
    refused: list[dict[str, Any]] = [
        {**good, "vocab": "Typlus"},
        {**good, "vocab": ["x" * 201]},
        {**good, "vocab": ["w"] * 2001},
        {**good, "context": {"before": "b" * 1001}},
        {**good, "raw": 3},
        {**good, "language": "x" * 100},
    ]
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "polish", good, "a" * 32)
        answered = ws.receive_json()
        replies = []
        for index, args in enumerate(refused):
            ask_op(ws, "polish", args, f"{index:032d}")
            replies.append(ws.receive_json())
    assert answered == {"type": "reply", "id": "a" * 32, "ok": True, "value": {"text": "Done."}}
    assert seen == [
        (
            "um done",
            {"app": "Ghostty", "window": "", "selected": "", "before": ""},
            "en",
            ["Typlus"],
        )
    ]
    assert [(reply["ok"], reply["code"]) for reply in replies] == [(False, "bad_args")] * 6

    (tmp_path / "bare").mkdir()
    bare, _hub, _log = listening_client(tmp_path / "bare")
    with ExitStack() as stack:
        ws = connect_listening(bare, tmp_path / "bare", "macbook", stack)
        ask_op(ws, "polish", good, "b" * 32)
        assert ws.receive_json()["code"] == "not_supported"


def test_a_brain_that_does_not_listen_says_so_and_a_tell_to_it_is_dropped(tmp_path: Path) -> None:
    """`ready` names it, and an ask is answered with a refusal rather than left to time out."""
    token = pair_device(tmp_path, "macbook")
    client, hub, log = brain_client(tmp_path)
    assert hub.listening is None
    with client.websocket_connect(TERMINAL_WS, headers=bearer(token)) as ws:
        ws.send_text(hello_frame(voice=True, rows_after=None))
        assert ws.receive_json()["listen"] is False
        ask_op(ws, "utterance", utterance_frame("T1"), "a" * 32)
        refusal = ws.receive_json()
        ask_op(ws, "hold", {"held": True}, None)
        ask_op(ws, "working", {}, "b" * 32)
        second = ws.receive_json()
    assert (refusal["ok"], refusal["code"]) == (False, "not_listening")
    assert second["code"] == "not_listening"
    assert logged_utterances(log) == []


def test_an_ask_waits_for_the_tells_sent_before_it_and_runs_them_in_order(tmp_path: Path) -> None:
    """A tell precedes every ask sent after it, whatever the hook's speed."""
    from contextlib import ExitStack  # noqa: PLC0415

    seen: list[str] = []

    def hold(held: bool) -> None:  # noqa: FBT001
        seen.append(f"hold:{held}")

    def say(turn: str, reason: str, text: str) -> None:
        del reason, text
        seen.append(f"say:{turn}:start")
        time.sleep(0.2)
        seen.append(f"say:{turn}:end")

    def working_hook() -> bool:
        seen.append("working")
        return True

    def controls_hook() -> dict[str, Any]:
        seen.append("controls")
        return {"conversation": True, "quiet": "off"}

    client, _hub, _log = listening_client(
        tmp_path, hold_runs=hold, answer_words=say, turn_working=working_hook,
        controls=controls_hook,
    )
    with ExitStack() as stack:
        ws = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(ws, "hold", {"held": True}, None)
        ask_op(ws, "say", {"turn_id": "T1", "reason": "stop", "text": "ok"}, None)
        ask_op(ws, "working", {}, "a" * 32)
        ask_op(ws, "controls", {}, "b" * 32)
        replies = {reply["id"]: reply for reply in (ws.receive_json(), ws.receive_json())}
    assert seen[:3] == ["hold:True", "say:T1:start", "say:T1:end"]
    assert sorted(seen[3:]) == ["controls", "working"]  # answered asks run beside each other
    assert replies["a" * 32]["value"] == {"working": True}
    assert replies["b" * 32]["value"] == {"conversation": True, "quiet": "off"}


def test_the_answer_goes_to_the_terminal_that_heard_the_utterance(tmp_path: Path) -> None:
    """Not the one connected last: the turn is routed from the utterance, before its row exists."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, hub, log = listening_client(tmp_path)
    with ExitStack() as stack:
        heard = connect_listening(client, tmp_path, "macbook", stack)
        last = connect_listening(
            client, tmp_path, "ipad", stack
        )  # connected after: the old default
        ask_op(heard, "utterance", utterance_frame("T-M"), "a" * 32)
        assert heard.receive_json()["ok"] is True
        mine = append_answer(log, "T-M", "For the macbook.")
        other = append_answer(log, "T-X", "For whoever is last.")  # a turn nobody routed
        assert [row["event_uid"] for row in ws_rows(heard, 3)] == mine
        assert [row["event_uid"] for row in ws_rows(last, 3)] == other
        # Whatever a terminal was sent comes before the answer to its next frame: the ack must
        # be the first thing each receives after its own rows, so nothing else came.
        probe = playback_frame("1" * 32)
        assert ack_frame(heard, probe)["type"] == "ack"
        assert ack_frame(last, playback_frame("2" * 32))["type"] == "ack"
    assert [node for _uid, node, _p in logged_utterances(log)] == ["macbook"]
    assert hub.voice is not None


def test_a_turn_heard_by_a_terminal_that_then_left_is_spoken_by_the_one_connected(
    tmp_path: Path,
) -> None:
    """The route is where the owner was; with that terminal gone, the answer is not lost."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, _hub, log = listening_client(tmp_path)
    with ExitStack() as stack:
        stay = connect_listening(client, tmp_path, "ipad", stack)
        with ExitStack() as short:
            went = connect_listening(client, tmp_path, "macbook", short)
            ask_op(went, "utterance", utterance_frame("T-M"), "a" * 32)
            assert went.receive_json()["ok"] is True
        # The macbook was connected last, so it is gone and the ipad is the only one.
        wait_for(lambda: len(_hub.connected()) == 1, "the macbook never left")
        uids = append_answer(log, "T-M", "Still heard.")
        assert [row["event_uid"] for row in ws_rows(stay, 3)] == uids


# --- the brain's commands to a terminal ---------------------------------------------------------


def _serve_call(ws: Any, output: dict[str, Any], seen: list[dict[str, Any]]) -> threading.Thread:  # noqa: ANN401
    """Answer the next ``call`` frame of ``ws`` with ``output``, on a thread of its own."""

    def run() -> None:
        frame = ws.receive_json()
        seen.append(frame)
        ws.send_text(
            json.dumps({"type": "result", "id": frame["id"], "ok": True, "output": output})
        )

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_drop_and_stop_commands_reach_the_terminals_that_hold_the_playback(
    tmp_path: Path,
) -> None:
    """Each turn goes to the terminal that heard it; a turn nobody routed to the newest."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, hub, _log = listening_client(tmp_path)
    assert hub.listening is not None
    listening = hub.listening
    with ExitStack() as stack:
        mac = connect_listening(client, tmp_path, "macbook", stack)
        ipad = connect_listening(client, tmp_path, "ipad", stack)
        ask_op(mac, "utterance", utterance_frame("T-M"), "a" * 32)
        assert mac.receive_json()["ok"] is True
        ask_op(ipad, "utterance", utterance_frame("T-I"), "b" * 32)
        assert ipad.receive_json()["ok"] is True

        seen_mac: list[dict[str, Any]] = []
        seen_ipad: list[dict[str, Any]] = []
        servers = [
            _serve_call(mac, {"dropped": ["T-M"]}, seen_mac),
            _serve_call(ipad, {"dropped": ["T-I"]}, seen_ipad),  # T-N began playing: not named
        ]
        dropped = listening.drop_unspoken(frozenset({"T-M", "T-I", "T-N"}))
        for server in servers:
            server.join(timeout=5)
        assert dropped == frozenset({"T-M", "T-I"})
        assert (seen_mac[0]["tool"], seen_mac[0]["arguments"]) == (
            DROP_UNSPOKEN,
            {"turn_ids": ["T-M"]},
        )
        assert seen_ipad[0]["arguments"] == {"turn_ids": ["T-I", "T-N"]}

        # The terminal that heard the owner last is the one a correction stops.
        seen_stop: list[dict[str, Any]] = []
        server = _serve_call(ipad, {"outcome": "applied"}, seen_stop)
        assert listening.stop_output("correction") == "applied"
        server.join(timeout=5)
        assert (seen_stop[0]["tool"], seen_stop[0]["arguments"]) == (
            STOP_OUTPUT,
            {"reason": "correction"},
        )


def test_a_terminal_that_does_not_answer_a_command_drops_and_stops_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Said plainly as no drop (so the run is left running) and as unreachable."""
    from contextlib import ExitStack  # noqa: PLC0415

    monkeypatch.setattr(terminal_listen, "COMMAND_TIMEOUT_S", 0.2)
    client, hub, _log = listening_client(tmp_path)
    assert hub.listening is not None
    with ExitStack() as stack:
        mac = connect_listening(client, tmp_path, "macbook", stack)
        ask_op(mac, "utterance", utterance_frame("T-M"), "a" * 32)
        assert mac.receive_json()["ok"] is True
        assert hub.listening.drop_unspoken(frozenset({"T-M"})) == frozenset()
        assert hub.listening.stop_output("correction") == "unreachable"
    assert hub.listening.stop_output("correction") == "no_terminal"
    assert hub.listening.drop_unspoken(frozenset({"T-M"})) == frozenset()


def test_the_terminals_executor_runs_the_two_commands_on_its_media_actor() -> None:
    """The two names are not menu tools; every other tool goes on to the ordinary executor."""
    calls: list[tuple[Any, ...]] = []

    class Actor:
        def drop_unspoken(self, turn_ids: frozenset[str]) -> frozenset[str]:
            calls.append(("drop", turn_ids))
            return frozenset(turn_ids - {"T-PLAYING"})

        def stop_foreground_output(self, response_id: str | None, *, reason: str = "") -> str:
            calls.append(("stop", response_id, reason))
            return "applied"

    def ordinary(tool: str, _arguments: Any, ref: str | None) -> dict[str, Any]:  # noqa: ANN401
        calls.append(("ordinary", tool, ref))
        return {"ok": True, "output": {"tool": tool}}

    run = with_voice_commands(ordinary, Actor())
    assert run(DROP_UNSPOKEN, {"turn_ids": ["T1", "T-PLAYING", 5]}, None) == {
        "ok": True,
        "output": {"dropped": ["T1"]},
    }
    assert run(STOP_OUTPUT, {"reason": "correction"}, None) == {
        "ok": True,
        "output": {"outcome": "applied"},
    }
    assert run(STOP_OUTPUT, {}, None)["output"] == {"outcome": "applied"}
    assert run("read_clipboard", {}, "ref") == {"ok": True, "output": {"tool": "read_clipboard"}}
    assert calls == [
        ("drop", frozenset({"T1", "T-PLAYING"})),
        ("stop", None, "correction"),
        ("stop", None, "user_stop"),
        ("ordinary", "read_clipboard", "ref"),
    ]


# --- the capture session on a terminal ------------------------------------------------------


def _say(path: Path) -> Path:
    """One second of room, then 0.6 s of voice; the replay goes on with silence."""
    quiet = np.zeros(16_000, dtype="<i2")
    voice = np.full(int(0.6 * 16_000), 10_000, dtype="<i2")
    return write_wav(path, np.concatenate([quiet, voice]))


def test_a_microphone_that_cannot_be_opened_leaves_a_terminal_that_still_speaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Said once and clearly, not retried, and the answer in the brain's log is still played."""
    starts: list[int] = []
    wav = _say(tmp_path / "unused.wav")

    class Busy(voice_backend.FileReplayBackend):
        def start(self, **kwargs: Any) -> voice_backend.BackendStartResult:  # noqa: ANN401
            starts.append(kwargs["stream_epoch"])
            return voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OWNER_BUSY,
                stream_epoch=kwargs["stream_epoch"],
                profile=None,
                reason="another process holds the microphone",
                attempt_id=kwargs["attempt_id"],
            )

    power = heard_session(monkeypatch, lambda: Busy(wav))
    token = pair_device(tmp_path, "macbook")
    brain = VoiceBrain(tmp_path)
    brain.hub.listening = BrainListening(brain.hub, brain.hub.events)  # type: ignore[arg-type]
    root = tmp_path / "terminal-root"
    with (
        caplog.at_level(logging.INFO, logger="jarvis"),
        brain,
        VoiceTerminal(brain.url, token, root, LISTEN_CONFIG) as terminal,
    ):
        wait_for(
            lambda: bool(brain.hub.voice and brain.hub.voice._peers),  # noqa: SLF001
            "the terminal never connected as a voice terminal",
        )
        wait_for(
            lambda: any("does not listen" in r.getMessage() for r in caplog.records),
            "the terminal never said it could not listen",
        )
        time.sleep(1.5)  # a hot loop would have tried again by now
        append_answer(brain.log, "T1", "Still speaking.")
        wait_for(
            lambda: "surface.playback_completed" in logged_types(brain.log),
            "a terminal without a microphone did not speak",
        )
        assert terminal.error is None
    assert len(starts) == 1
    assert power.installs == 0  # no capture session, so no power hooks to carry
    refusal = [r for r in caplog.records if "does not listen" in r.getMessage()]
    assert len(refusal) == 1
    assert refusal[0].levelno == logging.ERROR
    assert "stop that one and restart" in refusal[0].getMessage()
    assert "utterance.received" not in logged_types(brain.log)


def test_a_voice_terminals_log_survives_portaudio_initialising_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2026-10-21 04:03: the first ``import sounddevice`` of a terminal hung in CoreAudio.

    sounddevice points fd 2 at /dev/null around ``Pa_Initialize``, so every line any thread
    logged meanwhile went nowhere, among them the reason the microphone did not open. A voice
    terminal's log writes to its own copy of stderr instead.
    """

    async def stuck(*_args: object, **_kwargs: object) -> None:
        with stderr_sent_nowhere():
            logging.getLogger("jarvis.runtime.inherent_loop").warning("said while stuck")
        logging.getLogger("jarvis.runtime.inherent_loop").warning("said after")

    monkeypatch.setattr(terminal_module, "_run", stuck)
    err = tmp_path / "terminal.err.log"
    with stderr_in(err):
        code = terminal_module.run_terminal(
            "http://127.0.0.1:1", "t", runtime_root=tmp_path / "root", voice=True,
        )
    assert code == 0
    lines = err.read_text(encoding="utf-8").splitlines()
    assert "jarvis.runtime.inherent_loop: said while stuck" in lines
    assert "jarvis.runtime.inherent_loop: said after" in lines


def test_a_listening_terminal_runs_the_device_watch_and_the_power_observer_and_ends_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """ADR 0054 and ADR-0009 D3 on a terminal: the daemon's two watches beside the microphone.

    Both are the daemon's own functions over one power coordinator that holds this terminal's
    capture session and media actor. A change of the default microphone is re-read (the log line
    the daemon prints), the system's sleep notice reaches the coordinator, the Settings page's
    device pick reaches the watch, and ending the terminal shuts the observer and the watch.
    """
    wav = write_wav(tmp_path / "room.wav", np.zeros(16_000 * 20, dtype="<i2"))  # nobody speaks
    power = heard_session(monkeypatch, lambda: voice_backend.FileReplayBackend(wav))
    default_mic = [1]
    monkeypatch.setattr(
        voice_backend,
        "coreaudio_devices",
        lambda kind: (default_mic[0] if kind == "input" else 9, {}),
    )
    monkeypatch.setattr(devices, "AUDIO_DEVICE_POLL_S", 0.05)
    refreshed: list[tuple[str | None, str | None]] = []

    def refresh(_self: object, *, input_device: str | None, output_device: str | None) -> str:
        """The re-read itself is test_audio_device_refresh's; PortAudio is the machine's."""
        refreshed.append((input_device, output_device))
        return "refreshed"

    monkeypatch.setattr(voice_power.VoicePowerCoordinator, "refresh_devices", refresh)
    watches: list[asyncio.Task[None]] = []
    start_watch = devices.start_audio_device_watch

    def record(*args: Any) -> asyncio.Task[None]:  # noqa: ANN401
        watches.append(start_watch(*args))
        return watches[-1]

    monkeypatch.setattr(terminal_module, "start_audio_device_watch", record)
    page = Settings(tmp_path / "page", LISTEN_CONFIG, lambda _kind: [])
    token = pair_device(tmp_path, "macbook")
    brain = VoiceBrain(tmp_path)
    brain.hub.listening = BrainListening(brain.hub, brain.hub.events)  # type: ignore[arg-type]
    with (
        caplog.at_level(logging.INFO, logger="jarvis"),
        brain,
        VoiceTerminal(
            brain.url, token, tmp_path / "terminal-root", LISTEN_CONFIG, page
        ) as terminal,
    ):
        wait_for(lambda: power.hooks is not None and bool(watches), "no watch started", 40)
        assert power.installs == 1
        assert not watches[0].done()
        assert page.on_devices is not None  # a pick on the page reaches the watch
        assert power.hooks is not None
        before_sleep, on_wake = power.hooks
        coordinator = before_sleep.__self__
        assert isinstance(coordinator, voice_power.VoicePowerCoordinator)
        assert on_wake.__self__ is coordinator
        time.sleep(0.3)  # the watch has taken its first look
        default_mic[0] = 2  # the microphone the system defaults to has changed
        wait_for(
            lambda: any("audio devices re-read" in r.getMessage() for r in caplog.records),
            "the device watch never re-read the devices",
        )
        assert refreshed == [(None, None)]  # the pick is the system default for both
        before_sleep()
        assert coordinator.asleep
        assert terminal.error is None
        assert power.shutdowns == 0
    assert power.shutdowns == 1
    assert watches[0].done()
    assert power.log is not None
    with pytest.raises(sqlite3.ProgrammingError):
        power.log.execute("SELECT 1")  # the observer's log is closed with it


def test_the_reSpeaker_look_is_kept_while_a_terminal_runs_and_a_pick_on_the_page_reaches_it(  # noqa: N802
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR 0147: no look saved, no writes; a saved look is put back; the page's save is applied.

    The board is a recorder in place of the USB write. The terminal here neither speaks nor
    listens: the board look needs no voice. Ending the terminal ends the watch.
    """
    looks: list[dict[str, Any]] = []

    def ensure(look: dict[str, Any]) -> str:
        looks.append(dict(look))
        return "kept"

    monkeypatch.setattr(respeaker_board, "ensure", ensure)
    monkeypatch.setattr(devices, "BOARD_POLL_S", 0.02)

    async def link(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(terminal_module, "run_terminal_client", link)
    page = Settings(tmp_path, {}, lambda _kind: [])

    async def main() -> None:
        task = asyncio.create_task(
            terminal_module._run(  # noqa: SLF001
                "http://127.0.0.1:1",
                "t",
                tools=frozenset(),
                execute=lambda *_: {},
                watched=None,
                page=page,
            )
        )
        await asyncio.sleep(0.3)
        assert looks == []  # nothing saved on the page: nothing written to the board
        assert page.on_board is not None
        page.update({"board_light": "solid"})
        await asyncio.sleep(0.3)
        assert looks
        assert looks[-1]["light"] == "solid"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        settled = len(looks)
        await asyncio.sleep(0.2)
        assert len(looks) == settled  # ended with the terminal

    asyncio.run(main())


def test_a_terminal_hears_the_owner_and_the_brains_answer_is_played_where_it_was_heard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path over a real websocket, and the latency number read from the trace.

    A replayed WAV runs through the real capture session (wake, VAD, endpointing, the real
    pipeline with a fake recognizer) of a terminal built by the real wiring. Its utterance must
    be in the brain's log under the terminal's name, with the audio kept on the terminal's disk;
    the brain answers with rows and a fake provider; the terminal plays them and the log ends
    with what it heard. The trace of the terminal alone then names the end-of-speech to
    first-audible time, as the report tool reads it on a daemon.
    """
    wav = _say(tmp_path / "say.wav")
    heard_session(monkeypatch, lambda: voice_backend.FileReplayBackend(wav))
    trace = tmp_path / "trace.jsonl"
    reset_realtime_trace()
    configure_realtime_trace_jsonl(trace)
    token = pair_device(tmp_path, "macbook")
    root = tmp_path / "terminal-root"
    brain = VoiceBrain(tmp_path)
    working = threading.Event()
    brain.hub.listening = BrainListening(brain.hub, brain.hub.events)  # type: ignore[arg-type]
    brain.hub.listening.hooks = ListenHooks(
        turn_working=working.is_set,
        recent_speech=lambda: "",
    )
    try:
        with brain, VoiceTerminal(brain.url, token, root, LISTEN_CONFIG) as terminal:
            wait_for(
                lambda: "utterance.received" in logged_types(brain.log), "nothing was heard", 40
            )
            conn = open_event_log(brain.log)
            [heard] = [e for e in iter_events(conn) if e.type == "utterance.received"]
            turn = str(heard.payload["turn_id"])
            working.set()
            append_answer(brain.log, turn, "Done, the light is on.")
            wait_for(
                lambda: "surface.playback_completed" in logged_types(brain.log),
                "the terminal never played the brain's answer",
            )
            completed = [e for e in iter_events(conn) if e.type == "surface.playback_completed"]
            with sqlite3.connect(brain.log) as raw:
                node = raw.execute(
                    "SELECT ingestion_node FROM events WHERE type = 'utterance.received'",
                ).fetchone()[0]
            conn.close()
            assert terminal.error is None
    finally:
        configure_realtime_trace_jsonl(None)  # drains the exporter before the file is read

    assert node == "macbook"
    assert heard.payload["transcript"] == "把灯打开。"
    assert heard.payload["channel"] == "inherent_wake"
    assert heard.correlation == {"turn_id": turn}
    [done] = completed
    assert done.payload["turn_id"] == turn
    assert done.payload["heard_text"] == "Done, the light is on."

    # The audio stays on the terminal: the reference is a path under its runtime root and the
    # file is there; the brain's side of the test directory has no audio at all.
    ref = Path(str(heard.payload["audio_artifact_ref"]))
    assert ref.is_relative_to(root)
    voice_artifact_store.flush()  # the file is written after the utterance is sent (ADR 0277)
    assert ref.exists()
    assert not (tmp_path / "memory").exists()

    report = summarize_trace(load_trace(trace), scenario="routine", turn_id=turn)
    durations = report["durations_ms"]
    assert 0 < durations["speech_end_to_first_audible_ms"] < 30_000
    assert durations["endpoint_candidate_to_first_audible_ms"] > 0
    assert (
        durations["speech_end_to_first_audible_ms"]
        >= durations["endpoint_candidate_to_first_audible_ms"]
    )


def test_the_terminals_broadcaster_logs_at_debug_and_drops(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A terminal has no surface of its own: partial captions and capability changes go nowhere."""
    quiet = terminal_module._QuietBroadcaster()  # noqa: SLF001
    with caplog.at_level(logging.DEBUG, logger="jarvis.runtime.terminal"):
        quiet.broadcast_voice_sync("listening", turn_id="T1", text="partial")
        quiet.broadcast_voice_capability_sync(
            version=1,
            state="running",
            stream_epoch=1,
            reason="started",
            wake_available=True,
            local_capture_available=True,
            ptt_upload_available=False,
            text_available=True,
        )
        quiet.broadcast_op_sync("anything", a=1)
    assert [r.levelno for r in caplog.records] == [logging.DEBUG] * 3
