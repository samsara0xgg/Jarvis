"""ADR 0209: a phone holds a voice and text conversation over one socket, ``/phone/ws``.

Acceptance checks against a real uvicorn server with the real route, the real
``PhoneAudioStreamPlayer`` and a real ``StreamingTTSPipeline`` for the phone, the Mac's own media
actor beside it fed by the real ``_tts_watcher``, and a fake provider (no MiniMax, no key). The
phone is a Python client that implements the renderer side of the protocol: it sends READY,
"plays" the PCM16 frames at real-time pace, sends REPORT and STATUS, and answers DISCARD with
DISCARD_ACK. The answers are rows appended to the log by the test, as the decision layer would.

Shown: (a) a spoken ``say`` is one ``utterance.received`` on ``phone_voice`` under the device's
name and a resend writes nothing; (b) the turn's rows reach the phone; (c) PCM16 reaches the phone
and the playback rows run started to completed; (d) the Mac's actor spoke a Mac turn and not this
one, and the phone's actor the reverse; (e) a barge-in is a DISCARD, and the interrupted row and
the heard prefix come only after the phone's acknowledgement, frozen at what it reported; (f) a
typed ``say`` gets rows and no audio; (g) the local key and a bad token are refused, a second
connection replaces the first; (h) a real daemon in ``role: all`` that listens, with no terminal
hub, serves the same socket; (i) (ADR 0215) the turn's question cards (the bus card's ``trip``
included) reach the phone for its own turns only, the cursor that feeds its actor never sees them,
the final emitted row carries its written part and the times it refers to, and the phone's token
reads the card slot.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as _ws_connect

from jarvis.runtime import inherent_loop
from jarvis.runtime.phone_voice import PhoneSpeech
from jarvis.shared.realtime import Wave1FeatureFlags, Wave4ResponseFlags
from jarvis.state.device_tokens import device_name_for_token, device_token_matches, pair_device
from jarvis.state.event_log import (
    PHONE_VOICE_CHANNEL,
    emit_event,
    open_event_log,
    turn_intent_channel,
    turn_origin,
)
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import voice_media, voice_tts
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.phone_link import PHONE_PATH, REPLACED_CLOSE_CODE, PhoneHub
from jarvis.surface.phone_player import PCM16, PHONE_SAMPLE_RATE_HZ, PhoneAudioStreamPlayer
from jarvis.surface.terminal_events import BrainEvents
from jarvis.surface.voice_tts import TTSAudioChunk, TTSResponseSegment, TTSSegmentFinished
from tests.integration.test_mac_alone_listens import _bearer as _bearer_header
from tests.integration.test_mac_alone_listens import _daemon, _own_private_address
from tests.integration.test_terminal_voice import (
    _AsRemote,
    _FakeProvider,
    _free_port,
    _wait_for,
)
from tests.integration.test_wave2_streaming_media import _CallbackPump
from tests.integration.test_wave2_streaming_media import _config as _mac_config
from tools.phone_fake_client import (
    DEFAULT_CLOCK_OFFSET_NS,
    DISCARD,
    PRESENTATION_DELAY_NS,
    READY,
    FakePhone,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

RATE = PHONE_SAMPLE_RATE_HZ
CHAR_SAMPLES = 3_200  # the fake provider speaks 100 ms a character at 32 kHz


# --- a fake provider: 100 ms of audio and one word boundary a character, at the phone's rate --


def _speak32(segment: TTSResponseSegment) -> list[Any]:
    samples = CHAR_SAMPLES * len(segment.text)
    wave = (np.arange(samples) % 200 * 40 - 4000).astype("<i2").tobytes()
    boundaries = tuple((i + 1, 100.0 * (i + 1)) for i in range(len(segment.text)))
    return [
        TTSAudioChunk(segment.sequence, wave, RATE),
        TTSAudioChunk(
            segment.sequence, b"", RATE, timing_text=segment.text, word_boundaries=boundaries,
        ),
        TTSSegmentFinished(segment.sequence, usage={"usage_characters": len(segment.text)}),
    ]


def _sent_texts(provider: _FakeProvider) -> list[str]:
    return [
        call[1].text for session in provider.sessions for call in session.calls
        if call[0] == "send"
    ]


# --- the phone: the renderer side of the protocol ---------------------------------------------


def connect(url: str, *, additional_headers: dict[str, str]) -> Any:  # noqa: ANN401
    """A websocket client that ignores the sandbox's proxy variables: the host is local."""
    return _ws_connect(url, additional_headers=additional_headers, proxy=None)


# --- the host: the real route, the real phone actor, and the Mac's own actor beside it -------


def _config32() -> voice_media.StreamingMediaConfig:
    return replace(
        _mac_config(), canonical_sample_rate_hz=RATE, ring_seconds=0.5, response_timeout_s=30.0,
    )


def _log(path: Path) -> sqlite3.Connection:
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


class _Host:
    """A uvicorn server in this process with the phone route, as the daemon wires it."""

    def __init__(self, root: Path, *, voice: bool = True, remote: bool = True) -> None:
        self.root = root
        self.log = root / "events.db"
        _log(self.log).close()
        self.token = pair_device(root, "iphone")
        self.second_token = pair_device(root, "ipad")
        self.port = _free_port()
        self.voice = voice
        self.remote = remote
        self.phone_provider = _FakeProvider(script=_speak32)
        self.mac_provider = _FakeProvider()
        self.barges: list[str] = []
        self.cancels: list[tuple[str, str]] = []
        self.voices: list[Any] = []
        self.server: uvicorn.Server | None = None
        self.thread = threading.Thread(target=lambda: asyncio.run(self._main()), daemon=True)

    def _build_phone_pipeline(
        self,
        player: PhoneAudioStreamPlayer,
        speaks_turn: Callable[[sqlite3.Connection, str], bool],
        boot: int,
    ) -> voice_media.StreamingTTSPipeline:
        return voice_media.StreamingTTSPipeline(
            provider=cast("Any", self.phone_provider),
            player=player,
            conn_factory=lambda: open_event_log(self.log),
            boot_high_water_id=boot,
            config=_config32(),
            start_player=False,
            speaks_turn=speaks_turn,
        )

    def _barge(self, device: str) -> str:
        self.barges.append(device)
        return "cancelled"

    def _cancel(self, turn: str, reason: str) -> str:
        self.cancels.append((turn, reason))
        return "cancelled"

    async def _main(self) -> None:
        conn = sqlite3.connect(self.log, check_same_thread=False)
        events = BrainEvents(conn)
        rows = inherent_loop._PhoneRows(conn)  # noqa: SLF001
        speech = PhoneSpeech(build=self._build_phone_pipeline, rows=rows, ring_seconds=0.5)

        async def open_voice(device: str, send_binary: Callable[[bytes], bool]) -> Any:  # noqa: ANN401
            voice = await speech.open(device, send_binary)
            self.voices.append(voice)
            return voice

        hub = PhoneHub(
            events=events,
            rows=rows,
            open_voice=open_voice if self.voice else None,
            barge_in=self._barge,
            cancel_turn=self._cancel,
        )
        mac_player = voice_tts.AudioStreamPlayer(
            sample_rate_hz=8_000, ring_seconds=0.5, lazy_open=True, generation_safe=True,
            estimated_output_latency_s=0.0,
        )
        mac = voice_media.StreamingTTSPipeline(
            provider=cast("Any", self.mac_provider),
            player=mac_player,
            conn_factory=lambda: open_event_log(self.log),
            boot_high_water_id=inherent_loop._latest_id(conn),  # noqa: SLF001
            config=_mac_config(),
            start_player=False,
            silent_intent_channels=inherent_loop._TTS_SILENT_CHANNELS,  # noqa: SLF001
        )
        watcher = asyncio.create_task(
            inherent_loop._tts_watcher(conn=conn, pipeline=mac, poll_interval_s=0.01),  # noqa: SLF001
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                phone=hub,
                device_name=functools.partial(device_name_for_token, self.root),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(self.root)),
            device_token_matches=functools.partial(device_token_matches, self.root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                _AsRemote(app) if self.remote else app, host="127.0.0.1", port=self.port,
                log_level="warning", lifespan="off",
            ),
        )
        with _CallbackPump(mac_player):
            try:
                await self.server.serve()
            finally:
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)
                await asyncio.to_thread(mac.close)

    def __enter__(self) -> Self:
        self.thread.start()
        _wait_for(lambda: bool(self.server and self.server.started), "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        assert self.server is not None
        self.server.should_exit = True
        self.thread.join(timeout=20)

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{PHONE_PATH}"

    def phone(
        self, *, token: str | None = None, voice: bool = True, speed: float = 1.0,
    ) -> FakePhone:
        return FakePhone(self.url, token or self.token, voice=voice, speed=speed).start()

    # -- the log

    def types(self, wanted: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.log) as raw:
            return [
                {**json.loads(row[0]), "_node": row[1], "_uid": row[2]}
                for row in raw.execute(
                    "SELECT payload_json, ingestion_node, event_uid FROM events WHERE type = ? "
                    "ORDER BY id", (wanted,),
                )
            ]

    def playback(self, response_id: str) -> list[str]:
        with sqlite3.connect(self.log) as raw:
            return [
                row[0] for row in raw.execute(
                    "SELECT type FROM events WHERE type LIKE 'surface.playback_%' "
                    "AND json_extract(payload_json, '$.response_id') = ? ORDER BY id",
                    (response_id,),
                )
            ]


def _answer(
    log: Path, turn_id: str, response_id: str, text: str | list[str],
    emitted: dict[str, Any] | None = None,
) -> None:
    """The decision layer's answer rows for a turn: open, its chunks, emitted.

    One string is a finished text answer; a list is a stream, one speakable segment a chunk.
    ``emitted`` adds fields to the final ``surface.response_emitted`` row.
    """
    chunks = [text] if isinstance(text, str) else text
    conn = open_event_log(log)
    try:
        group = f"G-{response_id}"
        emit_event(conn, type="surface.response_open", payload={
            "turn_id": turn_id, "query": "q", "kind": "text" if isinstance(text, str) else "stream",
            "response_id": response_id, "response_group_id": group, "phase": "final",
            "channel": "speech",
        })
        for sequence, chunk in enumerate(chunks):
            emit_event(conn, type="surface.response_chunk", payload={
                "turn_id": turn_id, "text": chunk, "response_id": response_id,
                "response_group_id": group, "sequence": sequence, "phase": "final",
                "channel": "speech", "segment_hash": "h",
            })
        emit_event(conn, type="surface.response_emitted", payload={
            "turn_id": turn_id, "text": "".join(chunks), "response_id": response_id,
            "response_group_id": group, "phase": "final", "channel": "speech", **(emitted or {}),
        })
    finally:
        conn.close()


def _mac_utterance(log: Path, turn_id: str, text: str) -> None:
    """Words heard by the Mac's own microphone: what its capture session writes."""
    conn = open_event_log(log)
    try:
        emit_event(
            conn, type="utterance.received",
            payload={"transcript": text, "turn_id": turn_id, "channel": "inherent_wake"},
            correlation={"turn_id": turn_id},
        )
    finally:
        conn.close()


# --- the checks --------------------------------------------------------------------------------


def test_a_spoken_say_is_one_utterance_answered_in_rows_and_played_by_the_phones_own_actor(  # noqa: PLR0915
    tmp_path: Path,
) -> None:
    """(a) (b) (c) (d): one row per utterance, rows to the phone, PCM16 to the phone, Mac silent."""
    phone_text, mac_text = "The first sentence.", "Mac only line."
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            ready = phone.hello()
            assert ready == {
                "type": "ready", "device": "iphone", "voice": True, "sample_rate": RATE,
                "judges": True,
            }
            phone.send_ready()
            phone.send({"type": "ping", "t_ns": phone.phone_ns()})
            pong = phone.wait_text("pong")
            assert isinstance(pong["host_ns"], int)

            # (a) the utterance is written once, on the phone's channel, under the device name
            phone.say("u-1", "what is the weather", spoken=True)
            said = phone.wait_text("said")
            assert said["utterance_id"] == "u-1"
            assert said["verdict"] == "turn"  # ADR 0216: an unflagged say is a turn
            turn = said["turn_id"]
            phone.say("u-1", "what is the weather", spoken=True)
            with phone.lock:
                phone.lock.wait_for(
                    lambda: sum(1 for t in phone.texts if t.get("type") == "said") == 2, 5,
                )
                assert [t["turn_id"] for t in phone.texts if t.get("type") == "said"] == [
                    turn, turn,
                ]
            [heard] = host.types("utterance.received")
            assert heard["_node"] == "iphone"
            assert (heard["channel"], heard["transcript"], heard["turn_id"]) == (
                PHONE_VOICE_CHANNEL, "what is the weather", turn,
            )
            assert not host.types("surface.user_intent")

            # a turn heard by the Mac, answered at the same time
            _mac_utterance(host.log, "TMAC", "and the time")
            _answer(host.log, "TMAC", "R-mac", mac_text)
            _wait_for(lambda: "surface.playback_completed" in host.playback("R-mac"), "Mac silent")

            # (b) (c) the phone's answer: rows reach the phone, audio is played, rows are written
            _answer(host.log, turn, "R-phone", phone_text)
            phone.wait_until(
                lambda: {r["event_type"] for r in phone.rows()} >= {
                    "surface.response_open", "surface.response_chunk", "surface.response_emitted",
                },
                "the answer's rows never reached the phone",
            )
            _wait_for(
                lambda: "surface.playback_completed" in host.playback("R-phone"),
                "the phone's actor never completed playback",
                timeout_s=20,
            )
            kinds = host.playback("R-phone")
            assert kinds[0] == "surface.playback_started"
            assert kinds[-1] == "surface.playback_completed"
            assert kinds.count("surface.playback_started") == 1
            [done] = [
                p for p in host.types("surface.playback_completed")
                if p["response_id"] == "R-phone"
            ]
            assert done["heard_text"] == phone_text

            # the frames: PCM16 only, for exactly this answer's samples
            with phone.lock:
                assert 1 not in phone.binary_types, "float32 PCM was sent to a phone"
                assert PCM16 in phone.binary_types
                [(generation, samples)] = phone.pcm_samples.items()
                assert samples == CHAR_SAMPLES * len(phone_text)
                assert phone.played_per_generation[generation] == samples
            turns_on_phone = {r["payload"]["turn_id"] for r in phone.rows()}
            assert turns_on_phone == {turn}, "the phone was sent a turn that is not its own"

            # (d) the Mac's actor spoke the Mac's turn and not the phone's; the phone's the reverse
            assert _sent_texts(host.mac_provider) == [mac_text]
            assert _sent_texts(host.phone_provider) == [phone_text]
            assert host.playback("R-mac").count("surface.playback_started") == 1
            assert host.playback("R-phone").count("surface.playback_started") == 1

            # the phone's clock is mapped onto the host's, not trusted as it is
            [voice] = host.voices
            offset = voice.player.clock_offset_ns
            assert offset is not None
            assert abs(offset + DEFAULT_CLOCK_OFFSET_NS) < 200_000_000, offset
        finally:
            phone.close()


def test_a_barge_in_is_a_discard_and_the_heard_prefix_freezes_after_the_phones_ack(
    tmp_path: Path,
) -> None:
    """(e): the phone holds, the host sends DISCARD, and settles only on DISCARD_ACK."""
    first = "This is the first sentence."  # 26 characters: 2.6 s, aligned once it is written
    second = " And this second one goes on for quite a long while yet."
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-barge", "tell me a story", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            _answer(host.log, turn, "R-long", [first, second])
            phone.wait_until(
                lambda: phone.played >= 3 * RATE, "the answer never got into its second sentence",
            )

            # Allen speaks over her: the phone holds at once, then tells the host.
            phone.hold_output()
            with phone.lock:
                held_at = phone.played
            phone.send({"type": "barge"})
            _wait_for(
                lambda: "surface.playback_interrupted" in host.playback("R-long"),
                "the host never settled the interruption",
            )
            with phone.lock:
                order = list(phone.order)
                [generation] = phone.pcm_samples
                at_discard = phone.played_at_discard[generation]
            assert DISCARD in phone.binary_types
            # DISCARD precedes its ack, every report for the discarded samples precedes the
            # ack, and no audio of this answer was sent after the DISCARD frame.
            discard_at = order.index(("discard", 1))
            ack_at = order.index(("ack", 1))
            assert discard_at < ack_at
            assert ("report", generation) not in order[ack_at:]
            assert ("pcm", generation) not in order[discard_at:]
            assert at_discard == held_at or at_discard - held_at < RATE // 50  # held really held

            [interrupted] = host.types("surface.playback_interrupted")
            assert interrupted["response_id"] == "R-long"
            assert interrupted["reason"] == "barge_in"
            # Frozen at what the phone reported: the samples it played, less what it had not yet
            # presented when it held; the first sentence it finished, and nothing of the second.
            audible = interrupted["estimated_audible_samples"]
            assert at_discard - PRESENTATION_DELAY_NS * RATE // 10**9 - 64 <= audible <= at_discard
            assert interrupted["heard_text"] == first
            assert host.barges == ["iphone"]
            assert "surface.playback_completed" not in host.playback("R-long")
        finally:
            phone.close()


def test_a_typed_say_gets_rows_and_no_audio(tmp_path: Path) -> None:
    """(f): a typed turn is a silent-channel intent; its rows reach the phone, nothing plays."""
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-typed", "what is on my calendar", spoken=False)
            turn = phone.wait_text("said")["turn_id"]
            [intent] = host.types("surface.user_intent")
            assert (intent["channel"], intent["_node"], intent["turn_id"]) == (
                "cli_stdin", "iphone", turn,
            )
            assert not host.types("utterance.received")
            _answer(host.log, turn, "R-typed", "Nothing today.")
            phone.wait_until(
                lambda: "surface.response_emitted" in {r["event_type"] for r in phone.rows()},
                "the typed answer's rows never reached the phone",
            )
            time.sleep(0.5)  # a late audio frame would show by now
            with phone.lock:
                assert PCM16 not in phone.binary_types
                assert not phone.pcm_samples
            assert not host.playback("R-typed")
            assert _sent_texts(host.phone_provider) == []
            assert _sent_texts(host.mac_provider) == []
        finally:
            phone.close()


def test_a_phone_without_voice_is_told_so_and_still_gets_rows(tmp_path: Path) -> None:
    """The host cannot speak, or the phone did not ask: ``voice: false``, text and rows only."""
    with _Host(tmp_path, voice=False) as host:
        phone = host.phone(voice=True)
        try:
            ready = phone.hello()
            assert ready["voice"] is False
            phone.say("u-1", "hello", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            _answer(host.log, turn, "R-text", "Hi.")
            phone.wait_until(
                lambda: "surface.response_emitted" in {r["event_type"] for r in phone.rows()},
                "rows never reached the phone",
            )
            phone.ws.send(bytes([READY]))  # binary frames without voice are refused, not played
            assert phone.wait_text("error")["code"] == "no_voice"
        finally:
            phone.close()


def test_only_a_paired_devices_token_opens_the_socket_and_a_newer_connection_replaces(
    tmp_path: Path,
) -> None:
    """(g): no token, a wrong one and the local key are refused; the second connection wins."""
    with _Host(tmp_path) as host:
        key = local_key(tmp_path)
        for bad in (None, "not-a-token", key):
            headers = {} if bad is None else {"Authorization": f"Bearer {bad}"}
            with pytest.raises(InvalidStatus) as refused:
                connect(host.url, additional_headers=headers)
            assert refused.value.response.status_code == 403

        first = host.phone()
        second = None
        try:
            first.hello()
            second = host.phone()
            second.hello()
            first.wait_until(lambda: first.closed is not None, "the first connection stayed open")
            assert first.closed is not None
            assert first.closed.rcvd is not None
            assert first.closed.rcvd.code == REPLACED_CLOSE_CODE
            second.say("u-2", "still here", spoken=False)
            assert second.wait_text("said")["utterance_id"] == "u-2"
            # another device is not the same device: it does not replace this one
            other = host.phone(token=host.second_token)
            try:
                assert other.hello()["device"] == "ipad"
                second.say("u-3", "and me", spoken=False)
                assert second.wait_text("said", after=len(second.texts) - 1)
                assert second.closed is None
            finally:
                other.close()
        finally:
            first.close()
            if second is not None:
                second.close()


def test_the_route_itself_refuses_the_local_key_from_a_peer_on_loopback(tmp_path: Path) -> None:
    """The middleware lets the local key through on loopback; the route still wants a device."""
    root = tmp_path
    pair_device(root, "iphone")
    conn = _log(root / "events.db")
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            phone=PhoneHub(events=BrainEvents(conn), rows=inherent_loop._PhoneRows(conn)),  # noqa: SLF001
            device_name=functools.partial(device_name_for_token, root),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(root)),
        device_token_matches=functools.partial(device_token_matches, root),
    )
    client = TestClient(app, base_url="http://127.0.0.1:8006", client=("127.0.0.1", 50000))
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(
        f"ws://127.0.0.1:8006{PHONE_PATH}", headers=_bearer_header(local_key(root)),
    ):
        pass
    # the route closes it as a policy violation
    assert cast("Any", refused.value).code == 1008


def test_the_phone_channel_is_spoken_for_l3_and_silent_for_the_macs_speaker(
    tmp_path: Path,
) -> None:
    """A turn's origin is read from ``utterance.received`` too; ``phone_voice`` is Mac-silent."""
    from jarvis.decision.pre_route import SPOKEN_CHANNELS  # noqa: PLC0415 - the runtime wires L3

    assert PHONE_VOICE_CHANNEL in SPOKEN_CHANNELS
    assert PHONE_VOICE_CHANNEL in inherent_loop._TTS_SILENT_CHANNELS  # noqa: SLF001
    conn = open_event_log(tmp_path / "events.db")
    try:
        _mac_utterance(tmp_path / "events.db", "T-mac", "hello")
        BrainEvents(conn).record_phone_say("iphone", "u1", "hi", spoken=True)
        BrainEvents(conn).record_phone_say("iphone", "u2", "hi", spoken=False)
        turns = [
            row[0] for row in conn.execute("SELECT payload_json FROM events ORDER BY id")
        ]
        by_turn = {json.loads(p)["turn_id"] for p in turns}
        assert len(by_turn) == 3
        mine = {turn: turn_origin(conn, turn) for turn in by_turn}
        assert sorted(mine.values(), key=str) == sorted(
            [("inherent_wake", "mac"), (PHONE_VOICE_CHANNEL, "iphone"), ("cli_stdin", "iphone")],
            key=str,
        )
        assert turn_intent_channel(conn, "T-mac") == "inherent_wake"
        assert turn_intent_channel(conn, "no-such-turn") is None
    finally:
        conn.close()


def test_the_speech_builder_needs_a_key_and_the_streaming_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key or streaming off: ``voice: false``. Otherwise a speech builder."""
    conn = _log(tmp_path / "events.db")
    config: dict[str, Any] = {
        "realtime": {"enabled": True, "streaming_output": {"enabled": True}},
    }
    host = SimpleNamespace(
        config=config,
        wave1_features=Wave1FeatureFlags(
            transactional_event_append=True, lifecycle_terminal_cas=True,
        ),
        response_flags=Wave4ResponseFlags(),
        runtime_paths=SimpleNamespace(event_log=tmp_path / "events.db"),
        voice_settings=None,
    )
    rows = inherent_loop._PhoneRows(conn)  # noqa: SLF001
    knobs = inherent_loop._VoiceKnobs()  # noqa: SLF001
    runtime = cast("Any", host)

    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    assert inherent_loop._build_phone_speech(runtime, knobs, rows) is None  # noqa: SLF001
    monkeypatch.setenv("MINIMAX_API_KEY", "a-test-value-not-a-key")
    assert isinstance(
        inherent_loop._build_phone_speech(runtime, knobs, rows), PhoneSpeech,  # noqa: SLF001
    )
    config["realtime"]["streaming_output"]["enabled"] = False
    assert inherent_loop._build_phone_speech(runtime, knobs, rows) is None  # noqa: SLF001


def test_a_daemon_running_alone_serves_the_phone_socket_with_no_terminal_hub(
    tmp_path: Path,
) -> None:
    """(h): a real `serve` in role all that listens: the phone pairs and talks."""
    import httpx  # noqa: PLC0415

    address = _own_private_address()
    if address is None:
        pytest.skip("this machine has no private non-loopback IPv4 address")
    with _daemon(tmp_path, address) as (root, port):
        local, remote = f"http://127.0.0.1:{port}", f"http://{address}:{port}"
        owner = _bearer_header(local_key(root))
        minted = httpx.post(
            f"{local}/inherent/devices/pairing", json={"name": "iphone"}, headers=owner, timeout=5,
        )
        claimed = httpx.post(
            f"{remote}/inherent/devices/claim", json={"code": minted.json()["code"]}, timeout=5,
        )
        token = claimed.json()["token"]
        url = f"ws://{address}:{port}{PHONE_PATH}"
        # The terminal link is a brain's alone; the phone's socket is any listening host's.
        with pytest.raises(InvalidStatus):
            connect(f"ws://{address}:{port}/terminal/ws", additional_headers={
                "Authorization": f"Bearer {token}",
            })
        with pytest.raises(InvalidStatus) as refused:
            connect(url, additional_headers=owner)
        assert refused.value.response.status_code == 403

        phone = FakePhone(url, token, voice=True).start()
        try:
            ready = phone.hello()
            assert ready == {
                "type": "ready", "device": "iphone", "voice": False, "sample_rate": RATE,
                "judges": True,
            }  # the daemon here has no speech key: rows and text only
            phone.send({"type": "ping", "t_ns": 5})
            assert phone.wait_text("pong")["t_ns"] == 5
            phone.say("u-1", "hello from the phone", spoken=True)
            turn = phone.wait_text("said")["turn_id"]
            phone.say("u-2", "typed hello", spoken=False)
            phone.wait_until(
                lambda: sum(1 for t in phone.texts if t.get("type") == "said") == 2, "no 2nd said",
            )
        finally:
            phone.close()
        log = root / "mac_events.db"
        with sqlite3.connect(log) as raw:
            spoken = raw.execute(
                "SELECT json_extract(payload_json, '$.channel'), ingestion_node FROM events "
                "WHERE type = 'utterance.received' AND json_extract(payload_json, '$.turn_id') = ?",
                (turn,),
            ).fetchall()
            typed = raw.execute(
                "SELECT json_extract(payload_json, '$.channel'), ingestion_node FROM events "
                "WHERE type = 'surface.user_intent' AND ingestion_node = 'iphone'",
            ).fetchall()
        assert spoken == [(PHONE_VOICE_CHANNEL, "iphone")]
        assert typed == [("cli_stdin", "iphone")]


TRIP: dict[str, Any] = {
    "offer_id": "offer-1a2b3c4d", "to": "UVic", "made_at": "2026-10-10T08:00-07:00",
    "options": [{
        "index": 0, "route": "28", "board_stop": "Hillside", "leave_at": "08:20",
        "departs": "08:25", "arrive_at": "08:55", "to": "UVic",
    }],
}


def _card(log: Path, turn_id: str, clarification_id: str, *, trip: bool = False) -> None:
    """The ask card of a turn, as ``ask_user`` (fields) or ``transit`` (``trip``) writes it."""
    conn = open_event_log(log)
    try:
        emit_event(conn, type="clarification.requested", payload={
            "clarification_id": clarification_id, "question": "Which one?" if not trip else "UVic",
            "fields": [] if trip else [{"label": "Which", "kind": "text"}],
            "turn_id": turn_id, "action_id": f"A-{clarification_id}",
            **({"trip": TRIP} if trip else {}),
        })
    finally:
        conn.close()


def _withdraw(log: Path, turn_id: str, clarification_id: str) -> None:
    conn = open_event_log(log)
    try:
        emit_event(conn, type="clarification.withdrawn", payload={
            "clarification_id": clarification_id, "turn_id": turn_id,
        })
    finally:
        conn.close()


def test_question_cards_reach_the_phone_for_its_own_turns_and_never_its_speech(
    tmp_path: Path,
) -> None:
    """(i): the device's spoken and typed turns get cards; the Mac's turn and the speaker none."""
    times = [{"kind": "reminder", "at_ms": 1_791_619_200_000, "label": "mom", "ref": "reminder-1"}]
    with _Host(tmp_path) as host:
        phone = host.phone()
        try:
            phone.hello()
            phone.send_ready()
            phone.say("u-spoken", "when is the bus", spoken=True)
            spoken_turn = phone.wait_text("said")["turn_id"]
            phone.say("u-typed", "and the next one", spoken=False)
            typed_turn = phone.wait_text("said", after=len(phone.texts))["turn_id"]
            _mac_utterance(host.log, "TMAC", "something on the mac")

            _card(host.log, spoken_turn, "offer-1a2b3c4d", trip=True)
            _card(host.log, "TMAC", "offer-mac")
            _card(host.log, typed_turn, "ask-typed")
            _withdraw(host.log, typed_turn, "ask-typed")
            _withdraw(host.log, "TMAC", "offer-mac")
            _answer(
                host.log, spoken_turn, "R-card", "Take the 28.",
                emitted={
                    "voice_text": "Take the 28.", "document_text": "Leave 08:20, Hillside.",
                    "written_apart": True, "times": times,
                },
            )
            phone.wait_until(
                lambda: "surface.response_emitted" in {r["event_type"] for r in phone.rows()},
                "the answer's rows never reached the phone",
            )
            rows = phone.rows()
            cards = [r for r in rows if r["event_type"].startswith("clarification.")]
            assert [(r["event_type"], r["payload"]["clarification_id"]) for r in cards] == [
                ("clarification.requested", "offer-1a2b3c4d"),
                ("clarification.requested", "ask-typed"),
                ("clarification.withdrawn", "ask-typed"),
            ]
            assert cards[0]["payload"]["trip"] == TRIP
            assert {r["payload"]["turn_id"] for r in rows} == {spoken_turn, typed_turn}
            # in log order: the cards come between the turn's own rows as they were written
            assert [r["event_type"] for r in rows].index("clarification.requested") < [
                r["event_type"] for r in rows
            ].index("surface.response_emitted")
            [emitted] = [r for r in rows if r["event_type"] == "surface.response_emitted"]
            assert emitted["payload"]["document_text"] == "Leave 08:20, Hillside."
            assert emitted["payload"]["written_apart"] is True
            assert emitted["payload"]["times"] == times

            # the cursor that feeds the phone's media actor reads the answer rows and no card
            with closing(sqlite3.connect(host.log)) as raw:
                rows_of = inherent_loop._PhoneRows(raw)  # noqa: SLF001
                _, voice_rows = rows_of.cursor("iphone", 0, spoken_only=True).poll()
                _, all_rows = rows_of.cursor("iphone", 0).poll()
            assert not any(e.type.startswith("clarification.") for _id, e in voice_rows)
            assert {e.type for _id, e in voice_rows} >= {"surface.response_emitted"}
            assert sum(e.type.startswith("clarification.") for _id, e in all_rows) == 3
            # the card is never spoken: the phone's provider heard the answer's text alone
            _wait_for(
                lambda: "surface.playback_completed" in host.playback("R-card"),
                "the answer never finished playing", timeout_s=20,
            )
            assert _sent_texts(host.phone_provider) == ["Take the 28."]
        finally:
            phone.close()


def test_the_phones_token_reads_the_question_slot_like_every_route(tmp_path: Path) -> None:
    """``GET /inherent/clarification`` is open to a paired device's token and to no other."""
    root = tmp_path
    token = pair_device(root, "iphone")
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            question_read=lambda: {"card": None},
            question_answer=lambda _id, _answers, _device: None,
            device_name=functools.partial(device_name_for_token, root),
        ),
    )
    remote = "100.87.250.92"
    require_local_key(
        app, functools.partial(local_key_matches, local_key(root)), extra_hosts=[remote],
        device_token_matches=functools.partial(device_token_matches, root),
    )
    client = TestClient(app, base_url=f"http://{remote}:8006", client=(remote, 50000))
    assert client.get("/inherent/clarification", headers=_bearer_header(token)).json() == {
        "card": None,
    }
    assert client.get("/inherent/clarification").status_code == 401
    assert client.get(
        "/inherent/clarification", headers=_bearer_header(local_key(root)),
    ).status_code == 401, "the local key is not for a remote peer"
