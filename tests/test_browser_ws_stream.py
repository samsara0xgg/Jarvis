# tests/test_browser_ws_stream.py
"""Tests for browser-side WebSocket TTS streaming (BrowserWSPlayer + server routes)."""
import asyncio
import struct
from unittest.mock import MagicMock

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Task 1: BrowserWSPlayer
# ---------------------------------------------------------------------------

class TestBrowserWSPlayer:
    def test_write_forwards_header_plus_int16_bytes(self):
        """BrowserWSPlayer.write packs uint16 header + int16LE samples
        and schedules ws.send_bytes via run_coroutine_threadsafe."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        import ui.web.browser_ws_player as mod

        captured_payloads: list[bytes] = []

        async def fake_send_bytes(data: bytes) -> None:
            captured_payloads.append(data)

        ws = MagicMock()
        ws.send_bytes = fake_send_bytes

        # Replace asyncio.run_coroutine_threadsafe with a sync driver so the
        # coroutine runs inline in the test. The production path needs
        # cross-thread scheduling; the test doesn't have two threads.
        loop = asyncio.new_event_loop()
        original = mod.asyncio.run_coroutine_threadsafe
        def fake_run_cot(coro, _loop):
            loop.run_until_complete(coro)
            return MagicMock()
        mod.asyncio.run_coroutine_threadsafe = fake_run_cot
        try:
            player = BrowserWSPlayer(ws=ws, sentence_index=3, loop=loop, pace=False)
            pcm = np.array([1.0, -1.0, 0.5, 0.0], dtype=np.float32)
            player.write(pcm)

            assert len(captured_payloads) == 1
            payload = captured_payloads[0]
            # Header: uint16 LE = 3 → b"\x03\x00"
            assert payload[:2] == b"\x03\x00"
            # Body: 4 * int16 LE. 1.0 → 32767, -1.0 → -32767, 0.5 → 16383, 0.0 → 0
            import struct as _s
            samples = _s.unpack("<4h", payload[2:])
            assert samples[0] == 32767
            assert samples[1] == -32767
            assert 16000 <= samples[2] <= 16383  # round/clip leeway
            assert samples[3] == 0
        finally:
            mod.asyncio.run_coroutine_threadsafe = original
            loop.close()

    def test_played_samples_reads_cursor(self):
        """played_samples must reflect the browser AudioWorklet cursor
        (what was actually played) — not the encoded-queue count, which
        over-reports by up to _MAX_AHEAD_SECONDS of buffered-ahead PCM."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            cursor_value = 0

            def get_cursor() -> int:
                return cursor_value

            ws = MagicMock()
            player = BrowserWSPlayer(
                ws=ws, sentence_index=0, loop=loop, pace=False,
                get_cursor=get_cursor,
            )
            assert player.played_samples == 0
            cursor_value = 4800
            assert player.played_samples == 4800
            # Encode path must not mutate played_samples.
            player.write(np.zeros(100, dtype=np.float32))
            assert player.played_samples == 4800
            cursor_value = 7200
            assert player.played_samples == 7200
        finally:
            loop.close()

    def test_played_samples_no_cursor_returns_zero(self):
        """Without a cursor callable (tests/legacy), played_samples == 0.
        WP5 falls through to L3 (whole-sentence unheard) rather than
        trusting a fake counter."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            player = BrowserWSPlayer(
                ws=MagicMock(), sentence_index=0, loop=loop, pace=False,
            )
            player.write(np.zeros(100, dtype=np.float32))
            assert player.played_samples == 0
        finally:
            loop.close()

    def test_drain_returns_true(self):
        """D11: drain() must return True so PlaybackResult.completed=True
        and WP5 records this sentence in played_texts."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            player = BrowserWSPlayer(ws=MagicMock(), sentence_index=0, loop=loop, pace=False)
            assert player.drain(5.0) is True
            assert player.drain() is True  # default timeout
        finally:
            loop.close()

    def test_write_never_blocks(self):
        """write() must be non-blocking — server-side pacing was tried and
        dropped because time.sleep() inside write() stalls the FastAPI loop
        (write is called from _stream_to_player_async running on it).
        Client ring buffer is large enough to absorb fast TTS arrival."""
        import time as _time
        from ui.web.browser_ws_player import BrowserWSPlayer

        loop = asyncio.new_event_loop()
        try:
            player = BrowserWSPlayer(ws=MagicMock(), sentence_index=0, loop=loop)
            t0 = _time.monotonic()
            for _ in range(10):
                player.write(np.zeros(32000, dtype=np.float32))  # 10 s of audio
            t1 = _time.monotonic()
            assert (t1 - t0) < 0.1, f"write must be non-blocking, took {(t1-t0):.3f}s"
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Task 2: /api/tts/stream endpoint + session routing
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from unittest.mock import patch


@pytest.fixture
def mock_jarvis_app():
    app_mock = MagicMock()
    app_mock.handle_text = MagicMock(return_value="")
    app_mock.speech_recognizer = MagicMock()
    app_mock._get_tts = MagicMock(return_value=None)
    app_mock.event_bus = MagicMock()
    app_mock.event_bus.on = MagicMock()
    app_mock.config = {
        "tts": {
            "browser_streaming": True,
            "minimax_base_url": "https://api-uw.minimax.io",
            "minimax_key": "sk-test",
            "minimax_model": "speech-2.8-turbo",
            "minimax_voice": "voice",
            "minimax_volume": 1,
        },
    }
    return app_mock


@pytest.fixture
def web_client(mock_jarvis_app):
    with patch("ui.web.server.create_jarvis_app", return_value=mock_jarvis_app):
        from ui.web.server import create_app
        yield TestClient(create_app(mock_jarvis_app))


class TestTTSStreamEndpoint:
    def test_ws_rejects_unknown_session(self, web_client):
        with pytest.raises(Exception):
            with web_client.websocket_connect("/api/tts/stream?session_id=nope"):
                pass  # should close immediately with 1008

    def test_ws_accepts_known_session(self, web_client):
        sid = web_client.post("/api/session").json()["session_id"]
        with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}") as ws:
            # If accept() ran, we can assert the route dict contains sid.
            from ui.web import server as srv
            assert sid in srv._ws_routes

    def test_ws_last_writer_wins(self, web_client):
        """Second WS for the same session supersedes the first.
        The first should see a close with code 1001."""
        sid = web_client.post("/api/session").json()["session_id"]
        with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}") as ws1:
            from ui.web import server as srv
            assert srv._ws_routes.get(sid) is not None
            first_ws = srv._ws_routes[sid]
            with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}") as ws2:
                assert srv._ws_routes.get(sid) is not None
                assert srv._ws_routes[sid] is not first_ws

    def test_ws_cleanup_on_disconnect(self, web_client):
        sid = web_client.post("/api/session").json()["session_id"]
        with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}"):
            from ui.web import server as srv
            assert sid in srv._ws_routes
        # After context exit, client closed the WS — server should have removed the entry.
        import time; time.sleep(0.05)  # let the finally block run
        from ui.web import server as srv
        assert sid not in srv._ws_routes


# ---------------------------------------------------------------------------
# Task 4: X-Turn-Id response header
# ---------------------------------------------------------------------------

class TestTurnIdCorrelation:
    def test_chat_response_carries_turn_id(self, web_client):
        """POST /api/chat response must include an X-Turn-Id header matching
        the 32-char uuid4 hex used by upcoming WS turn_start + SSE events."""
        sid = web_client.post("/api/session").json()["session_id"]
        resp = web_client.post(
            "/api/chat",
            json={"text": "hello", "session_id": sid},
        )
        assert resp.status_code == 200
        tid = resp.headers.get("X-Turn-Id")
        assert tid is not None
        assert len(tid) == 32 and all(c in "0123456789abcdef" for c in tid)


# ---------------------------------------------------------------------------
# Task 5: new-chat cancel + _active_chats state
# ---------------------------------------------------------------------------

class TestNewChatCancel:
    """A POST /api/chat cancels any prior active turn on the same session."""

    def test_active_chat_state_triggers_cancel_frame(self, web_client):
        """Seed _active_chats with a fake prior turn; send a new chat;
        verify cancel{reason:new_chat} appears on the WS and the prior
        abort_event is set."""
        import threading as _t
        import json as _j
        import ui.web.server as srv

        sid = web_client.post("/api/session").json()["session_id"]
        with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}") as ws:
            prior_abort = _t.Event()
            prior_turn_id = "a" * 32
            srv._active_chats[sid] = {
                "turn_id": prior_turn_id,
                "abort_event": prior_abort,
            }

            resp = web_client.post(
                "/api/chat", json={"text": "two", "session_id": sid},
            )
            new_turn = resp.headers["X-Turn-Id"]
            assert new_turn != prior_turn_id

            # The first text frame on the WS should be the cancel for the
            # prior turn (server sends it before turn_start of the new one).
            data = ws.receive_text()
            payload = _j.loads(data)
            assert payload["type"] == "cancel"
            assert payload["turn_id"] == prior_turn_id
            assert payload["reason"] == "new_chat"
            assert prior_abort.is_set()


class TestVADCancel:
    """EventBus emit → server fans out cancel frame on every active chat's WS."""

    def test_tts_cancelled_event_sends_cancel_frame(self, web_client, mock_jarvis_app):
        """When jarvis_app.event_bus fires 'jarvis.tts_cancelled', the server
        emits a cancel{reason:'vad'} frame on every live ws_route with an
        active chat."""
        import ui.web.server as srv
        import threading as _t

        # Server subscribed to jarvis.tts_cancelled during create_app.
        bus = mock_jarvis_app.event_bus
        assert bus.on.called, "server did not subscribe to event_bus"
        sub_call = bus.on.call_args_list[0]
        assert sub_call.args[0] == "jarvis.tts_cancelled"
        cb = sub_call.args[1]

        sid = web_client.post("/api/session").json()["session_id"]
        with web_client.websocket_connect(f"/api/tts/stream?session_id={sid}") as ws:
            # Fake an active chat.
            srv._active_chats[sid] = {"turn_id": "abc123", "abort_event": _t.Event()}
            # Fire the event.
            cb({"reason": "vad"})
            # Read the next frame from the browser WS — should be a cancel.
            data = ws.receive_text()
            import json as _j
            payload = _j.loads(data)
            assert payload["type"] == "cancel"
            assert payload["turn_id"] == "abc123"
            assert payload["reason"] == "vad"
            assert srv._active_chats[sid]["abort_event"].is_set()


# ---------------------------------------------------------------------------
# Step 2 (heard_response): _compute_played_texts WP5 reconstruction
# ---------------------------------------------------------------------------


class TestComputePlayedTexts:
    """Web-side mirror of TTSPipeline.abort's WP5 logic."""

    @staticmethod
    def _mk_result(completed: bool, played: int, start: int = 0,
                   total: int | None = None, subtitle: str | None = None):
        from core.tts import PlaybackResult
        return PlaybackResult(
            completed=completed,
            played_samples=played,
            sentence_start_samples=start,
            total_samples=total,
            subtitle_url=subtitle,
        )

    def test_no_abort_returns_empty(self):
        """Normal completion shouldn't produce heard_response — LLM history
        already contains the full turn."""
        import threading as _t
        from ui.web.server import _compute_played_texts
        entries = [{"idx": 0, "text": "hello", "result": self._mk_result(True, 32000, 0, 32000)}]
        ev = _t.Event()  # not set
        assert _compute_played_texts(entries, ev, 32000) == []

    def test_abort_partial_sentence_truncates(self):
        """Sentence 1 fully played, sentence 2 half-played → full text1 +
        half text2 (WP5 L2 by ratio, snapped to punctuation)."""
        import threading as _t
        from ui.web.server import _compute_played_texts
        # Sentence 0: done. 32000 samples total, played 32000 (cursor).
        r0 = self._mk_result(True, 32000, 0, 32000)
        # Sentence 1: started at 32000, total would be 32000, played 48000
        # (i.e., 16000 into this sentence = 50%).
        r1 = self._mk_result(False, 48000, 32000, 32000)
        entries = [
            {"idx": 0, "text": "今天天气真好，我们出去散步吧。", "result": r0},
            {"idx": 1, "text": "先去公园，然后吃饭，再看电影。", "result": r1},
        ]
        ev = _t.Event()
        ev.set()
        out = _compute_played_texts(entries, ev, 32000)
        assert len(out) == 2
        assert out[0] == "今天天气真好，我们出去散步吧。"
        # Half of ~15-char sentence 1 should snap to first comma.
        assert out[1].startswith("先去公园")
        assert len(out[1]) < len(entries[1]["text"])

    def test_abort_before_any_playback(self):
        """abort_before_feed path: result is None; nothing to record."""
        import threading as _t
        from ui.web.server import _compute_played_texts
        entries = [
            {"idx": 0, "text": "s1", "result": None},
            {"idx": 1, "text": "s2", "result": None},
        ]
        ev = _t.Event()
        ev.set()
        assert _compute_played_texts(entries, ev, 32000) == []

    def test_abort_stops_at_first_partial(self):
        """Under tts_lock serialization only one sentence can be mid-play;
        any later non-None results are queue-raced and should be ignored."""
        import threading as _t
        from ui.web.server import _compute_played_texts
        r0 = self._mk_result(True, 32000, 0, 32000)
        r1 = self._mk_result(False, 40000, 32000, 32000)  # partial
        r2 = self._mk_result(True, 0, 0, 0)  # shouldn't be reached
        entries = [
            {"idx": 0, "text": "第一句话完成了。", "result": r0},
            {"idx": 1, "text": "第二句被打断了吧。", "result": r1},
            {"idx": 2, "text": "第三句不应出现。", "result": r2},
        ]
        ev = _t.Event()
        ev.set()
        out = _compute_played_texts(entries, ev, 32000)
        assert len(out) == 2
        assert out[0] == "第一句话完成了。"
        assert "第三句" not in " ".join(out)


# ---------------------------------------------------------------------------
# Step 3 (heard_response): _apply_heard_response → conversation_store
# ---------------------------------------------------------------------------


class TestApplyHeardResponse:
    def test_replaces_last_assistant_with_heard(self):
        """Fetches history, delegates to _truncate_assistant_for_interrupt,
        writes back via conversation_store.replace."""
        from ui.web.server import _apply_heard_response

        history = [
            {"role": "user", "content": "给我讲个长故事"},
            {"role": "assistant", "content": "从前有个国王。他住在城堡里。有一天..."},
        ]
        app = MagicMock()
        app.conversation_store.get_history.return_value = list(history)

        truncated = [
            {"role": "user", "content": "给我讲个长故事"},
            {"role": "assistant", "content": "从前有个国王。..."},
            {"role": "user", "content": "[Interrupted by user]"},
        ]
        app._truncate_assistant_for_interrupt = MagicMock(return_value=truncated)

        ok = _apply_heard_response(app, "sid1", ["从前有个国王。"])
        assert ok is True
        app._truncate_assistant_for_interrupt.assert_called_once_with(
            history, ["从前有个国王。"],
        )
        app.conversation_store.replace.assert_called_once_with("sid1", truncated)

    def test_empty_history_skips_writeback(self):
        """Nothing to truncate — no replace call. Prevents phantom interrupt
        markers on sessions that never produced an assistant message."""
        from ui.web.server import _apply_heard_response
        app = MagicMock()
        app.conversation_store.get_history.return_value = []
        assert _apply_heard_response(app, "sid1", ["hi"]) is False
        app._truncate_assistant_for_interrupt.assert_not_called()
        app.conversation_store.replace.assert_not_called()

    def test_store_failure_is_swallowed(self):
        """Turn teardown runs in a finally; a broken conversation_store
        must not propagate out and crash _run."""
        from ui.web.server import _apply_heard_response
        app = MagicMock()
        app.conversation_store.get_history.side_effect = RuntimeError("db down")
        assert _apply_heard_response(app, "sid1", ["hi"]) is False
