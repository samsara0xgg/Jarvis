# MiniMax WebSocket Streaming TTS — Design Spec

**Date**: 2026-04-18
**Branch**: `feat/minimax-ws-streaming`
**Goal**: Cut TTS first-sentence latency from ~2.5-3s to ~250ms and per-sentence latency on multi-sentence responses from ~1.1s to ~50ms, by switching MiniMax TTS from HTTP `api.minimax.chat` to WebSocket streaming on `api-uw.minimax.io`.

## Context

### Current pipeline

`core/tts.py` `_synth_minimax` posts to `https://api.minimax.chat/v1/t2a_v2`, waits for the full MP3 response, writes it to a temp file, returns `(path, deletable)`. `_play_audio_file` decodes the MP3 via miniaudio, resamples with soxr, pushes PCM to `AudioStreamPlayer`. `TTSPipeline` runs synthesis and playback on two threads, decoupled by an `audio_queue` of file paths.

### Measured problems

Empirical latency (3 runs each, Canada → MiniMax):

| Endpoint | TCP | TLS | TTFB |
|---|---|---|---|
| `api.minimax.chat` (domestic) | 265ms | 803ms | **1149ms** |
| `api.minimax.io` (international main) | 85ms | 260ms | 347ms |
| `api-uw.minimax.io` (US West) | 47ms | 150ms | **200ms** |

With HTTP non-streaming, the current domestic endpoint produces first-audio-playing latency of ~2.5-3s (TTFB + await entire MP3 body + decode + push). `api-uw` + WebSocket streaming produces first-audio-playing latency of ~450ms (verified in `scripts/test_minimax_ws.py`, now deleted).

### Validated by prior-art probe

Standalone probe `scripts/test_minimax_ws.py` (deleted after validation) confirmed:

| Configuration | First chunk | Total bytes |
|---|---|---|
| baseline mp3 no emotion | 474ms | 94 KB |
| emotion=happy mp3 | 952ms | 91 KB |
| **baseline pcm no emotion** | **452ms** | 377 KB |
| emotion=happy pcm | 1071ms | 397 KB |

`emotion` field is accepted (though undocumented); `pcm` format is accepted; enabling emotion adds ~500ms server-side tax. These findings are load-bearing for the design below.

## Design Decisions

| # | Decision | Value |
|---|---|---|
| D1 | HTTP base URL | `https://api-uw.minimax.io` (config `tts.minimax_base_url`) |
| D2 | WebSocket endpoint | `wss://api-uw.minimax.io/ws/v1/t2a_v2` |
| D3 | Default model | `speech-2.8-turbo` (upgrade from `speech-02-turbo`) |
| D4 | Default voice | `Chinese (Mandarin)_ExplorativeGirl` (unchanged; available on international platform) |
| D5 | Audio format | `pcm` 32 kHz mono 16-bit (bypass mp3 decoder; native PCM push to AudioStreamPlayer) |
| D6 | WS session granularity | **Turn-level**: one WS per LLM turn, multiple `task_continue` calls inside |
| D7 | Prewarm trigger | Open WS as soon as LLM emits first token; auto-close after 2s idle |
| D8 | Emotion strategy | Skip `emotion` field for NEUTRAL/EMO_UNKNOWN (save 500ms tax); send for other emotions |
| D9 | Fallback ladder | ws → internal HTTP MiniMax → edge-tts → pyttsx3 |
| D10 | WP5 semantic (played_texts under streaming) | **Truncated text with punctuation snap**, three-level degradation L1→L2→L3 |

## Architecture

### Before (HTTP, file-mediated)

```
LLM stream → sentence split → TTSPipeline.submit
                                        │
           ┌────────────────────────────┤
           │ synth thread               │
           │   _synth_minimax()         │
           │   HTTP POST → MP3 bytes    │
           │   write /tmp/xxx.mp3       │
           │   return (path, deletable) │
           └────────────────────────────┤
                                        │
                                  audio_queue (paths)
                                        │
           ┌────────────────────────────┤
           │ play thread                │
           │   miniaudio.decode_file    │
           │   soxr resample            │
           │   player.write(pcm)        │
           │   player.drain()           │
           └────────────────────────────┘
```

### After (WebSocket streaming, PCM direct)

```
LLM first token → pipeline.prewarm(emotion) → MinimaxWSClient connect
                                                         │
sentence 1 split → submit → task_continue                │
                                 ↓                       │
                              chunks (hex pcm) ──────────┤
                                                         │
                                              ┌──────────┤
                                              │ stream_to_player
                                              │   hex → int16 → f32
                                              │   soxr.ResampleStream
                                              │   player.write(chunk)
                                              │   on is_final: mark played
                                              └──────────┤
                                                         │
sentence 2 split → submit → task_continue (same WS) ─────┤
                                 ↓                       │
                              chunks ─────────────────────┘
                                                         │
turn finish → task_finish → ws close
```

Traditional engines (edge-tts / pyttsx3 / azure / openai_tts / minimax HTTP fallback) continue using the file-mediated path unchanged.

## Component Design

### 1. `MinimaxWSClient` (new, `core/tts_minimax_ws.py`)

**Purpose**: Encapsulate MiniMax T2A WebSocket protocol with turn-level session management, prewarm, subtitle fetch, chunk alignment, and streaming resample.

**Public API**:
```python
class MinimaxWSClient:
    def __init__(self, base_url: str, api_key: str, model: str, voice_id: str,
                 volume: int, sample_rate_out: int = 48000, logger=LOGGER): ...

    async def open_session(self, emotion: str | None, audio_settings: dict) -> None:
        """Connect + task_start. Emotion=None means don't send field (saves 500ms)."""

    async def feed(self, text: str) -> AsyncIterator[bytes]:
        """Send task_continue, yield resampled float32 PCM chunks until is_final
        for THIS text. Does NOT close the session."""

    async def close_session(self) -> dict:
        """task_finish + ws.close. Return metadata (subtitle_url if any).
        Idempotent — safe to call twice."""

    def is_open(self) -> bool: ...

    @property
    def last_subtitle_url(self) -> str | None: ...
```

**Internals**:
- `_conn`: `websockets.WebSocketClientProtocol`
- `_resampler`: `soxr.ResampleStream` persistent across chunks (prevent boundary clicks)
- `_chunk_buffer`: bytearray, absorbs odd-byte chunks (hex decode to even-byte boundary)
- `_idle_timer`: asyncio task that closes session after 2s without activity (prewarm safety)
- `_session_id`, `_trace_id`: for log correlation with MiniMax server logs

**Error handling**:
- WS connect timeout: 3s → raise `WSConnectError`
- `task_start` response status != 0: raise `WSProtocolError(base_resp)`
- First chunk timeout after task_continue: 3s → raise `WSChunkTimeout`
- Between-chunk timeout: 5s → raise `WSChunkTimeout`
- Any ws error during active feed() propagates; caller decides fallback

### 2. `AudioStreamPlayer` additions (`core/audio_stream_player.py`)

Two additions to support streaming synthesis:

```python
def drain(self, abort_event: threading.Event | None = None, timeout: float = 2.0) -> None:
    """Block until ring buffer is empty, abort_event is set, or timeout elapses.
    Returns normally on the first two; raises TimeoutError on the third.
    Checking abort_event at the same granularity as existing drain() polling (~10ms)."""

@property
def played_samples(self) -> int:
    """Monotonic count of samples that have crossed the output stream.
    Used to compute fraction-played for WP5 truncation.
    Updated in the sounddevice callback; read-only externally."""
```

`played_samples` is updated atomically inside the sounddevice callback. Exposed as read-only to external code.

### 3. `TTSEngine` changes (`core/tts.py`)

**New method** on TTSEngine (enables streaming contract):

```python
SUPPORTS_STREAMING = {"minimax"}  # engines that have a stream_to_player method

def stream_to_player(self, text: str, emotion: str,
                     player: AudioStreamPlayer,
                     ws_client: MinimaxWSClient,
                     abort_event: threading.Event) -> PlaybackResult:
    """Stream text via ws_client → player. Returns PlaybackResult with:
       - completed: bool  (True iff is_final received AND drain() finished)
       - played_samples: int
       - total_samples: int | None  (known from is_final metadata, or None)
       - subtitle_url: str | None
       - raised: Exception | None
    Raises nothing; all errors surfaced via PlaybackResult.raised."""
```

**Modified**: `_synth_minimax` — internal HTTP → internal WS collect-then-file (commit 3); URL from config.

**Modified** (commit 5): emotion skipped when NEUTRAL / EMO_UNKNOWN; cache key normalizes skipped emotion to `""` to prevent fragmentation.

### 4. `TTSPipeline` changes (`core/tts.py`)

**New method**:

```python
def prewarm(self, emotion: str) -> None:
    """Open a WS session eagerly (called on LLM first token).
    No-op if engine is not a streaming engine. Safe to call multiple times."""
```

**Routing fork** in `_tts_worker`:

```python
if self._engine.engine_name in SUPPORTS_STREAMING and self._ws_client:
    # Streaming path: synth_thread streams directly to player, no audio_queue
    result = self._engine.stream_to_player(
        text, emotion, self._player, self._ws_client, self._aborted,
    )
    self._record_playback(text, result)  # populates played_texts / truncated version
else:
    # Legacy path: unchanged
    result = self._synthesize_to_file(text, emotion)
    if result:
        self._audio_queue.put((path, sentence_type, deletable, text))
```

The legacy `_play_worker` is untouched — it handles file-based `audio_queue` entries. In streaming path, `_play_worker` sees no items; at `finish()` the synth_worker still propagates `_SENTINEL` into `audio_queue` so `_play_worker` exits cleanly.

**Modified `abort()`**:
```python
def abort(self) -> list[str]:
    self._aborted.set()
    if self._ws_client and self._ws_client.is_open():
        asyncio.run_coroutine_threadsafe(self._ws_client.close_session(), ...)
    self._engine.stop()  # flush player (existing)
    # ... rest unchanged, but unplayed list uses TRUNCATED text per WP5
```

### 5. WP5 Played-Texts Truncation Logic

Three-level degradation strategy. At abort time, for the currently-playing sentence:

**L1: Subtitle-based precision**
- Condition: `subtitle_url` was received AND fetch succeeds within 500ms
- Mechanism: GET subtitle JSON → parse `[{start_ms, end_ms, text}]` → duration = `end_ms - start_ms` → `fraction = played_ms / duration` where `played_ms = (played_samples - sentence_start_samples) / sample_rate * 1000`
- Snap: truncate at index `k = int(len(text) * fraction)`, then scan `text[k : k + int(len(text) * 0.2)]` for the nearest 。！？，、 or space; if found, truncate after that punctuation; if none in window, keep `text[:k]` as-is

**L2: Ring buffer estimate**
- Condition: L1 fails (no subtitle / fetch timeout / subtitle URL not in response)
- Mechanism: `total_samples_estimate = total_hex_bytes_received / 2` (16-bit PCM) → `fraction = played_samples / total_samples_estimate`; snap as L1
- Caveat: if is_final not yet received, `total_samples_estimate` understates true total. Accept the slight over-count.

**L3: Strict fallback**
- Condition: L1 and L2 both unavailable (abort fires before any chunk played OR player unavailable)
- Mechanism: mark as unplayed (full text goes to `abort()` return list)

All three paths normalize to `played_texts: list[str]` externally — the truncated string IS the "heard" text. A separate `played_progress: dict[text, float]` is stored internally for optional future use (memory injection enhancement), but not exposed in this PR.

## File Changes

**New files**:
- `core/tts_minimax_ws.py` — MinimaxWSClient (~300 lines)
- `tests/test_tts_minimax_ws.py` — unit tests (~400 lines)
- `docs/superpowers/specs/2026-04-18-minimax-ws-streaming-design.md` — this doc

**Modified files**:
- `core/tts.py` — `TTSEngine.stream_to_player`, `_synth_minimax` body, cache key; `TTSPipeline.prewarm`, `_tts_worker` fork, `abort`, `_record_playback` with truncation
- `core/audio_stream_player.py` — `drain(abort_event, timeout)`, `played_samples` property
- `config.yaml` — `tts.minimax_base_url`, `tts.minimax_ws`, `tts.minimax_prewarm`; default model → `speech-2.8-turbo`; default endpoint → `api-uw.minimax.io`
- `jarvis.py` — 1 line: `self._tts_pipeline.prewarm(emotion)` at LLM streaming start (in `_process_turn` or equivalent)
- `core/health.py` — if a MiniMax probe exists that hardcodes URL, update to use the new configured `base_url`; behavior unchanged otherwise. Implementation plan will verify existence before modifying.

**Untouched** (guaranteed by design):
- `core/interrupt_monitor.py` — calls `pipeline.abort()` (same API)
- `core/personality.py`, `core/intent_router.py`, `core/llm.py` — no TTS coupling
- `memory/*`, `skills/*`, `devices/*`, `auth/*` — no TTS coupling
- All skill implementations calling `tts.speak()` directly — continue through legacy file path

## Commit Sequence

| # | Commit | Scope | Independently revertable |
|---|---|---|---|
| 1 | `refactor(tts): extract minimax_base_url to config` | `_minimax_url` hardcode → `config.yaml` field (default keeps `.chat`); `core/tts.py` + `config.yaml` only | Yes |
| 2 | `feat(tts): switch minimax default to api-uw international + speech-2.8-turbo` | `config.yaml` defaults change; `core/health.py` probe path updated | Yes |
| 3 | `feat(tts): replace minimax http with websocket (collect-then-file)` | `_synth_minimax` uses WS internally, collects all chunks, writes file, returns `(path, deletable)`. Public API unchanged. | Yes (revert keeps `.io` HTTP baseline) |
| 4 | `feat(tts): add MinimaxWSClient with turn-level session + prewarm` | New `core/tts_minimax_ws.py`; streaming soxr; chunk alignment; subtitle fetch; idle-close | Yes (file not yet wired in) |
| 5 | `feat(tts): add streaming contract + pipeline streaming route + prewarm + emotion skip` | `TTSEngine.stream_to_player` + `SUPPORTS_STREAMING`; `TTSPipeline.prewarm` + worker fork; `jarvis.py` prewarm hook; emotion skip for NEUTRAL/EMO_UNKNOWN + cache key normalization to prevent fragmentation | Depends on 4 |
| 6 | `feat(tts): ws abort + WP5 truncated played_texts (L1/L2/L3)` | `abort()` closes ws; `AudioStreamPlayer.drain(abort_event)` + `played_samples`; `_record_playback` with truncation + punctuation snap | Depends on 5 |
| 7 | `test(tts): ws integration + abort race + truncation + soxr state` | 13 new test cases; `tests/test_tts_minimax_ws.py` | Independent |

**Partial shipping**: commits 1–3 alone deliver ~700ms first-sentence improvement with no architectural change. If commits 4–7 hit unexpected trouble, merge only 1–3 and defer B.

## Test Matrix

All new tests live in `tests/test_tts_minimax_ws.py` (uses `pytest-asyncio`, mocks `websockets` with `aiohttp`-free custom mock).

1. **WS basic handshake** — mock ws accepts `task_start`, responds `task_started`; client transitions to open state
2. **Emotion skip vs send** — `emotion=""` / `"NEUTRAL"` / `"EMO_UNKNOWN"` skip field; `emotion="HAPPY"` sends `{"emotion": "happy"}`; verify outgoing JSON
3. **Abort mid-stream** — mock server sends 3 chunks, client aborts → ws.close called, player.flush called, all within 100ms of abort trigger
4. **Abort mid-drain** — mock sends is_final, client in `drain(abort_event)` → abort event causes drain to return NORMALLY (no exception); total abort-to-return < 150ms
5. **WP5 L1 truncation** — mock subtitle JSON `[{start:0, end:3000, text:"今天天气真好，我们出去散步吧"}]`, player played_samples correspond to 1800ms → expect "今天天气真好，我们" (snapped to comma at char 9)
6. **WP5 L2 fallback** — subtitle unavailable, chunks total 100KB, played 60KB → fraction 0.6 → expected truncation matches
7. **WP5 L3 fallback** — abort before any chunk played → sentence in unplayed list, not in played_texts
8. **Chunk odd-byte + soxr state** — mock sends 4095 and 4097 hex chunks interleaved; verify buffer re-aligns; compare final PCM MD5 against reference from file-mode synth
9. **Turn-level session reuse** — 3 consecutive `task_continue` calls on one ws → each sentence's first-chunk latency < 200ms (except the first, which includes handshake)
10. **Prewarm** — call `pipeline.prewarm("HAPPY")` → verify WS opens immediately; subsequent `submit()` finds session already open; first `task_start` sent without additional handshake wait
11. **Fallback chain** — mock ws unreachable → HTTP path taken; mock HTTP 2049 → edge-tts path taken; mock edge-tts exception → pyttsx3 path taken
12. **Cache hit bypass** — short text (≤50 chars) cached from prior call → new call returns cached `.pcm` file without opening ws
13. **Subtitle fetch failure** — mock subtitle URL returns 500 → L2 fallback triggered, truncation still computed from ring buffer

**Regression**:
- Run full `pytest tests/ -q` after each commit
- Run `system_tests/runner.py --suite general` after commit 6

## Risk & Rollback

| Risk | Likelihood | Impact | Mitigation | Rollback |
|---|---|---|---|---|
| `subtitle_enable` not supported on WS endpoint | Medium | Medium (WP5 L1 unavailable) | Auto-fall to L2; log once per session | None needed |
| `soxr.ResampleStream` API missing in installed soxr version | Low | Medium (audio boundary clicks) | Pre-check in commit 4; fall back to overlap-save hand-rolled buffer | `git revert 4-7`; use commit 3 (collect-then-file decode via miniaudio) |
| Prewarm WS opens on LLM that returns only tool calls (no TTS) | Medium | Low (wasted ws, ~minor) | 2s idle close; log count | Disable via `config.yaml: minimax_prewarm: false` |
| MiniMax rate limit on rapid task_continue | Unknown | Medium | Exponential backoff on 429; fallback to HTTP path | Disable WS: `config.yaml: minimax_ws: false` |
| RPi does not have international API key provisioned | High initially | High (TTS broken) | Deploy checklist enforces `.env` sync | Fallback to edge-tts logs warning loudly |
| Truncation + punctuation snap produces weird mid-word English cut | Low (mostly Chinese) | Low | Accept as known imperfection; snap window ±20% | N/A |
| Abort + ws close race with in-flight chunks | Medium | Low | Separate per-session task group; drain after close | N/A — verified via test 3 |

## Deployment Checklist

After merging to main:

- [ ] Verify local `MINIMAX_API_KEY` is the international sk-api key (not the JWT legacy domestic)
- [ ] `scp .env pi@rpi:~/jarvis/.env` — sync int'l key to RPi
- [ ] Archive domestic key to password vault / `.env.domestic`
- [ ] `python -m pytest tests/ -q` — full unit regression
- [ ] `python system_tests/runner.py --suite general` — end-to-end smoke
- [ ] Manual: run `python jarvis.py --no-wake`, speak 5 turns, listen for:
  - First-sentence latency feel (should be ~250ms from send to audio)
  - No click/pop at chunk boundaries
  - Interrupt cleanly cuts audio AND logs correct truncated text in memory
- [ ] RPi: same manual test (latency should be slightly better than Mac due to lower baseline hardware work)
- [ ] Observe 24h: log shows no runaway fallbacks, cache hit rate stable, no rate-limit 429s

## Out of Scope (explicitly deferred)

These are tempting adjacent improvements. They are **not** part of this PR:

- Multi-WS parallel sentence synthesis (gain questionable, order assembly complex)
- Per-(voice, emotion) persistent WS pool (state management complexity)
- Prometheus/OTel metric export (keep logs; instrument later if data justifies it)
- WP5 Option ④ (memory injection with `heard_fraction`) — would require ADR and memory-layer changes
- Voice cloning migration from domestic to international account
- MiniMax M2 LLM integration (different product; if pursued, separate spec)
