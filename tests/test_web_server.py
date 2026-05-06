"""Tests for the Live2D web server API."""
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_jarvis_app():
    app = MagicMock()
    app.handle_text = MagicMock(return_value="你好呀")
    app.speech_recognizer = MagicMock()
    app._get_tts = MagicMock()
    return app


@pytest.fixture
def client(mock_jarvis_app):
    with patch("ui.web.server.create_jarvis_app", return_value=mock_jarvis_app):
        from ui.web.server import create_app
        app = create_app(mock_jarvis_app)
        yield TestClient(app)


class TestHealthEndpoint:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestSessionEndpoints:
    def test_create_session(self, client):
        resp = client.post("/api/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["status"] == "connected"

    def test_delete_session(self, client):
        create_resp = client.post("/api/session")
        sid = create_resp.json()["session_id"]
        resp = client.delete(f"/api/session/{sid}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "disconnected"

    def test_delete_nonexistent_session(self, client):
        resp = client.delete("/api/session/nonexistent")
        assert resp.status_code == 404


class TestChatEndpoint:
    def test_chat_requires_session(self, client):
        resp = client.post("/api/chat", json={"text": "hi", "session_id": "bad"})
        assert resp.status_code == 404

    def test_chat_returns_sse(self, client, mock_jarvis_app):
        sid = client.post("/api/session").json()["session_id"]

        def fake_handle(text, session_id, on_sentence=None, emotion=""):
            if on_sentence:
                on_sentence("你好呀", emotion="happy")
            return "你好呀"
        mock_jarvis_app.handle_text = fake_handle

        tts = MagicMock()
        tts.synth_to_file = MagicMock(return_value="/tmp/test.mp3")
        mock_jarvis_app._get_tts = MagicMock(return_value=tts)

        resp = client.post(
            "/api/chat",
            json={"text": "你好", "session_id": sid},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "event: sentence" in body
        assert "event: done" in body


class TestInherentWsBridge:
    """1b: jarvis response.{start,chunk,final} → /inherent/ws siri:* JSON.

    Uses a real EventBus (not MagicMock) so subscribe + emit actually fire
    handlers. Module-level _inherent_clients is cleared per test to avoid
    leaking stale ws entries across runs.
    """

    def test_response_events_forwarded_to_card(self):
        import json as _json
        import ui.web.server as _server_module
        from core.event_bus import EventBus
        from ui.web.server import create_app

        bus = EventBus()
        jarvis_app = MagicMock()
        jarvis_app.event_bus = bus
        _server_module._inherent_clients.clear()

        app = create_app(jarvis_app)
        client = TestClient(app)

        with client.websocket_connect("/inherent/ws") as ws:
            # response.start → siri:open with streaming flag
            bus.emit("response.start", {"path": "cloud"})
            assert _json.loads(ws.receive_text()) == {
                "op": "open",
                "payload": {"content": "", "streaming": True, "kind": "text"},
            }

            # response.chunk → siri:append per chunk
            bus.emit("response.chunk", {"text": "你好"})
            assert _json.loads(ws.receive_text()) == {
                "op": "append", "payload": {"token": "你好"},
            }
            bus.emit("response.chunk", {"text": "世界"})
            assert _json.loads(ws.receive_text()) == {
                "op": "append", "payload": {"token": "世界"},
            }

            # Empty / missing text suppressed (no ws send) — verified by
            # sending a real chunk after and confirming it's the next message
            bus.emit("response.chunk", {"text": ""})
            bus.emit("response.chunk", None)
            bus.emit("response.chunk", {"text": "尾"})
            assert _json.loads(ws.receive_text()) == {
                "op": "append", "payload": {"token": "尾"},
            }

            # response.final → siri:done with default fadeMs
            bus.emit("response.final", {"text": "你好世界尾", "path": "cloud"})
            assert _json.loads(ws.receive_text()) == {
                "op": "done", "payload": {"fadeMs": 5000},
            }

    def test_no_ws_clients_no_crash(self):
        """Emit before any client connects → broadcast is a no-op (loop
        not yet captured), no exception raised."""
        import ui.web.server as _server_module
        from core.event_bus import EventBus
        from ui.web.server import create_app

        bus = EventBus()
        jarvis_app = MagicMock()
        jarvis_app.event_bus = bus
        _server_module._inherent_clients.clear()

        create_app(jarvis_app)
        # No ws connect → _web_loop never captured. Emit must not raise.
        bus.emit("response.start", {"path": "cloud"})
        bus.emit("response.chunk", {"text": "x"})
        bus.emit("response.final", {"text": "x", "path": "cloud"})
