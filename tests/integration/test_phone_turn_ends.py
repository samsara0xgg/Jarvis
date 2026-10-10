"""ADR 0222: a phone voice answer always ends, and only its own device follows it.

Live 2026-10-10 15:24 PT: the brain ran on the Pi, the Mac was a voice terminal and the iPhone
talked over ``/phone/ws``. A phone turn whose phone had closed its voice was answered and wrote
no ``surface.playback_*`` row, and the Mac's companion stayed on "speaking" for it until it was
restarted: the only ``spoken`` that reached it was the brain's ``no_voice``, which the terminal
drops because it speaks, and the Mac's actor was never given the phone's turn.

Shown, against the real watchers, a real ``InherentBroadcaster`` and, for the actor, the real
phone route and ``StreamingTTSPipeline`` of ``test_phone_voice``:

(a) a phone turn answered while its device has no voice actor (a phone that never asked for
voice, or is not connected) gets a ``surface.speech_dropped``, once, and the next turn is told
the answer was never spoken;
(b) a phone that leaves mid-answer ends its played answer by the actor's terminal and every
answer queued or still buffering behind it by a ``speech_dropped``, and an answer that finishes
after it left is not ended twice;
(c) a phone turn cancelled before playback gets an end row, with or without an actor, once;
(d) a client that is the Mac, or a voice terminal, is sent none of a phone turn's wire (open,
append, done, tool, voice, failed, cancelled), so its companion never answers or speaks for it,
and is still sent its own turns, the host's and those with no opening row; the phone's own
client is sent the phone's turn; a client with no device is sent everything;
(e) the push socket takes its device from the token it carries.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import sqlite3
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from fastapi.testclient import TestClient

from jarvis.runtime import inherent_loop
from jarvis.runtime.phone_voice import (
    UNPLAYED_LEFT,
    UNPLAYED_NO_ACTOR,
    UnplayedAnswers,
    drop_unplayed,
    end_open_answers,
)
from jarvis.state.device_tokens import device_name_for_token, pair_device
from jarvis.state.event_log import PHONE_VOICE_CHANNEL, emit_event, open_event_log
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app
from jarvis.surface.terminal_voice import RESPONSE_ROW_TYPES
from tests.integration.test_phone_voice import RATE, _answer, _Host
from tests.integration.test_previous_answer_line import _next_turn_line
from tests.integration.test_terminal_voice import _wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from tools.phone_fake_client import FakePhone


def _phone_utterance(log: Path, turn_id: str, *, device: str = "iphone") -> None:
    """Words a phone heard, as ``record_phone_say`` writes them (ADR 0209)."""
    conn = open_event_log(log)
    try:
        emit_event(
            conn, type="utterance.received", ingestion_node=device,
            payload={
                "transcript": "拜拜爸爸嗯", "turn_id": turn_id, "channel": PHONE_VOICE_CHANNEL,
            },
            correlation={"turn_id": turn_id},
        )
    finally:
        conn.close()


def _cancelled(log: Path, turn_id: str, response_id: str) -> None:
    conn = open_event_log(log)
    try:
        emit_event(conn, type="response.cancelled", payload={
            "turn_id": turn_id, "response_id": response_id, "response_group_id": f"G-{response_id}",
            "reason": "user_stop",
        })
    finally:
        conn.close()


def _ends(log: Path, response_id: str) -> list[tuple[str, str | None]]:
    """The rows that end ``response_id``'s playback, with the dropped row's reason."""
    with sqlite3.connect(log) as raw:
        return [
            (row[0], row[1]) for row in raw.execute(
                "SELECT type, json_extract(payload_json, '$.reason') FROM events "
                "WHERE type IN ('surface.playback_completed', 'surface.playback_interrupted', "
                "'surface.playback_failed', 'surface.speech_dropped') "
                "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
                (response_id,),
            )
        ]


def _gone(host: _Host, device: str) -> None:
    """Wait until the host has finished with ``device``'s connection, its voice closed."""
    assert host.hub is not None
    hub = host.hub
    _wait_for(lambda: not hub.connected(device), f"{device} was never let go")


def _turn_of(phone: FakePhone, utterance_id: str) -> str:
    """The turn the host recorded for ``utterance_id``."""
    def said() -> list[dict[str, Any]]:
        with phone.lock:
            return [
                t for t in phone.texts
                if t.get("type") == "said" and t.get("utterance_id") == utterance_id
            ]

    phone.wait_until(lambda: bool(said()), f"{utterance_id} was never answered")
    return str(said()[0]["turn_id"])


def _answer_unwatched(log: Path) -> sqlite3.Connection:
    open_event_log(log).close()
    return sqlite3.connect(log, check_same_thread=False)


# --- (a) no actor ------------------------------------------------------------------------------


def test_a_phone_turn_answered_while_its_device_has_no_voice_actor_still_ends(
    tmp_path: Path,
) -> None:
    """A phone that never asked for voice, and one that is not connected at all."""
    with _Host(tmp_path) as host:
        phone = host.phone(voice=False)
        try:
            assert phone.hello()["voice"] is False
            phone.say("u-1", "what is the weather", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            _answer(host.log, turn, "R-novoice", "It is sunny.")
            _wait_for(lambda: bool(_ends(host.log, "R-novoice")), "the answer never ended")
        finally:
            phone.close()
        _gone(host, "iphone")
        # the phone is gone now; a turn it spoke earlier is answered after
        _phone_utterance(host.log, "T-away")
        _answer(host.log, "T-away", "R-away", "You were saying?")
        _wait_for(lambda: bool(_ends(host.log, "R-away")), "the unattended answer never ended")
        # the answer streams: opened, chunk after chunk, and emitted
        _phone_utterance(host.log, "T-stream")
        _answer(host.log, "T-stream", "R-stream", ["One. ", "Two."])
        _wait_for(lambda: bool(_ends(host.log, "R-stream")), "the streamed answer never ended")
        assert _ends(host.log, "R-novoice") == [("surface.speech_dropped", UNPLAYED_NO_ACTOR)]
        assert _ends(host.log, "R-away") == [("surface.speech_dropped", UNPLAYED_NO_ACTOR)]
        assert _ends(host.log, "R-stream") == [("surface.speech_dropped", UNPLAYED_NO_ACTOR)]
        assert host.voices == []  # no actor was ever built for either
        assert host.mac_provider.sessions == []
        assert host.phone_provider.sessions == []


def test_the_next_turn_is_told_an_answer_nobody_could_play_was_never_spoken(
    tmp_path: Path,
) -> None:
    """Without the row it read as heard whole (ADR 0106); with it, shown on screen only."""
    log = tmp_path / "events.db"
    watcher_conn = _answer_unwatched(log)
    ends = UnplayedAnswers(lambda _device: None)
    _phone_utterance(log, "T-phone")
    _answer(log, "T-phone", "R-phone", "Goodbye, Dad.")
    # the phone's answer, as the watcher meets it
    for row_id, event in inherent_loop._fetch_events_after(  # noqa: SLF001
        watcher_conn, after_id=0,
        event_types=RESPONSE_ROW_TYPES,
    ):
        ends.observe(watcher_conn, row_id, event)
    line = _next_turn_line(watcher_conn)
    assert line is not None
    assert "never spoken aloud" in line
    watcher_conn.close()


# --- (b) the phone leaves mid-answer -----------------------------------------------------------


def test_a_phone_that_leaves_mid_answer_ends_its_played_and_queued_answers_once(
    tmp_path: Path,
) -> None:
    """(b): the played answer ends by the actor's terminal, the rest by a drop, none twice."""
    first = "This is the first sentence."
    second = " And this second one goes on for quite a long while yet."
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-1", "tell me a story", spoken=True)
            turn_1 = _turn_of(phone, "u-1")
            _answer(host.log, turn_1, "R-playing", [first, second])
            phone.wait_until(lambda: phone.played >= RATE, "the answer never began to play")
            # two more turns while it plays: one finished and queued, one still being written
            phone.say("u-2", "and another", spoken=True)
            turn_2 = _turn_of(phone, "u-2")
            _answer(host.log, turn_2, "R-queued", "Queued behind the first.")
            phone.say("u-3", "and one more", spoken=True)
            turn_3 = _turn_of(phone, "u-3")
            conn = open_event_log(host.log)
            group = "G-R-buffering"
            emit_event(conn, type="surface.response_open", payload={
                "turn_id": turn_3, "query": "q", "kind": "stream", "response_id": "R-buffering",
                "response_group_id": group, "phase": "final", "channel": "speech",
            })
            emit_event(conn, type="surface.response_chunk", payload={
                "turn_id": turn_3, "text": "Still being", "response_id": "R-buffering",
                "response_group_id": group, "sequence": 0, "phase": "final",
                "channel": "speech", "segment_hash": "h",
            })
            _wait_for(
                lambda: "surface.playback_started" in host.playback("R-playing"),
                "the first answer never started",
            )
        finally:
            phone.close()  # the connection is gone mid-answer
        _wait_for(
            lambda: all(
                _ends(host.log, r) for r in ("R-playing", "R-queued", "R-buffering")
            ),
            "an answer of the departed phone was never ended",
            timeout_s=20,
        )
        # the answer that was being written finishes after the phone left: still one end
        emit_event(conn, type="surface.response_emitted", payload={
            "turn_id": turn_3, "text": "Still being written.", "response_id": "R-buffering",
            "response_group_id": group, "phase": "final", "channel": "speech",
        })
        conn.close()
        _wait_for(lambda: len(host.types("surface.response_emitted")) == 3, "emitted row")
        time.sleep(0.5)  # one settle for the watcher to see the late row
        # Losing the socket fails the playing answer, which may start the queued one before the
        # close reaches it: that one is then ended by the actor's terminal, else by a drop.
        stopped = {"surface.playback_interrupted", "surface.playback_failed"}
        [(kind, _)] = _ends(host.log, "R-playing")  # one end, not two
        assert kind in stopped
        [(kind, reason)] = _ends(host.log, "R-queued")
        assert (kind, reason) == ("surface.speech_dropped", UNPLAYED_LEFT) or kind in stopped
        # nothing of this one ever reached the actor's lease
        assert _ends(host.log, "R-buffering") == [("surface.speech_dropped", UNPLAYED_LEFT)]


def test_ending_a_departed_voices_answers_leaves_the_ones_it_does_not_own(
    tmp_path: Path,
) -> None:
    """Another device's and the Mac's answers, and ones before the actor, stay as they are."""
    log = tmp_path / "events.db"
    conn = _answer_unwatched(log)
    _phone_utterance(log, "T-before")
    _answer(log, "T-before", "R-before", "Said before the actor was built.")
    boot = conn.execute("SELECT max(id) FROM events").fetchone()[0]
    _phone_utterance(log, "T-mine")
    _answer(log, "T-mine", "R-mine", "Mine.")
    _phone_utterance(log, "T-ipad", device="ipad")
    _answer(log, "T-ipad", "R-ipad", "Another phone's.")
    emit_event(
        conn, type="utterance.received",
        payload={"transcript": "hi", "turn_id": "T-mac", "channel": "inherent_wake"},
        correlation={"turn_id": "T-mac"},
    )
    _answer(log, "T-mac", "R-mac", "The Mac's.")
    assert end_open_answers(conn, "iphone", after_id=boot, reason=UNPLAYED_LEFT) == 1
    assert end_open_answers(conn, "iphone", after_id=boot, reason=UNPLAYED_LEFT) == 0
    assert [bool(_ends(log, r)) for r in ("R-before", "R-mine", "R-ipad", "R-mac")] == [
        False, True, False, False,
    ]
    conn.close()


# --- (c) dismissed or cancelled before playback ------------------------------------------------


def test_a_phone_turn_cancelled_before_playback_gets_one_end_row_with_or_without_an_actor(
    tmp_path: Path,
) -> None:
    """(c): the actor drops the cancelled answer it holds; the watcher drops it when none does."""
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-1", "bye", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            conn = open_event_log(host.log)
            emit_event(conn, type="surface.response_open", payload={
                "turn_id": turn, "query": "q", "kind": "stream", "response_id": "R-held",
                "response_group_id": "G-R-held", "phase": "final", "channel": "speech",
            })
            conn.close()
            _cancelled(host.log, turn, "R-held")
            _wait_for(lambda: bool(_ends(host.log, "R-held")), "the actor never ended it")
        finally:
            phone.close()
        _gone(host, "iphone")
        # the same cancel with the phone gone
        _phone_utterance(host.log, "T-gone")
        conn = open_event_log(host.log)
        emit_event(conn, type="surface.response_open", payload={
            "turn_id": "T-gone", "query": "q", "kind": "stream", "response_id": "R-gone",
            "response_group_id": "G-R-gone", "phase": "final", "channel": "speech",
        })
        conn.close()
        _cancelled(host.log, "T-gone", "R-gone")
        _wait_for(lambda: bool(_ends(host.log, "R-gone")), "the cancelled answer never ended")
        time.sleep(0.5)  # one settle for the late duplicates this checks for
        assert _ends(host.log, "R-held") == [("surface.speech_dropped", "response_cancelled")]
        assert _ends(host.log, "R-gone") == [("surface.speech_dropped", "response_cancelled")]


def test_dropping_an_answer_that_already_ended_writes_nothing(tmp_path: Path) -> None:
    """The watcher and a closing voice may both look: the second finds the first's row."""
    log = tmp_path / "events.db"
    conn = _answer_unwatched(log)
    assert drop_unplayed(
        conn, response_id="R1", turn_id="T1", reason="a", source_event_id=None,
    ) is True
    assert drop_unplayed(
        conn, response_id="R1", turn_id="T1", reason="b", source_event_id=None,
    ) is False
    assert _ends(log, "R1") == [("surface.speech_dropped", "a")]
    conn.close()


# --- (d) the wire ------------------------------------------------------------------------------


class _Client:
    """A companion's socket: what the broadcaster sent it."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.frames.append(message)

    def turns(self) -> set[str]:
        return {
            str(f["payload"]["turn_id"]) for f in self.frames
            if isinstance(f.get("payload"), dict) and "turn_id" in f["payload"]
        }

    def ops(self, turn_id: str) -> list[str]:
        return [f["op"] for f in self.frames if f["payload"].get("turn_id") == turn_id]


def _asked(log: Path, turn_id: str, node: str | None, channel: str) -> None:
    conn = open_event_log(log)
    try:
        if node is None:
            return
        emit_event(
            conn, type="surface.user_intent", ingestion_node=node,
            payload={"transcript": "hi", "turn_id": turn_id, "channel": channel},
        )
    finally:
        conn.close()


def test_a_phone_turn_never_reaches_the_macs_companion_and_every_other_turn_does(
    tmp_path: Path,
) -> None:
    """(d): the wire a companion is sent, by the device its socket is."""
    log = tmp_path / "events.db"
    conn = _answer_unwatched(log)
    _asked(log, "T-phone", "iphone", PHONE_VOICE_CHANNEL)
    _asked(log, "T-phone-typed", "iphone", "cli_stdin")
    _asked(log, "T-terminal", "macbook", "inherent_wake")
    _asked(log, "T-host", "mac", "cli_stdin")
    _asked(log, "T-sweep", None, "")  # a turn with no opening row
    wire = InherentBroadcaster()
    wire.turn_device = inherent_loop._turn_devices(conn)  # noqa: SLF001
    clients = {name: _Client() for name in ("mac", "macbook", "iphone", "none")}

    async def run() -> None:
        for name, client in clients.items():
            await wire.register(client, None if name == "none" else name)  # type: ignore[arg-type]
        for turn in ("T-phone", "T-phone-typed", "T-terminal", "T-host", "T-sweep"):
            await wire.broadcast_op("tool", turn_id=turn, label="x")
            await wire.broadcast_voice("playing", turn_id=turn, played=1, ahead=2, held=False)

    asyncio.run(run())

    async def watch() -> None:
        task = asyncio.create_task(
            inherent_loop._response_watcher(  # noqa: SLF001 - the production watcher
                SimpleNamespace(conn=conn), wire, poll_interval_s=0.01, voiced=False,  # type: ignore[arg-type]
            ),
        )
        await asyncio.sleep(0.05)
        for turn in ("T-phone", "T-terminal"):
            await wire.broadcast_op("failed", turn_id=turn, message="m")
            await wire.broadcast_op("cancelled", turn_id=turn)
        # rows written after the watcher started
        for turn in ("T-phone", "T-phone-typed", "T-terminal", "T-host", "T-sweep"):
            _answer(log, f"{turn}", f"R2-{turn}", "Again.")
        await asyncio.sleep(0.4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(watch())
    phone_turns = {"T-phone", "T-phone-typed"}
    own = {"T-terminal", "T-host", "T-sweep"}
    # the Mac's own (local-key) client: the host's turns and the ones nobody opened
    assert clients["mac"].turns() == {"T-host", "T-sweep"}
    # the voice terminal's upstream: its own, the host's and the unopened
    assert clients["macbook"].turns() == own
    # the phone's client follows its own turns and the host's, not the Mac terminal's
    assert clients["iphone"].turns() == phone_turns | {"T-host", "T-sweep"}
    assert clients["none"].turns() == phone_turns | own  # no device, no filter
    # a turn it follows is sent whole: its answer, its end and what the voice said of it
    assert "done" in clients["iphone"].ops("T-phone")
    assert {"open", "append", "done", "voice"} <= set(clients["macbook"].ops("T-terminal"))
    assert {"failed", "cancelled"} <= set(clients["macbook"].ops("T-terminal"))
    assert {"failed", "cancelled"} <= set(clients["iphone"].ops("T-phone"))
    assert not {"failed", "cancelled"} & set(clients["mac"].ops("T-phone"))
    conn.close()


def test_the_push_socket_is_the_device_its_token_names(tmp_path: Path) -> None:
    """The terminal's upstream carries its token; a local-key client is the host."""
    log = tmp_path / "events.db"
    conn = _answer_unwatched(log)
    token = pair_device(tmp_path, "macbook")
    _asked(log, "T-terminal", "macbook", "inherent_wake")
    _asked(log, "T-phone", "iphone", PHONE_VOICE_CHANNEL)
    wire = InherentBroadcaster()
    wire.turn_device = inherent_loop._turn_devices(conn)  # noqa: SLF001
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1", broadcaster=wire,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )

    def sent(turn_id: str, headers: dict[str, str]) -> list[str]:
        with TestClient(app) as client, client.websocket_connect(
            "/inherent/ws", headers=headers,
        ) as ws:
            async def push() -> None:
                await wire.broadcast_voice("spoken", turn_id=turn_id, output_outcome="x")
                await wire.broadcast_op("controls", mic_muted=False)

            client.portal.call(push)  # type: ignore[union-attr]
            frames = [json.loads(ws.receive_text())]
            if frames[0]["op"] == "voice":
                frames.append(json.loads(ws.receive_text()))
            return [f["op"] for f in frames]

    terminal = {"Authorization": f"Bearer {token}"}
    host = {"Authorization": "Bearer not-a-device-token"}
    assert sent("T-terminal", terminal) == ["voice", "controls"]
    assert sent("T-phone", terminal) == ["controls"]  # the phone's turn never reaches it
    assert sent("T-terminal", host) == ["controls"]  # a client that is the host: not the terminal's
    assert sent("T-phone", host) == ["controls"]
    conn.close()
