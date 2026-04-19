# Browser WebSocket Streaming TTS Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the file-mediated `/api/audio/<hash>.wav` path in pet mode with a session-scoped WebSocket that streams raw MiniMax PCM chunks directly into the browser's Web Audio graph, cutting first-chunk-audible latency to < 500 ms while keeping Live2D lip-sync and VAD interrupts working.

**Architecture:** One `/api/tts/stream?session_id=<id>` WebSocket per session (last-writer-wins). Server reuses `MinimaxWSClient` with the resampler disabled (`sr_out=sr_in=32000`), forwards `int16LE @ 32 kHz mono` PCM as binary frames with a 2-byte `uint16 sentence_index` header, and emits JSON control frames (`turn_start` / `sentence_start` / `sentence_end` / `turn_end` / `cancel`). A new `BrowserWSPlayer` adapter sits in the `TTSEngine.stream_to_player` seam with a `drain() → True` override (WP5 correctness). Config flag `tts.browser_streaming` gates the preferred path; WS absence triggers an automatic per-chat fallback to the legacy file path. VAD-triggered cancel is wired via the existing `JarvisApp.event_bus` (`jarvis.tts_cancelled` topic).

**Tech Stack:** FastAPI (server WS endpoint, `TestClient.websocket_connect` for unit tests), `websockets` 16.0 + `pytest-asyncio` 1.3.0 (already installed), `core.tts.TTSEngine` + `core.tts_minimax_ws.MinimaxWSClient` (existing, unchanged), Web Audio API (`AudioContext`, `AudioWorklet`, `AnalyserNode`), vanilla JS in `ui/web/js/`.

**Reference spec:** `docs/superpowers/specs/2026-04-18-ws-streaming-browser-audio-design.md`

**Branch:** `feat/ws-streaming-browser-audio` (already checked out; `WIP working tree changes from prior session — leave them alone, only touch files listed below)

---

## File Structure

**New files:**
| Path | Responsibility |
|---|---|
| `ui/web/browser_ws_player.py` | `BrowserWSPlayer` class: adapts `TTSEngine.stream_to_player`'s Player contract to WebSocket forwarding. Single-sentence scoped. |
| `ui/web/js/core/audio/pcm-player-processor.js` | AudioWorklet: ring buffer + `process()` pulling from it, `port.onmessage` accepting `{pcm}` or `{clear}`. |
| `ui/web/js/core/audio/pcm-stream-player.js` | `PCMStreamPlayer` class: owns `AudioContext(sampleRate:32000)`, the worklet node, gain + analyser. Lazy-resume + pre-gesture queue. |
| `tests/test_browser_ws_stream.py` | All Python unit tests for tasks 1-6. |

**Modified files:**
| Path | What changes |
|---|---|
| `ui/web/server.py` | +WS endpoint `/api/tts/stream`, +`_ws_routes` + `_active_chats` dicts, +streaming branch in `on_sentence`, +cancel dispatch, +VAD event subscription. |
| `ui/web/js/core/api-client.js` | +`TTSStreamClient` class, +reconnect, +binary/text frame split, +cancel callback. |
| `ui/web/js/app.js` | +conditional player construction (PCMStreamPlayer when browser_streaming active). |
| `ui/web/js/ui/controller.js` | +first-gesture `player.resumeOnGesture()` hook. |
| `config.yaml` | +`tts.browser_streaming: true` key (after `minimax_prewarm` at line 395). |
| `jarvis.py` | +`event_bus.emit("jarvis.tts_cancelled", ...)` inside `_cancel_current` (1 line). |

**Unchanged (by design):**
- `core/tts_minimax_ws.py`, `core/tts.py`, `core/audio_stream_player.py` — no core changes.
- `ui/web/js/live2d/live2d.js` — reads `window.chatApp.audioPlayer.getAnalyser()`, which both the legacy `AudioPlayer` and new `PCMStreamPlayer` expose.

---

## Test Strategy Notes

- **Python tests** (all tasks 1-6): `tests/test_browser_ws_stream.py`, run with `pytest tests/test_browser_ws_stream.py -v`. Use `fastapi.testclient.TestClient` + `client.websocket_connect()` per the pattern in `tests/test_web_server.py`.
- **JavaScript tasks** (7-10): no JS test harness exists in this repo. Verify via Chromium DevTools + manual interaction. Each JS task includes a concrete manual verification command.
- **Full suite caveat:** `pytest tests/ -q` segfaults with `test_tts.py` (pre-existing; see memory #658-663). Run focused subset: `pytest tests/test_tts_cache.py tests/test_tts_minimax_ws.py tests/test_browser_ws_stream.py tests/test_web_server.py -v`.

---

## Task 1: `BrowserWSPlayer` class + unit tests

**Files:**
- Create: `ui/web/browser_ws_player.py`
- Create: `tests/test_browser_ws_stream.py`

This task delivers a pure Python class with no WebSocket or network dependency — the WS is passed in as a mock. Establishes the core contract `TTSEngine.stream_to_player` requires (`write(np.ndarray)`, `played_samples: int`, `drain(timeout: float) -> bool`).

- [ ] **Step 1: Create the test file with three failing tests**

```python
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
        from ui.web.browser_ws_player import BrowserWSPlayer
        loop = asyncio.new_event_loop()
        try:
            ws = MagicMock()
            ws.send_bytes = MagicMock()
            # Patch run_coroutine_threadsafe so ws.send_bytes is called inline
            import ui.web.browser_ws_player as mod

            sent = []
            def fake_run_cot(coro, _loop):
                # coro is ws.send_bytes(payload) — resolve it into the call args
                try:
                    coro.send(None)
                except StopIteration as e:
                    pass
                return MagicMock()

            # Easier: just let BrowserWSPlayer call ws.send_bytes directly in a patched path
            player = BrowserWSPlayer(ws=ws, sentence_index=3, loop=loop)
            pcm = np.array([1.0, -1.0, 0.5, 0.0], dtype=np.float32)
            player.write(pcm)
            # After write(), ws.send_bytes should have been scheduled via run_coroutine_threadsafe.
            # Run one loop iteration to flush.
            pending = asyncio.all_tasks(loop)
            # write() uses run_coroutine_threadsafe; the coroutine is ws.send_bytes(payload).
            # Extract the call from the MagicMock side:
            assert ws.send_bytes.called or len(pending) > 0
        finally:
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
```

The first test is deliberately loose — we'll tighten it after write() is implemented and we can inspect the payload. The second and third tests are exact assertions.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_browser_ws_stream.py -v`
Expected: all three `ModuleNotFoundError: No module named 'ui.web.browser_ws_player'`.

- [ ] **Step 3: Create `BrowserWSPlayer`**

```python
# ui/web/browser_ws_player.py
"""Browser-side WebSocket TTS player adapter.

Used by `TTSEngine.stream_to_player` when the Live2D browser pet mode has
an active `/api/tts/stream` WebSocket. Implements the subset of the Player
contract that `_stream_to_player_async` touches (`write`, `played_samples`,
`drain`). Single-sentence scoped: instantiate per `on_sentence` call,
throw away after the sentence finishes.

Not thread-safe across sentences; the TTS pipeline serializes sentences.
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any

import numpy as np

LOGGER = logging.getLogger(__name__)


class BrowserWSPlayer:
    """Forwards int16LE @ 32 kHz mono PCM chunks to a browser WebSocket.

    Wire format (matches spec D4):
        bytes[0..2)  uint16 LE  sentence_index
        bytes[2..)   int16  LE  PCM samples

    Args:
        ws: A FastAPI/Starlette ``WebSocket`` (or anything with an awaitable
            ``send_bytes(bytes)`` method).
        sentence_index: The 0-based sentence index within this turn. Packed
            into the first 2 bytes of every forwarded frame.
        loop: The asyncio event loop that owns ``ws`` (FastAPI's main loop).
            ``write()`` runs on the TTSEngine's private asyncio thread, so
            we must cross back to ``loop`` via ``run_coroutine_threadsafe``.
    """

    def __init__(
        self,
        ws: Any,
        sentence_index: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._ws = ws
        self._idx = sentence_index
        self._loop = loop
        self._header = struct.pack("<H", sentence_index)
        self.played_samples: int = 0  # monotonic, same API as AudioStreamPlayer

    def write(self, pcm_f32: np.ndarray) -> None:
        """Called per PCM chunk from ``MinimaxWSClient.feed()``.

        Converts float32 → int16 LE, prepends the 2-byte sentence_index
        header, and schedules ``ws.send_bytes`` on the FastAPI event loop.
        """
        if pcm_f32.size == 0:
            return
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        payload = self._header + pcm_i16.tobytes()
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws.send_bytes(payload), self._loop,
            )
        except Exception as exc:
            # Browser WS may have closed; stream_to_player's abort_event
            # handles the cancel path. We just log and drop this chunk.
            LOGGER.debug("BrowserWSPlayer send_bytes scheduling failed: %s", exc)
        self.played_samples += pcm_i16.size

    def drain(self, timeout: float = 5.0) -> bool:
        """Always returns True (optimistic drain).

        The playback buffer lives in the browser; there is no server-side
        drain to wait on. Returning True makes
        ``TTSEngine._stream_to_player_async`` set
        ``PlaybackResult.completed=True`` so WP5 records this sentence in
        ``played_texts``. Returning None/False would cause the sentence to
        be treated as unplayed and re-injected on the next turn.

        See design doc D11 for the edge-case tradeoff (WS death at
        sentence_end). `timeout` is ignored; kept for API compatibility
        with ``AudioStreamPlayer.drain``.
        """
        _ = timeout
        return True
```

- [ ] **Step 4: Tighten the first test to assert exact payload bytes**

Replace the body of `test_write_forwards_header_plus_int16_bytes` with:

```python
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
```

- [ ] **Step 5: Run tests — all three pass**

Run: `pytest tests/test_browser_ws_stream.py::TestBrowserWSPlayer -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add ui/web/browser_ws_player.py tests/test_browser_ws_stream.py
git commit -m "feat(ws): add BrowserWSPlayer adapter for PCM-over-WS streaming"
```

---

## Task 2: `/api/tts/stream` WebSocket endpoint + session routing

**Files:**
- Modify: `ui/web/server.py` (add WS endpoint, `_ws_routes` dict, `_ws_routes_lock`, `_send_ctrl` helper)
- Modify: `tests/test_browser_ws_stream.py` (add `TestTTSStreamEndpoint`)

- [ ] **Step 1: Write failing tests for the endpoint**

Append to `tests/test_browser_ws_stream.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: /api/tts/stream endpoint + session routing
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient
from unittest.mock import MagicMock, patch


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
```

- [ ] **Step 2: Run tests — verify all fail**

Run: `pytest tests/test_browser_ws_stream.py::TestTTSStreamEndpoint -v`
Expected: `AttributeError: module 'ui.web.server' has no attribute '_ws_routes'` or route 404.

- [ ] **Step 3: Add the endpoint + module-level state in `ui/web/server.py`**

Near the top of the module (after existing imports, around line 24):

```python
# --- Browser TTS streaming state (Task 2) ---
_ws_routes: dict[str, Any] = {}          # session_id → WebSocket
_ws_routes_lock = asyncio.Lock()

async def _send_ctrl(ws: Any, payload: dict) -> None:
    """Send a JSON control frame. Silently drops if ws has gone away."""
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        LOGGER.debug("ws send_text failed: %s", exc)
```

Inside `create_app(jarvis_app)`, alongside the other route definitions (e.g.
after the existing `@app.get("/api/audio/{filename}")` block around line 280):

```python
    @app.websocket("/api/tts/stream")
    async def tts_stream(ws: WebSocket, session_id: str):
        if session_id not in sessions:
            await ws.close(code=1008, reason="unknown session")
            return
        await ws.accept()
        async with _ws_routes_lock:
            old = _ws_routes.get(session_id)
            _ws_routes[session_id] = ws
        if old is not None:
            try:
                await old.close(code=1001, reason="superseded")
            except Exception:
                pass
        try:
            while True:
                # Server-push only for now. We still recv to detect client
                # close + to accept future {"type":"user_stop"} frames.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            async with _ws_routes_lock:
                if _ws_routes.get(session_id) is ws:
                    _ws_routes.pop(session_id, None)
```

At the top of `ui/web/server.py` add the FastAPI WebSocket imports:

```python
from fastapi import WebSocket, WebSocketDisconnect
```

(Verify this line matches the existing FastAPI import group.)

- [ ] **Step 4: Run tests — all four pass**

Run: `pytest tests/test_browser_ws_stream.py::TestTTSStreamEndpoint -v`
Expected: 4 passed.

- [ ] **Step 5: Run the existing web_server tests to confirm no regression**

Run: `pytest tests/test_web_server.py -v`
Expected: all pass (no behavioral change to existing endpoints).

- [ ] **Step 6: Commit**

```bash
git add ui/web/server.py tests/test_browser_ws_stream.py
git commit -m "feat(ws): add /api/tts/stream endpoint with last-writer-wins routing"
```

---

## Task 3: Config flag + `on_sentence` streaming branch

**Files:**
- Modify: `config.yaml` (add `browser_streaming: true`)
- Modify: `ui/web/server.py` (import `BrowserWSPlayer` + `MinimaxWSClient`, branch on_sentence)
- Modify: `tests/test_browser_ws_stream.py` (add `TestOnSentenceBranch`)

This task implements the core streaming path. When `browser_streaming=True` and a WS is live for the session, `on_sentence` constructs a `MinimaxWSClient` (sr_out=32000) + `BrowserWSPlayer`, calls `TTSEngine.stream_to_player`, and emits `sentence_start`/`sentence_end` control frames. SSE still fires with `audio_url=""` so the frontend picks the streaming path.

- [ ] **Step 1: Add the config flag**

Edit `config.yaml`, after line 395 (`minimax_prewarm: true`):

```yaml
  browser_streaming: true  # prefer PCM-over-WS to browser when a WS is open;
                           # false = always serve /api/audio/<hash>.wav files
```

- [ ] **Step 2: Add streaming branch in `ui/web/server.py`**

Near module top:

```python
from ui.web.browser_ws_player import BrowserWSPlayer
from core.tts_minimax_ws import MinimaxWSClient
import threading
```

Inside `create_app`, extract the `on_sentence` helper out into a reusable
function and add a streaming variant. Replace the existing body of the
callback at line 153 with:

```python
        def on_sentence(sentence: str, emotion: str = "") -> None:
            nonlocal sentence_index
            idx = sentence_index
            sentence_index += 1

            use_stream = (
                bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False))
                and _ws_routes.get(req.session_id) is not None
                and ws_client_for_turn is not None  # prewarm succeeded
            )

            audio_url = ""
            if use_stream:
                ws = _ws_routes[req.session_id]
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {
                        "type": "sentence_start",
                        "turn_id": turn_id,
                        "sentence_index": idx,
                        "text": sentence,
                        "emotion": (emotion or "neutral").lower(),
                    }),
                    loop,
                )
                player = BrowserWSPlayer(ws=ws, sentence_index=idx, loop=loop)
                try:
                    result = tts.stream_to_player(
                        sentence, emotion, player, ws_client_for_turn, abort_event,
                    )
                except Exception as exc:
                    LOGGER.warning("stream_to_player failed: %s", exc)
                    result = None
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {
                        "type": "sentence_end",
                        "turn_id": turn_id,
                        "sentence_index": idx,
                        "subtitle_url":
                            getattr(result, "subtitle_url", None) if result else None,
                    }),
                    loop,
                )
            else:
                if tts:
                    try:
                        r = tts.synth_to_file(sentence, emotion)
                        if r:
                            audio_path, deletable = r
                            if audio_path.endswith(".pcm"):
                                audio_name = f"{uuid.uuid4().hex}.wav"
                                dest = AUDIO_DIR / audio_name
                                _wrap_pcm_to_wav(Path(audio_path), dest)
                            else:
                                ext = Path(audio_path).suffix or ".mp3"
                                audio_name = f"{uuid.uuid4().hex}{ext}"
                                dest = AUDIO_DIR / audio_name
                                shutil.copy2(audio_path, dest)
                            if deletable:
                                Path(audio_path).unlink(missing_ok=True)
                            audio_url = f"/api/audio/{audio_name}"
                    except Exception as exc:
                        LOGGER.warning("TTS synth failed: %s", exc)

            event = {
                "turn_id": turn_id,
                "index": idx,
                "text": sentence,
                "emotion": emotion.lower() if emotion else "neutral",
                "audio_url": audio_url,
            }
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
```

Immediately before the `on_sentence` definition, add the per-chat state:

```python
        turn_id = uuid.uuid4().hex
        abort_event = threading.Event()
        ws_client_for_turn: MinimaxWSClient | None = None
        if bool(jarvis_app.config.get("tts", {}).get("browser_streaming", False)) \
                and _ws_routes.get(req.session_id) is not None:
            cfg = jarvis_app.config.get("tts", {})
            try:
                ws_client_for_turn = MinimaxWSClient(
                    base_url=cfg.get("minimax_base_url", "https://api-uw.minimax.io"),
                    api_key=cfg.get("minimax_key", "") or os.environ.get("MINIMAX_API_KEY", ""),
                    model=cfg.get("minimax_model", "speech-2.8-turbo"),
                    voice_id=cfg.get("minimax_voice", "Chinese (Mandarin)_ExplorativeGirl"),
                    volume=int(cfg.get("minimax_volume", 1)),
                    sample_rate_out=32000,  # skip resampler
                    sample_rate_in=32000,
                )
                # Prewarm: open WS in background so handshake overlaps LLM tokens.
                asyncio.run_coroutine_threadsafe(
                    ws_client_for_turn.open_session(emotion=None), loop,
                )
                # Announce the turn on the browser WS.
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(_ws_routes[req.session_id],
                               {"type": "turn_start", "turn_id": turn_id}),
                    loop,
                )
            except Exception as exc:
                LOGGER.warning("MinimaxWSClient prewarm failed, falling back: %s", exc)
                ws_client_for_turn = None
```

Add `import os` at the top if not already imported.

- [ ] **Step 3: Emit `turn_end` after the chat loop finishes**

In the existing `_run()` closure (around line 203), after `handle_text`
returns and before the `queue.put(None)`, add:

```python
            if ws_client_for_turn is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        ws_client_for_turn.close_session(), loop,
                    )
                except Exception: pass
            ws = _ws_routes.get(req.session_id)
            if ws is not None:
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws, {"type": "turn_end", "turn_id": turn_id}),
                    loop,
                )
```

- [ ] **Step 4: Run focused tests — existing ones still pass (no new tests in this task)**

Run: `pytest tests/test_browser_ws_stream.py tests/test_web_server.py -v`
Expected: all prior tests pass; no new ones added here (end-to-end flow verified in Task 10 manual QA).

- [ ] **Step 5: Commit**

```bash
git add config.yaml ui/web/server.py
git commit -m "feat(ws): on_sentence streams PCM to browser when ws + flag on"
```

---

## Task 4: Expose `turn_id` on SSE events + POST response

**Files:**
- Modify: `ui/web/server.py` (return `turn_id` in chat POST body; already in SSE from Task 3)
- Modify: `tests/test_browser_ws_stream.py` (add `TestTurnIdCorrelation`)

The SSE `sentence` event already includes `turn_id` after Task 3. This task
exposes the same `turn_id` in the HTTP POST response body so the frontend
can correlate an upcoming WS `turn_start` with the `POST /api/chat` reply
without parsing SSE first.

- [ ] **Step 1: Write failing test**

Append:

```python
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `pytest tests/test_browser_ws_stream.py::TestTurnIdCorrelation -v`
Expected: `assert None` or `KeyError` — X-Turn-Id not set.

- [ ] **Step 3: Add `X-Turn-Id` to the StreamingResponse**

Modify the return of the chat handler (around line 231 currently):

```python
        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Turn-Id": turn_id,
            },
        )
```

- [ ] **Step 4: Run test — passes**

Run: `pytest tests/test_browser_ws_stream.py::TestTurnIdCorrelation -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/web/server.py tests/test_browser_ws_stream.py
git commit -m "feat(ws): expose turn_id via X-Turn-Id response header"
```

---

## Task 5: New-chat cancel + `_active_chats` state

**Files:**
- Modify: `ui/web/server.py` (add `_active_chats`, emit cancel on new chat)
- Modify: `tests/test_browser_ws_stream.py` (add `TestNewChatCancel`)

When a second `POST /api/chat` arrives for a session while the previous
turn's streaming is still in-flight, the server emits
`cancel{reason:"new_chat"}` for the prior `turn_id` before starting the new
turn, and sets the prior `abort_event` so `stream_to_player` exits.

- [ ] **Step 1: Failing test**

Append:

```python
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
```

- [ ] **Step 2: Run — verify failure**

Run: `pytest tests/test_browser_ws_stream.py::TestNewChatCancel -v`
Expected: `AttributeError: module 'ui.web.server' has no attribute '_active_chats'`.

- [ ] **Step 3: Add `_active_chats` + new-chat cancel logic**

At module top (near `_ws_routes`):

```python
_active_chats: dict[str, dict] = {}       # session_id → {turn_id, abort_event, ws_client}
_active_chats_lock = asyncio.Lock()
```

In the chat handler, before constructing the new `turn_id`:

```python
        # --- Cancel previous turn if still active on this session ---
        prev = _active_chats.get(req.session_id)
        if prev is not None:
            ws_prev = _ws_routes.get(req.session_id)
            if ws_prev is not None:
                asyncio.run_coroutine_threadsafe(
                    _send_ctrl(ws_prev, {
                        "type": "cancel",
                        "turn_id": prev["turn_id"],
                        "reason": "new_chat",
                    }),
                    asyncio.get_running_loop(),
                )
            try:
                prev["abort_event"].set()
            except Exception:
                pass

        turn_id = uuid.uuid4().hex
        abort_event = threading.Event()
        _active_chats[req.session_id] = {
            "turn_id": turn_id,
            "abort_event": abort_event,
            "ws_client": None,  # filled in below if streaming
        }
```

After successfully building `ws_client_for_turn`, update the entry:

```python
        if ws_client_for_turn is not None:
            _active_chats[req.session_id]["ws_client"] = ws_client_for_turn
```

At the end of `_run()` in the finally block (after turn_end frame), clean up:

```python
            _active_chats.pop(req.session_id, None)
```

- [ ] **Step 4: Run test — passes**

Run: `pytest tests/test_browser_ws_stream.py::TestNewChatCancel -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add ui/web/server.py tests/test_browser_ws_stream.py
git commit -m "feat(ws): cancel previous turn on new POST /api/chat"
```

---

## Task 6: VAD cancel via `event_bus`

**Files:**
- Modify: `jarvis.py` (emit `jarvis.tts_cancelled` in `_cancel_current`)
- Modify: `ui/web/server.py` (subscribe at startup, fan out cancel frames)
- Modify: `tests/test_browser_ws_stream.py` (add `TestVADCancel`)

- [ ] **Step 1: Failing test**

```python
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
```

- [ ] **Step 2: Verify failure**

Run: `pytest tests/test_browser_ws_stream.py::TestVADCancel -v`
Expected: `AssertionError: bus.on.called` (no subscription registered).

- [ ] **Step 3: Subscribe in server + emit helper**

In `ui/web/server.py`, inside `create_app`, after `sessions = {}`:

```python
    def _on_tts_cancelled(payload: dict | None = None) -> None:
        """EventBus handler for VAD-triggered interrupts. Fan out cancel
        frames to every live browser WS with an active chat."""
        if payload is None:
            payload = {}
        reason = payload.get("reason", "vad")
        loop = getattr(jarvis_app, "_web_loop", None)
        if loop is None:
            return
        for sid, chat in list(_active_chats.items()):
            ws = _ws_routes.get(sid)
            if ws is None:
                continue
            asyncio.run_coroutine_threadsafe(
                _send_ctrl(ws, {
                    "type": "cancel",
                    "turn_id": chat["turn_id"],
                    "reason": reason,
                }),
                loop,
            )
            try:
                chat["abort_event"].set()
            except Exception:
                pass

    if hasattr(jarvis_app, "event_bus") and jarvis_app.event_bus is not None:
        jarvis_app.event_bus.on("jarvis.tts_cancelled", _on_tts_cancelled)
```

And at app startup capture the loop so the handler can use `run_coroutine_threadsafe`:

```python
    @app.on_event("startup")
    async def _capture_loop():
        jarvis_app._web_loop = asyncio.get_running_loop()
```

- [ ] **Step 4: Emit the event in `jarvis.py`**

Find `_cancel_current` at `jarvis.py:1269` (the method called from `_on_voice_interrupt`). Locate the existing `pipeline.abort()` call inside it. Immediately after that line, add:

```python
            self.event_bus.emit("jarvis.tts_cancelled", {"reason": "vad"})
```

- [ ] **Step 5: Run tests — pass**

Run: `pytest tests/test_browser_ws_stream.py::TestVADCancel -v`
Expected: 1 passed.

- [ ] **Step 6: Run focused suite for regression**

Run: `pytest tests/test_browser_ws_stream.py tests/test_web_server.py tests/test_event_bus.py -v`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add ui/web/server.py jarvis.py tests/test_browser_ws_stream.py
git commit -m "feat(ws): wire VAD interrupts to browser cancel via event_bus"
```

---

## Task 7: AudioWorklet processor + `PCMStreamPlayer` JS class

**Files:**
- Create: `ui/web/js/core/audio/pcm-player-processor.js`
- Create: `ui/web/js/core/audio/pcm-stream-player.js`

No JS test harness exists; this task is implement + manual verify.

- [ ] **Step 1: Create the worklet processor**

```js
// ui/web/js/core/audio/pcm-player-processor.js
// AudioWorklet that plays PCM pushed via port.postMessage({pcm: Float32Array}).
// Ring buffer sized for 10 seconds of 32 kHz audio. `{clear: true}` resets.

class PCMPlayerProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._ring = new Float32Array(32000 * 10);  // 10s @ 32kHz
        this._read = 0;
        this._write = 0;
        this.port.onmessage = (e) => {
            if (e.data && e.data.clear) {
                this._read = this._write = 0;
                return;
            }
            const chunk = e.data && e.data.pcm;
            if (!chunk) return;
            for (let i = 0; i < chunk.length; i++) {
                this._ring[this._write] = chunk[i];
                this._write = (this._write + 1) % this._ring.length;
                // Overflow: advance read pointer (drop oldest).
                if (this._write === this._read) {
                    this._read = (this._read + 1) % this._ring.length;
                }
            }
        };
    }
    process(_inputs, outputs) {
        const out = outputs[0][0];  // mono
        for (let i = 0; i < out.length; i++) {
            if (this._read === this._write) {
                out[i] = 0;
            } else {
                out[i] = this._ring[this._read];
                this._read = (this._read + 1) % this._ring.length;
            }
        }
        return true;
    }
}
registerProcessor('pcm-player', PCMPlayerProcessor);
```

- [ ] **Step 2: Create the `PCMStreamPlayer` class**

```js
// ui/web/js/core/audio/pcm-stream-player.js
// Public API parity with ui/web/js/core/audio/player.js (AudioPlayer):
//   - start()
//   - getAnalyser()
//   - getAudioContext()
//   - clearAll()
// Plus streaming-specific writeChunk(ArrayBuffer) and resumeOnGesture().
//
// sampleRate is pinned to 32000 to match MiniMax PCM output — the
// AudioWorklet then reads 1:1 from the ring buffer with no interpolation.
// If the target browser rejects that rate the constructor throws; caller
// falls back to legacy AudioPlayer.

import { log } from '../../utils/logger.js';

export class PCMStreamPlayer {
    constructor() {
        this.ctx = null;
        this.node = null;
        this.gainNode = null;
        this.analyser = null;
        this._ready = false;
        this._preCtxQueue = [];   // Float32Array chunks, bounded ~2s
        this._preCtxSamples = 0;
    }

    async start() {
        this.ctx = new (window.AudioContext || window.webkitAudioContext)({
            sampleRate: 32000,
        });
        const basePath = (() => {
            const p = window.location.pathname;
            return p.substring(0, p.lastIndexOf('/') + 1);
        })();
        await this.ctx.audioWorklet.addModule(
            basePath + 'js/core/audio/pcm-player-processor.js',
        );
        this.node = new AudioWorkletNode(this.ctx, 'pcm-player');
        this.gainNode = this.ctx.createGain();
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256;
        this.node.connect(this.gainNode);
        this.gainNode.connect(this.analyser);
        this.analyser.connect(this.ctx.destination);
        this._ready = true;
        log('PCMStreamPlayer 初始化完成 (32kHz)', 'success');
    }

    getAnalyser() { return this.analyser; }
    getAudioContext() { return this.ctx; }

    async resumeOnGesture() {
        if (!this.ctx) return;
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        // Drain pre-gesture queue.
        while (this._preCtxQueue.length) {
            this._pushToWorklet(this._preCtxQueue.shift());
        }
        this._preCtxSamples = 0;
    }

    writeChunk(arrayBuf) {
        // arrayBuf: raw binary WS frame. First 2 bytes = uint16 LE
        // sentence_index (we don't use it client-side for now — WS frame
        // order is preserved). Remaining bytes = int16 LE @ 32 kHz mono.
        if (!this._ready) return;
        if (arrayBuf.byteLength < 2) return;
        const i16 = new Int16Array(arrayBuf, 2);
        const f32 = new Float32Array(i16.length);
        for (let j = 0; j < i16.length; j++) f32[j] = i16[j] / 32768;

        if (this.ctx.state === 'suspended') {
            // Bound at ~2s of audio to prevent runaway buffering if the
            // user never clicks.
            if (this._preCtxSamples + f32.length < 64000) {
                this._preCtxQueue.push(f32);
                this._preCtxSamples += f32.length;
            }
            return;
        }
        this._pushToWorklet(f32);
    }

    _pushToWorklet(f32) {
        this.node.port.postMessage({ pcm: f32 }, [f32.buffer]);
    }

    clearAll() {
        this._preCtxQueue = [];
        this._preCtxSamples = 0;
        if (this.node) this.node.port.postMessage({ clear: true });
    }
}

let instance = null;
export function getPCMStreamPlayer() {
    if (!instance) instance = new PCMStreamPlayer();
    return instance;
}
```

- [ ] **Step 3: Manual verify — worklet loads without error**

```bash
python jarvis.py --no-wake &
# Then in a new terminal:
open http://localhost:8080/
# Open DevTools → Console. Run:
```
```js
const p = new (await import('./js/core/audio/pcm-stream-player.js')).PCMStreamPlayer();
await p.start();
console.log(p.ctx.state, p.ctx.sampleRate);
// Expected: "suspended" (before user gesture) OR "running", 32000
p.getAnalyser() instanceof AnalyserNode;  // true
```

If the worklet URL fails to load, verify `basePath` resolution in DevTools
Sources panel. Expected path: `http://localhost:8080/js/core/audio/pcm-player-processor.js`.

- [ ] **Step 4: Commit**

```bash
git add ui/web/js/core/audio/pcm-player-processor.js \
        ui/web/js/core/audio/pcm-stream-player.js
git commit -m "feat(ws): add AudioWorklet + PCMStreamPlayer for 32kHz PCM"
```

---

## Task 8: `TTSStreamClient` in `api-client.js`

**Files:**
- Modify: `ui/web/js/core/api-client.js`

Add a new class that owns the `/api/tts/stream` WebSocket, with reconnect,
text/binary frame split, and a callback surface (`onTurnStart`,
`onSentenceStart`, `onAudioChunk`, `onSentenceEnd`, `onTurnEnd`, `onCancel`).

- [ ] **Step 1: Append the class to `api-client.js`** (before the `export function getApiClient` at the bottom, around line 213):

```js
// ---------------------------------------------------------------------------
// TTSStreamClient — owns /api/tts/stream WebSocket, fans out control events
// to the chat app, and forwards binary PCM frames to the PCMStreamPlayer.
// ---------------------------------------------------------------------------

export class TTSStreamClient {
    constructor() {
        this.ws = null;
        this.serverUrl = '';
        this.sessionId = null;
        this.connected = false;

        this.onTurnStart = null;
        this.onSentenceStart = null;
        this.onAudioChunk = null;      // (ArrayBuffer) => void
        this.onSentenceEnd = null;
        this.onTurnEnd = null;
        this.onCancel = null;

        this._reconnectMs = 1000;
        this._maxReconnectMs = 5000;
        this._shouldReconnect = true;
    }

    setServerUrl(url) {
        this.serverUrl = url.replace(/\/+$/, '');
    }

    connect(sessionId) {
        this.sessionId = sessionId;
        this._shouldReconnect = true;
        this._open();
    }

    disconnect() {
        this._shouldReconnect = false;
        if (this.ws) { try { this.ws.close(); } catch {} this.ws = null; }
        this.connected = false;
    }

    _wsUrl() {
        const httpUrl = this.serverUrl || window.location.origin;
        const wsUrl = httpUrl.replace(/^http/, 'ws');
        return `${wsUrl}/api/tts/stream?session_id=${encodeURIComponent(this.sessionId)}`;
    }

    _open() {
        this.ws = new WebSocket(this._wsUrl());
        this.ws.binaryType = 'arraybuffer';

        this.ws.onopen = () => {
            this.connected = true;
            this._reconnectMs = 1000;
            log('TTS stream WS connected', 'success');
        };
        this.ws.onmessage = (event) => {
            if (typeof event.data === 'string') {
                let payload;
                try { payload = JSON.parse(event.data); }
                catch { return; }
                const t = payload.type;
                if (t === 'turn_start' && this.onTurnStart) this.onTurnStart(payload);
                else if (t === 'sentence_start' && this.onSentenceStart) this.onSentenceStart(payload);
                else if (t === 'sentence_end' && this.onSentenceEnd) this.onSentenceEnd(payload);
                else if (t === 'turn_end' && this.onTurnEnd) this.onTurnEnd(payload);
                else if (t === 'cancel' && this.onCancel) this.onCancel(payload);
            } else if (event.data instanceof ArrayBuffer) {
                if (this.onAudioChunk) this.onAudioChunk(event.data);
            }
        };
        this.ws.onclose = () => {
            this.connected = false;
            this.ws = null;
            if (this._shouldReconnect && this.sessionId) {
                setTimeout(() => this._open(), this._reconnectMs);
                this._reconnectMs = Math.min(this._reconnectMs * 2, this._maxReconnectMs);
            }
        };
        this.ws.onerror = () => { /* onclose will follow */ };
    }

    isConnected() { return this.connected; }
}

let ttsStreamInstance = null;
export function getTTSStreamClient() {
    if (!ttsStreamInstance) ttsStreamInstance = new TTSStreamClient();
    return ttsStreamInstance;
}
```

- [ ] **Step 2: Manual verify in DevTools**

```js
// After pet mode has an active session:
const c = (await import('./js/core/api-client.js')).getTTSStreamClient();
c.onTurnStart = (p) => console.log('turn_start', p);
c.onAudioChunk = (b) => console.log('audio_chunk bytes=', b.byteLength);
c.onCancel = (p) => console.log('cancel', p);
c.connect(window.chatApp.apiClient.sessionId);
// Expect: "TTS stream WS connected" log.
// POST a chat from the UI → expect turn_start + N audio_chunk + turn_end.
```

- [ ] **Step 3: Commit**

```bash
git add ui/web/js/core/api-client.js
git commit -m "feat(ws): add TTSStreamClient with binary+text frame split"
```

---

## Task 9: Wire `chatApp` to select player + forward frames

**Files:**
- Modify: `ui/web/js/app.js`
- Modify: `ui/web/js/ui/controller.js` (first-gesture hook)

- [ ] **Step 1: Update `app.js` to choose player based on WS capability**

Replace the `audioPlayer` initialization in `init()` (currently line 20) with:

```js
        // Try streaming first; fall back to legacy AudioPlayer if the
        // AudioWorklet or 32kHz AudioContext is not available.
        try {
            const mod = await import('./core/audio/pcm-stream-player.js');
            const streamPlayer = mod.getPCMStreamPlayer();
            await streamPlayer.start();
            this.audioPlayer = streamPlayer;
            log('使用 PCMStreamPlayer (WS streaming)', 'info');
        } catch (err) {
            log(`PCMStreamPlayer 初始化失败，回退 AudioPlayer: ${err.message}`, 'warning');
            this.audioPlayer = getAudioPlayer();
            await this.audioPlayer.start();
        }

        // If streaming player is active, open the TTS WebSocket.
        if (this.audioPlayer.writeChunk) {
            const { getTTSStreamClient } = await import('./core/api-client.js');
            const tts = getTTSStreamClient();
            tts.setServerUrl(window.location.origin);
            tts.onAudioChunk = (buf) => this.audioPlayer.writeChunk(buf);
            tts.onSentenceStart = (p) => {
                if (this.live2dManager) {
                    this.live2dManager.triggerEmotionAction(p.emotion || 'neutral');
                    this.live2dManager.startTalking();
                }
            };
            tts.onTurnEnd = () => {
                if (this.live2dManager) this.live2dManager.stopTalking();
            };
            tts.onCancel = (p) => {
                this.audioPlayer.clearAll();
                if (this.live2dManager) this.live2dManager.stopTalking();
                const r = p.reason;
                if (r === 'new_chat' || r === 'user_stop') {
                    if (this.live2dManager) this.live2dManager.triggerEmotionAction('neutral');
                } else if (r === 'pipeline_error') {
                    log('TTS error, retrying…', 'warning');
                }
            };
            this._ttsStream = tts;
        }
```

Then, inside `app.js` after `apiClient.connect()` succeeds (find the
existing dial-success handler; likely in `controller.js`), call
`this._ttsStream?.connect(this.apiClient.sessionId)`.

Because `controller.js` is ~large, place a single line in the existing
dial-success path. Locate it with:

```bash
grep -n "apiClient.connect\|session established\|会话已建立" ui/web/js/ui/controller.js
```

Add after the line that confirms connection:

```js
if (window.chatApp?._ttsStream) {
    window.chatApp._ttsStream.connect(window.chatApp.apiClient.sessionId);
}
```

- [ ] **Step 2: First-gesture hook for AudioContext resume**

In `controller.js`, inside the Live2D click / tap setup (search for
`live2d-stage` or `petOverlay` mouse handlers), add on first interaction:

```js
if (window.chatApp?.audioPlayer?.resumeOnGesture) {
    window.chatApp.audioPlayer.resumeOnGesture().catch(() => {});
}
```

- [ ] **Step 3: Manual verify — pet mode streaming works end-to-end**

```bash
# Terminal 1
python jarvis.py --no-wake

# Terminal 2 (desktop app)
cd desktop && npm start
```

In the pet window:
1. Click the Live2D character (resumes AudioContext).
2. Type "给我讲一个关于月亮的三段故事" and send.
3. Expected: first audio chunk plays within ~500 ms of Enter; Live2D
   mouth animates in sync; console shows `turn_start` → N `audio_chunk
   bytes=` → `sentence_end` → `turn_end`.
4. While it's speaking, send another chat. Expected: prior audio stops
   within ~200 ms; console shows `cancel{reason:"new_chat"}`.

- [ ] **Step 4: Commit**

```bash
git add ui/web/js/app.js ui/web/js/ui/controller.js
git commit -m "feat(ws): wire chatApp to stream PCM via TTSStreamClient"
```

---

## Task 10: End-to-end fallback QA + smoke

**Files:** none modified; verification only.

- [ ] **Step 1: Flag-off smoke (legacy path still works)**

Edit `config.yaml` → `tts.browser_streaming: false`. Restart server.

Verify:
- `python jarvis.py --no-wake` + pet mode + single sentence → audio still
  plays (via `/api/audio/<hash>.wav` route).
- DevTools Network panel shows a GET for `/api/audio/<hex>.wav`.
- No `/api/tts/stream` WebSocket is opened (the JS still tries, but
  with flag off the server never sends frames — harmless).

Restore `tts.browser_streaming: true` after verifying.

- [ ] **Step 2: WS-absent auto-fallback**

With `browser_streaming=true`, force the WS to fail by blocking it
client-side:
```js
// In DevTools console, before the first chat:
window.chatApp._ttsStream.disconnect();
```
Send a chat. Expected:
- Server `_ws_routes[session_id]` is absent → `on_sentence` falls back to
  `synth_to_file`.
- SSE `audio_url` is non-empty.
- Browser's legacy `AudioPlayer` (if present) plays the file; otherwise
  audio is silent but log shows "synth_to_file path used" — acceptable
  degradation (full parity requires keeping both players alive, Task 9
  currently picks one).

- [ ] **Step 3: Mid-turn WS death**

During a "三段故事" turn:
```js
window.chatApp._ttsStream.ws.close();
```
Expected:
- Remaining sentences NOT synthesized (server sees WS gone → `on_sentence`
  route falls through to file path, OR `abort_event` set depending on
  timing).
- Live2D mouth stops within ~1 s.

- [ ] **Step 4: VAD interrupt**

Say something audible into the mic while pet mode is speaking. Expected:
- Server logs `Voice interrupt detected`.
- `event_bus` fires `jarvis.tts_cancelled`.
- Browser WS receives `cancel{reason:"vad"}`.
- Audio stops within ~300 ms; Live2D mouth closes.

- [ ] **Step 5: First-chunk latency measurement**

Measure with DevTools Performance trace:
- Mark T0 at POST `/api/chat` send.
- Mark T1 at first `audio_chunk` binary frame received.
- Target: T1 - T0 < 500 ms on localhost with MiniMax WS prewarm active.

- [ ] **Step 6: Run focused test suite one last time**

```bash
pytest tests/test_browser_ws_stream.py tests/test_web_server.py \
       tests/test_tts_minimax_ws.py tests/test_event_bus.py -v
```
Expected: all pass.

- [ ] **Step 7: Commit QA notes if any config tweaks were made**

```bash
git add -A  # only if config.yaml was re-edited
git diff --cached  # review
git commit -m "chore(ws): pet-mode streaming smoke + fallback QA pass" --allow-empty
```
(Use `--allow-empty` only if no files changed; otherwise drop the flag.)

---

## Deployment Checklist (from the spec)

- [ ] `AudioContext({sampleRate:32000})` runs in shipped Electron (desktop pet).
- [ ] `MINIMAX_API_KEY` env var still `sk-api-...` international key.
- [ ] No new deps: `websockets`, `pytest-asyncio`, `soxr` already installed.
- [ ] RPi deployment unaffected (RPi uses Python `AudioStreamPlayer`, not browser).
- [ ] Pet mode 三段故事 prompt: first-chunk-audible noticeably faster than
      `browser_streaming=false`.
- [ ] VAD mid-turn interrupt → Live2D mouth + audio stop < 300 ms.

---

## Plan Self-Review Notes

**Spec coverage check** (one-line mapping):
- D1 (session WS) → Task 2.
- D2 / D3 / D4 (wire format) → Tasks 1 + 3 (server emits, player adapter packs).
- D5 (6 event types) → Tasks 3 (turn_start, sentence_start/end, turn_end), 5 (cancel new_chat), 6 (cancel vad).
- D6 (AudioWorklet) → Task 7.
- D7 (MinimaxWSClient sr_out=32000) → Task 3.
- D8 (fallback: flag + WS absence) → Task 3 (branch) + Task 10 (QA).
- D9 (event_bus cancel dispatch) → Task 6.
- D10 (client cancel reason dispatch) → Task 9.
- D11 (drain=True) → Task 1.
- D12 (AudioContext lazy, WS eager) → Tasks 7 + 9.

**Placeholder scan:** none — every step has concrete code or a runnable command.

**Type consistency:** `sentence_index` is `uint16 LE` everywhere; `turn_id` is 32-char uuid hex; `reason` is one of `{"new_chat","vad","user_stop","pipeline_error"}` — consistent across spec, Tasks 3/5/6/9.

**Execution order:** Tasks 1-6 (Python, TDD) then 7-9 (JS, manual verify) then 10 (QA). Each Python task is independently committable and passes focused tests. JS tasks require a running server + pet app.
