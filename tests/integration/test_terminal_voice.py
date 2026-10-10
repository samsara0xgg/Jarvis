"""ADR 0172 step 5a: a terminal speaks with the brain's provider over the terminal link.

Acceptance checks, each against the real code and fakes only for the provider and the
speaker: the proxy session keeps the provider's events in order and whole, answers every call
or says why not, aborts at once, and fails everything in flight when the link drops; the row
stream starts from the log's high-water mark, resumes after what the terminal journaled, and
speaks to the terminal connected last; the allowlist takes playback rows from a voice terminal
only; the journal is emptied at start; the media actor's builder takes the remote provider;
and one end-to-end run over a real websocket, where the answer appended to a brain's log is
played by a terminal's real media actor and the brain's log ends with what was heard.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self, cast

import numpy as np
import pytest
import uvicorn
from fastapi.testclient import TestClient

from jarvis.cli import main
from jarvis.runtime import inherent_loop
from jarvis.runtime.terminal import JOURNAL, _run, _Speaking
from jarvis.shared import Event
from jarvis.shared.realtime import Wave1FeatureFlags, Wave4ResponseFlags
from jarvis.state.conversation import fold_conversation_history
from jarvis.state.device_tokens import (
    device_name_for_token,
    device_token_matches,
    pair_device,
)
from jarvis.state.event_log import emit_event, iter_events, open_event_log
from jarvis.state.plugin_settings import local_key, local_key_matches
from jarvis.surface import terminal_voice, voice_media, voice_tts
from jarvis.surface.inherent_output import InherentBroadcaster
from jarvis.surface.inherent_server import InherentDeps, create_app, require_local_key
from jarvis.surface.terminal_events import (
    OBSERVER_EVENT_TYPES,
    PLAYBACK_EVENT_TYPES,
    BrainEvents,
    EventOutbox,
)
from jarvis.surface.terminal_link import MAX_FRAME_CHARS, TerminalHub
from jarvis.surface.terminal_speaker import (
    REMOTE_ENDPOINTS,
    Journal,
    RemoteTTSError,
    RemoteTTSProvider,
    VoiceLink,
)
from jarvis.surface.terminal_voice import BrainVoice, encode_event
from jarvis.surface.voice_tts import (
    TTSAudioChunk,
    TTSConcurrentSendError,
    TTSResponseSegment,
    TTSSegmentFinished,
    TTSSessionClosedError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine

    from starlette.types import ASGIApp, Receive, Scope, Send

REMOTE = "100.87.250.92"
TERMINAL_WS = "ws://127.0.0.1:8006/terminal/ws"
RATE = 8_000
PARAMS: dict[str, Any] = {
    "endpoint_index": 0, "language": "en", "idle_close_s": 1.0,
    "command_queue_capacity": 1, "audio_queue_capacity": 2,
}
_END = object()


# --- a fake provider: deterministic PCM with word boundaries ---------------------------------


def _speak(segment: TTSResponseSegment) -> list[Any]:
    """What a provider says for one segment: audio, its word times, and the segment's end."""
    samples = 160 * len(segment.text)  # 20 ms a character at 8 kHz
    wave = (np.arange(samples) % 100 * 20).astype("<i2").tobytes()
    half = len(wave) // 2
    boundaries = tuple((i + 1, 20.0 * (i + 1)) for i in range(len(segment.text)))
    return [
        TTSAudioChunk(segment.sequence, wave[:half], RATE),
        TTSAudioChunk(segment.sequence, wave[half:], RATE),
        TTSAudioChunk(
            segment.sequence, b"", RATE, timing_text=segment.text, word_boundaries=boundaries,
        ),
        TTSSegmentFinished(segment.sequence, usage={"usage_characters": len(segment.text)}),
    ]


class _FakeSession:
    """The brain's real half, replaced: records every call, speaks `script(segment)`."""

    def __init__(self, provider: _FakeProvider, endpoint_index: int, language: str) -> None:
        self.provider = provider
        self.endpoint_index = endpoint_index
        self.language = language
        self.calls: list[tuple[Any, ...]] = []
        self.events: asyncio.Queue[Any] = asyncio.Queue()
        self.closed = False
        self.send_error: Exception | None = None

    async def connect(self) -> None:
        self.calls.append(("connect",))
        if self.provider.connect_error is not None:
            raise self.provider.connect_error

    async def open(self, response_id: str, playback_generation_id: int) -> None:
        self.calls.append(("open", response_id, playback_generation_id))

    async def send(self, segment: TTSResponseSegment) -> None:
        self.calls.append(("send", segment))
        if self.send_error is not None:
            raise self.send_error
        for event in self.provider.script(segment):
            self.events.put_nowait(event)

    def audio_events(self) -> AsyncIterator[Any]:
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        while True:
            item = await self.events.get()
            if item is _END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def finish(self) -> None:
        self.calls.append(("finish",))
        await self.close()

    async def abort(self, reason: str) -> None:
        self.calls.append(("abort", reason))
        await self.close()

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.calls.append(("close",))
            self.events.put_nowait(_END)


class _FakeProvider:
    def __init__(self, script: Callable[[TTSResponseSegment], list[Any]] = _speak) -> None:
        self.script = script
        self.sessions: list[_FakeSession] = []
        self.connect_error: Exception | None = None

    @property
    def streaming_candidate_count(self) -> int:
        return REMOTE_ENDPOINTS

    def create_tts_session(
        self, *, endpoint_index: int, language: str, idle_close_s: float,
        command_queue_capacity: int, audio_queue_capacity: int,
    ) -> _FakeSession:
        del idle_close_s, command_queue_capacity, audio_queue_capacity
        session = _FakeSession(self, endpoint_index, language)
        self.sessions.append(session)
        return session

    def request_close(self) -> None:
        return


class _NoRows:
    def high_water(self) -> int:
        return 0

    def cursor(self, after: int) -> _NoRows:
        del after
        return self

    def poll(self) -> terminal_voice.RowBatch:
        return 0, []


# --- the proxy: a brain's BrainVoice and a terminal's VoiceLink in one loop ------------------


class _Wire:
    """Both ends joined with no socket; every frame still goes through JSON text."""

    def __init__(
        self, tmp_path: Path, provider: _FakeProvider, name: str = "macbook",
        voice: BrainVoice | None = None,
    ) -> None:
        self.provider = provider
        self.voice = voice or BrainVoice(cast("Any", provider), _NoRows())
        self.journal = Journal(tmp_path / f"{name}.db")
        self.link = VoiceLink(self.journal)
        self.peer = self.voice.attach(name, self._to_terminal, None)
        self.frames_to_brain: list[dict[str, Any]] = []
        self.up()

    def up(self) -> None:
        self.link.up(self._to_brain, asyncio.get_running_loop(), {"type": "ready", "voice": True})

    async def _to_terminal(self, text: str) -> None:
        self.link.on_frame(json.loads(text))

    async def _to_brain(self, text: str) -> None:
        frame = json.loads(text)
        self.frames_to_brain.append(frame)
        self.voice.on_frame(self.peer, frame)

    def session(self) -> Any:  # noqa: ANN401 — the remote session under test.
        return RemoteTTSProvider(self.link).create_tts_session(
            endpoint_index=0, language="en", idle_close_s=1.0,
            command_queue_capacity=1, audio_queue_capacity=2,
        )


def _run_wire(
    tmp_path: Path, scenario: Callable[[_Wire], Coroutine[Any, Any, Any]],
    provider: _FakeProvider | None = None,
) -> Any:  # noqa: ANN401 — the scenario's result.
    async def go() -> Any:  # noqa: ANN401
        wire = _Wire(tmp_path, provider or _FakeProvider())
        try:
            return await asyncio.wait_for(scenario(wire), 10)
        finally:
            await wire.voice.detach(wire.peer)

    return asyncio.run(go())


def _segment(text: str = "Hello there.", sequence: int = 0, response: str = "R1") -> Any:  # noqa: ANN401
    return TTSResponseSegment(response, 3, sequence, text)


async def _drain(session: Any, count: int) -> list[Any]:  # noqa: ANN401
    events = session.audio_events().__aiter__()
    return [await asyncio.wait_for(events.__anext__(), 5) for _ in range(count)]


def test_provider_events_cross_in_order_with_every_field(tmp_path: Path) -> None:
    """Chunks, the word-time chunk and the end come back equal, in the provider's order."""

    async def scenario(wire: _Wire) -> tuple[list[Any], _FakeSession]:
        session = wire.session()
        await session.connect()  # ahead of the answer, as a prewarm does
        await session.open("R1", 3)
        await session.send(_segment("Hello there."))
        await session.send(_segment("Second one.", sequence=1))
        return await _drain(session, 8), wire.provider.sessions[0]

    got, real = _run_wire(tmp_path, scenario)
    assert got == [*_speak(_segment("Hello there.")), *_speak(_segment("Second one.", 1))]
    # (the brain closes what a terminal leaves open when the link ends, as the last call)
    assert [c[0] for c in real.calls] == ["connect", "open", "send", "send", "close"]
    assert real.calls[1] == ("open", "R1", 3)
    assert real.calls[2][1] == _segment("Hello there.")
    assert (real.endpoint_index, real.language) == (0, "en")
    boundaries = got[2].word_boundaries
    assert boundaries[0] == (1, 20.0)
    assert got[3].usage == {"usage_characters": 12}


def test_a_chunk_is_one_frame_never_split_even_when_large(tmp_path: Path) -> None:
    """A 400 KB provider chunk crosses as one `audio` frame under the frame cap, bit for bit."""
    pcm = bytes(range(256)) * 1600

    def script(segment: TTSResponseSegment) -> list[Any]:
        return [TTSAudioChunk(segment.sequence, pcm, 32_000), TTSSegmentFinished(segment.sequence)]

    async def scenario(wire: _Wire) -> list[Any]:
        session = wire.session()
        await session.open("R1", 3)
        await session.send(_segment())
        return await _drain(session, 2)

    seen: list[int] = []
    original = terminal_voice.Peer.send

    async def watching(self: terminal_voice.Peer, frame: Any) -> None:  # noqa: ANN401
        if isinstance(frame, dict) and frame.get("op") == "audio":
            seen.append(len(json.dumps(frame)))
        await original(self, frame)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(terminal_voice.Peer, "send", watching)
        got = _run_wire(tmp_path, scenario, _FakeProvider(script))
    assert got == [TTSAudioChunk(0, pcm, 32_000), TTSSegmentFinished(0)]
    assert len(seen) == 2
    assert 400_000 < max(seen) < MAX_FRAME_CHARS // 8


def test_each_call_is_answered_and_a_provider_error_keeps_its_name(tmp_path: Path) -> None:
    """A rejected `send` raises on the terminal with the provider's class name in the message."""

    async def scenario(wire: _Wire) -> tuple[str, str]:
        session = wire.session()
        await session.open("R1", 3)
        wire.provider.sessions[0].send_error = TTSConcurrentSendError("a segment feed is active")
        with pytest.raises(RemoteTTSError) as sent:
            await session.send(_segment())
        wire.provider.connect_error = OSError("unreachable")
        late = wire.session()
        with pytest.raises(RemoteTTSError) as connect:
            await late.connect()
        return str(sent.value), str(connect.value)

    sent, connect = _run_wire(tmp_path, scenario)
    assert sent == "TTSConcurrentSendError: a segment feed is active"
    assert connect == "OSError: unreachable"


def test_abort_stops_the_brains_session_and_does_not_wait_for_it(tmp_path: Path) -> None:
    """The terminal's abort returns at once, the brain aborts and forgets the session."""

    async def scenario(wire: _Wire) -> tuple[Any, ...]:
        session = wire.session()
        await session.open("R1", 3)
        await session.send(_segment())
        reader = session.audio_events().__aiter__()
        first = await asyncio.wait_for(reader.__anext__(), 5)
        started = time.monotonic()
        await session.abort("barge_in")
        took = time.monotonic() - started
        drained = [event async for event in reader]  # what was already queued, then the end
        assert all(isinstance(event, TTSAudioChunk | TTSSegmentFinished) for event in drained)
        with pytest.raises(TTSSessionClosedError):
            await session.send(_segment(sequence=1))
        for _ in range(100):
            if not wire.peer.sessions:
                break
            await asyncio.sleep(0.01)
        return first, took, wire.provider.sessions[0].calls, dict(wire.peer.sessions)

    first, took, calls, remaining = _run_wire(tmp_path, scenario)
    assert isinstance(first, TTSAudioChunk)
    assert took < 0.2
    assert ("abort", "barge_in") in calls
    assert calls[-1] == ("close",)
    assert remaining == {}


def test_finish_and_close_end_both_sides_and_close_may_repeat(tmp_path: Path) -> None:
    """`finish` is acknowledged by the brain; `close` of a closed or unknown session is quiet."""

    async def scenario(wire: _Wire) -> list[tuple[Any, ...]]:
        session = wire.session()
        await session.open("R1", 3)
        await session.send(_segment())
        await _drain(session, 4)
        await session.finish()
        await session.close()
        await session.close()
        stranger = wire.session()
        await stranger.close()  # the brain never heard of it: nothing to send, nothing raised
        return wire.provider.sessions[0].calls

    calls = _run_wire(tmp_path, scenario)
    assert [c[0] for c in calls] == ["open", "send", "finish", "close"]


def test_the_brain_ending_the_stream_ends_the_reader_and_a_failure_reaches_it(
    tmp_path: Path,
) -> None:
    """Events before a provider failure still arrive, then the reader raises with the reason."""
    boom = OSError("provider connection reset")

    def script(segment: TTSResponseSegment) -> list[Any]:
        return [*_speak(segment)[:2], boom]

    async def scenario(wire: _Wire) -> tuple[list[Any], str]:
        session = wire.session()
        await session.open("R1", 3)
        await session.send(_segment())
        events = session.audio_events().__aiter__()
        before = [await asyncio.wait_for(events.__anext__(), 5) for _ in range(2)]
        with pytest.raises(RemoteTTSError) as failed:
            await asyncio.wait_for(events.__anext__(), 5)
        return before, str(failed.value)

    before, why = _run_wire(tmp_path, scenario, _FakeProvider(script))
    assert before == _speak(_segment())[:2]
    assert why == "OSError: provider connection reset"


def test_a_dropped_link_fails_everything_in_flight_and_the_brain_aborts_that_terminals_sessions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A reader and a later call fail at once; the brain closes this terminal's sessions only."""
    caplog.set_level(logging.INFO, logger="jarvis.surface.terminal_voice")

    async def scenario(wire: _Wire) -> list[_FakeSession]:
        other = _Wire(tmp_path, wire.provider, name="other", voice=wire.voice)
        theirs = other.session()
        await theirs.open("R9", 1)
        mine = wire.session()
        await mine.open("R1", 3)
        waiting = asyncio.ensure_future(mine.audio_events().__aiter__().__anext__())
        await asyncio.sleep(0.05)  # nothing said yet: the reader waits
        wire.link.down()
        with pytest.raises(RemoteTTSError, match="dropped"):
            await asyncio.wait_for(waiting, 5)
        with pytest.raises(RemoteTTSError):
            await mine.send(_segment())
        await wire.voice.detach(wire.peer)  # what serve_terminal does when the socket ends
        assert [s.closed for s in wire.provider.sessions] == [False, True]
        assert f"aborted 1 tts session(s) for {wire.peer.name}" in caplog.messages
        assert list(other.peer.sessions)
        await theirs.send(_segment(response="R9"))  # the other terminal's session still works
        assert await _drain(theirs, 1)
        await wire.voice.detach(other.peer)
        return wire.provider.sessions

    sessions = _run_wire(tmp_path, scenario)
    assert all(s.closed for s in sessions)


def test_with_no_brain_connected_a_call_fails_at_once_and_says_why(tmp_path: Path) -> None:
    """No link: connect and open fail fast with a plain sentence; abort and close are quiet."""

    async def scenario(_wire: _Wire) -> tuple[float, str, str]:
        journal = Journal(tmp_path / "alone.db")
        provider = RemoteTTSProvider(VoiceLink(journal))
        session = provider.create_tts_session(
            endpoint_index=0, language="zh", idle_close_s=1.0,
            command_queue_capacity=1, audio_queue_capacity=2,
        )
        started = time.monotonic()
        with pytest.raises(RemoteTTSError) as connect:
            await session.connect()
        with pytest.raises(RemoteTTSError) as opened:
            await session.open("R1", 1)
        took = time.monotonic() - started
        await session.abort("x")
        await session.close()
        return took, str(connect.value), str(opened.value)

    took, connect, opened = _run_wire(tmp_path, scenario)
    assert took < 0.1
    assert connect == opened == "the brain is not reachable, so there is no speech provider"


def test_a_session_from_before_a_reconnect_stays_dead_and_a_new_one_works(
    tmp_path: Path,
) -> None:
    """The link coming back does not revive a session the drop failed."""

    async def scenario(wire: _Wire) -> None:
        old = wire.session()
        await old.open("R1", 3)
        wire.link.down()
        await asyncio.sleep(0.05)
        wire.up()
        with pytest.raises(RemoteTTSError):
            await old.send(_segment())
        fresh = wire.session()
        await fresh.open("R2", 4)
        await fresh.send(_segment(response="R2"))
        assert await _drain(fresh, 1)

    _run_wire(tmp_path, scenario)


def test_refusals_carry_the_brains_reasons(tmp_path: Path) -> None:
    """Each refusal is a `Refused:` sentence, and nothing was created for it."""

    async def scenario(wire: _Wire) -> list[str]:
        provider = RemoteTTSProvider(wire.link)
        out: list[str] = []
        for changes in ({"endpoint_index": 2}, {"language": "fr"}, {"idle_close_s": 0.0},
                        {"audio_queue_capacity": 10_000}):
            session = provider.create_tts_session(**{**PARAMS, **changes})
            with pytest.raises(RemoteTTSError) as why:
                await session.connect()
            out.append(str(why.value))
        held = [provider.create_tts_session(**PARAMS) for _ in range(terminal_voice.MAX_SESSIONS)]
        for session in held:
            await session.connect()
        extra = provider.create_tts_session(**PARAMS)
        with pytest.raises(RemoteTTSError) as why:
            await extra.connect()
        out.append(str(why.value))
        assert len(wire.provider.sessions) == terminal_voice.MAX_SESSIONS
        await held[0].close()
        await asyncio.sleep(0.05)
        gone = wire.session()
        await gone.open("R1", 3)
        wire.peer.sessions.pop(gone.sid)  # as if the brain had lost it
        with pytest.raises(RemoteTTSError) as unknown:
            await gone.send(_segment())
        out.append(str(unknown.value))
        return out

    reasons = _run_wire(tmp_path, scenario)
    assert all(reason.startswith("Refused: ") for reason in reasons)
    assert "at most 8 provider sessions" in reasons[-2]
    assert reasons[-1] == "Refused: no such session"


def test_a_frame_the_brain_cannot_decode_fails_the_session_not_the_link(
    tmp_path: Path,
) -> None:
    """Audio out of order or of an unknown kind is a failure the reader sees."""

    async def scenario(wire: _Wire) -> tuple[str, str]:
        session = wire.session()
        await session.open("R1", 3)
        reader = session.audio_events().__aiter__()
        wire.link.on_frame({"type": "tts", "sid": session.sid, "op": "audio", "n": 5,
                            "event": encode_event(TTSSegmentFinished(0))})
        with pytest.raises(RemoteTTSError) as order:
            await asyncio.wait_for(reader.__anext__(), 5)
        other = wire.session()
        await other.open("R2", 3)
        reader = other.audio_events().__aiter__()
        wire.link.on_frame({"type": "tts", "sid": other.sid, "op": "audio", "n": 0,
                            "event": {"kind": "burp", "sequence": 0}})
        with pytest.raises(RemoteTTSError) as kind:
            await asyncio.wait_for(reader.__anext__(), 5)
        return str(order.value), str(kind.value)

    order, kind = _run_wire(tmp_path, scenario)
    assert "out of order" in order
    assert "unknown audio event kind" in kind


# --- the journal ------------------------------------------------------------------------------


def _row(
    uid: str = "a" * 32, kind: str = "surface.response_open", **payload: Any,  # noqa: ANN401
) -> dict[str, Any]:
    return {
        "type": "row", "id": 7, "event_uid": uid, "event_type": kind, "schema_version": 1,
        "ts_epoch_ms": 1_700_000_000_000,
        "payload": payload or {"turn_id": "T1", "query": "q", "kind": "text"},
    }


def test_the_journal_is_emptied_every_start_and_keeps_a_row_exactly_as_it_came(
    tmp_path: Path,
) -> None:
    """A second journal at the same path starts empty; a row keeps its uid, type, time, payload."""
    path = tmp_path / "terminal" / "voice-journal.db"
    first = Journal(path)
    assert first.append(_row())
    assert first.append(_row())  # the same row twice (a replay) is not a second row
    [event] = list(iter_events(first.conn))
    assert (event.event_uid, event.type, event.ts_epoch_ms) == (
        "a" * 32, "surface.response_open", 1_700_000_000_000,
    )
    assert event.payload == {"turn_id": "T1", "query": "q", "kind": "text"}
    first.conn.close()

    second = Journal(path)
    assert list(iter_events(second.conn)) == []
    assert sorted(p.name for p in path.parent.iterdir() if p.is_file()) == sorted(
        p.name for p in path.parent.iterdir() if p.name.startswith("voice-journal.db")
    )


@pytest.mark.parametrize(
    "bad",
    [
        _row(kind="surface.playback_completed"),  # not an answer row: never journaled
        _row(kind="action.result_observed"),
        {**_row(), "event_uid": "not-hex"},
        {**_row(), "payload": []},
        {**_row(), "ts_epoch_ms": "now"},
        {**_row(), "schema_version": None},
        _row(kind="surface.response_chunk"),  # the payload misses a required field
    ],
)
def test_the_journal_takes_only_well_formed_answer_rows(
    tmp_path: Path, bad: dict[str, Any],
) -> None:
    """Anything else the brain might send is refused, and `last_row` does not move."""
    journal = Journal(tmp_path / "j.db")
    link = VoiceLink(journal)
    link.on_frame(bad)
    assert list(iter_events(journal.conn)) == []
    assert link.last_row is None
    link.on_frame(_row())
    assert link.last_row == 7


# --- the brain's end of /terminal/ws -----------------------------------------------------------


def _brain_log(path: Path) -> sqlite3.Connection:
    """The brain's event log, usable from the server's own thread."""
    open_event_log(path).close()
    return sqlite3.connect(path, check_same_thread=False)


def _brain_client(
    tmp_path: Path, *, provider: _FakeProvider | None = None, speaks: bool = True,
) -> tuple[TestClient, TerminalHub, Path]:
    log = tmp_path / "events.db"
    conn = _brain_log(log)
    hub = TerminalHub(events=BrainEvents(conn))
    if speaks:
        hub.voice = BrainVoice(
            cast("Any", provider or _FakeProvider()), inherent_loop._SpokenRows(conn),  # noqa: SLF001
            poll_interval_s=0.01,
        )
    app = create_app(
        InherentDeps(
            submit_callable=lambda _text: "T1",
            broadcaster=InherentBroadcaster(),
            terminals=hub,
            phone_events=hub.events,
            device_name=functools.partial(device_name_for_token, tmp_path),
        ),
    )
    require_local_key(
        app,
        functools.partial(local_key_matches, local_key(tmp_path)),
        device_token_matches=functools.partial(device_token_matches, tmp_path),
    )
    return TestClient(app, base_url="http://127.0.0.1:8006", client=(REMOTE, 50000)), hub, log


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _hello(**extra: Any) -> str:  # noqa: ANN401
    return json.dumps({"type": "hello", "tools": ["read_clipboard"], **extra})


def _answer(log: Path, turn: str, text: str = "Hello.", *, channel: str | None = None) -> list[str]:
    """Append one answer's three rows to the brain's log; their uids."""
    conn = open_event_log(log)
    try:
        group = f"G-{turn}"
        extra = {} if channel is None else {"attention_channel": channel}
        events = [
            emit_event(conn, type="surface.response_open", payload={
                "turn_id": turn, "query": "q", "kind": "text", "response_id": f"R-{turn}",
                "response_group_id": group, "phase": "final", "channel": "speech", **extra,
            }),
            emit_event(conn, type="surface.response_chunk", payload={
                "turn_id": turn, "text": text, "response_id": f"R-{turn}",
                "response_group_id": group, "sequence": 0, "phase": "final",
                "channel": "speech", "segment_hash": "h",
            }),
            emit_event(conn, type="surface.response_emitted", payload={
                "turn_id": turn, "text": text, "response_id": f"R-{turn}",
                "response_group_id": group, "phase": "final", "channel": "speech",
            }),
        ]
    finally:
        conn.close()
    return [event.event_uid for event in events]


def _rows(ws: Any, count: int) -> list[dict[str, Any]]:  # noqa: ANN401
    return [dict(ws.receive_json()) for _ in range(count)]


def test_a_first_connection_hears_only_what_is_said_after_it(tmp_path: Path) -> None:
    """Old answers are not replayed; the new answer arrives whole, in order, as it was logged."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    old = _answer(log, "OLD")
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True, rows_after=None))
        assert ws.receive_json() == {"type": "ready", "device": "macbook", "voice": True,
                                     "listen": False, "baseline": []}
        new = _answer(log, "NEW", "Fresh answer.")
        rows = _rows(ws, 3)
    assert [row["event_uid"] for row in rows] == new
    assert old
    assert not set(old) & {row["event_uid"] for row in rows}
    assert [row["event_type"] for row in rows] == [
        "surface.response_open", "surface.response_chunk", "surface.response_emitted",
    ]
    assert rows[1]["payload"]["text"] == "Fresh answer."
    assert all(isinstance(row["id"], int) and row["ts_epoch_ms"] > 0 for row in rows)
    assert rows[0]["id"] < rows[1]["id"] < rows[2]["id"]


def test_a_reconnect_resumes_after_what_the_terminal_journaled(tmp_path: Path) -> None:
    """Rows said while the terminal was away arrive once, in order, after its last row id."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        ws.receive_json()
        _answer(log, "ONE")
        first = _rows(ws, 3)
    missed = _answer(log, "TWO") + _answer(log, "THREE")
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True, rows_after=first[-1]["id"]))
        ws.receive_json()
        rows = _rows(ws, 6)
        later = _answer(log, "FOUR")
        rows += _rows(ws, 3)
    assert [row["event_uid"] for row in rows] == [*missed, *later]


def test_a_resume_point_past_the_log_starts_at_its_end(tmp_path: Path) -> None:
    """A terminal that remembers a row this log never had (a replaced brain) gets no replay."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    _answer(log, "OLD")
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True, rows_after=10_000))
        ws.receive_json()
        new = _answer(log, "NEW")
        assert [row["event_uid"] for row in _rows(ws, 3)] == new


def test_turns_on_a_silent_channel_are_not_streamed(tmp_path: Path) -> None:
    """What the local speaker would not say is not sent: a silent attention channel."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        ws.receive_json()
        _answer(log, "QUIET", channel="silent_log")
        spoken = _answer(log, "LOUD")
        assert [row["event_uid"] for row in _rows(ws, 3)] == spoken


def test_only_the_voice_terminal_connected_last_is_spoken_to(tmp_path: Path) -> None:
    """The rows go to the newest voice terminal; an older one and a mute one get none."""
    first = pair_device(tmp_path, "macbook")
    second = pair_device(tmp_path, "ipad")
    third = pair_device(tmp_path, "watch")
    client, _hub, log = _brain_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(first)) as old, \
            client.websocket_connect(TERMINAL_WS, headers=_bearer(second)) as new, \
            client.websocket_connect(TERMINAL_WS, headers=_bearer(third)) as mute:
        for ws, voice in ((old, True), (new, True), (mute, False)):
            ws.send_text(_hello(voice=True) if voice else _hello())
            ws.receive_json()
        uids = _answer(log, "T1")
        assert [row["event_uid"] for row in _rows(new, 3)] == uids
        # Whatever a terminal was sent comes before the answer to its next frame: the answer
        # to this frame must be the first thing each of the others receives.
        probe = _playback_frame("1" * 32)
        assert _ack(old, probe)["type"] == "ack"
        assert _ack(mute, probe)["code"] == "event_type_not_allowed"


def test_a_brain_without_a_provider_says_so_and_refuses_speech_requests(tmp_path: Path) -> None:
    """`ready` says `voice: false`; a request is answered with a refusal, not left to time out."""
    token = pair_device(tmp_path, "macbook")
    client, hub, _log = _brain_client(tmp_path, speaks=False)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        assert ws.receive_json()["voice"] is False
        ws.send_text(json.dumps({"type": "tts", "sid": "a" * 32, "rid": 1, "op": "connect",
                                 "params": PARAMS}))
        refusal = dict(ws.receive_json())
        assert (refusal["rid"], refusal["ok"], refusal["error"]["name"]) == (1, False, "Refused")
        assert hub.connected()


def test_a_terminal_told_the_brain_cannot_speak_fails_its_sessions_at_once(
    tmp_path: Path,
) -> None:
    """The `ready` that says `voice: false` makes a session refuse before it sends anything."""

    async def scenario(wire: _Wire) -> str:
        wire.link.up(wire._to_brain, asyncio.get_running_loop(), {"type": "ready"})  # noqa: SLF001
        with pytest.raises(RemoteTTSError) as why:
            await wire.session().connect()
        assert wire.frames_to_brain == []
        return str(why.value)

    assert _run_wire(tmp_path, scenario) == "the brain has no speech provider"


@pytest.mark.parametrize(
    "hello",
    [{"voice": "yes"}, {"voice": True, "rows_after": -1}, {"voice": True, "rows_after": "7"},
     {"voice": True, "rows_after": True}],
)
def test_a_malformed_voice_hello_closes_the_socket(tmp_path: Path, hello: dict[str, Any]) -> None:
    """The voice fields are checked like the tool list is."""
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    token = pair_device(tmp_path, "macbook")
    client, hub, _log = _brain_client(tmp_path)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(**hello))
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1008
    assert hub.connected() == []


def test_tts_frames_over_the_real_socket_create_run_and_abort_a_session(tmp_path: Path) -> None:
    """The brain's end through `serve_terminal`: open, send, audio frames, abort, link drop."""
    token = pair_device(tmp_path, "macbook")
    provider = _FakeProvider()
    client, _hub, _log = _brain_client(tmp_path, provider=provider)
    sid = "b" * 32
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        ws.receive_json()

        def call(rid: int, op: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
            ws.send_text(json.dumps({"type": "tts", "sid": sid, "rid": rid, "op": op, **fields}))
            while True:
                frame = dict(ws.receive_json())
                if frame.get("rid") == rid:
                    return frame

        assert call(1, "open", params=PARAMS, response_id="R1", playback_generation_id=2) == {
            "type": "tts", "sid": sid, "rid": 1, "ok": True,
        }
        assert call(2, "send", response_id="R1", playback_generation_id=2, sequence=0,
                    text="Hi.")["ok"] is True
        audio = [dict(ws.receive_json()) for _ in range(4)]
        assert [(f["op"], f["n"]) for f in audio] == [("audio", i) for i in range(4)]
        assert [f["event"]["kind"] for f in audio] == ["chunk", "chunk", "chunk", "finished"]
        assert audio[2]["event"]["word_boundaries"] == [[1, 20.0], [2, 40.0], [3, 60.0]]
        assert audio[3]["event"]["usage"] == {"usage_characters": 3}
        assert call(3, "send", response_id="R1", playback_generation_id=2, sequence="x",
                    text="no")["error"]["name"] == "Refused"
        ws.send_text(json.dumps({"type": "tts", "sid": sid, "op": "abort", "reason": "stop"}))
    deadline = time.monotonic() + 5
    while not provider.sessions[0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ("abort", "stop") in provider.sessions[0].calls


def test_a_session_left_open_when_the_socket_closes_is_aborted(tmp_path: Path) -> None:
    """Link drop on the brain: the real session is closed, not left to idle out."""
    token = pair_device(tmp_path, "macbook")
    provider = _FakeProvider()
    client, _hub, _log = _brain_client(tmp_path, provider=provider)
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        ws.receive_json()
        ws.send_text(json.dumps({"type": "tts", "sid": "c" * 32, "rid": 1, "op": "connect",
                                 "params": PARAMS}))
        assert ws.receive_json()["ok"] is True
    deadline = time.monotonic() + 5
    while not provider.sessions[0].closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert provider.sessions[0].closed


# --- the allowlist ------------------------------------------------------------------------------


def _playback_frame(
    uid: str, kind: str = "surface.playback_completed", **extra: Any,  # noqa: ANN401
) -> dict[str, Any]:
    payloads: dict[str, dict[str, Any]] = {
        "surface.playback_completed": {
            "session_id": "S", "response_id": "R", "turn_id": "T", "playback_generation_id": 1,
            "heard_through_sequence": 0, "submitted_samples": 160, "heard_text_hash": "h",
            "speech_text_hash": "s", "heard_text": "Hi.",
        },
        "tts.usage_observed": {
            "provider": "minimax", "characters": 3, "response_id": "R", "sequence": 0,
            "actor": "observer",
        },
        "surface.speech_dropped": {"response_id": "R", "turn_id": "T", "reason": "stopped"},
    }
    return {
        "type": "event", "event_uid": uid, "event_type": kind,
        "ts_epoch_ms": int(time.time() * 1000),
        "payload": payloads[kind], **extra,
    }


def _ack(ws: Any, frame: dict[str, Any]) -> dict[str, Any]:  # noqa: ANN401
    ws.send_text(json.dumps(frame))
    return dict(ws.receive_json())


def test_playback_rows_are_taken_from_a_voice_terminal_only_and_once(tmp_path: Path) -> None:
    """Allowed with `voice`, refused without; deduped by uid; ids and links are kept."""
    token = pair_device(tmp_path, "macbook")
    client, _hub, log = _brain_client(tmp_path)
    anchor = _answer(log, "T")[0]
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello())
        ws.receive_json()
        refused = _ack(ws, _playback_frame("1" * 32))
        assert (refused["ok"], refused["code"]) == (False, "event_type_not_allowed")
    with client.websocket_connect(TERMINAL_WS, headers=_bearer(token)) as ws:
        ws.send_text(_hello(voice=True))
        ws.receive_json()
        frame = _playback_frame(
            "1" * 32, schema_version=1, source_event_id=anchor, correlation={"turn_id": "T"},
        )
        assert _ack(ws, frame) == {"type": "ack", "event_uid": "1" * 32, "ok": True}
        assert _ack(ws, frame)["ok"] is True  # a resend is acknowledged, not appended twice
        for uid, kind in (("2" * 32, "tts.usage_observed"), ("3" * 32, "surface.speech_dropped")):
            assert _ack(ws, _playback_frame(uid, kind))["ok"] is True
        dangling = _playback_frame("4" * 32, source_event_id="f" * 32)
        assert _ack(ws, dangling)["code"] == "bad_event"
        bad_link = _playback_frame("5" * 32, correlation={"turn_id": 7})
        assert _ack(ws, bad_link)["code"] == "bad_event"
        wrong = _ack(ws, {**_playback_frame("6" * 32), "event_type": "utterance.received"})
        assert wrong["code"] == "event_type_not_allowed"
    with sqlite3.connect(log) as conn:
        found = conn.execute(
            "SELECT event_uid, type, ingestion_node, source_event_id, correlation_json "
            "FROM events WHERE event_uid IN (?, ?, ?) ORDER BY id", ("1" * 32, "2" * 32, "3" * 32),
        ).fetchall()
    assert found[0][:4] == ("1" * 32, "surface.playback_completed", "macbook", anchor)
    assert json.loads(found[0][4]) == {"turn_id": "T"}
    assert [row[1] for row in found] == [
        "surface.playback_completed", "tts.usage_observed", "surface.speech_dropped",
    ]


def test_the_allowlists_are_what_the_media_actor_writes() -> None:
    """Every row type the media actor emits is forwardable, and observer types are unchanged."""
    source = Path(voice_media.__file__).read_text(encoding="utf-8")
    emitted = {
        kind for kind in PLAYBACK_EVENT_TYPES if f'"{kind}"' in source
    }
    assert emitted == PLAYBACK_EVENT_TYPES
    assert not PLAYBACK_EVENT_TYPES & OBSERVER_EVENT_TYPES
    outbox_types = {
        "surface.playback_started", "surface.playback_segment_prepared",
        "surface.playback_alignment", "surface.playback_checkpoint", "surface.playback_completed",
        "surface.playback_interrupted", "surface.playback_failed",
        "surface.playback_lane_isolated", "surface.speech_dropped", "tts.usage_observed",
    }
    assert outbox_types == PLAYBACK_EVENT_TYPES


def test_the_outbox_forwards_a_playback_row_under_its_own_id_and_refuses_the_rest() -> None:
    """`forward` keeps uid, time and links; observers' types and oversize rows are errors."""

    async def scenario() -> list[dict[str, Any]]:
        outbox = EventOutbox()
        event = Event(
            "7" * 32, "surface.playback_completed", 1, 1_700_000_000_123,
            _playback_frame("7" * 32)["payload"], "8" * 32, {"turn_id": "T"},
        )
        outbox.forward(event)
        with pytest.raises(ValueError, match="not a row a voice terminal may send"):
            outbox.forward(Event("9" * 32, "repo.state_observed", 1, 1, {}, None, None))
        huge = Event("a" * 32, "surface.playback_completed", 1, 1, {"heard_text": "x" * 70_000},
                     None, None)
        with pytest.raises(ValueError, match="over the"):
            outbox.forward(huge)
        return [json.loads(text) for text in outbox._pending.values()]  # noqa: SLF001

    [frame] = asyncio.run(scenario())
    assert frame["event_uid"] == "7" * 32
    assert (frame["ts_epoch_ms"], frame["source_event_id"], frame["correlation"]) == (
        1_700_000_000_123, "8" * 32, {"turn_id": "T"},
    )


# --- the media actor's builder takes the remote provider ---------------------------------


class _Stream:
    """A PortAudio stream with nobody on the other end: `start` begins pulling blocks."""

    active = True
    latency = 0.0

    def __init__(self, callback: Callable[..., None], *, pull: bool) -> None:
        self._callback = callback
        self._pull = pull
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        if self._pull:
            self._thread.start()

    def _run(self) -> None:
        out = np.zeros((128, 1), dtype=np.float32)
        while not self._stop.is_set():
            self._callback(out, 128, None, None)
            time.sleep(0.001)

    def stop(self) -> None:
        self._stop.set()
        self.active = False

    def close(self) -> None:
        self.stop()


def _open_stream(*, pull: bool) -> Callable[..., _Stream]:
    def open_stream(*, callback: Callable[..., None], **_ignored: Any) -> _Stream:  # noqa: ANN401
        return _Stream(callback, pull=pull)

    return open_stream


@dataclass(frozen=True)
class _Paths:
    event_log: Path


def _host(config: dict[str, Any], journal: Journal) -> Any:  # noqa: ANN401
    return SimpleNamespace(
        config=config, wave1_features=Wave1FeatureFlags(
            transactional_event_append=True, lifecycle_terminal_cas=True,
        ),
        response_flags=Wave4ResponseFlags(), runtime_paths=_Paths(journal.path), conn=journal.conn,
        memory=None,
    )


SPEECH_CONFIG: dict[str, Any] = {
    "realtime": {
        "enabled": True,
        "concurrency_safety": {"transactional_event_append": True, "lifecycle_terminal_cas": True},
        "streaming_output": {
            "enabled": True, "canonical_sample_rate_hz": RATE, "presentation_poll_s": 0.002,
            "ring_retry_s": 0.001, "enable_macos_say_fallback": False,
            "prefetch_network_lost_line": False, "response_timeout_s": 10.0,
            "session_idle_close_s": 5.0,
        },
    },
}


def test_the_media_actor_builder_runs_around_a_remote_provider_with_no_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same builder `all` uses takes the remote provider; no key, and no legacy fallback."""
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    journal = Journal(tmp_path / "j.db")
    remote = RemoteTTSProvider(VoiceLink(journal))
    broadcaster = cast("Any", SimpleNamespace())
    monkeypatch.setattr(voice_tts, "_open_output_stream", _open_stream(pull=False))
    pipeline = inherent_loop._build_tts_pipeline(  # noqa: SLF001
        _host(SPEECH_CONFIG, journal), broadcaster, remote=remote,
        network_lost_dir=tmp_path / "kept",
    )
    assert isinstance(pipeline, voice_media.StreamingTTSPipeline)
    assert pipeline._provider is remote  # noqa: SLF001
    assert pipeline._config.network_lost_cache_dir == tmp_path / "kept"  # noqa: SLF001
    assert pipeline.close()

    off = {"realtime": {"enabled": False}}
    assert inherent_loop._build_tts_pipeline(  # noqa: SLF001
        _host(off, journal), broadcaster, remote=remote,
    ) is None
    assert inherent_loop._build_tts_pipeline(  # noqa: SLF001
        _host(SPEECH_CONFIG, journal), broadcaster,
    ) is None  # on one machine, no key is still no speech


def test_the_command_takes_voice_as_opt_in_and_declares_it_in_the_hello(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--voice` hands the client a VoiceLink; without it there is none and no journal."""
    token = tmp_path / "token"
    token.write_text("t\n", encoding="utf-8")
    token.chmod(0o600)
    seen: list[VoiceLink | None] = []

    async def client(*_args: object, **kwargs: Any) -> None:  # noqa: ANN401
        seen.append(kwargs["voice"])
        raise KeyboardInterrupt

    monkeypatch.setattr("jarvis.runtime.terminal.run_terminal_client", client)
    monkeypatch.setattr(voice_tts, "_open_output_stream", _open_stream(pull=False))
    monkeypatch.setattr(
        "jarvis.runtime.terminal._build_tts_pipeline", lambda *_a, **_k: None,
    )
    base = ["terminal", "--brain", "http://127.0.0.1:1", "--brain-token-file", str(token),
            "--runtime-root", str(tmp_path)]
    assert main(base) == 0
    assert seen == [None]
    assert not (tmp_path / JOURNAL).exists()
    assert main([*base, "--voice"]) == 0
    assert seen == [None, None]  # the builder said no: a mute terminal does not claim to speak
    assert (tmp_path / JOURNAL).exists()

    real = VoiceLink(Journal(tmp_path / "x.db"))
    assert real.hello() == {"voice": True, "rows_after": None}


# --- end to end over a real websocket -------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _AsRemote:
    """ASGI shim: the server sees every peer as a device on the tailnet, not on loopback."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            scope = {**scope, "client": (REMOTE, 50000)}
        await self.app(scope, receive, send)


class _Brain:
    """A real uvicorn server: the terminal route, a brain log, and a fake speech provider."""

    def __init__(self, root: Path) -> None:
        self.port = _free_port()
        self.log = root / "events.db"
        conn = _brain_log(self.log)
        self.provider = _FakeProvider()
        self.hub = TerminalHub(events=BrainEvents(conn))
        self.hub.voice = BrainVoice(
            cast("Any", self.provider), inherent_loop._SpokenRows(conn),  # noqa: SLF001
            poll_interval_s=0.01,
        )
        app = create_app(
            InherentDeps(
                submit_callable=lambda _text: "T1",
                broadcaster=InherentBroadcaster(),
                terminals=self.hub,
                device_name=functools.partial(device_name_for_token, root),
            ),
        )
        require_local_key(
            app,
            functools.partial(local_key_matches, local_key(root)),
            device_token_matches=functools.partial(device_token_matches, root),
        )
        self.server = uvicorn.Server(
            uvicorn.Config(_AsRemote(app), host="127.0.0.1", port=self.port,
                           log_level="warning", lifespan="off"),
        )
        self.thread = threading.Thread(
            target=lambda: asyncio.run(self.server.serve()), daemon=True,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        _wait_for(lambda: self.server.started, "server never started")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _Terminal:
    """`python -m jarvis terminal --voice` on its own thread and loop: the real wiring."""

    def __init__(
        self, url: str, token: str, root: Path, config: dict[str, Any] | None = None,
    ) -> None:
        self.loop = asyncio.new_event_loop()
        self.task: asyncio.Task[None] | None = None
        self.error: BaseException | None = None
        self._args = (url, token, root)
        self._config = SPEECH_CONFIG if config is None else config
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        async def main() -> None:
            url, token, root = self._args
            self.task = asyncio.create_task(_run(
                url, token, tools=frozenset({"read_clipboard"}),
                execute=lambda *_: {"ok": True, "output": {}}, watched=None,
                speaking=_Speaking(self._config, root),
            ))
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self.task
                except BaseException as exc:  # noqa: BLE001 — handed to the test
                    self.error = exc

        self.loop.run_until_complete(main())

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.loop.call_soon_threadsafe(lambda: self.task and self.task.cancel())
        self.thread.join(timeout=15)
        assert not self.thread.is_alive()


def _wait_for(condition: Callable[[], bool], what: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.02)


def _types(log: Path) -> list[str]:
    with sqlite3.connect(log) as conn:
        return [row[0] for row in conn.execute("SELECT type FROM events ORDER BY id")]


def test_an_answer_in_the_brains_log_is_spoken_by_the_terminal_and_the_log_ends_with_what_it_heard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole path: rows -> terminal journal -> real media actor -> brain's provider -> player.

    The brain has a fake provider, the terminal a real StreamingTTSPipeline over a player whose
    stream is pulled by a thread instead of PortAudio. The brain's log must end with the
    terminal's `surface.playback_completed`, written under the terminal's device name, whose
    heard text is the whole answer, and the conversation fold on the brain must agree.
    """
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
    monkeypatch.setattr(voice_tts, "_open_output_stream", _open_stream(pull=True))
    token = pair_device(tmp_path, "macbook")
    root = tmp_path / "terminal-root"
    answer = ["The first sentence.", "And the second."]
    with _Brain(tmp_path) as brain, _Terminal(brain.url, token, root) as terminal:
        _wait_for(lambda: bool(brain.hub.connected()), "the terminal never connected")
        _wait_for(
            lambda: bool(brain.hub.voice and brain.hub.voice._peers),  # noqa: SLF001
            "the brain never took the terminal as a voice terminal",
        )
        conn = open_event_log(brain.log)
        emit_event(conn, type="surface.user_intent", payload={"transcript": "hi", "turn_id": "T1"})
        emit_event(conn, type="surface.response_open", payload={
            "turn_id": "T1", "query": "hi", "kind": "text", "response_id": "R1",
            "response_group_id": "G1", "phase": "final", "channel": "speech",
        })
        for sequence, text in enumerate(answer):
            emit_event(conn, type="surface.response_chunk", payload={
                "turn_id": "T1", "text": text, "response_id": "R1", "response_group_id": "G1",
                "sequence": sequence, "phase": "final", "channel": "speech", "segment_hash": "",
            })
        emit_event(conn, type="surface.response_emitted", payload={
            "turn_id": "T1", "text": "".join(answer), "response_id": "R1",
            "response_group_id": "G1", "phase": "final", "channel": "speech",
        })
        _wait_for(
            lambda: "surface.playback_completed" in _types(brain.log),
            "the brain never heard the terminal finish playing",
        )
        kinds = _types(brain.log)
        completed = [
            e for e in iter_events(conn) if e.type == "surface.playback_completed"
        ]
        history = fold_conversation_history(iter_events(conn))
        with sqlite3.connect(brain.log) as raw:
            nodes = dict(raw.execute(
                "SELECT type, ingestion_node FROM events WHERE type LIKE 'surface.playback_%' "
                "OR type = 'tts.usage_observed'",
            ).fetchall())
        conn.close()
        assert terminal.error is None

    [done] = completed
    assert done.payload["heard_text"] == "".join(answer)
    assert done.payload["provider"] != "macos_say"
    assert kinds[-1] == "surface.playback_completed"
    assert kinds[:5] == [
        "surface.user_intent", "surface.response_open", "surface.response_chunk",
        "surface.response_chunk", "surface.response_emitted",
    ]
    assert kinds[5] == "surface.playback_started"
    assert {kind: kinds.count(kind) for kind in PLAYBACK_EVENT_TYPES if kind in kinds} | {
        "surface.playback_checkpoint": 0,
    } == {
        "surface.playback_started": 1, "surface.playback_segment_prepared": 2,
        "surface.playback_alignment": 2, "tts.usage_observed": 2,
        "surface.playback_completed": 1, "surface.playback_checkpoint": 0,
    }
    assert nodes["surface.playback_completed"] == "macbook"
    assert nodes["surface.playback_started"] == "macbook"
    heard = history.turns[0].responses[0].spoken_heard
    assert heard is not None
    assert heard.text == "".join(answer)
    [session] = brain.provider.sessions
    assert [c[0] for c in session.calls[:2]] in (["open", "send"], ["connect", "open"])
    sent = [c[1].text for c in session.calls if c[0] == "send"]
    assert "".join(sent) == "".join(answer)
    assert session.closed
    # The journal is the terminal's scratch: it holds the answer rows, not the owner's state,
    # and every playback row the brain has is the terminal's own, under the same id.
    with sqlite3.connect(root / JOURNAL) as raw:
        held = {row[0] for row in raw.execute("SELECT type FROM events")}
        theirs = {row[0] for row in raw.execute(
            "SELECT event_uid FROM events WHERE type LIKE 'surface.playback_%' "
            "OR type = 'tts.usage_observed'",
        )}
    with sqlite3.connect(brain.log) as raw:
        ours = {row[0] for row in raw.execute(
            "SELECT event_uid FROM events WHERE ingestion_node = 'macbook'",
        )}
    assert "surface.response_open" in held
    assert "surface.user_intent" not in held
    assert ours == theirs
