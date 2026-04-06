# Pipeline Latency Optimization Design

**Date**: 2026-04-04
**Goal**: Reduce end-to-end latency for both local and cloud paths on RPi5
**Target**: Local path ~600ms → ~120ms (cached) / ~350ms (uncached); Cloud path ~2s → ~1.7s

## Context

小月 voice pipeline stages and measured latencies (from Mac benchmark, RPi5 will be slower for local compute but same for network):

| Stage | Latency | Type |
|-------|---------|------|
| Recording (VAD) | 1-3s | Fixed (user) |
| Speaker verify + ASR (parallel) | ~200-400ms | Local compute |
| Memory query | ~50-100ms | Local compute + SQLite |
| Direct answer | ~50ms | Local compute + SQLite |
| Intent route (Groq 8B) | ~170ms | Network |
| Local execution | ~10-50ms | Local |
| Cloud LLM first token | ~800-2000ms | Network |
| TTS synthesis (MiniMax) | ~300-800ms | Network |

Bottlenecks: memory + route are sequential (~250ms combined); TTS re-synthesizes identical responses.

## Design

### A. Pipeline Parallelization

#### Current flow (serial)
```
ASR+Verify → resolve_user → history → memory_query → direct_answer
  → keyword_check → learning_check → route → execute → TTS
```

#### Optimized flow
```
ASR+Verify complete
  │
  ├─ keyword_check + learning_check (<1ms, do first to avoid wasted route)
  │   └─ if hit → return immediately, no route/memory needed
  │
  ├─ (none hit) → launch in parallel:
  │   ├─ Future 1: intent_router.route(text)           ~170ms
  │   └─ Future 2: memory_manager.query(text, user_id)  ~80ms
  │
  ├─ Main thread: direct_answerer.try_answer()          ~50ms
  │   └─ if hit → return, ignore route Future
  │
  └─ Await Futures (route should be done by now, ~0ms wait)
      → proceed with route result + memory_context
```

**Savings**: ~150ms in the common path (route overlaps with memory+direct_answer).

#### Implementation details

**File: `jarvis.py` — `_handle_utterance_inner()`**

Reorder steps 4-6:
1. After user identity resolved, do keyword_check and learning_check (free, <1ms)
2. If no early exit, submit `route()` and `memory_query()` to `self._executor` (existing ThreadPoolExecutor)
3. Run `direct_answer` on main thread while Futures execute
4. If direct_answer hits, return (route Future ignored, result garbage-collected)
5. Otherwise, `route_future.result()` and `memory_future.result()` — both should be ready

Error handling: wrap `.result()` in try/except. If route Future fails, fall back to `RouteResult(intent="complex", provider="none")` (same as current all-providers-down behavior). If memory Future fails, `memory_context = ""`.

**File: `core/intent_router.py` — `_SESSION` thread safety**

Change module-level `_SESSION = requests.Session()` to instance-level `self._session = requests.Session()` in `__init__`. Prevents potential issues if web frontend calls `route()` concurrently.

**File: `memory/embedder.py` — single-entry cache**

Both `memory_manager.query()` and `direct_answerer.try_answer()` call `embedder.encode(text)` with the same text in the same turn. On RPi5, each encode may take 50-100ms.

Add a 1-entry cache:
```python
def encode(self, text: str) -> np.ndarray:
    if text == self._last_text:
        return self._last_vec.copy()
    vec = self._compute(text)
    self._last_text = text
    self._last_vec = vec
    return vec
```

Thread-safe enough: worst case is double computation (identical to current behavior). The `.copy()` prevents mutation across threads.

### B. Caching

#### B.1 Intent Route Cache

**Location**: `IntentRouter` internal, `route()` interface unchanged.

**Behavior**:
```
route(text)
  → cache lookup by text.strip()
  → hit: return cached RouteResult copy (0ms)
  → miss: call Groq/Cerebras → cache result → return
```

**Rules**:
- Key: `text.strip()` (exact match)
- Value: `RouteResult` (copied on return to prevent mutation)
- Capacity: 256 entries, LRU eviction via `collections.OrderedDict`
- Only cache successful results: `provider != "none"`
- No TTL needed: intent classification is deterministic for same system_prompt + text
- Invalidation: cache lives on the instance; restart = new instance = fresh cache

**What's NOT cached**: failed routes (transient errors), which return `provider="none"`.

#### B.2 TTS Audio Cache

**Location**: `TTSEngine._synth_minimax()` internal, `speak()` interface unchanged.

**Behavior**:
```
_synth_minimax(text, emotion)
  → if len(text) > 50: synthesize to temp file, return (path, deletable=True)
  → cache key = md5(f"{text}|{voice_id}|{emotion}")
  → hit: return (cache_path, deletable=False)
  → miss: synthesize → write to cache dir → return (cache_path, deletable=False)
```

**Rules**:
- Cache dir: `data/cache/tts/`
- Filename: `{md5_hex}.mp3`
- Only cache text <= 50 characters (short, high-repeat responses)
- Max 500 files, LRU eviction by file mtime (`os.utime` on access)
- Cache persists across restarts (deterministic md5 key)
- Write atomically: temp file + `os.rename()` to prevent corruption

**File deletion safety**: callers (`_speak_minimax`, TTSPipeline play worker) currently delete temp files after playback. Changed behavior:
- `_synth_minimax` returns a tuple `(path: str, deletable: bool)`
- Callers only `unlink()` if `deletable is True`
- Cache files (`deletable=False`) persist until LRU eviction

**Time response pollution**: responses like "现在是3点15分" are <=50 chars, unique each minute, and would churn the cache. Accepted: LRU naturally retains frequently-accessed entries ("好的，灯开了" accessed 20x/day) over rarely-repeated time responses. 500-entry capacity is sufficient headroom.

#### B.3 Startup Pre-warm (optional)

On startup, submit to background thread:
```python
_PREWARM_TEXTS = ["好的，灯开了。", "好的，灯关了。", "没听清，能再说一遍吗？"]
```
Pre-synthesize these with default emotion. First "开灯" command gets 0ms TTS.

Also pre-warm HTTP connections to Groq and MiniMax (single lightweight request to establish keep-alive, saves ~100ms TCP+TLS on first real call).

### Expected Results

| Scenario | Current | Optimized | Saved |
|----------|---------|-----------|-------|
| "开灯" (first time) | ~600ms | ~350ms | -250ms |
| "开灯" (cached) | ~600ms | ~120ms | -480ms |
| "几点了" | ~500ms | ~300ms | -200ms |
| "帮我写首诗" (cloud) | ~2000ms | ~1700ms | -300ms |

Cached local commands approach near-instant feel (~120ms = route 0ms + TTS 0ms + ASR overhead only).

## Files Changed

| File | Change | Risk |
|------|--------|------|
| `jarvis.py` | Reorder steps, parallel Futures in `_handle_utterance_inner()` | Medium — core pipeline logic, thorough testing needed |
| `core/intent_router.py` | Instance-level session, LRU route cache | Low — internal optimization, `route()` interface unchanged |
| `memory/embedder.py` | 1-entry encode cache | Low — performance optimization, no behavior change |
| `core/tts.py` | Disk cache in `_synth_minimax()`, return tuple, `_speak_minimax` deletion logic, TTSPipeline play worker deletion logic | Medium — must not delete cache files |

## What's NOT Changed

- All public module interfaces (route(), speak(), handle_utterance() signatures)
- TTS engine selection and fallback chain
- ASR + speaker verification parallel execution
- Cloud LLM streaming + TTSPipeline dual-thread architecture
- Memory save (already async in background)
- Web frontend handle_text() interface (benefits from same parallelization internally)

## Testing Plan

- Unit tests for route cache: hit, miss, LRU eviction, failed-result exclusion
- Unit tests for TTS cache: hit, miss, eviction, atomic write, deletion safety
- Unit tests for Embedder cache: same-text reuse, different-text miss
- Integration test: full pipeline with mocked providers, verify parallel execution order
- Existing 720 tests must pass unchanged (no interface changes)
- Manual benchmark: run `benchmark_router_latency.py` style test on full pipeline before/after
