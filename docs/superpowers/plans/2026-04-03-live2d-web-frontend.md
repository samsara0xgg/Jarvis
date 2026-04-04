# Live2D Web 前端集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Integrate the Live2D virtual character chat frontend with Jarvis backend via HTTP API + SSE, enabling text chat, voice input, TTS playback with lip-sync, and skill execution.

**Architecture:** FastAPI server wraps JarvisApp, exposing REST + SSE endpoints. Frontend replaces WebSocket/Opus with HTTP fetch + EventSource + Web Audio `<audio>` playback. JarvisApp gets a new `handle_text()` method that reuses the full pipeline (memory → routing → skills → LLM → conversation save) without audio/voiceprint steps.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, soundfile. Frontend: vanilla ES Modules, Web Audio API, PixiJS + Cubism 4.0 SDK.

**Worktree:** `~/.config/superpowers/worktrees/jarvis/live2d-web` (branch `feature/live2d-web`)

---

## File Structure

### New files
- `ui/web/server.py` — FastAPI app, SSE streaming, audio cache management
- `ui/web/__init__.py` — package marker
- `tests/test_web_server.py` — backend API tests
- `ui/web/js/core/api-client.js` — HTTP + SSE client (replaces websocket.js)

### Modified files
- `jarvis.py` — add `handle_text()` method (~lines 486-771 refactor)
- `ui/web/index.html` — rebrand, remove MCP/Opus/OTA
- `ui/web/js/app.js` — remove Opus/MCP init, import api-client
- `ui/web/js/ui/controller.js` — dial→session, remove MCP/OTA logic
- `ui/web/js/config/manager.js` — jarvis_ prefix, server URL
- `ui/web/js/core/audio/player.js` — URL queue player with AnalyserNode
- `ui/web/js/core/audio/recorder.js` — AudioWorklet → WAV blob → POST
- `ui/web/js/core/audio/stream-context.js` — simplified analyser bridge
- `ui/web/js/live2d/live2d.js` — connect analyser to new player
- `ui/web/css/test_page.css` — remove MCP styles

### Deleted files
- `ui/web/js/core/network/ota-connector.js`
- `ui/web/js/core/network/websocket.js`
- `ui/web/js/core/audio/opus-codec.js`
- `ui/web/js/core/mcp/tools.js`
- `ui/web/js/config/default-mcp-tools.json`
- `ui/web/js/utils/libopus.js`
- `ui/web/js/utils/blocking-queue.js`

<!-- END_HEADER -->

## Task 1: Install dependencies and add `handle_text()` to JarvisApp

**Files:**
- Modify: `jarvis.py:486-771`
- Test: `tests/test_jarvis.py`

- [ ] **Step 1: Install Python dependencies**

```bash
cd ~/.config/superpowers/worktrees/jarvis/live2d-web
pip install fastapi uvicorn python-multipart soundfile
```

- [ ] **Step 2: Write failing test for handle_text()**

Add to `tests/test_jarvis.py`:

```python
class TestHandleText:
    """Tests for JarvisApp.handle_text() — text-only pipeline."""

    def test_handle_text_returns_string(self, jarvis_app):
        sentences = []
        result = jarvis_app.handle_text(
            "你好", session_id="_test",
            on_sentence=lambda s, **kw: sentences.append(s),
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_handle_text_calls_on_sentence(self, jarvis_app):
        sentences = []
        jarvis_app.handle_text(
            "你好", session_id="_test",
            on_sentence=lambda s, **kw: sentences.append(s),
        )
        assert len(sentences) >= 1

    def test_handle_text_no_callback(self, jarvis_app):
        result = jarvis_app.handle_text("你好", session_id="_test")
        assert isinstance(result, str)
```

Run: `python -m pytest tests/test_jarvis.py::TestHandleText -v`
Expected: FAIL — `handle_text` not defined

- [ ] **Step 3: Implement handle_text() in jarvis.py**

Add after `_handle_utterance_inner()` (around line 771). Extract steps 4-9 from `_handle_utterance_inner`, skipping audio/ASR/voiceprint (steps 0-3). Key signature:

```python
def handle_text(self, text: str, session_id: str = "_web",
                on_sentence: Any = None, emotion: str = "") -> str:
```

The method must:
1. Load conversation history via `self.conversation_store.get_history(session_id)`
2. Try `self.direct_answerer.try_answer()` (Level 1)
3. Check `_REMEMBER_KEYWORDS` shortcut
4. Check `self.learning_router.detect()` 
5. Run `self.rule_manager.check_keyword()` + `self.intent_router.route()`
6. Fall back to `self.llm.chat_stream(on_sentence=_on_sentence)`
7. Save via `self.conversation_store.replace()`
8. For non-streamed local responses, call `on_sentence(response_text, emotion=emotion)` once at the end
9. Use `user_id="default_user"`, `user_role="owner"` (no voiceprint)

The `on_sentence` callback signature is `fn(sentence: str, emotion: str = "")`.

See `_handle_utterance_inner()` lines 524-771 for the exact logic to replicate. Do NOT call `self.speak()` or `self._speak_nonblocking()` — the web server handles TTS separately.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_jarvis.py::TestHandleText -v
python -m pytest tests/ -q
```

Expected: TestHandleText passes, no new failures beyond pre-existing 3.

- [ ] **Step 5: Commit**

```bash
git add jarvis.py tests/test_jarvis.py
git commit -m "feat: add handle_text() method for web frontend text pipeline"
```

---

## Task 2: FastAPI server with health, session, and chat SSE endpoints

**Files:**
- Create: `ui/web/__init__.py`
- Create: `ui/web/server.py`
- Create: `tests/test_web_server.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_web_server.py`:

```python
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
        # Create session first
        sid = client.post("/api/session").json()["session_id"]

        # Mock handle_text to call on_sentence
        def fake_handle(text, session_id, on_sentence=None, emotion=""):
            if on_sentence:
                on_sentence("你好呀", emotion="happy")
            return "你好呀"
        mock_jarvis_app.handle_text = fake_handle

        # Mock TTS
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
```

Run: `python -m pytest tests/test_web_server.py -v`
Expected: FAIL — `ui.web.server` not found

- [ ] **Step 2: Create ui/web/__init__.py**

```python
```

(Empty file — package marker.)

- [ ] **Step 3: Implement ui/web/server.py**

```python
"""Jarvis Live2D Web Server — FastAPI + SSE."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    text: str
    session_id: str

class SessionResponse(BaseModel):
    session_id: str
    status: str

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

_tts_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tts-web")
AUDIO_DIR = Path(__file__).parent / "audio_cache"


def create_app(jarvis_app: Any) -> FastAPI:
    """Create FastAPI app wrapping a JarvisApp instance."""
    app = FastAPI(title="Jarvis Live2D Web")
    sessions: dict[str, dict] = {}

    # Ensure audio cache dir
    AUDIO_DIR.mkdir(exist_ok=True)

    # --- Health ---
    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    # --- Session ---
    @app.post("/api/session", response_model=SessionResponse)
    def create_session():
        sid = str(uuid.uuid4())
        sessions[sid] = {"active": True}
        LOGGER.info("Session created: %s", sid)
        return SessionResponse(session_id=sid, status="connected")

    @app.delete("/api/session/{session_id}", response_model=SessionResponse)
    def delete_session(session_id: str):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
        del sessions[session_id]
        LOGGER.info("Session deleted: %s", session_id)
        return SessionResponse(session_id=session_id, status="disconnected")

    # --- Chat SSE ---
    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if req.session_id not in sessions:
            raise HTTPException(404, "Session not found — dial first")

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        tts = jarvis_app._get_tts()

        sentence_index = 0

        def on_sentence(sentence: str, emotion: str = "") -> None:
            nonlocal sentence_index
            idx = sentence_index
            sentence_index += 1

            # TTS synthesis (blocking, runs in LLM callback thread)
            audio_url = ""
            if tts:
                try:
                    audio_path = tts.synth_to_file(sentence, emotion)
                    if audio_path:
                        audio_name = f"{uuid.uuid4().hex}.mp3"
                        dest = AUDIO_DIR / audio_name
                        shutil.copy2(audio_path, dest)
                        Path(audio_path).unlink(missing_ok=True)
                        audio_url = f"/api/audio/{audio_name}"
                except Exception as exc:
                    LOGGER.warning("TTS synth failed: %s", exc)

            event = {
                "index": idx,
                "text": sentence,
                "emotion": emotion.lower() if emotion else "neutral",
                "audio_url": audio_url,
            }
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        # Run handle_text in thread (it's synchronous)
        def _run():
            try:
                jarvis_app.handle_text(
                    req.text,
                    session_id=req.session_id,
                    on_sentence=on_sentence,
                )
            except Exception as exc:
                LOGGER.error("handle_text failed: %s", exc)
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop)

        loop.run_in_executor(None, _run)

        async def event_stream():
            while True:
                event = await queue.get()
                if event is None:
                    yield f"event: done\ndata: {{}}\n\n"
                    break
                yield f"event: sentence\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Audio files ---
    @app.get("/api/audio/{filename}")
    def get_audio(filename: str):
        path = AUDIO_DIR / filename
        if not path.exists():
            raise HTTPException(404, "Audio file not found")
        return FileResponse(path, media_type="audio/mpeg")

    # --- Static files (frontend) ---
    web_dir = Path(__file__).parent
    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

    return app


def create_jarvis_app(config_path: str = "config.yaml") -> Any:
    """Load config and create JarvisApp."""
    import yaml
    from jarvis import JarvisApp
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return JarvisApp(config, config_path=config_path)


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="Jarvis Live2D Web Server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    # Clean audio cache on startup
    if AUDIO_DIR.exists():
        shutil.rmtree(AUDIO_DIR)
    AUDIO_DIR.mkdir(exist_ok=True)

    jarvis = create_jarvis_app(args.config)
    app = create_app(jarvis)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_web_server.py -v
python -m pytest tests/ -q
```

Expected: All test_web_server tests pass, no new failures.

- [ ] **Step 5: Commit**

```bash
git add ui/web/__init__.py ui/web/server.py tests/test_web_server.py
git commit -m "feat: add FastAPI web server with health, session, chat SSE endpoints"
```

---

## Task 3: ASR endpoint

**Files:**
- Modify: `ui/web/server.py`
- Modify: `tests/test_web_server.py`

- [ ] **Step 1: Add ASR test**

Append to `tests/test_web_server.py`:

```python
import io
import wave
import numpy as np

class TestASREndpoint:
    def test_asr_requires_session(self, client):
        wav = _make_wav_bytes()
        resp = client.post(
            "/api/asr",
            data={"session_id": "bad"},
            files={"audio": ("test.wav", wav, "audio/wav")},
        )
        assert resp.status_code == 404

    def test_asr_returns_text(self, client, mock_jarvis_app):
        from core.speech_recognizer import TranscriptionResult
        mock_jarvis_app.speech_recognizer.transcribe.return_value = TranscriptionResult(
            text="测试语音", language="zh", confidence=0.9, emotion="NEUTRAL",
        )
        sid = client.post("/api/session").json()["session_id"]
        wav = _make_wav_bytes()
        resp = client.post(
            "/api/asr",
            data={"session_id": sid},
            files={"audio": ("test.wav", wav, "audio/wav")},
        )
        assert resp.status_code == 200
        assert resp.json()["text"] == "测试语音"

def _make_wav_bytes(duration=0.5, sr=16000):
    samples = np.zeros(int(sr * duration), dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(samples.tobytes())
    buf.seek(0)
    return buf.read()
```

Run: `python -m pytest tests/test_web_server.py::TestASREndpoint -v`
Expected: FAIL — no `/api/asr` endpoint

- [ ] **Step 2: Add ASR endpoint to server.py**

Add inside `create_app()`, after the chat endpoint:

```python
    @app.post("/api/asr")
    async def asr(session_id: str = Form(...), audio: UploadFile = File(...)):
        if session_id not in sessions:
            raise HTTPException(404, "Session not found")
        import soundfile as sf
        import numpy as np
        audio_bytes = await audio.read()
        data, sr = sf.read(io.BytesIO(audio_bytes), dtype="float32")
        if sr != 16000:
            LOGGER.warning("ASR received %dHz audio, expected 16000Hz", sr)
        result = jarvis_app.speech_recognizer.transcribe(data)
        return {
            "text": result.text,
            "emotion": getattr(result, "emotion", "") or "",
        }
```

Add imports at top of server.py: `import io` and `from fastapi import Form`.

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/test_web_server.py -v
python -m pytest tests/ -q
```

- [ ] **Step 4: Commit**

```bash
git add ui/web/server.py tests/test_web_server.py
git commit -m "feat: add /api/asr endpoint for browser voice input"
```

---

## Task 4: Frontend cleanup — delete unused files, rebrand HTML

**Files:**
- Delete: 7 files (ota-connector, websocket, opus-codec, mcp/tools, default-mcp-tools.json, libopus, blocking-queue)
- Modify: `ui/web/index.html`
- Modify: `ui/web/css/test_page.css`

- [ ] **Step 1: Delete unused files**

```bash
cd ~/.config/superpowers/worktrees/jarvis/live2d-web
rm ui/web/js/core/network/ota-connector.js
rm ui/web/js/core/network/websocket.js
rm ui/web/js/core/audio/opus-codec.js
rm ui/web/js/core/mcp/tools.js
rm ui/web/js/config/default-mcp-tools.json
rm ui/web/js/utils/libopus.js
rm ui/web/js/utils/blocking-queue.js
```

- [ ] **Step 2: Edit index.html**

Changes to make in `ui/web/index.html`:

1. Line 7: Title `小智服务器测试页面` → `小贾 | Jarvis`
2. Lines 21-32: Update file:// warning text — change `xiaozhi-server/test` to `jarvis` directory, change URL to `http://localhost:8006/`
3. Lines 48-59: Delete entire camera container div (`<div class="camera-container" ...>...</div>`)
4. Lines 104-110: Delete camera button from control bar
5. Lines 139-140: Replace settings tabs — remove MCP tab, rename "设备配置" to "连接配置":
   ```html
   <button class="tab-btn active" data-tab="device">连接配置</button>
   <button class="tab-btn" data-tab="other">数字人皮肤</button>
   ```
6. Lines 144-185: Replace deviceTab content — remove MAC/clientId/deviceName/OTA/WS/vision fields, replace with single server URL field:
   ```html
   <div class="tab-content active" id="deviceTab">
       <div class="connection-controls">
           <div class="input-group">
               <label for="serverUrl">Jarvis 服务器地址:</label>
               <input type="text" id="serverUrl" value="http://localhost:8006"
                   placeholder="http://localhost:8006" />
           </div>
       </div>
   </div>
   ```
7. Lines 187-204: Delete entire mcpTab div
8. Lines 242-345: Delete both MCP modal divs (`#mcpToolModal` and `#mcpPropertyModal`)
9. Lines 360-361: Delete Opus script tag: `<script src="js/utils/libopus.js?v=0205"></script>`

- [ ] **Step 3: Remove MCP-related CSS from test_page.css**

Search for and delete all CSS blocks containing `mcp` in selectors (`.mcp-tools-container`, `.mcp-tools-header`, `.mcp-tools-panel`, `.mcp-tools-list`, `.mcp-actions`, `.mcp-empty-state`, `.mcp-properties-list`, `.mcp-checkbox-label`, `#mcpToolModal`, `#mcpPropertyModal`, `.property-modal`). Also delete `.camera-container` and `.camera-switch` related styles.

- [ ] **Step 4: Verify HTML loads without JS errors**

```bash
cd ~/.config/superpowers/worktrees/jarvis/live2d-web
python -c "from ui.web.server import create_app; print('server imports ok')"
```

- [ ] **Step 5: Commit**

```bash
git add -A ui/web/
git commit -m "refactor: remove OTA/MCP/Opus/camera, rebrand to Jarvis"
```

---

## Task 5: Frontend API client (replaces WebSocket handler)

**Files:**
- Create: `ui/web/js/core/api-client.js`

- [ ] **Step 1: Create api-client.js**

```javascript
// Jarvis HTTP + SSE API client (replaces WebSocket handler)
import { log } from '../utils/logger.js';

class ApiClient {
    constructor() {
        this.serverUrl = '';
        this.sessionId = null;
        this.connected = false;
        // Callbacks
        this.onConnectionStateChange = null;
        this.onChatMessage = null;
        this.onSentence = null; // fn(sentence: {index, text, emotion, audio_url})
        this.onSessionStateChange = null;
    }

    setServerUrl(url) {
        this.serverUrl = url.replace(/\/+$/, '');
    }

    async checkHealth() {
        try {
            const resp = await fetch(`${this.serverUrl}/api/health`);
            return resp.ok;
        } catch {
            return false;
        }
    }

    async connect() {
        const healthy = await this.checkHealth();
        if (!healthy) {
            log('服务器不可达', 'error');
            return false;
        }
        try {
            const resp = await fetch(`${this.serverUrl}/api/session`, { method: 'POST' });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            this.sessionId = data.session_id;
            this.connected = true;
            log(`会话已建立: ${this.sessionId}`, 'success');
            if (this.onConnectionStateChange) this.onConnectionStateChange(true);
            return true;
        } catch (err) {
            log(`连接失败: ${err.message}`, 'error');
            if (this.onConnectionStateChange) this.onConnectionStateChange(false);
            return false;
        }
    }

    async disconnect() {
        if (!this.sessionId) return;
        try {
            await fetch(`${this.serverUrl}/api/session/${this.sessionId}`, { method: 'DELETE' });
        } catch { /* ignore */ }
        this.sessionId = null;
        this.connected = false;
        log('会话已断开', 'info');
        if (this.onConnectionStateChange) this.onConnectionStateChange(false);
    }

    async sendTextMessage(text) {
        if (!this.connected || !this.sessionId) return false;
        if (!text.trim()) return false;

        if (this.onSessionStateChange) this.onSessionStateChange(true);

        try {
            const resp = await fetch(`${this.serverUrl}/api/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ text, session_id: this.sessionId }),
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });

                // Parse SSE events from buffer
                const lines = buffer.split('\n');
                buffer = lines.pop(); // keep incomplete line

                let eventType = '';
                let eventData = '';
                for (const line of lines) {
                    if (line.startsWith('event: ')) {
                        eventType = line.slice(7).trim();
                    } else if (line.startsWith('data: ')) {
                        eventData = line.slice(6);
                    } else if (line === '' && eventType) {
                        // End of event
                        if (eventType === 'sentence') {
                            try {
                                const parsed = JSON.parse(eventData);
                                if (this.onSentence) this.onSentence(parsed);
                                if (this.onChatMessage) this.onChatMessage(parsed.text, false);
                            } catch (e) {
                                log(`SSE parse error: ${e.message}`, 'error');
                            }
                        } else if (eventType === 'done') {
                            // Stream complete
                        }
                        eventType = '';
                        eventData = '';
                    }
                }
            }
        } catch (err) {
            log(`聊天请求失败: ${err.message}`, 'error');
            if (this.onChatMessage) this.onChatMessage(`请求失败: ${err.message}`, false);
        } finally {
            if (this.onSessionStateChange) this.onSessionStateChange(false);
        }
        return true;
    }

    async sendAudio(wavBlob) {
        if (!this.connected || !this.sessionId) return null;
        try {
            const formData = new FormData();
            formData.append('audio', wavBlob, 'recording.wav');
            formData.append('session_id', this.sessionId);
            const resp = await fetch(`${this.serverUrl}/api/asr`, {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            return await resp.json();
        } catch (err) {
            log(`ASR 请求失败: ${err.message}`, 'error');
            return null;
        }
    }

    isConnected() {
        return this.connected && this.sessionId !== null;
    }
}

// Singleton
let instance = null;
export function getApiClient() {
    if (!instance) instance = new ApiClient();
    return instance;
}
```

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/core/api-client.js
git commit -m "feat: add HTTP+SSE api-client replacing WebSocket handler"
```

---

## Task 6: Frontend audio player (URL queue + AnalyserNode)

**Files:**
- Rewrite: `ui/web/js/core/audio/player.js`
- Rewrite: `ui/web/js/core/audio/stream-context.js`

- [ ] **Step 1: Rewrite player.js**

Replace entire file with:

```javascript
// Audio player — fetches MP3 URLs, decodes, plays in sequence via Web Audio API.
// Exposes AnalyserNode for Live2D lip-sync.
import { log } from '../../utils/logger.js';

export class AudioPlayer {
    constructor() {
        this.audioContext = null;
        this.analyser = null;
        this.gainNode = null;
        this._queue = [];       // [{url, resolve}]
        this._playing = false;
    }

    getAudioContext() {
        if (!this.audioContext) {
            this.audioContext = new (window.AudioContext || window.webkitAudioContext)();
        }
        return this.audioContext;
    }

    getAnalyser() {
        if (!this.analyser) {
            const ctx = this.getAudioContext();
            this.analyser = ctx.createAnalyser();
            this.analyser.fftSize = 256;
            this.gainNode = ctx.createGain();
            this.gainNode.connect(this.analyser);
            this.analyser.connect(ctx.destination);
        }
        return this.analyser;
    }

    /**
     * Enqueue an audio URL for playback. Returns a promise that resolves
     * when this particular clip finishes playing.
     */
    enqueue(url) {
        return new Promise((resolve) => {
            this._queue.push({ url, resolve });
            if (!this._playing) this._playNext();
        });
    }

    async _playNext() {
        if (this._queue.length === 0) {
            this._playing = false;
            return;
        }
        this._playing = true;
        const { url, resolve } = this._queue.shift();

        try {
            const ctx = this.getAudioContext();
            if (ctx.state === 'suspended') await ctx.resume();

            const resp = await fetch(url);
            if (!resp.ok) throw new Error(`Fetch ${url} failed: ${resp.status}`);
            const arrayBuf = await resp.arrayBuffer();
            const audioBuf = await ctx.decodeAudioData(arrayBuf);

            const source = ctx.createBufferSource();
            source.buffer = audioBuf;
            // Route through analyser for lip-sync
            this.getAnalyser();
            source.connect(this.gainNode);

            source.onended = () => {
                resolve();
                this._playNext();
            };
            source.start(0);
        } catch (err) {
            log(`音频播放失败: ${err.message}`, 'error');
            resolve();
            this._playNext();
        }
    }

    clearAll() {
        this._queue = [];
        // Current playing source will finish naturally
    }

    async start() {
        // Pre-init audio context (needs user gesture on some browsers)
        this.getAudioContext();
        this.getAnalyser();
        log('AudioPlayer 初始化完成', 'success');
    }
}

let instance = null;
export function getAudioPlayer() {
    if (!instance) instance = new AudioPlayer();
    return instance;
}
```

- [ ] **Step 2: Simplify stream-context.js**

Replace entire file with a thin bridge — the new player already has the AnalyserNode:

```javascript
// Simplified audio stream context — delegates to AudioPlayer's AnalyserNode.
import { getAudioPlayer } from './player.js';

export function getStreamAnalyser() {
    return getAudioPlayer().getAnalyser();
}
```

- [ ] **Step 3: Commit**

```bash
git add ui/web/js/core/audio/player.js ui/web/js/core/audio/stream-context.js
git commit -m "feat: rewrite audio player as URL queue with AnalyserNode for lip-sync"
```

---

## Task 7: Frontend recorder (AudioWorklet → WAV blob → POST)

**Files:**
- Rewrite: `ui/web/js/core/audio/recorder.js`

- [ ] **Step 1: Rewrite recorder.js**

Replace entire file with:

```javascript
// Audio recorder — AudioWorklet captures 16kHz mono PCM, builds WAV blob.
import { log } from '../../utils/logger.js';
import { getAudioPlayer } from './player.js';
import { getApiClient } from '../api-client.js';

class AudioRecorder {
    constructor() {
        this.isRecording = false;
        this.audioContext = null;
        this.workletNode = null;
        this.sourceNode = null;
        this.stream = null;
        this.pcmChunks = [];
        this.recordingTimer = null;
        // Callbacks
        this.onRecordingStart = null;
        this.onRecordingStop = null;
    }

    getAudioContext() {
        return getAudioPlayer().getAudioContext();
    }

    _workletCode() {
        return `
            class RecorderProcessor extends AudioWorkletProcessor {
                constructor() {
                    super();
                    this.recording = false;
                    this.port.onmessage = (e) => {
                        if (e.data.command === 'start') this.recording = true;
                        if (e.data.command === 'stop') this.recording = false;
                    };
                }
                process(inputs) {
                    if (!this.recording || !inputs[0][0]) return true;
                    const float32 = inputs[0][0];
                    const int16 = new Int16Array(float32.length);
                    for (let i = 0; i < float32.length; i++) {
                        int16[i] = Math.max(-32768, Math.min(32767, Math.floor(float32[i] * 32767)));
                    }
                    this.port.postMessage({ pcm: int16 }, [int16.buffer]);
                    return true;
                }
            }
            registerProcessor('jarvis-recorder', RecorderProcessor);
        `;
    }

    async start() {
        if (this.isRecording) return false;
        try {
            this.stream = await navigator.mediaDevices.getUserMedia({
                audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 }
            });
            this.audioContext = this.getAudioContext();
            if (this.audioContext.state === 'suspended') await this.audioContext.resume();

            const blob = new Blob([this._workletCode()], { type: 'application/javascript' });
            const url = URL.createObjectURL(blob);
            await this.audioContext.audioWorklet.addModule(url);
            URL.revokeObjectURL(url);

            this.workletNode = new AudioWorkletNode(this.audioContext, 'jarvis-recorder');
            this.sourceNode = this.audioContext.createMediaStreamSource(this.stream);
            this.sourceNode.connect(this.workletNode);
            // Sink to prevent GC
            const silent = this.audioContext.createGain();
            silent.gain.value = 0;
            this.workletNode.connect(silent);
            silent.connect(this.audioContext.destination);

            this.pcmChunks = [];
            this.workletNode.port.onmessage = (e) => {
                if (e.data.pcm) this.pcmChunks.push(e.data.pcm);
            };
            this.workletNode.port.postMessage({ command: 'start' });
            this.isRecording = true;

            let seconds = 0;
            if (this.onRecordingStart) this.onRecordingStart(0);
            this.recordingTimer = setInterval(() => {
                seconds += 0.1;
                if (this.onRecordingStart) this.onRecordingStart(seconds);
            }, 100);

            log('录音已开始', 'success');
            return true;
        } catch (err) {
            log(`录音启动失败: ${err.message}`, 'error');
            this.isRecording = false;
            return false;
        }
    }

    stop() {
        if (!this.isRecording) return null;
        this.isRecording = false;
        if (this.workletNode) {
            this.workletNode.port.postMessage({ command: 'stop' });
            this.workletNode.disconnect();
            this.workletNode = null;
        }
        if (this.sourceNode) {
            this.sourceNode.disconnect();
            this.sourceNode = null;
        }
        if (this.stream) {
            this.stream.getTracks().forEach(t => t.stop());
            this.stream = null;
        }
        if (this.recordingTimer) {
            clearInterval(this.recordingTimer);
            this.recordingTimer = null;
        }
        if (this.onRecordingStop) this.onRecordingStop();

        const wavBlob = this._buildWav();
        log(`录音已停止，WAV 大小: ${wavBlob.size} bytes`, 'success');

        // Auto-send to ASR then chat
        this._sendToASR(wavBlob);

        return wavBlob;
    }

    _buildWav() {
        // Merge all PCM chunks
        let totalLen = 0;
        for (const chunk of this.pcmChunks) totalLen += chunk.length;
        const merged = new Int16Array(totalLen);
        let offset = 0;
        for (const chunk of this.pcmChunks) {
            merged.set(chunk, offset);
            offset += chunk.length;
        }
        this.pcmChunks = [];

        // Build WAV header (16kHz, mono, 16-bit)
        const sr = 16000;
        const dataBytes = merged.length * 2;
        const buffer = new ArrayBuffer(44 + dataBytes);
        const view = new DataView(buffer);
        const writeStr = (off, str) => { for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i)); };
        writeStr(0, 'RIFF');
        view.setUint32(4, 36 + dataBytes, true);
        writeStr(8, 'WAVE');
        writeStr(12, 'fmt ');
        view.setUint32(16, 16, true);
        view.setUint16(20, 1, true);       // PCM
        view.setUint16(22, 1, true);       // mono
        view.setUint32(24, sr, true);
        view.setUint32(28, sr * 2, true);  // byte rate
        view.setUint16(32, 2, true);       // block align
        view.setUint16(34, 16, true);      // bits per sample
        writeStr(36, 'data');
        view.setUint32(40, dataBytes, true);
        const pcmView = new Int16Array(buffer, 44);
        pcmView.set(merged);

        return new Blob([buffer], { type: 'audio/wav' });
    }

    async _sendToASR(wavBlob) {
        const apiClient = getApiClient();
        const result = await apiClient.sendAudio(wavBlob);
        if (result && result.text) {
            log(`ASR 识别: ${result.text}`, 'info');
            // Show user message
            if (apiClient.onChatMessage) apiClient.onChatMessage(result.text, true);
            // Auto-send to chat
            await apiClient.sendTextMessage(result.text);
        } else {
            log('ASR 未识别到文字', 'warning');
        }
    }
}

let instance = null;
export function getAudioRecorder() {
    if (!instance) instance = new AudioRecorder();
    return instance;
}

export async function checkMicrophoneAvailability() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;
    try {
        const stream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, sampleRate: 16000, channelCount: 1 }
        });
        stream.getTracks().forEach(t => t.stop());
        return true;
    } catch { return false; }
}

export function isHttpNonLocalhost() {
    if (window.location.protocol !== 'http:') return false;
    const h = window.location.hostname;
    if (h === 'localhost' || h === '127.0.0.1') return false;
    if (h.startsWith('192.168.') || h.startsWith('10.') || h.startsWith('172.')) return false;
    return true;
}
```

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/core/audio/recorder.js
git commit -m "feat: rewrite recorder as AudioWorklet → WAV blob → POST /api/asr"
```

---

## Task 8: Config manager (jarvis_ prefix, server URL)

**Files:**
- Rewrite: `ui/web/js/config/manager.js`

- [ ] **Step 1: Rewrite manager.js**

Replace entire file:

```javascript
// Config manager — localStorage with jarvis_ prefix

export function loadConfig() {
    const serverUrlInput = document.getElementById('serverUrl');
    if (serverUrlInput) {
        const saved = localStorage.getItem('jarvis_serverUrl');
        if (saved) serverUrlInput.value = saved;
    }
}

export function saveConfig() {
    const serverUrlInput = document.getElementById('serverUrl');
    if (serverUrlInput) {
        localStorage.setItem('jarvis_serverUrl', serverUrlInput.value.trim());
    }
}

export function getServerUrl() {
    const input = document.getElementById('serverUrl');
    const url = input ? input.value.trim() : '';
    return url || localStorage.getItem('jarvis_serverUrl') || 'http://localhost:8006';
}
```

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/config/manager.js
git commit -m "refactor: simplify config manager with jarvis_ prefix"
```

---

## Task 9: Wire up controller.js (dial→session, remove MCP/OTA)

**Files:**
- Rewrite: `ui/web/js/ui/controller.js`

- [ ] **Step 1: Rewrite controller.js**

Key modifications:

1. Replace imports: `getWebSocketHandler` → `getApiClient` from `../core/api-client.js`, add `getServerUrl` from config
2. `handleConnect()`: call `apiClient.setServerUrl()` + `apiClient.connect()`
3. Dial button: `apiClient.connect()`/`disconnect()`, check `serverUrl` not OTA
4. Chat input: `apiClient.sendTextMessage()`
5. In `init()`, wire `apiClient.onSentence` to play audio + trigger Live2D emotion
6. Remove: all MCP methods, camera button logic, OTA references
7. Add helper methods: `startLive2DTalking()`, `stopLive2DTalking()`, `triggerLive2DEmotionAction()` (moved from websocket.js)

Import block:

```javascript
import { loadConfig, saveConfig, getServerUrl } from '../config/manager.js';
import { getAudioPlayer } from '../core/audio/player.js';
import { getAudioRecorder } from '../core/audio/recorder.js';
import { getApiClient } from '../core/api-client.js';
```

New `handleConnect()`:

```javascript
async handleConnect() {
    const apiClient = getApiClient();
    apiClient.setServerUrl(getServerUrl());
    this.addChatMessage('正在连接 Jarvis...', false);
    const ok = await apiClient.connect();
    if (ok) {
        this.updateDialButton(true);
        this.updateConnectionUI(true);
        this.addChatMessage('已连接，随时待命~', false);
    } else {
        this.addChatMessage('连接失败，请检查服务器地址', false);
        this.updateDialButton(false);
    }
}
```

In `init()`, after `loadConfig()`, wire callbacks:

```javascript
const apiClient = getApiClient();
apiClient.onChatMessage = (text, isUser) => this.addChatMessage(text, isUser);
apiClient.onSentence = async (sentence) => {
    if (sentence.audio_url) {
        const player = getAudioPlayer();
        const fullUrl = getServerUrl() + sentence.audio_url;
        if (sentence.index === 0) this.startLive2DTalking();
        await player.enqueue(fullUrl);
        if (player._queue.length === 0) this.stopLive2DTalking();
    }
    if (sentence.emotion && sentence.emotion !== 'neutral') {
        this.triggerLive2DEmotionAction(sentence.emotion);
    }
};
```

Dial button click handler:

```javascript
const dialBtn = document.getElementById('dialBtn');
if (dialBtn) {
    dialBtn.addEventListener('click', () => {
        dialBtn.disabled = true;
        setTimeout(() => { dialBtn.disabled = false; }, 2000);
        const apiClient = getApiClient();
        if (apiClient.isConnected()) {
            apiClient.disconnect();
            this.updateDialButton(false);
            this.updateConnectionUI(false);
            this.addChatMessage('已断开连接~', false);
        } else {
            if (!getServerUrl()) {
                this.showModal('settingsModal');
                this.switchTab('device');
                return;
            }
            this.handleConnect();
        }
    });
}
```

Chat input:

```javascript
const chatIpt = document.getElementById('chatIpt');
if (chatIpt) {
    chatIpt.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.target.value.trim()) {
            const text = e.target.value.trim();
            e.target.value = '';
            const apiClient = getApiClient();
            if (!apiClient.isConnected()) {
                this.addChatMessage('请先点击拨号连接', false);
                return;
            }
            this.addChatMessage(text, true);
            apiClient.sendTextMessage(text);
        }
    });
}
```

Live2D helpers (add as methods on UIController):

```javascript
startLive2DTalking() {
    const live2d = window.chatApp?.live2dManager;
    if (live2d && live2d.live2dModel) live2d.startTalking();
}
stopLive2DTalking() {
    const live2d = window.chatApp?.live2dManager;
    if (live2d) live2d.stopTalking();
}
triggerLive2DEmotionAction(emotion) {
    const live2d = window.chatApp?.live2dManager;
    if (live2d) live2d.triggerEmotionAction(emotion);
}
```

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/ui/controller.js
git commit -m "refactor: wire controller to API client, remove MCP/OTA/camera"
```

---

## Task 10: Update app.js (remove Opus/MCP, import api-client)

**Files:**
- Modify: `ui/web/js/app.js`

- [ ] **Step 1: Rewrite app.js**

Key changes:
1. Remove imports: `checkOpusLoaded`, `initOpusEncoder` from opus-codec, `initMcpTools` from mcp/tools
2. Remove from `init()`: `checkOpusLoaded()`, `initOpusEncoder()`, `initMcpTools()`
3. Remove: `checkCameraAvailability()`, `initCamera()`, camera-related code, `xz_tester_vision` references
4. Remove: `dataURItoBlob()` helper, `window.takePhoto`, `window.startCamera`, `window.stopCamera`, `window.switchCamera`
5. Keep: `initLive2D()`, `checkMicrophoneAvailability()`, `setModelLoadingStatus()`

Simplified `init()`:

```javascript
async init() {
    log('正在初始化应用...', 'info');
    this.uiController = uiController;
    this.uiController.init();
    this.audioPlayer = getAudioPlayer();
    await this.audioPlayer.start();
    await this.checkMicrophoneAvailability();
    await this.initLive2D();
    this.setModelLoadingStatus(false);
    log('应用初始化完成', 'success');
}
```

Import block:

```javascript
import { getAudioPlayer } from './core/audio/player.js';
import { checkMicrophoneAvailability, isHttpNonLocalhost } from './core/audio/recorder.js';
import { uiController } from './ui/controller.js';
import { log } from './utils/logger.js';
```

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/app.js
git commit -m "refactor: simplify app.js, remove Opus/MCP/camera init"
```

---

## Task 11: Update Live2D to use new player's AnalyserNode

**Files:**
- Modify: `ui/web/js/live2d/live2d.js`

- [ ] **Step 1: Update Live2D audio connection**

In `live2d.js`, modify `initializeAudioAnalyzer()` (line ~385) and `connectToAudioPlayer()` (line ~416):

Replace `initializeAudioAnalyzer()`:

```javascript
initializeAudioAnalyzer() {
    try {
        const audioPlayer = window.chatApp?.audioPlayer;
        if (!audioPlayer) {
            console.warn('音频播放器未初始化');
            return false;
        }
        this.analyser = audioPlayer.getAnalyser();
        if (!this.analyser) {
            console.warn('无法获取分析器节点');
            return false;
        }
        this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        return true;
    } catch (error) {
        console.error('初始化音频分析器失败:', error);
        return false;
    }
}
```

Replace `connectToAudioPlayer()`:

```javascript
connectToAudioPlayer() {
    try {
        const audioPlayer = window.chatApp?.audioPlayer;
        if (!audioPlayer) return false;
        this.analyser = audioPlayer.getAnalyser();
        if (!this.analyser) return false;
        this.dataArray = new Uint8Array(this.analyser.frequencyBinCount);
        return true;
    } catch (error) {
        console.error('连接到音频播放器失败:', error);
        return false;
    }
}
```

These methods no longer reference `streamingContext` — they go directly to `AudioPlayer.getAnalyser()`.

- [ ] **Step 2: Commit**

```bash
git add ui/web/js/live2d/live2d.js
git commit -m "refactor: connect Live2D analyser to new AudioPlayer"
```

---

## Task 12: End-to-end smoke test

**Files:**
- No new files

- [ ] **Step 1: Run backend tests**

```bash
cd ~/.config/superpowers/worktrees/jarvis/live2d-web
python -m pytest tests/ -q
```

Expected: No new failures beyond pre-existing 3.

- [ ] **Step 2: Start server and verify frontend loads**

```bash
cd ~/.config/superpowers/worktrees/jarvis/live2d-web
python -m ui.web.server --port 8006 &
sleep 3
curl -s http://localhost:8006/api/health
# Expected: {"status":"ok"}
curl -s -X POST http://localhost:8006/api/session
# Expected: {"session_id":"...","status":"connected"}
kill %1
```

- [ ] **Step 3: Verify no broken JS imports**

Open `http://localhost:8006/` in browser, check console for import errors. All deleted modules should have no remaining references.

Grep for stale references:

```bash
grep -r "opus-codec\|blocking-queue\|ota-connector\|mcp/tools\|libopus\|websocket.js" ui/web/js/ --include="*.js" | grep -v node_modules
```

Expected: No matches.

- [ ] **Step 4: Commit any fixes**

```bash
git add -A
git commit -m "fix: resolve any remaining stale references"
```

- [ ] **Step 5: Final full test run**

```bash
python -m pytest tests/ -q
```

Expected: Same pass count as baseline (691 passed, 3 pre-existing failures).
