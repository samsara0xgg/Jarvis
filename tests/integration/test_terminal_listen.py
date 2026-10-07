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
- one end-to-end run over a real websocket: a replayed WAV, heard by a real capture session with a
  fake recognizer, becomes an ``utterance.received`` in the brain's log under the terminal's
  name, the brain's answer is played by the terminal, and the end-of-speech to first-audible
  number is read from the trace.
"""

# ruff: noqa: FBT003 - the capture session's callbacks take their one bit positionally.

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from jarvis.runtime import terminal as terminal_module
from jarvis.shared.realtime_trace import configure_realtime_trace_jsonl, reset_realtime_trace
from jarvis.state.device_tokens import pair_device
from jarvis.state.event_log import iter_events, open_event_log
from jarvis.surface import (
    terminal_listen,
    voice_asr,
    voice_audio,
    voice_backend,
    voice_tts,
    voice_wake,
)
from jarvis.surface.terminal_listen import (
    DROP_UNSPOKEN,
    STOP_OUTPUT,
    BrainListening,
    LinkedControls,
    LinkedTurn,
    ListenHooks,
    with_voice_commands,
)
from jarvis.surface.terminal_speaker import BrainCallError, Journal, VoiceLink
from tests.integration.test_terminal_voice import (
    SPEECH_CONFIG,
    TERMINAL_WS,
    _ack,
    _answer,
    _bearer,
    _Brain,
    _brain_client,
    _hello,
    _open_stream,
    _playback_frame,
    _rows,
    _Terminal,
    _types,
    _wait_for,
)
from tests.integration.test_voice_file_replay import _EnergySession, _write_wav
from tools.realtime_trace_report import load_trace, summarize_trace

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from jarvis.surface.terminal_link import TerminalHub


# --- the terminal's end, against a brain we play by hand --------------------------------------


class _Bench:
    """A terminal's VoiceLink on a loop of its own; ``answer`` plays the brain, or is silent."""

    def __init__(
        self, tmp_path: Path,
        answer: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
    ) -> None:
        self.frames: list[dict[str, Any]] = []
        self.answer = answer
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.link = VoiceLink(Journal(tmp_path / "journal.db"))
        self.link.up(self._send, self.loop, {"type": "ready", "voice": True, "listen": True})

    async def _send(self, text: str) -> None:
        frame = json.loads(text)
        self.frames.append(frame)
        rid = frame.get("id")
        if rid is not None and self.answer is not None:
            reply = self.answer(frame)
            if reply is not None:
                self.link.on_frame({"type": "reply", "id": rid, **reply})

    def ops(self) -> list[str]:
        return [str(frame["op"]) for frame in self.frames]

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def _ok(value: object) -> dict[str, Any]:
    return {"ok": True, "value": value}


@pytest.fixture
def fast_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-call deadlines, short: what they guard is the fallback, not the wait."""
    for name in (
        "UTTERANCE_TIMEOUT_S", "WORDS_TIMEOUT_S", "WORKING_TIMEOUT_S", "RECENT_TIMEOUT_S",
        "INTERRUPT_TIMEOUT_S", "RUNS_TIMEOUT_S",
    ):
        monkeypatch.setattr(terminal_listen, name, 0.1)


def test_the_brains_answers_are_what_the_terminals_callbacks_return(tmp_path: Path) -> None:
    """Asks return what the brain said; tells carry no id and arrive in the order told."""

    def brain(frame: dict[str, Any]) -> dict[str, Any] | None:
        replies: dict[str, object] = {
            "words": {"choice": "stop"}, "working": {"working": True},
            "recent": {"text": "I said this."}, "interrupt": {"outcome": "cancelled"},
            "supersede": {}, "cancel_runs": {},
            "utterance": {"event_uid": "a" * 32},
        }
        return _ok(replies[frame["op"]])

    bench = _Bench(tmp_path, brain)
    try:
        turn = LinkedTurn(bench.link)
        assert turn.ask_words("T1", "stop", "I said", True, False) == "stop"
        assert turn.turn_working() is True
        assert turn.recent_speech() == "I said this."
        assert turn.interrupt("conversation_speech") == "cancelled"
        turn.supersede("T1")
        turn.cancel_runs()
        event = turn.emit_utterance({"transcript": "hi", "turn_id": "T1"}, {"turn_id": "T1"})
        assert (event.event_uid, event.type, event.correlation) == (
            "a" * 32, "utterance.received", {"turn_id": "T1"},
        )
        turn.hold_runs(True)
        turn.note_words("T1", "backchannel", "mm", False, True)
        turn.begin_line("T1", "hello", "", False, False)
        turn.answer_words("T1", "wait", "Okay.")
        _wait_for(lambda: len(bench.frames) == 11, "frames did not reach the brain")
    finally:
        bench.close()
    told = [frame for frame in bench.frames if "id" not in frame]
    assert [frame["op"] for frame in told] == ["hold", "note", "begin", "say"]
    assert told[1]["args"] == {
        "turn_id": "T1", "verdict": "backchannel", "text": "mm", "over_her": False,
        "conversation": True,
    }
    asked = [frame for frame in bench.frames if "id" in frame]
    assert [frame["op"] for frame in asked] == [
        "words", "working", "recent", "interrupt", "supersede", "cancel_runs", "utterance",
    ]
    assert all(frame["type"] == "ask" for frame in bench.frames)


@pytest.mark.parametrize("brain_is", ["silent", "refusing", "gone"])
@pytest.mark.usefixtures("fast_deadlines")
def test_a_call_with_a_deadline_keeps_its_fallback_when_the_brain_cannot_answer(
    tmp_path: Path, brain_is: str,
) -> None:
    """The word judge's absence is a turn, nothing in flight, nothing said, a stop that stops."""
    refuse: dict[str, Any] = {"ok": False, "code": "not_listening", "message": "no"}
    bench = _Bench(tmp_path, (lambda _frame: refuse) if brain_is == "refusing" else None)
    try:
        if brain_is == "gone":
            bench.link.down()
        turn = LinkedTurn(bench.link)
        started = time.monotonic()
        assert turn.ask_words("T1", "stop", "", True, False) is None
        assert turn.turn_working() is False
        assert turn.recent_speech() == ""
        assert turn.interrupt("conversation_speech") == "brain_unreachable"
        turn.supersede("T1")  # neither raises: the local stop already happened
        turn.cancel_runs()
        assert time.monotonic() - started < 3.0
        with pytest.raises(BrainCallError):
            turn.emit_utterance({"transcript": "hi", "turn_id": "T1"}, {"turn_id": "T1"})
        turn.hold_runs(True)  # fire and forget, never an error
        turn.note_words("T1", "turn", "hi", False, False)
        time.sleep(0.2)
        sent = bench.ops()
    finally:
        bench.close()
    if brain_is == "gone":
        assert sent == []  # a tell to a gone brain is dropped, not queued for later
    else:
        assert sent[:7] == [
            "words", "working", "recent", "interrupt", "supersede", "cancel_runs", "utterance",
        ]


def test_an_ask_in_flight_fails_at_once_when_the_link_drops(tmp_path: Path) -> None:
    """A call with a long deadline does not wait it out once the link is known to be down."""
    bench = _Bench(tmp_path)  # the brain never answers
    outcome: list[BaseException | object] = []

    def asker() -> None:
        try:
            outcome.append(bench.link.ask("working", {}, timeout_s=30.0))
        except BrainCallError as exc:
            outcome.append(exc)

    try:
        thread = threading.Thread(target=asker)
        thread.start()
        _wait_for(lambda: bool(bench.frames), "the ask never left")
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
    bench = _Bench(tmp_path)
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
            return _ok(dict(state))
        taken.append((op, args))
        if op == "conversation":
            state["conversation"] = args["on"]
        if op == "quiet":
            state["quiet"] = args["level"]
        return _ok({})

    bench = _Bench(tmp_path, brain)
    left: list[bool] = []
    controls = LinkedControls(bench.link, poll_s=0.02)
    controls.on_surface_exit = lambda: left.append(True)
    controls.start()
    try:
        controls.set_conversation(True, "wake")
        _wait_for(lambda: ("conversation", {"on": True, "reason": "wake"}) in taken, "not taken")
        assert controls.conversation() is True
        time.sleep(0.15)  # polls come and go; the brain agrees, so nothing flips back
        assert controls.conversation() is True
        controls.set_quiet("quiet")
        _wait_for(lambda: state["quiet"] == "quiet", "quiet never reached the brain")
        state["conversation"] = False  # the owner switched it off from a surface
        _wait_for(lambda: controls.conversation() is False, "the terminal never followed")
        _wait_for(lambda: left == [True], "the session was not told to leave conversation")
        state["conversation"] = True  # and on again: mirrored, no exit callback
        _wait_for(controls.conversation, "the terminal never followed it back on")
    finally:
        controls.stop()
        bench.close()
    assert left == [True]


# --- the brain's end of /terminal/ws ----------------------------------------------------------


def _listening_client(
    tmp_path: Path, **hooks: Any,  # noqa: ANN401 — whichever hooks a test binds.
) -> tuple[TestClient, TerminalHub, Path]:
    client, hub, log = _brain_client(tmp_path)
    listening = BrainListening(hub, hub.events)  # type: ignore[arg-type]
    listening.hooks = ListenHooks(**hooks)
    hub.listening = listening
    return client, hub, log


def _ask(ws: Any, op: str, args: dict[str, Any] | None = None, rid: str | None = "r" * 32) -> None:  # noqa: ANN401
    frame: dict[str, Any] = {"type": "ask", "op": op, "args": args or {}}
    if rid is not None:
        frame["id"] = rid
    ws.send_text(json.dumps(frame))


def _utterance(turn: str, said: str = "turn on the light", **extra: object) -> dict[str, Any]:
    return {
        "transcript": said, "turn_id": turn, "utterance_id": f"U-{turn}",
        "channel": "inherent_wake", "confidence": 0.9, "language": "zh",
        **extra,
    }


def _utterances(log: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(log) as conn:
        return conn.execute(
            "SELECT event_uid, ingestion_node, payload_json FROM events "
            "WHERE type = 'utterance.received' ORDER BY id",
        ).fetchall()


def _connect(client: TestClient, tmp_path: Path, name: str, stack: Any) -> Any:  # noqa: ANN401
    ws = stack.enter_context(
        client.websocket_connect(TERMINAL_WS, headers=_bearer(pair_device(tmp_path, name))),
    )
    ws.send_text(_hello(voice=True, rows_after=None))
    ready = ws.receive_json()
    assert ready["listen"] is True
    return ws


def test_an_utterance_is_written_once_under_the_devices_name_however_often_it_is_sent(
    tmp_path: Path,
) -> None:
    """Idempotent by utterance id; the payload is the pipeline's fields, the audio a reference."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, _hub, log = _listening_client(tmp_path)
    ref = "/Users/allen/.jarvis/memory/audio/U-T1.wav"
    with ExitStack() as stack:
        ws = _connect(client, tmp_path, "macbook", stack)
        _ask(ws, "utterance", _utterance("T1", audio_artifact_ref=ref, unknown_key="x"), "a" * 32)
        first = ws.receive_json()
        _ask(ws, "utterance", _utterance("T1", audio_artifact_ref=ref), "b" * 32)  # a retry
        second = ws.receive_json()
        _ask(ws, "utterance", _utterance("T2"), "c" * 32)
        third = ws.receive_json()
    assert (first["type"], first["id"], first["ok"]) == ("reply", "a" * 32, True)
    assert second["value"] == first["value"]  # the same event, not a second one
    assert third["value"]["event_uid"] != first["value"]["event_uid"]
    rows = _utterances(log)
    assert [(uid, node) for uid, node, _ in rows] == [
        (first["value"]["event_uid"], "macbook"), (third["value"]["event_uid"], "macbook"),
    ]
    payload = json.loads(rows[0][2])
    assert payload == {
        "transcript": "turn on the light", "turn_id": "T1", "channel": "inherent_wake",
        "confidence": 0.9, "language": "zh", "audio_artifact_ref": ref, "utterance_id": "U-T1",
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

    client, _hub, log = _listening_client(tmp_path)
    bad_utterances = [
        {**_utterance("T1"), "transcript": ""},
        {**_utterance("T1"), "channel": "text"},
        {**_utterance("../T1")},
        {**_utterance("T1"), "utterance_id": 7},
        {**_utterance("T1"), "language": "x" * 100},
        {**_utterance("T1"), "confidence": "high"},
    ]
    with ExitStack() as stack:
        ws = _connect(client, tmp_path, "macbook", stack)
        replies = []
        for index, args in enumerate(bad_utterances):
            _ask(ws, "utterance", args, f"{index:032d}")
            replies.append(ws.receive_json())
        _ask(ws, "no_such_op", {}, "e" * 32)
        replies.append(ws.receive_json())
        _ask(ws, "quiet", {"level": "deafening"}, "f" * 32)
        replies.append(ws.receive_json())
        _ask(ws, "words", {"turn_id": "T1", "text": "x", "recent": "", "over_her": "yes",
                           "confirm": False}, "1" * 32)
        replies.append(ws.receive_json())
        _ask(ws, "working", {}, "2" * 32)
        alive = ws.receive_json()
    assert [(reply["ok"], reply.get("code")) for reply in replies] == [
        (False, "bad_args"), (False, "bad_args"), (False, "bad_args"), (False, "bad_args"),
        (False, "bad_args"), (False, "bad_args"), (False, "unknown_op"), (False, "bad_args"),
        (False, "bad_args"),
    ]
    assert alive == {"type": "reply", "id": "2" * 32, "ok": True, "value": {"working": False}}
    assert _utterances(log) == []


def test_a_brain_that_does_not_listen_says_so_and_a_tell_to_it_is_dropped(tmp_path: Path) -> None:
    """`ready` names it, and an ask is answered with a refusal rather than left to time out."""
    token = pair_device(tmp_path, "macbook")
    client, hub, log = _brain_client(tmp_path)
    assert hub.listening is None
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True, rows_after=None))
        assert ws.receive_json()["listen"] is False
        _ask(ws, "utterance", _utterance("T1"), "a" * 32)
        refusal = ws.receive_json()
        _ask(ws, "hold", {"held": True}, None)
        _ask(ws, "working", {}, "b" * 32)
        second = ws.receive_json()
    assert (refusal["ok"], refusal["code"]) == (False, "not_listening")
    assert second["code"] == "not_listening"
    assert _utterances(log) == []


def test_an_ask_waits_for_the_tells_sent_before_it_and_runs_them_in_order(tmp_path: Path) -> None:
    """The judge's note and request precede the line's words, whatever the hooks' speed."""
    from contextlib import ExitStack  # noqa: PLC0415

    seen: list[str] = []

    def hold(held: bool) -> None:  # noqa: FBT001
        seen.append(f"hold:{held}")

    def begin(turn: str, text: str, recent: str, over: bool, conv: bool) -> None:  # noqa: FBT001
        del text, recent, over, conv
        seen.append(f"begin:{turn}:start")
        time.sleep(0.2)
        seen.append(f"begin:{turn}:end")

    def ask(turn: str, text: str, recent: str, over: bool, confirm: bool) -> str:  # noqa: FBT001
        del text, recent, over, confirm
        seen.append(f"words:{turn}")
        return "stop"

    def working_hook() -> bool:
        seen.append("working")
        return True

    client, _hub, _log = _listening_client(
        tmp_path, hold_runs=hold, begin_line=begin, ask_words=ask,
        turn_working=working_hook,
    )
    with ExitStack() as stack:
        ws = _connect(client, tmp_path, "macbook", stack)
        _ask(ws, "hold", {"held": True}, None)
        _ask(ws, "begin", {"turn_id": "T1", "text": "stop", "recent": "", "over_her": True,
                           "conversation": False}, None)
        _ask(ws, "words", {"turn_id": "T1", "text": "stop", "recent": "", "over_her": True,
                           "confirm": False}, "a" * 32)
        _ask(ws, "working", {}, "b" * 32)
        replies = {reply["id"]: reply for reply in (ws.receive_json(), ws.receive_json())}
    assert seen[:3] == ["hold:True", "begin:T1:start", "begin:T1:end"]
    assert sorted(seen[3:]) == ["words:T1", "working"]  # answered asks run beside each other
    assert replies["a" * 32]["value"] == {"choice": "stop"}
    assert replies["b" * 32]["value"] == {"working": True}


def test_the_answer_goes_to_the_terminal_that_heard_the_utterance(tmp_path: Path) -> None:
    """Not the one connected last: the turn is routed from the utterance, before its row exists."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, hub, log = _listening_client(tmp_path)
    with ExitStack() as stack:
        heard = _connect(client, tmp_path, "macbook", stack)
        last = _connect(client, tmp_path, "ipad", stack)  # connected after: the old default
        _ask(heard, "utterance", _utterance("T-M"), "a" * 32)
        assert heard.receive_json()["ok"] is True
        mine = _answer(log, "T-M", "For the macbook.")
        other = _answer(log, "T-X", "For whoever is last.")  # a turn nobody routed
        assert [row["event_uid"] for row in _rows(heard, 3)] == mine
        assert [row["event_uid"] for row in _rows(last, 3)] == other
        # Whatever a terminal was sent comes before the answer to its next frame: the ack must
        # be the first thing each receives after its own rows, so nothing else came.
        probe = _playback_frame("1" * 32)
        assert _ack(heard, probe)["type"] == "ack"
        assert _ack(last, _playback_frame("2" * 32))["type"] == "ack"
    assert [node for _uid, node, _p in _utterances(log)] == ["macbook"]
    assert hub.voice is not None


def test_a_turn_heard_by_a_terminal_that_then_left_is_spoken_by_the_one_connected(
    tmp_path: Path,
) -> None:
    """The route is where the owner was; with that terminal gone, the answer is not lost."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, _hub, log = _listening_client(tmp_path)
    with ExitStack() as stack:
        stay = _connect(client, tmp_path, "ipad", stack)
        with ExitStack() as short:
            went = _connect(client, tmp_path, "macbook", short)
            _ask(went, "utterance", _utterance("T-M"), "a" * 32)
            assert went.receive_json()["ok"] is True
        # The macbook was connected last, so it is gone and the ipad is the only one.
        _wait_for(lambda: len(_hub.connected()) == 1, "the macbook never left")
        uids = _answer(log, "T-M", "Still heard.")
        assert [row["event_uid"] for row in _rows(stay, 3)] == uids


# --- the brain's commands to a terminal ---------------------------------------------------------


def _serve_call(ws: Any, output: dict[str, Any], seen: list[dict[str, Any]]) -> threading.Thread:  # noqa: ANN401
    """Answer the next ``call`` frame of ``ws`` with ``output``, on a thread of its own."""

    def run() -> None:
        frame = ws.receive_json()
        seen.append(frame)
        ws.send_text(json.dumps({"type": "result", "id": frame["id"], "ok": True,
                                 "output": output}))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_drop_and_stop_commands_reach_the_terminals_that_hold_the_playback(
    tmp_path: Path,
) -> None:
    """Each turn goes to the terminal that heard it; a turn nobody routed to the newest."""
    from contextlib import ExitStack  # noqa: PLC0415

    client, hub, _log = _listening_client(tmp_path)
    assert hub.listening is not None
    listening = hub.listening
    with ExitStack() as stack:
        mac = _connect(client, tmp_path, "macbook", stack)
        ipad = _connect(client, tmp_path, "ipad", stack)
        _ask(mac, "utterance", _utterance("T-M"), "a" * 32)
        assert mac.receive_json()["ok"] is True
        _ask(ipad, "utterance", _utterance("T-I"), "b" * 32)
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
            DROP_UNSPOKEN, {"turn_ids": ["T-M"]},
        )
        assert seen_ipad[0]["arguments"] == {"turn_ids": ["T-I", "T-N"]}

        # The terminal that heard the owner last is the one a correction stops.
        seen_stop: list[dict[str, Any]] = []
        server = _serve_call(ipad, {"outcome": "applied"}, seen_stop)
        assert listening.stop_output("correction") == "applied"
        server.join(timeout=5)
        assert (seen_stop[0]["tool"], seen_stop[0]["arguments"]) == (
            STOP_OUTPUT, {"reason": "correction"},
        )


def test_a_terminal_that_does_not_answer_a_command_drops_and_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Said plainly as no drop (so the run is left running) and as unreachable."""
    from contextlib import ExitStack  # noqa: PLC0415

    monkeypatch.setattr(terminal_listen, "COMMAND_TIMEOUT_S", 0.2)
    client, hub, _log = _listening_client(tmp_path)
    assert hub.listening is not None
    with ExitStack() as stack:
        mac = _connect(client, tmp_path, "macbook", stack)
        _ask(mac, "utterance", _utterance("T-M"), "a" * 32)
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
        "ok": True, "output": {"dropped": ["T1"]},
    }
    assert run(STOP_OUTPUT, {"reason": "correction"}, None) == {
        "ok": True, "output": {"outcome": "applied"},
    }
    assert run(STOP_OUTPUT, {}, None)["output"] == {"outcome": "applied"}
    assert run("read_clipboard", {}, "ref") == {"ok": True, "output": {"tool": "read_clipboard"}}
    assert calls == [
        ("drop", frozenset({"T1", "T-PLAYING"})), ("stop", None, "correction"),
        ("stop", None, "user_stop"), ("ordinary", "read_clipboard", "ref"),
    ]


# --- the capture session on a terminal ------------------------------------------------------


class _Ears:
    """The final recognizer, replaced: always hears the same words."""

    def __init__(self, **_ignored: object) -> None:
        pass

    def prewarm(self) -> None:
        return None

    def warm(self) -> None:
        return None

    def partial_text(self, audio: bytes) -> str:
        del audio
        return "把灯打开。"

    def recognize(self, audio: bytes) -> voice_asr.TranscriptionResult:
        del audio
        return voice_asr.TranscriptionResult("把灯打开。", 0.9, "zh", None)


class _Wake:
    """A wake engine that always hears the wake word."""

    model_name = "replay"

    def start(self) -> None:
        return None

    def predict(self, _frame: bytes) -> dict[str, float]:
        return {self.model_name: 1.0}

    def reset(self) -> None:
        return None

    def close(self) -> None:
        return None


LISTEN_CONFIG: dict[str, Any] = {
    "realtime": {
        **SPEECH_CONFIG["realtime"],
        "single_audio_ingress": {
            "enabled": True, "echo_cancellation": False, "pre_roll_ms": 64,
            "max_utterance_s": 4.0, "min_voiced_s": 0.2, "session_worker_poll_s": 0.001,
            "partial_asr": {"enabled": True, "interval_ms": 32, "candidate_ms": 64,
                            "max_hold_ms": 160, "post_roll_ms": 32},
        },
    },
}
"""This terminal's own config: wake, VAD and endpointing knobs live here, not on the brain."""


def _heard(
    monkeypatch: pytest.MonkeyPatch, backend: Callable[[], voice_backend.AudioDuplexBackend],
) -> None:
    """Replace the machine under the capture session: models, microphone, speaker device."""
    monkeypatch.setattr(terminal_module, "_voice_models_preflight", lambda **_k: (True, []))
    monkeypatch.setattr(voice_asr, "SenseVoiceRecognizer", _Ears)
    monkeypatch.setattr(voice_wake, "MicroWakeWordEngine", _Wake)
    monkeypatch.setattr(voice_audio, "_load_silero_session", lambda *_a, **_k: _EnergySession())
    monkeypatch.setitem(
        voice_audio._MODE_THRESHOLDS,  # noqa: SLF001
        "record", voice_audio.VadThresholds(0.4, -45.0, 1, 1, 2),
    )
    monkeypatch.setattr(voice_backend, "SoundDeviceDuplexBackend", lambda **_k: backend())
    monkeypatch.setattr(voice_tts, "_open_output_stream", _open_stream(pull=True))
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)


def _say(path: Path) -> Path:
    """One second of room, then 0.6 s of voice; the replay goes on with silence."""
    quiet = np.zeros(16_000, dtype="<i2")
    voice = np.full(int(0.6 * 16_000), 10_000, dtype="<i2")
    return _write_wav(path, np.concatenate([quiet, voice]))


def test_a_microphone_that_cannot_be_opened_leaves_a_terminal_that_still_speaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Said once and clearly, not retried, and the answer in the brain's log is still played."""
    starts: list[int] = []
    wav = _say(tmp_path / "unused.wav")

    class Busy(voice_backend.FileReplayBackend):
        def start(self, **kwargs: Any) -> voice_backend.BackendStartResult:  # noqa: ANN401
            starts.append(kwargs["stream_epoch"])
            return voice_backend.BackendStartResult(
                status=voice_backend.BackendStartStatus.OWNER_BUSY,
                stream_epoch=kwargs["stream_epoch"], profile=None,
                reason="another process holds the microphone", attempt_id=kwargs["attempt_id"],
            )

    _heard(monkeypatch, lambda: Busy(wav))
    token = pair_device(tmp_path, "macbook")
    brain = _Brain(tmp_path)
    brain.hub.listening = BrainListening(brain.hub, brain.hub.events)  # type: ignore[arg-type]
    root = tmp_path / "terminal-root"
    with (
        caplog.at_level(logging.INFO, logger="jarvis"),
        brain,
        _Terminal(brain.url, token, root, LISTEN_CONFIG) as terminal,
    ):
        _wait_for(
            lambda: bool(brain.hub.voice and brain.hub.voice._peers),  # noqa: SLF001
            "the terminal never connected as a voice terminal",
        )
        _wait_for(
            lambda: any("does not listen" in r.getMessage() for r in caplog.records),
            "the terminal never said it could not listen",
        )
        time.sleep(1.5)  # a hot loop would have tried again by now
        _answer(brain.log, "T1", "Still speaking.")
        _wait_for(
            lambda: "surface.playback_completed" in _types(brain.log),
            "a terminal without a microphone did not speak",
        )
        assert terminal.error is None
    assert len(starts) == 1
    refusal = [r for r in caplog.records if "does not listen" in r.getMessage()]
    assert len(refusal) == 1
    assert refusal[0].levelno == logging.ERROR
    assert "stop that one and restart" in refusal[0].getMessage()
    assert "utterance.received" not in _types(brain.log)


def test_a_terminal_hears_the_owner_and_the_brains_answer_is_played_where_it_was_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
    _heard(monkeypatch, lambda: voice_backend.FileReplayBackend(wav))
    trace = tmp_path / "trace.jsonl"
    reset_realtime_trace()
    configure_realtime_trace_jsonl(trace)
    token = pair_device(tmp_path, "macbook")
    root = tmp_path / "terminal-root"
    brain = _Brain(tmp_path)
    working = threading.Event()
    brain.hub.listening = BrainListening(brain.hub, brain.hub.events)  # type: ignore[arg-type]
    brain.hub.listening.hooks = ListenHooks(
        turn_working=working.is_set, recent_speech=lambda: "",
    )
    try:
        with brain, _Terminal(brain.url, token, root, LISTEN_CONFIG) as terminal:
            _wait_for(lambda: "utterance.received" in _types(brain.log), "nothing was heard", 40)
            conn = open_event_log(brain.log)
            [heard] = [e for e in iter_events(conn) if e.type == "utterance.received"]
            turn = str(heard.payload["turn_id"])
            working.set()
            _answer(brain.log, turn, "Done, the light is on.")
            _wait_for(
                lambda: "surface.playback_completed" in _types(brain.log),
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
            version=1, state="running", stream_epoch=1, reason="started", wake_available=True,
            local_capture_available=True, ptt_upload_available=False, text_available=True,
        )
        quiet.broadcast_op_sync("anything", a=1)
    assert [r.levelno for r in caplog.records] == [logging.DEBUG] * 3
