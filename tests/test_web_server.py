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
