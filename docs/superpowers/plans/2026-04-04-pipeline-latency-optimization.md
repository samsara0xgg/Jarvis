# Pipeline Latency Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Reduce 小月 end-to-end latency via pipeline parallelization and caching — local path from ~600ms to ~120ms (cached), cloud path from ~2s to ~1.7s.

**Architecture:** Three independent changes: (1) Embedder single-entry cache eliminates duplicate encode calls, (2) IntentRouter gets LRU route cache + instance-level HTTP session, (3) TTSEngine gets disk-based audio cache for short responses. Then jarvis.py pipeline reordered to run route + memory in parallel.

**Tech Stack:** Python 3.11, threading, collections.OrderedDict, hashlib, pathlib

**Spec:** `docs/superpowers/specs/2026-04-04-pipeline-latency-optimization-design.md`

---

### Task 1: Embedder single-entry cache

**Files:**
- Modify: `memory/embedder.py:44-60` — add cache to `encode()`
- Test: `tests/test_embedder_cache.py` (create)

- [x] **Step 1: Write the failing test**

Create `tests/test_embedder_cache.py`:

```python
"""Tests for Embedder single-entry cache."""
from __future__ import annotations

from unittest.mock import MagicMock, patch
import numpy as np
import pytest

from memory.embedder import Embedder


@pytest.fixture
def embedder():
    """Embedder with mocked model to avoid loading real weights."""
    e = Embedder.__new__(Embedder)
    e._model_name = "test"
    e._model = MagicMock()
    e._lock = __import__("threading").Lock()
    e._last_text = None
    e._last_vec = None
    # Model returns a deterministic 4-dim vector
    e._model.embed = MagicMock(return_value=iter([np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)]))
    return e


class TestEmbedderCache:
    def test_same_text_hits_cache(self, embedder):
        """Second encode() with same text should NOT call model.embed again."""
        v1 = embedder.encode("开灯")
        v2 = embedder.encode("开灯")
        assert embedder._model.embed.call_count == 1
        np.testing.assert_array_equal(v1, v2)

    def test_different_text_misses_cache(self, embedder):
        """Different text should call model.embed again."""
        embedder.encode("开灯")
        embedder._model.embed.return_value = iter([np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)])
        embedder.encode("关灯")
        assert embedder._model.embed.call_count == 2

    def test_cache_returns_copy(self, embedder):
        """Cached result should be a copy, not the same object."""
        v1 = embedder.encode("开灯")
        v2 = embedder.encode("开灯")
        assert v1 is not v2

    def test_cache_does_not_break_normalization(self, embedder):
        """Cached vector should still be unit-norm."""
        embedder._model.embed.return_value = iter([np.array([3.0, 4.0, 0.0, 0.0], dtype=np.float32)])
        v = embedder.encode("测试")
        assert abs(np.linalg.norm(v) - 1.0) < 1e-6
        v2 = embedder.encode("测试")
        assert abs(np.linalg.norm(v2) - 1.0) < 1e-6
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_embedder_cache.py -v`
Expected: FAIL — `Embedder` doesn't have `_last_text`/`_last_vec` attributes yet.

- [x] **Step 3: Implement the cache in Embedder.encode()**

In `memory/embedder.py`, add cache fields to `__init__` and modify `encode`:

```python
# In __init__, after self._lock:
self._last_text: str | None = None
self._last_vec: np.ndarray | None = None
```

Replace `encode()` method (lines 44-60):

```python
def encode(self, text: str) -> np.ndarray:
    """Encode a single text into a unit-norm embedding vector.

    Uses a single-entry cache: if *text* matches the last call,
    returns a copy without recomputing.
    """
    if text == self._last_text and self._last_vec is not None:
        return self._last_vec.copy()
    self._load()
    embeddings = list(self._model.embed([text]))
    vec = np.array(embeddings[0], dtype=np.float32)
    norm = np.linalg.norm(vec)
    if norm > 0:
        vec /= norm
    self._last_text = text
    self._last_vec = vec
    return vec.copy()
```

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_embedder_cache.py -v`
Expected: 4 passed

- [x] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass (720+)

- [x] **Step 6: Commit**

```bash
git add memory/embedder.py tests/test_embedder_cache.py
git commit -m "perf: add single-entry cache to Embedder.encode()"
```

---

### Task 2: IntentRouter — instance-level session + LRU route cache

**Files:**
- Modify: `core/intent_router.py:20-22,92-120,123-161,162-180` — session + cache
- Test: `tests/test_intent_router.py` — add cache tests

- [x] **Step 1: Write the failing tests**

Append to `tests/test_intent_router.py`:

```python
class TestRouteCache:
    """Tests for the LRU route cache in IntentRouter."""

    @patch("core.intent_router._SESSION")
    def test_cache_hit_skips_api_call(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [{"device_id": "living_room_light", "action": "turn_on", "value": None}],
                "response": "好的",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        r1 = router.route("开灯")
        r2 = router.route("开灯")

        assert mock_session.post.call_count == 1  # only one API call
        assert r2.intent == "smart_home"
        assert r2.provider == "groq"

    @patch("core.intent_router._SESSION")
    def test_cache_miss_on_different_text(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [], "response": "好的",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        router.route("开灯")
        router.route("关灯")
        assert mock_session.post.call_count == 2

    def test_failed_route_not_cached(self, config):
        """provider='none' results should NOT be cached."""
        router = IntentRouter(config)  # no API keys → all fail
        router.route("开灯")
        router.route("开灯")
        assert len(router._route_cache) == 0

    @patch("core.intent_router._SESSION")
    def test_cache_lru_eviction(self, mock_session, config):
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "complex", "confidence": 0.9, "response": None,
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        router._cache_max = 3  # shrink for test

        for i in range(5):
            router.route(f"query_{i}")

        assert len(router._route_cache) == 3

    @patch("core.intent_router._SESSION")
    def test_cached_result_is_independent_copy(self, mock_session, config):
        """Mutating a returned RouteResult should not affect cache."""
        config["models"]["groq"]["api_key"] = "test_key"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps({
                "intent": "smart_home", "confidence": 0.95,
                "actions": [{"device_id": "x", "action": "turn_on", "value": None}],
                "response": "OK",
            })}}]
        }
        mock_session.post.return_value = mock_resp

        router = IntentRouter(config)
        r1 = router.route("开灯")
        r1.intent = "MUTATED"
        r2 = router.route("开灯")
        assert r2.intent == "smart_home"
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_intent_router.py::TestRouteCache -v`
Expected: FAIL — `_route_cache` attribute doesn't exist.

- [x] **Step 3: Implement session + cache changes**

In `core/intent_router.py`:

**3a.** Change module-level session to instance-level. Remove line 20-22 (`_SESSION = requests.Session()`). In `__init__`, add:

```python
self._session = requests.Session()
```

In `_call_cloud`, change `_SESSION.post(` to `self._session.post(`.

**3b.** Add cache to `__init__`:

```python
from collections import OrderedDict
import copy

# In __init__, after self._tracker:
self._route_cache: OrderedDict[str, RouteResult] = OrderedDict()
self._cache_max = 256
```

**3c.** Add cache logic to `route()`:

```python
def route(self, text: str) -> RouteResult:
    """分析用户指令。Groq 8B → Cerebras 8B → 直接走云端 LLM."""
    key = text.strip()

    # Cache hit
    if key in self._route_cache:
        self._route_cache.move_to_end(key)
        cached = self._route_cache[key]
        self.logger.info("Route cache hit: '%s' → %s/%s", key[:20], cached.tier, cached.intent)
        return copy.copy(cached)

    start = time.time()

    # 1. Groq (primary)
    ...existing code...

    # After getting result (before final return), cache it if successful:
    # (add before each successful return)
```

Add a helper method:

```python
def _cache_result(self, key: str, result: RouteResult) -> None:
    """Store a successful route result in LRU cache."""
    if result.provider == "none":
        return
    self._route_cache[key] = copy.copy(result)
    if len(self._route_cache) > self._cache_max:
        self._route_cache.popitem(last=False)
```

Call `self._cache_result(key, result)` before returning from Groq success and Cerebras success paths.

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_intent_router.py -v`
Expected: all pass (existing + 5 new)

- [x] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [x] **Step 6: Commit**

```bash
git add core/intent_router.py tests/test_intent_router.py
git commit -m "perf: add LRU route cache and instance-level session to IntentRouter"
```

---

### Task 3: TTS audio cache

**Files:**
- Modify: `core/tts.py:76-95,296-346,432-437,647-648` — cache in synth, safe deletion
- Test: `tests/test_tts_cache.py` (create)

- [x] **Step 1: Write the failing tests**

Create `tests/test_tts_cache.py`:

```python
"""Tests for TTS audio disk cache."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.tts import TTSEngine


@pytest.fixture
def tts_with_cache(tmp_path):
    """TTSEngine with cache pointed at a temp dir."""
    config = {"tts": {"engine": "minimax"}}
    with patch.object(TTSEngine, "__init__", lambda self, cfg, **kw: None):
        engine = TTSEngine.__new__(TTSEngine)
        engine.engine_name = "minimax"
        engine.minimax_key = "test_key"
        engine.minimax_model = "speech-02-turbo"
        engine.minimax_voice = "male-qn-qingse"
        engine._minimax_url = "https://api.minimax.chat/v1/t2a_v2"
        engine._http_session = MagicMock()
        engine._tracker = None
        engine._tts_cache_dir = tmp_path
        engine._tts_cache_max = 5
        engine.logger = MagicMock()
        engine._platform = "Darwin"
    return engine


class TestTTSCache:
    def test_cache_key_deterministic(self, tts_with_cache):
        """Same text+voice+emotion should produce same cache key."""
        k1 = tts_with_cache._tts_cache_key("好的", "calm")
        k2 = tts_with_cache._tts_cache_key("好的", "calm")
        assert k1 == k2

    def test_cache_key_differs_by_emotion(self, tts_with_cache):
        k1 = tts_with_cache._tts_cache_key("好的", "calm")
        k2 = tts_with_cache._tts_cache_key("好的", "happy")
        assert k1 != k2

    def test_cache_hit_returns_existing_file(self, tts_with_cache, tmp_path):
        """If cache file exists, _synth_minimax should return it without API call."""
        key = tts_with_cache._tts_cache_key("好的", "calm")
        cache_path = tmp_path / f"{key}.mp3"
        cache_path.write_bytes(b"fake_mp3_data")

        result_path, deletable = tts_with_cache._synth_minimax("好的", "calm")
        assert result_path == str(cache_path)
        assert deletable is False
        tts_with_cache._http_session.post.assert_not_called()

    def test_cache_miss_calls_api_and_saves(self, tts_with_cache, tmp_path):
        """On cache miss, should call API and save to cache dir."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"audio": b"deadbeef".hex()},
            "base_resp": {"status_msg": "ok"},
        }
        tts_with_cache._http_session.post.return_value = mock_resp

        result_path, deletable = tts_with_cache._synth_minimax("好的", "calm")
        assert deletable is False
        assert str(tmp_path) in result_path
        assert Path(result_path).exists()

    def test_long_text_bypasses_cache(self, tts_with_cache):
        """Text > 50 chars should NOT use cache."""
        long_text = "这是一段很长的文本，超过了五十个字符的限制，所以不应该被缓存起来"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "data": {"audio": b"deadbeef".hex()},
            "base_resp": {"status_msg": "ok"},
        }
        tts_with_cache._http_session.post.return_value = mock_resp

        result_path, deletable = tts_with_cache._synth_minimax(long_text, "calm")
        assert deletable is True  # temp file, should be deleted after play
        assert str(tts_with_cache._tts_cache_dir) not in result_path

    def test_cache_eviction(self, tts_with_cache, tmp_path):
        """When cache exceeds max, oldest file should be evicted."""
        tts_with_cache._tts_cache_max = 3
        # Create 4 cached files with staggered mtimes
        import time
        paths = []
        for i in range(4):
            p = tmp_path / f"file_{i}.mp3"
            p.write_bytes(b"data")
            os.utime(p, (1000 + i, 1000 + i))
            paths.append(p)

        tts_with_cache._evict_tts_cache()
        remaining = list(tmp_path.glob("*.mp3"))
        assert len(remaining) == 3
        assert not paths[0].exists()  # oldest evicted

    def test_is_cached_file(self, tts_with_cache, tmp_path):
        cached = tmp_path / "abc.mp3"
        assert tts_with_cache._is_cached_file(str(cached)) is True
        assert tts_with_cache._is_cached_file("/tmp/xyz.mp3") is False
```

- [x] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tts_cache.py -v`
Expected: FAIL — `_tts_cache_key`, `_tts_cache_dir`, etc. don't exist.

- [x] **Step 3: Implement TTS cache**

In `core/tts.py`:

**3a.** Add imports at top:

```python
import hashlib
```

**3b.** In `TTSEngine.__init__` (after `self._platform`), add cache setup:

```python
# TTS audio cache for short responses
self._tts_cache_dir = Path(tts_config.get("cache_dir", "data/cache/tts"))
self._tts_cache_dir.mkdir(parents=True, exist_ok=True)
self._tts_cache_max = int(tts_config.get("cache_max_files", 500))
```

**3c.** Add cache helper methods to `TTSEngine`:

```python
def _tts_cache_key(self, text: str, emotion: str) -> str:
    """Deterministic cache key from text + voice + emotion."""
    raw = f"{text}|{self.minimax_voice}|{emotion}"
    return hashlib.md5(raw.encode()).hexdigest()

def _is_cached_file(self, filepath: str) -> bool:
    """Check if a file path is inside the TTS cache directory."""
    try:
        return Path(filepath).resolve().is_relative_to(self._tts_cache_dir.resolve())
    except (ValueError, TypeError):
        return False

def _evict_tts_cache(self) -> None:
    """Remove oldest files when cache exceeds max size."""
    files = sorted(self._tts_cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
    while len(files) > self._tts_cache_max:
        files[0].unlink(missing_ok=True)
        files.pop(0)
```

**3d.** Modify `_synth_minimax` to return `tuple[str, bool]` and add cache logic:

```python
def _synth_minimax(self, text: str, emotion: str = "") -> tuple[str, bool]:
    """Synthesize with MiniMax TTS, return (file_path, deletable).

    Short text (<=50 chars) is cached to disk. Returns deletable=False
    for cached files, True for temp files.
    """
    if not self.minimax_key:
        raise RuntimeError("MiniMax API key not configured (MINIMAX_API_KEY)")

    minimax_emotion = _EMOTION_TO_MINIMAX.get(emotion, "calm")
    use_cache = len(text) <= 50

    # Cache hit
    if use_cache:
        cache_key = self._tts_cache_key(text, minimax_emotion)
        cache_path = self._tts_cache_dir / f"{cache_key}.mp3"
        if cache_path.exists():
            os.utime(cache_path)  # touch for LRU
            self.logger.info("TTS cache hit: %r", text[:30])
            return str(cache_path), False

    # API call (same as before)
    payload = {
        "model": self.minimax_model,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": self.minimax_voice,
            "speed": 1.0,
            "vol": 5,
            "pitch": 0,
            "emotion": minimax_emotion,
        },
        "audio_setting": {
            "format": "mp3",
            "sample_rate": 32000,
            "channel": 1,
        },
    }

    self.logger.info(
        "MiniMax TTS: voice=%s emotion=%s text=%r",
        self.minimax_voice, minimax_emotion, text[:50],
    )

    resp = self._http_session.post(
        self._minimax_url,
        headers={
            "Authorization": f"Bearer {self.minimax_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    if "data" not in data or "audio" not in data.get("data", {}):
        error_msg = data.get("base_resp", {}).get("status_msg", str(data))
        raise RuntimeError(f"MiniMax TTS error: {error_msg}")

    audio_bytes = bytes.fromhex(data["data"]["audio"])

    # Save to cache or temp
    if use_cache:
        with tempfile.NamedTemporaryFile(
            dir=str(self._tts_cache_dir), suffix=".tmp", delete=False,
        ) as tmp:
            tmp.write(audio_bytes)
        os.rename(tmp.name, str(cache_path))  # atomic
        self._evict_tts_cache()
        return str(cache_path), False

    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
        tmp.write(audio_bytes)
        return tmp.name, True
```

**3e.** Update `_speak_minimax` to handle the tuple return:

```python
def _speak_minimax(self, text: str, emotion: str = "") -> None:
    tmp_path, deletable = self._synth_minimax(text, emotion)
    try:
        self._play_audio_file(tmp_path)
    finally:
        if deletable:
            Path(tmp_path).unlink(missing_ok=True)
```

**3f.** Update `synth_to_file` — the TTSPipeline calls this. It must also return the tuple:

Change `synth_to_file` return type and the minimax path:

```python
def synth_to_file(self, text: str, emotion: str = "") -> tuple[str, bool] | None:
    """Synthesize text to an audio file and return (path, deletable).

    Returns None if the engine plays directly (pyttsx3).
    deletable=True means caller should delete after use.
    deletable=False means file is in cache, do not delete.
    """
    if self.engine_name == "openai_tts" and self.openai_tts_key:
        ...existing try/except...
            path = self._synth_openai(text, emotion)
            ...
            return (path, True)  # OpenAI results are temp files
        ...
    if self.engine_name == "minimax" and self.minimax_key:
        ...existing try/except...
            result = self._synth_minimax(text, emotion)  # already returns tuple
            ...
            return result
        ...
    if self.engine_name == "azure"...:
        ...
            return (path, True)
        ...
    if self.engine_name == "pyttsx3":
        self._speak_pyttsx3(text)
        return None
    return (self._synth_edge(text), True)
```

**3g.** Update TTSPipeline `_play_worker` to respect `deletable` flag:

```python
def _play_worker(self) -> None:
    """Consume audio_queue and play files sequentially."""
    while not self._aborted.is_set():
        try:
            item = self._audio_queue.get(timeout=1)
        except Empty:
            continue

        if item is _SENTINEL:
            self._done.set()
            return

        filepath, sentence_type, deletable = item
        try:
            if not self._aborted.is_set():
                self._engine._play_audio_file(filepath)
        except Exception as exc:
            self.logger.warning("Audio playback failed: %s", exc)
        finally:
            if deletable:
                Path(filepath).unlink(missing_ok=True)
```

And update `_tts_worker` to pass the `deletable` flag:

```python
def _tts_worker(self) -> None:
    while not self._aborted.is_set():
        ...
        text, sentence_type, emotion = item
        try:
            result = self._synthesize_to_file(text, emotion)
            if result and not self._aborted.is_set():
                filepath, deletable = result
                self._audio_queue.put((filepath, sentence_type, deletable))
        except Exception as exc:
            self.logger.warning("TTS synthesis failed: %s", exc)
```

And `_synthesize_to_file`:

```python
def _synthesize_to_file(self, text: str, emotion: str = "") -> tuple[str, bool] | None:
    """Delegate to TTSEngine.synth_to_file."""
    return self._engine.synth_to_file(text, emotion)
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tts_cache.py -v`
Expected: 7 passed

- [x] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass. Some existing TTS tests may need minor updates if they assert on `_synth_minimax` return type — fix any that break.

- [x] **Step 6: Commit**

```bash
git add core/tts.py tests/test_tts_cache.py
git commit -m "perf: add TTS audio disk cache for short responses"
```

---

### Task 4: Pipeline parallelization in jarvis.py

**Files:**
- Modify: `jarvis.py:524-622` — reorder steps, parallel Futures
- Test: `tests/test_jarvis.py` — add parallel pipeline test

- [x] **Step 1: Write the failing test**

Append to `tests/test_jarvis.py`:

```python
def test_route_and_memory_run_in_parallel(jarvis_app, mock_config):
    """Route and memory_query should be submitted to executor concurrently."""
    import time
    from unittest.mock import call

    original_submit = jarvis_app._executor.submit

    submitted_tasks = []

    def tracking_submit(fn, *args, **kwargs):
        submitted_tasks.append(fn.__name__ if hasattr(fn, '__name__') else str(fn))
        return original_submit(fn, *args, **kwargs)

    jarvis_app._executor.submit = tracking_submit

    # Mock route to return complex (will go to cloud LLM path)
    from core.intent_router import RouteResult
    jarvis_app.intent_router.route = MagicMock(return_value=RouteResult(
        tier="cloud", intent="complex", confidence=0.9,
        duration_ms=100, provider="groq",
    ))
    jarvis_app.memory_manager.query = MagicMock(return_value="<memory></memory>")
    jarvis_app.direct_answerer.try_answer = MagicMock(return_value=None)

    # Need to also mock LLM to avoid real API call
    jarvis_app.llm.chat_stream = MagicMock(return_value=("回复", [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "回复"},
    ]))

    audio = np.random.randn(16000).astype(np.float32)
    jarvis_app.handle_utterance(audio)

    # Both route and query should have been submitted to executor
    assert "route" in submitted_tasks or any("route" in t for t in submitted_tasks)
    assert "query" in submitted_tasks or any("query" in t for t in submitted_tasks)
```

Note: This test may need adjustment based on how the existing `jarvis_app` fixture is set up. The key assertion is that both `route` and `memory_query` are submitted to the thread pool.

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_jarvis.py::test_route_and_memory_run_in_parallel -v`
Expected: FAIL — currently they're called sequentially, not via executor.submit.

- [x] **Step 3: Implement pipeline parallelization**

In `jarvis.py`, replace the block from step 4 through step 6 (lines ~524-622). The new flow:

```python
        # 4. Load conversation history
        session_id = user_id or "_guest"
        self._last_user_id = user_id
        self._last_session_id = session_id
        history = self.conversation_store.get_history(session_id)

        # 4b. Fast local checks — keyword trigger + learning intent (<1ms)
        # Do these BEFORE launching route Future to avoid wasted API calls.
        if any(text.startswith(kw) or kw in text[:10] for kw in _REMEMBER_KEYWORDS):
            if "每次" not in text:
                reply = "好的，记住了。"
                self.logger.info("Memory shortcut: %s", text[:60])
                self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                print(f"🤖 小月: {reply}")
                self._speak_nonblocking(reply, emotion=detected_emotion)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self._executor.submit(
                        self.memory_manager.save, history, user_id, session_id,
                    )
                return reply

        learning = None
        if hasattr(self, "learning_router"):
            learning = self.learning_router.detect(text)
            if learning and learning.mode == "create":
                self.logger.info("Learning intent: create — %s", learning.description[:60])
                learn_response = self._learn_create(learning, user_id)
                self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                print(f"🤖 小月: {learn_response}")
                self._speak_nonblocking(learn_response, emotion=detected_emotion)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": learn_response})
                self.conversation_store.replace(session_id, history)
                if user_id:
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100], "route": "learn_create",
                    })
                return learn_response

        keyword_match = None
        if self.rule_manager and self.local_executor:
            keyword_match = self.rule_manager.check_keyword(text)

        # 4c. Parallel: route + memory (only if no keyword match)
        self.event_bus.emit("jarvis.state_changed", {"state": "thinking"})
        response_text = None
        updated_messages = None
        ar: ActionResponse | None = None
        sentence_count = 0
        use_llm_rephrase = False

        # Handle keyword match (skip routing)
        if keyword_match:
            keyword_actions, rule_name = keyword_match
            if keyword_actions and keyword_actions[0].get("skill"):
                ar = self.local_executor.execute_skill_alias(keyword_actions, user_role)
                if ar.action == Action.REQLLM:
                    use_llm_rephrase = True
                else:
                    response_text = ar.text
            else:
                ar = self.local_executor.execute_smart_home(
                    keyword_actions, user_role, response=f"好的，{rule_name}已执行。",
                )
                response_text = ar.text

        # Launch parallel futures for route + memory
        route_future = None
        memory_future = None

        if response_text is None and self.intent_router and self.local_executor:
            route_future = self._executor.submit(self.intent_router.route, text)

        if user_id:
            memory_future = self._executor.submit(self.memory_manager.query, text, user_id)

        # While futures run, try direct answer
        memory_context = ""
        if user_id:
            try:
                direct = self.direct_answerer.try_answer(text, user_id)
                if direct:
                    self.logger.info("Level 1 direct answer: %s", direct[:60])
                    self.event_bus.emit("jarvis.state_changed", {"state": "speaking"})
                    print(f"🤖 小月 (L1): {direct}")
                    self._speak_nonblocking(direct, emotion=detected_emotion)
                    self.behavior_log.log(user_id, "conversation", {
                        "text": text[:100],
                        "route": "memory_l1",
                        "answer": direct[:100],
                    })
                    return direct
            except Exception as exc:
                self.logger.warning("Level 1 answer failed: %s", exc)

        # Collect memory result
        if memory_future:
            try:
                memory_context = memory_future.result(timeout=5)
            except Exception as exc:
                self.logger.warning("Memory query failed: %s", exc)

        # 5. Route (await future, should already be done)
        if route_future and response_text is None:
            try:
                route = route_future.result(timeout=8)
            except Exception as exc:
                self.logger.warning("Route failed: %s", exc)
                route = None

            if route and route.tier == "local":
                if route.intent == "smart_home":
                    ar = self.local_executor.execute_smart_home(
                        route.actions, user_role, response=route.response,
                    )
                elif route.intent == "info_query":
                    if route.sub_type not in ("news", "stocks", "weather") and user_id:
                        try:
                            mem_answer = self.direct_answerer.try_answer(text, user_id)
                            if mem_answer:
                                ar = ActionResponse(Action.RESPONSE, mem_answer)
                        except Exception:
                            pass
                    if ar is None:
                        ar = self.local_executor.execute_info_query(
                            route.sub_type, route.query, user_role,
                        )
                elif route.intent == "time":
                    ar = self.local_executor.execute_time(route.sub_type)
                elif route.intent == "automation":
                    ar = self.local_executor.execute_automation(
                        route.sub_type, route.rule,
                    )
                    if route.response is not None:
                        ar = type(ar)(ar.action, route.response)
                else:
                    ar = None

                if ar is not None:
                    if ar.action == Action.REQLLM:
                        use_llm_rephrase = True
                        response_text = None
                    elif any(p in ar.text for p in ("没查到", "未找到", "暂不支持")):
                        response_text = None
                    else:
                        response_text = ar.text

        # 6. Cloud LLM (unchanged from here) ...
```

- [x] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_jarvis.py -v`
Expected: all pass

- [x] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass

- [x] **Step 6: Commit**

```bash
git add jarvis.py tests/test_jarvis.py
git commit -m "perf: parallelize route + memory query in utterance pipeline"
```

---

### Task 5: Startup pre-warm (optional)

**Files:**
- Modify: `jarvis.py:210-212` — add connection + TTS pre-warm

- [x] **Step 1: Add pre-warm after existing embedding warmup**

In `jarvis.py`, after line 211 (`self._executor.submit(self.memory_manager.embedder.encode, "warmup")`), add:

```python
# Pre-warm HTTP connections (establish keep-alive, skip TCP+TLS on first real call)
self._executor.submit(self._prewarm_connections)
```

Add the method:

```python
def _prewarm_connections(self) -> None:
    """Pre-warm HTTP connections to reduce first-call latency."""
    import requests
    # Groq
    if self.intent_router and self.intent_router.groq_key:
        try:
            requests.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {self.intent_router.groq_key}"},
                timeout=5,
            )
        except Exception:
            pass
    # MiniMax TTS
    tts = self._get_tts()
    if tts and tts.minimax_key:
        try:
            tts._http_session.get("https://api.minimax.chat/v1/t2a_v2", timeout=5)
        except Exception:
            pass
```

- [x] **Step 2: Run full test suite**

Run: `python -m pytest tests/ -q`
Expected: all pass (pre-warm runs in background, no impact)

- [x] **Step 3: Commit**

```bash
git add jarvis.py
git commit -m "perf: pre-warm HTTP connections on startup"
```

---

### Task 6: Final verification

- [x] **Step 1: Run full test suite**

Run: `python -m pytest tests/ -v`
Expected: all pass, no regressions

- [x] **Step 2: Manual smoke test**

Run: `source ~/.secrets && python jarvis.py --no-wake`
Test: say "开灯" twice. Second time should be noticeably faster (route cache + TTS cache).
Check logs for "Route cache hit" and "TTS cache hit" messages.

- [x] **Step 3: Verify cache files created**

```bash
ls data/cache/tts/
```
Expected: `.mp3` files present after first "开灯" response.

- [x] **Step 4: Run benchmark**

```bash
source ~/.secrets && python tests/benchmark_router_latency.py
```
Verify Groq 8B latency still ~170ms (sanity check, not a regression).
