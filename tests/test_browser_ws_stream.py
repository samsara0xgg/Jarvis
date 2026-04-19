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
            player = BrowserWSPlayer(ws=ws, sentence_index=3, loop=loop)
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

    def test_played_samples_monotonic(self):
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            ws = MagicMock()
            player = BrowserWSPlayer(ws=ws, sentence_index=0, loop=loop)
            assert player.played_samples == 0
            player.write(np.zeros(100, dtype=np.float32))
            assert player.played_samples == 100
            player.write(np.zeros(50, dtype=np.float32))
            assert player.played_samples == 150
        finally:
            loop.close()

    def test_drain_returns_true(self):
        """D11: drain() must return True so PlaybackResult.completed=True
        and WP5 records this sentence in played_texts."""
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            player = BrowserWSPlayer(ws=MagicMock(), sentence_index=0, loop=loop)
            assert player.drain(5.0) is True
            assert player.drain() is True  # default timeout
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
