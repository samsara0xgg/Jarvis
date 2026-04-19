# Browser WebSocket Streaming TTS (PCM over Web Audio) — Design Spec

Direction A for the desktop pet (Live2D) audio path: forward MiniMax PCM chunks
from the server to the browser over a session-scoped WebSocket, play them via
an `AudioWorklet`-backed ring buffer. Replaces the current "synthesize full
`.wav` → HTTP GET" bottleneck added on top of the just-shipped MiniMax
WebSocket streaming backend (merged to main: `feat/minimax-ws-streaming`).

## Context

### Current pet-mode audio path (post MiniMax WS migration)

```
browser POST /api/chat
  └─ server on_sentence(text, emotion):
      tts.synth_to_file(text, emotion)
        → MinimaxWSClient opens ws, feeds text, collects all PCM until is_final
        → writes .pcm file
        → server wraps .pcm → .wav with header
      → returns /api/audio/<hash>.wav URL
  └─ SSE: event:sentence { text, emotion, audio_url }

browser AudioPlayer.enqueue(url):
  fetch(url) → arrayBuffer → decodeAudioData → AudioBufferSourceNode
    → gainNode → AnalyserNode (Live2D) → destination
```

### Measured problem

- MiniMax WS streaming reduces the server-side first-byte latency to ~250 ms
  (ref: `2026-04-18-minimax-ws-streaming-design.md`).
- The browser does **not** see that improvement. Every sentence still:
  1. Waits for `is_final` (~full sentence synth) before the `.wav` file exists.
  2. Pays an HTTP round trip for `GET /api/audio/<hash>.wav` — on localhost
     this is cheap (~5 ms) but adds another decode step in `decodeAudioData`
     (~20-50 ms for a 2-second WAV in V8).
  3. Blocks Live2D lip-sync from starting until the full sentence begins
     playing.
- Net: the browser pet-mode latency is effectively the whole-sentence TTS
  latency (~1.5-3 s for a medium sentence) plus file IO plus decode.

### Goal

Reach first-chunk-audible latency < 500 ms on localhost / LAN, parity with
the Python `AudioStreamPlayer` path that `jarvis.py --no-wake` already gets.
Keep a zero-behavior-change fallback.

### Non-goals (see "Out of Scope")

Browser-side VAD, multi-user concurrent streams, streaming to remote pet
clients over the public internet.

## Design Decisions

**D1 — Transport: one WebSocket per session, not per chat.**
Pet sessions are long-lived; chats serialize; the handshake cost is not
something we want to pay per chat. Client opens `/api/tts/stream?session_id=<id>`
immediately after `POST /api/session` returns. Server keeps
`{session_id → WebSocket}` with **last-writer-wins** semantics for reconnect /
tab-switch scenarios.

**D2 — Wire format: JSON text frames for control, binary frames for audio.**
Audio frames are the only high-volume message type; base64 inflation (+33 %)
and CPU for base64 are avoided. Control frames stay human-readable in the
DevTools WS panel.

**D3 — Audio payload: `int16LE @ 32 kHz mono`, raw MiniMax output.**
MiniMax already returns PCM in this format. Sending it raw eliminates the
soxr 32k→48k resample on the server (saves CPU + ~5-10 ms per chunk) and
lets the browser handle rate conversion natively via `AudioContext`'s
sample-rate setting.

**D4 — Binary frame shape: `uint16LE sentence_index (2 bytes) || int16LE PCM`.**
2-byte header allows 65 535 sentences per turn (practical: < 50). Alignment
is free — the header is 2 bytes, the first PCM sample starts at offset 2,
which is int16-aligned. Client parses with
`new Int16Array(buf, 2, (buf.byteLength - 2) / 2)` — zero copy.

**D5 — Six event types cover MVP + interrupt.**
`turn_start` / `sentence_start` / `audio_chunk` / `sentence_end` / `turn_end`
for the happy path; `cancel` (server → browser) for the three interrupt
sources: new chat received, user stop button, `interrupt_monitor` VAD
triggered `TTSPipeline.abort()`.

**D6 — Client-side playback via AudioWorklet + ring buffer.**
Avoids the `AudioBufferSourceNode` scheduling drift that would cause
~1 ms gaps between chunks (audible as a click on vowels). Worklet runs at
context sample rate; we set `new AudioContext({ sampleRate: 32000 })` so
the worklet reads 1:1 from the ring with no interpolation. Cancel = post
`{clear:true}` to worklet, reset read/write indices. Live2D's `AnalyserNode`
still attaches to the gain-node output — zero change to lip-sync code.

**D7 — Server reuses `MinimaxWSClient` with the resampler disabled.**
Construct with `sample_rate_out == sample_rate_in == 32000`; the existing
branch at `core/tts_minimax_ws.py:173` sets `self._resampler = None`.
`MinimaxWSClient.feed()` still yields `float32` arrays (just at 32 kHz),
which `BrowserWSPlayer.write()` converts back to `int16LE` before forwarding.
The round-trip float conversion wastes ~1 % CPU but keeps `MinimaxWSClient`'s
public interface untouched — shared with the Python player path.

**D8 — Fallback: config flag decides priority, both paths always live.**
`tts.browser_streaming: true` (new) makes streaming the preferred route.
`false` → 100 % legacy file path. When `true` **and** the WebSocket is not
open at chat-start or dies mid-turn, the server **auto-falls back to the
file path for that chat** (no config flip needed). This gives a graceful
degradation if a Live2D window crashes its WebSocket.

**D9 — Cancel has three server-triggered sources; wired via `JarvisApp.event_bus`.**
(1) New `POST /api/chat` arrives while previous turn is still streaming →
server sends `cancel{reason:"new_chat"}` for the previous `turn_id`, then
normal `turn_start` for the new one. (2) Browser emits a `user_stop` JSON
text frame on the WS (UI wiring deferred — protocol reserved). (3) Silero
VAD in `core/interrupt_monitor.py` fires `_on_voice_interrupt` →
`_cancel_current()` → `pipeline.abort()` in `jarvis.py`; immediately after
the existing `pipeline.abort()` call in `_cancel_current`, emit
`self.event_bus.emit("jarvis.tts_cancelled", {"reason":"vad"})`.
The web server subscribes to that event at startup and emits
`cancel{reason:"vad"}` on every live entry in its `active_chats` dict. This
piggybacks on the existing `EventBus` plumbing (`jarvis.py:104`) — no new
subscriber API. Without (3) the browser keeps playing residual ring-buffer
PCM while the user is already speaking, breaking the full-duplex illusion.

**D10 — Client cancel behavior differs by `reason`.**

| `reason` | Client action |
|---|---|
| `vad` | Most urgent. `player.clearAll()` + `live2d.stopTalking()` immediately. No transition animation. |
| `new_chat` | `player.clearAll()` + `live2d.stopTalking()`; allowed to play a short Live2D "turn" motion as a non-silent transition. |
| `user_stop` | Same as `new_chat`. |
| `pipeline_error` | `player.clearAll()` + `live2d.stopTalking()` + show a small toast ("TTS error, retry"). |

All four clear the audio buffer; the difference is purely in the Live2D
transition and UI affordance. A single `reason` field is enough — no extra
protocol bytes.

**D11 — `BrowserWSPlayer.drain()` returns `True` (optimistic).**
`TTSEngine._stream_to_player_async` at `core/tts.py:660-664` sets
`PlaybackResult.completed = bool(drained) and not abort_event.is_set()`. If
`drain()` returned `None` (the Python default), `bool(None) == False` →
`completed == False` → WP5 treats the sentence as unplayed → memory
re-injects it next turn. Because the buffer lives in the browser with no
server-observable drain signal, we return `True` unconditionally (only the
abort path sets `completed=False`, which is the correct semantics). The
rare edge case — WS dies *exactly* at `sentence_end` and the browser's ring
buffer never drained — is the same imprecision documented in the existing
`heard_response` bug, acceptable for v1. Upgrade path (if needed): require
a client `{"type":"ack", "sentence_index":N}` frame before `drain()`
returns.

**D12 — `AudioContext` is lazy; WebSocket is eager.**
Browser autoplay policy requires a user gesture to start `AudioContext`.
WS connects on session creation; incoming `audio_chunk` frames received
before first gesture are buffered in a JS queue (bounded 2 s worth ~ 128 KB
to prevent runaway memory if the user never clicks). First click on the
Live2D canvas calls `ctx.resume()` and drains the queue into the worklet.

## Architecture

### Before (current, file-mediated after MiniMax WS merge)

```
POST /api/chat
  └─ handle_text (exec)
       └─ on_sentence(text, emo):
            synth_to_file → MinimaxWSClient.open+feed+close
            → collects full PCM → writes .pcm → server wraps .wav
          return /api/audio/<hash>.wav
       └─ SSE: sentence {text, emotion, audio_url}

browser: AudioPlayer.enqueue(url)
  fetch → decodeAudioData → BufferSource → gain → analyser → dest
```

### After (streaming path; legacy path kept as fallback)

```
POST /api/session → session_id
WS  /api/tts/stream?session_id=<id>           (eager, last-writer-wins)

POST /api/chat (returns turn_id in JSON body)
  └─ server: prewarm MinimaxWSClient (sr_out=32000)
       send ws: {type:"turn_start", turn_id}
  └─ handle_text (exec)
       └─ on_sentence(text, emo, idx):
            if browser_streaming and ws_for_session:
                send ws: {type:"sentence_start", turn_id, idx, text, emo}
                tts_engine.stream_to_player(
                    text, emo,
                    BrowserWSPlayer(ws, idx, loop),
                    ws_client,         # reused MinimaxWSClient
                    abort_event,
                )
                send ws: {type:"sentence_end", turn_id, idx,
                          played_samples, subtitle_url}
            else:
                legacy synth_to_file → /api/audio/... URL
            SSE: sentence {turn_id, idx, text, emo, audio_url=""}
       send ws: {type:"turn_end", turn_id}

browser (PCMStreamPlayer):
  WS recv binary → Int16Array view @ offset 2 → Float32 / 32768
    → worklet.port.postMessage({pcm}, [buf])
  WS recv text JSON → onTurnStart / onSentenceStart / ... / onCancel
  AudioWorkletNode → gainNode → AnalyserNode (Live2D) → destination
```

## Component Design

### 1. `core/tts_minimax_ws.py` — unchanged

The existing `MinimaxWSClient` already supports `sample_rate_out == sample_rate_in`
(skips `soxr.ResampleStream` creation at line 173). Instantiate with
`sample_rate_out=32000` for the browser path.

### 2. `ui/web/browser_ws_player.py` — new, ~50 lines

```python
class BrowserWSPlayer:
    """Minimal Player API subset used by TTSEngine.stream_to_player,
    forwarding PCM chunks to a browser WebSocket in int16LE framing.

    Instances are single-sentence scoped. Created in on_sentence and
    thrown away after sentence_end. Not thread-safe across sentences
    — TTSPipeline serializes sentences.
    """
    def __init__(
        self,
        ws: WebSocket,
        sentence_index: int,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._ws = ws
        self._idx = sentence_index
        self._loop = loop
        self.played_samples: int = 0  # monotonic, matches AudioStreamPlayer API

    def write(self, pcm_f32: np.ndarray) -> None:
        """Called from MinimaxWSClient.feed() loop per chunk.
        Runs on the TTSEngine's private asyncio loop thread."""
        pcm_i16 = (np.clip(pcm_f32, -1.0, 1.0) * 32767.0).astype("<i2")
        payload = struct.pack("<H", self._idx) + pcm_i16.tobytes()
        asyncio.run_coroutine_threadsafe(
            self._ws.send_bytes(payload), self._loop,
        )
        self.played_samples += pcm_i16.size

    def drain(self, timeout: float = 5.0) -> bool:
        # Buffer lives in the browser. There's no server-side audio to wait
        # on. Return True (optimistic) so TTSEngine._stream_to_player_async
        # sets PlaybackResult.completed=True and WP5 records this sentence
        # in played_texts. Returning None/False would mark the sentence as
        # unplayed → memory would re-inject it on the next turn.
        #
        # Edge case: if the browser WS drops *exactly* at sentence_end, the
        # last sentence gets recorded as played even though some trailing
        # audio may not have reached the speaker. Acceptable for v1 — we
        # already document the same imprecision in the heard_response bug.
        # Future upgrade path: require a client-side ACK frame keyed on
        # sentence_index before returning True.
        return True
```

Interface contract: matches the subset of `AudioStreamPlayer` used by
`TTSEngine._stream_to_player_async` (verified: `write` + `played_samples`).

### 3. `ui/web/server.py` — ~60 lines modified

**New WebSocket endpoint**
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
        try: await old.close(code=1001, reason="superseded")
        except Exception: pass
    try:
        while True:
            # Currently server-push only; recv to detect client close.
            # Future: accept {type:"user_stop"} client frames here.
            msg = await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _ws_routes_lock:
            if _ws_routes.get(session_id) is ws:
                _ws_routes.pop(session_id, None)
```

**New `/api/chat` integration** (diff from current at `ui/web/server.py:143`)
- Generate `turn_id = uuid.uuid4().hex` at entry, include in returned JSON
  body and in SSE `sentence` events.
- Resolve `ws = _ws_routes.get(session_id)` once at entry.
- If streaming active for this chat: construct a per-chat
  `MinimaxWSClient(base_url, key, model, voice, volume, sample_rate_out=32000,
  sample_rate_in=32000)` and call `await ws_client.open_session(emotion=None)`
  in a prewarm task spawned immediately (overlaps handshake with LLM first
  tokens — same pattern as `TTSPipeline.prewarm`). Emotion remains `None` for
  the session; per-sentence emotion gets applied inside `MinimaxWSClient.feed`
  via the task_continue event. After `turn_end`, close the client and release.
- Per-chat `abort_event = threading.Event()` held in `active_chats[session_id]
  = {"turn_id":..., "abort_event":..., "ws_client":...}` so VAD/new-chat paths
  can set it and look up the right WS to send `cancel` on.
- Send `turn_start` frame if `ws` present and `browser_streaming=True`.
- New callback registered on `JarvisApp` for VAD interrupts — web server
  subscribes at startup to receive `reason` strings and emits `cancel` frames
  on every entry in `active_chats`.

**Modified `on_sentence`** (current: `ui/web/server.py:153`)
```python
def on_sentence(sentence: str, emotion: str = "") -> None:
    nonlocal sentence_index
    idx = sentence_index
    sentence_index += 1

    use_stream = (
        cfg.tts.browser_streaming
        and ws is not None
        and _ws_routes.get(session_id) is ws  # still the live one
        and ws_client is not None              # prewarm succeeded
    )

    audio_url = ""
    if use_stream:
        asyncio.run_coroutine_threadsafe(
            _send_ctrl(ws, {"type":"sentence_start", "turn_id":turn_id,
                             "sentence_index":idx, "text":sentence,
                             "emotion":emotion or "neutral"}),
            loop,
        )
        player = BrowserWSPlayer(ws, idx, loop)
        result = tts_engine.stream_to_player(
            sentence, emotion, player, ws_client, abort_event,
        )
        asyncio.run_coroutine_threadsafe(
            _send_ctrl(ws, {"type":"sentence_end", "turn_id":turn_id,
                             "sentence_index":idx,
                             "played_samples":player.played_samples,
                             "subtitle_url":result.subtitle_url}),
            loop,
        )
    else:
        # Existing file path — unchanged from current server.py:158-179
        ...

    event = {"turn_id": turn_id, "index": idx, "text": sentence,
             "emotion": emotion.lower() if emotion else "neutral",
             "audio_url": audio_url}
    asyncio.run_coroutine_threadsafe(queue.put(event), loop)
```

**VAD cancel wiring uses the existing `EventBus`** — no new callback API.

```python
# jarvis.py — _cancel_current (existing method), after pipeline.abort():
self.event_bus.emit("jarvis.tts_cancelled", {"reason": "vad"})
```

Web server at startup subscribes:
```python
jarvis_app.event_bus.on("jarvis.tts_cancelled", self._on_vad_cancel)

# Server handler:
def _on_vad_cancel(self, payload: dict) -> None:
    reason = payload.get("reason", "vad")
    for sid, chat in list(self._active_chats.items()):
        ws = self._ws_routes.get(sid)
        if ws is None:
            continue
        asyncio.run_coroutine_threadsafe(
            _send_ctrl(ws, {"type":"cancel", "turn_id":chat["turn_id"],
                             "reason":reason}),
            self._loop,
        )
        chat["abort_event"].set()  # ensure stream_to_player exits
```

Single-user deployment means at most one entry in `_active_chats`. If
multi-user arrives later (Phase 3-4 roadmap), the payload can include a
`session_id` to scope the fan-out — protocol is already prepared to carry it.

### 4. `ui/web/js/core/audio/pcm-player-processor.js` — new, ~40 lines

```js
class PCMPlayerProcessor extends AudioWorkletProcessor {
    constructor() {
        super();
        this._ring = new Float32Array(32000 * 10); // 10 s buffer
        this._read = 0;
        this._write = 0;
        this.port.onmessage = (e) => {
            if (e.data.clear) { this._read = this._write = 0; return; }
            const chunk = e.data.pcm;
            for (let i = 0; i < chunk.length; i++) {
                this._ring[this._write] = chunk[i];
                this._write = (this._write + 1) % this._ring.length;
                // Overflow protection: if write catches read, drop one
                if (this._write === this._read) {
                    this._read = (this._read + 1) % this._ring.length;
                }
            }
        };
    }
    process(_inputs, outputs) {
        const out = outputs[0][0];
        for (let i = 0; i < out.length; i++) {
            if (this._read === this._write) { out[i] = 0; continue; }
            out[i] = this._ring[this._read];
            this._read = (this._read + 1) % this._ring.length;
        }
        return true;
    }
}
registerProcessor('pcm-player', PCMPlayerProcessor);
```

### 5. `ui/web/js/core/audio/pcm-stream-player.js` — new, ~80 lines

```js
export class PCMStreamPlayer {
    async init() {
        this.ctx = new AudioContext({ sampleRate: 32000 });
        await this.ctx.audioWorklet.addModule(
            'js/core/audio/pcm-player-processor.js',
        );
        this.node = new AudioWorkletNode(this.ctx, 'pcm-player');
        this.gainNode = this.ctx.createGain();
        this.analyser = this.ctx.createAnalyser();
        this.analyser.fftSize = 256;
        this.node.connect(this.gainNode);
        this.gainNode.connect(this.analyser);
        this.analyser.connect(this.ctx.destination);
        this._preCtxQueue = [];   // pre-user-gesture buffer
    }
    getAnalyser() { return this.analyser; }

    async resumeOnGesture() {
        if (this.ctx.state === 'suspended') await this.ctx.resume();
        while (this._preCtxQueue.length) {
            this._pushToWorklet(this._preCtxQueue.shift());
        }
    }

    writeChunk(arrayBuf) {
        const i16 = new Int16Array(arrayBuf, 2);   // skip 2-byte header
        const f32 = new Float32Array(i16.length);
        for (let j = 0; j < i16.length; j++) f32[j] = i16[j] / 32768;
        if (this.ctx.state === 'suspended') {
            // bound pre-ctx queue at ~2 s of audio
            if (this._preCtxQueue.reduce((n,a)=>n+a.length,0) < 64000) {
                this._preCtxQueue.push(f32);
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
        this.node.port.postMessage({ clear: true });
    }
}
```

### 6. `ui/web/js/core/api-client.js` — ~100 lines added

Add `TTSStreamClient` — WebSocket management, binary / text frame split,
reconnect on close, callback surface:
```js
client.onTurnStart = (turn_id) => {...}
client.onSentenceStart = ({turn_id, sentence_index, text, emotion}) => {...}
client.onAudioChunk = (arrayBuf) => player.writeChunk(arrayBuf)
client.onSentenceEnd = ({sentence_index, played_samples}) => {...}
client.onTurnEnd = (turn_id) => {...}
client.onCancel = ({turn_id, reason}) => {
    // All reasons: clear audio buffer + stop Live2D lip sync.
    player.clearAll();
    live2d.stopTalking();
    // Reason-specific polish (see D10):
    if (reason === 'new_chat' || reason === 'user_stop') {
        live2d.triggerEmotionAction('neutral');   // short "turn" motion
    } else if (reason === 'pipeline_error') {
        uiController.showToast('TTS error, retrying');
    }
    // reason === 'vad' → no extra polish; fastest possible stop.
}
```

Opened after `/api/session` success. `onclose` → exponential backoff reconnect
(1 s, 2 s, 5 s capped). Connection state exposed on `client.wsConnected`.

### 7. `ui/web/js/ui/controller.js` & `live2d.js` — minimal wiring

`controller.js`:
- First Live2D click → `player.resumeOnGesture()`.
- On `sentence_start` callback → `live2d.triggerEmotionAction(emotion)` + `live2d.startTalking()`.
- On `turn_end` or `cancel` → `live2d.stopTalking()`.

`live2d.js` — **zero change**. `connectToAudioPlayer` reads
`window.chatApp.audioPlayer.getAnalyser()`. Both `AudioPlayer` (legacy) and
`PCMStreamPlayer` (new) expose `getAnalyser()`. The chat app picks which
player instance to expose based on whether streaming is active this session.

### 8. `config.yaml` — 1 key added

```yaml
tts:
  browser_streaming: true   # prefer PCM-over-WS to browser when a WS is open;
                             # false = always use file /api/audio/<hash>.wav
```

## Wire Protocol (canonical reference)

**Connection**
```
WebSocket URL:  /api/tts/stream?session_id=<session_id>
Open timing:    Client-initiated, eager on POST /api/session success
Replacement:    Server last-writer-wins per session_id; old WS closed code=1001
```

**Control frames (text, JSON UTF-8)**
```
{ "type":"turn_start",     "turn_id":"<hex32>" }
{ "type":"sentence_start", "turn_id":"...", "sentence_index":<uint16>,
                           "text":"...", "emotion":"<label>" }
{ "type":"sentence_end",   "turn_id":"...", "sentence_index":<uint16>,
                           "played_samples":<int>, "subtitle_url":null|"..." }
{ "type":"turn_end",       "turn_id":"..." }
{ "type":"cancel",         "turn_id":"...",
                           "reason":"new_chat"|"vad"|"user_stop"|"pipeline_error" }
```

**Data frames (binary)**
```
offset 0, 2 bytes : uint16 little-endian sentence_index
offset 2, N bytes : int16 little-endian PCM, 32 kHz mono
Typical N:          ~6400-12800 (100-200 ms of audio)
```

## File Changes

**New**
- `ui/web/browser_ws_player.py` (~50 lines)
- `ui/web/js/core/audio/pcm-player-processor.js` (~40 lines, AudioWorklet)
- `ui/web/js/core/audio/pcm-stream-player.js` (~80 lines)
- `tests/test_browser_ws_stream.py` (~200 lines, 7 cases)

**Modified**
- `ui/web/server.py` (+60 / -5, add WS endpoint + on_sentence branch + VAD hook)
- `ui/web/js/core/api-client.js` (+100, TTSStreamClient class)
- `ui/web/js/ui/controller.js` (+30, wiring player lifecycle & cancel)
- `config.yaml` (+1 key)
- `jarvis.py` (+10, `on_turn_aborted` subscriber API)
- `jarvis.py` — inside `_cancel_current` (the VAD cancel path, reached from
  `_on_voice_interrupt` at `jarvis.py:1266`), after the existing
  `pipeline.abort()` call, emit
  `self.event_bus.emit("jarvis.tts_cancelled", {"reason":"vad"})`. (+1 line.)

**Unchanged**
- `core/tts_minimax_ws.py` — already supports `sr_out==sr_in`.
- `core/tts.py` — `stream_to_player` and `PlaybackResult` untouched.
- `core/audio_stream_player.py` — Python player path unchanged.
- `ui/web/js/live2d/live2d.js` — still reads `audioPlayer.getAnalyser()`.

## Commit Sequence (preview — `writing-plans` will detail TDD tasks)

1. **Add `BrowserWSPlayer` + unit tests.** Pure class; mock a WebSocket,
   assert header + int16 bytes are correct.
2. **Add `/api/tts/stream` endpoint + session routing.** Tests: open, accept,
   last-writer-wins replacement, disconnect cleanup.
3. **Add `browser_streaming` config + `on_sentence` streaming branch.** Tests:
   flag off → file path; flag on + no ws → file path; flag on + ws → streaming
   path with proper turn/sentence frames.
4. **Add `on_turn_aborted` hook on `JarvisApp` + wire VAD interrupt source.**
   Tests: abort callback fires with `reason="vad"`; server emits `cancel` frame.
5. **Add `PCMPlayerProcessor` AudioWorklet.** Headless audio test harness
   if feasible; otherwise manual verification check-in.
6. **Add `PCMStreamPlayer`.** Unit tests (jest-style in a browser test env if
   available; else deferred to manual).
7. **Add `TTSStreamClient` + wire `chatApp` to select player & forward frames.**
   Manual smoke: `python jarvis.py --no-wake`, pet mode, 三段故事 prompt.
8. **Pet-mode integration polish + fallback QA.** Flip `browser_streaming=false`,
   confirm parity with pre-change behavior; kill WS mid-turn, confirm fallback.

## Test Matrix

| # | Area | Case | Expected |
|---|---|---|---|
| 1 | BrowserWSPlayer | `write(np.ones(4, f32))` | ws.send_bytes called once with `b'\x00\x00' + 4 * int16LE(32767)` |
| 2 | BrowserWSPlayer | `played_samples` post-write | increments by sample count |
| 2b | BrowserWSPlayer | `drain(5.0)` | returns `True` (so PlaybackResult.completed is `True`) |
| 3 | WS routing | two WS opens same session_id | first closes with 1001, second stays |
| 4 | WS routing | WS disconnect | dict entry removed |
| 5 | on_sentence branch | `browser_streaming=false` + ws open | file path used, `audio_url != ""` |
| 6 | on_sentence branch | `browser_streaming=true` + no ws | file path, `audio_url != ""` |
| 7 | on_sentence branch | `browser_streaming=true` + ws | streaming path, `audio_url == ""`, 3 frames (sentence_start / N audio / sentence_end) sent |
| 8 | Cancel new_chat | second POST /api/chat during active turn | old turn gets `cancel{reason:"new_chat"}` |
| 9 | Cancel vad | fake VAD trigger via `_notify_abort("vad")` | ws receives `cancel{reason:"vad"}` |
| 10 | Fallback mid-turn | close ws during streaming sentence | `abort_event.set()`, rest of turn not synthesized |
| 11 | Prewarm | POST /api/chat | MinimaxWSClient opened before on_sentence fires (via `pipeline.prewarm`) |
| 12 | Manual E2E | pet mode + "三段故事" with streaming on vs off | first-chunk-audible measurable difference (~1-2 s less) |
| 13 | Manual E2E | mic VAD interrupt mid-story | Live2D mouth and audio stop within 300 ms |

Tests 1-11 are Python pytest (`tests/test_browser_ws_stream.py`); 12-13 are
manual (documented in the PR body). Tests 5-7 use a fake `WebSocket`
object and bypass the real MiniMax call via a stub `ws_client`.

## Risk & Rollback

**Risk 1 — AudioContext creation with `sampleRate: 32000` not supported on
target platform.** Electron 30+ (our shipping target) supports it. If a future
browser target rejects, fallback is easy: remove the option, let context run at
default 48 k, add linear interp inside the worklet. Scoped change (~15 lines).

**Risk 2 — Worklet module URL resolution.** The `addModule()` path is relative
to the current document URL. `ui/web/` is served at root by `StaticFiles`
mount; `js/core/audio/pcm-player-processor.js` is the relative path from root.
Verify in commit 5 with a console log.

**Risk 3 — Binary frame ordering vs sentence_start.** WebSocket preserves frame
order within a single connection. Client must handle the case where an
`audio_chunk` arrives for `sentence_index N` before the `sentence_start`
control frame for N (shouldn't happen due to server-side ordering, but if it
does the client should buffer the chunk and apply when sentence_start arrives).
Mitigation: server sends `sentence_start` strictly before the first `write()`
call on `BrowserWSPlayer`.

**Rollback**: flip `tts.browser_streaming: false` in `config.yaml` and
restart server — 100 % back to file path. No data migration, no client change
required.

## Deployment Checklist

- [ ] Confirm Electron (desktop pet) runs `AudioContext({sampleRate:32000})`.
- [ ] `config.yaml` change applied on dev Mac; RPi deployment not affected
      (RPi doesn't run the browser pet; uses Python `AudioStreamPlayer`).
- [ ] Verify MiniMax international key is unchanged — same key as
      `feat/minimax-ws-streaming` branch shipped.
- [ ] Smoke: `python jarvis.py --no-wake` + Electron pet → 三段故事 prompt,
      compare first-chunk wall-clock to `browser_streaming:false`.
- [ ] Smoke: mid-turn mic VAD interrupt → Live2D mouth and audio stop < 300 ms.
- [ ] Log inspection: no base64 / decodeAudioData calls in the network panel
      during a successful streaming session.

## Out of Scope (deferred)

- **Browser-side VAD / client-initiated interrupt.** Protocol reserves
  `user_stop` reason but client emission not wired.
- **Multi-user concurrent sessions on one server.** Today's jarvis runs
  single-user; last-writer-wins per session_id is fine.
- **Subtitle display from `sentence_end.played_samples` + `subtitle_url`.**
  Informational metadata forwarded now; no UI consumes it yet.
- **Remote pet clients over public internet.** Would need WSS, auth on WS
  open, rate limiting.
- **Browser audio device selection.** Uses default output device.
