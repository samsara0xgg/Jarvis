# Full-Duplex Interrupt System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable Jarvis to detect voice interrupts during TTS playback via streaming ASR keyword matching, stop audio within ~500ms, capture post-interrupt speech, and support resume from breakpoint.

**Architecture:** Three layers — (1) TTSEngine/Pipeline gain a real `stop()` that kills playback subprocess instantly, (2) a new `InterruptMonitor` runs streaming ASR + keyword matching during TTS, (3) `_process_turn` wires interrupt detection, full-audio re-transcription, keyword stripping, and resume. The XVF3800 hardware AEC handles echo cancellation; software does everything else.

**Tech Stack:** sherpa-onnx (OnlineRecognizer for streaming ASR, OfflineRecognizer for SenseVoice), sounddevice, existing TTSPipeline

**Design doc:** `notes/full-duplex-interrupt-design-2026-04-13.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `core/tts.py:560-605` | `_play_audio_file` → Popen, add `stop()`, pipeline `abort()` kills playback |
| Create | `core/interrupt_monitor.py` | Streaming ASR + VAD + keyword matching during TTS |
| Modify | `jarvis.py:864-934,1044-1062` | Wire interrupt monitor, fix `_cancel_current`, add resume |
| Modify | `config.yaml` | Add `interrupt:` config section |
| Create | `tests/test_tts_stop.py` | Tests for TTSEngine.stop() and pipeline abort |
| Create | `tests/test_interrupt_monitor.py` | Tests for InterruptMonitor |
| Create | `tests/test_interrupt_integration.py` | Tests for resume + keyword stripping |
| Create | `scripts/download_streaming_model.sh` | Download sherpa-onnx streaming model |

---

## Task 1: TTSEngine — Popen playback with kill handle

**Files:**
- Modify: `core/tts.py:77-96` (TTSEngine.__init__)
- Modify: `core/tts.py:560-605` (TTSEngine._play_audio_file)
- Create: `tests/test_tts_stop.py`

- [ ] **Step 1: Write failing tests for TTSEngine.stop()**

```python
# tests/test_tts_stop.py
"""Tests for TTSEngine.stop() and interruptible playback."""

from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from core.tts import TTSEngine


def _make_config(**overrides):
    base = {
        "tts": {
            "engine": "pyttsx3",
            "fallback_enabled": False,
        }
    }
    base["tts"].update(overrides)
    return base


class TestTTSEngineStop:
    def test_stop_has_method(self):
        tts = TTSEngine(_make_config())
        assert hasattr(tts, "stop")
        assert callable(tts.stop)

    def test_stop_when_nothing_playing(self):
        tts = TTSEngine(_make_config())
        # Should not raise
        tts.stop()

    def test_play_audio_file_saves_proc_handle(self):
        tts = TTSEngine(_make_config())
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        mock_proc.returncode = 0
        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            tts._platform = "Darwin"
            tts._play_audio_file("/tmp/test.mp3")
            mock_popen.assert_called_once()
            # After playback, handle should be cleared
            assert tts._play_proc is None

    def test_stop_terminates_playing_process(self):
        tts = TTSEngine(_make_config())
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = 0
        tts._play_proc = mock_proc
        tts.stop()
        mock_proc.terminate.assert_called_once()

    def test_stop_is_thread_safe(self):
        tts = TTSEngine(_make_config())
        # Concurrent stop calls should not raise
        threads = [threading.Thread(target=tts.stop) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tts_stop.py -v`
Expected: FAIL — `stop` not defined, `_play_proc` not defined

- [ ] **Step 3: Implement TTSEngine.stop() and Popen playback**

In `core/tts.py`, add to `TTSEngine.__init__` (after line 91 `self._platform = platform.system()`):

```python
        self._play_proc: subprocess.Popen | None = None
        self._play_lock = threading.Lock()
```

Add import at top of file if not present: `import subprocess` (already imported), `import threading` (already imported).

Replace `_play_audio_file` method (lines 560–605) entirely:

```python
    def _play_audio_file(self, filepath: str) -> None:
        """Play an audio file using the platform's native player."""
        system = self._platform
        cmd: list[str] | None = None
        if system == "Darwin":
            cmd = ["afplay", filepath]
        elif system == "Linux":
            for candidate in (
                ["mpv", "--no-video", filepath],
                ["ffplay", "-nodisp", "-autoexit", filepath],
                ["aplay", filepath],
            ):
                import shutil
                if shutil.which(candidate[0]):
                    cmd = candidate
                    break
            if cmd is None:
                self.logger.warning("No audio player found on Linux.")
                return
        elif system == "Windows":
            # PowerShell — no Popen kill support, keep subprocess.run
            ps_cmd = (
                f'(New-Object Media.SoundPlayer "{filepath}").PlaySync()'
            )
            try:
                subprocess.run(
                    ["powershell", "-Command", ps_cmd],
                    check=True, capture_output=True, timeout=30,
                )
            except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
                self.logger.warning("Audio playback failed: %s", exc)
            return
        else:
            self.logger.warning("Unsupported platform for audio playback: %s", system)
            return

        try:
            with self._play_lock:
                self._play_proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            self._play_proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.logger.warning("Audio playback timed out.")
            with self._play_lock:
                if self._play_proc:
                    self._play_proc.kill()
        except Exception as exc:
            self.logger.warning("Audio playback failed: %s", exc)
        finally:
            with self._play_lock:
                self._play_proc = None

    def stop(self) -> None:
        """Kill current audio playback immediately."""
        with self._play_lock:
            proc = self._play_proc
            if proc and proc.poll() is None:
                proc.terminate()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tts_stop.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run full test suite for regressions**

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: Same 11 failures as before, no new ones

- [ ] **Step 6: Commit**

```bash
git add core/tts.py tests/test_tts_stop.py
git commit -m "feat: TTSEngine.stop() — Popen playback with kill handle"
```

---

## Task 2: TTSPipeline abort kills playback + saves remaining sentences

**Files:**
- Modify: `core/tts.py:678-700` (TTSPipeline.abort, TTSPipeline.stop)
- Modify: `tests/test_tts_stop.py`

- [ ] **Step 1: Write failing tests for pipeline abort**

Append to `tests/test_tts_stop.py`:

```python
from core.tts import TTSPipeline, SentenceType


class TestTTSPipelineAbort:
    def test_abort_calls_engine_stop(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        pipeline = TTSPipeline(engine)
        pipeline.start()
        pipeline.abort()
        engine.stop.assert_called_once()
        pipeline.stop()

    def test_abort_returns_remaining_sentences(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        engine.synth_to_file = MagicMock(return_value=None)  # block synthesis
        pipeline = TTSPipeline(engine)
        pipeline.start()
        pipeline.submit("句子一", SentenceType.FIRST)
        pipeline.submit("句子二", SentenceType.MIDDLE)
        pipeline.submit("句子三", SentenceType.MIDDLE)
        time.sleep(0.1)  # let worker pick up first item
        remaining = pipeline.abort()
        pipeline.stop()
        assert isinstance(remaining, list)

    def test_abort_returns_empty_when_nothing_queued(self):
        engine = MagicMock()
        engine.stop = MagicMock()
        pipeline = TTSPipeline(engine)
        pipeline.start()
        remaining = pipeline.abort()
        pipeline.stop()
        assert remaining == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tts_stop.py::TestTTSPipelineAbort -v`
Expected: FAIL — `abort()` doesn't call `engine.stop()` or return remaining

- [ ] **Step 3: Modify TTSPipeline.abort() to kill playback and return remaining**

In `core/tts.py`, replace the `abort` method (lines 678–690):

```python
    def abort(self) -> list[str]:
        """Cancel all pending sentences, stop playback, return unplayed text.

        Returns:
            List of sentence texts that were queued but not yet played.
        """
        self._aborted.set()
        # Collect remaining text from text_queue
        remaining: list[str] = []
        while not self._text_queue.empty():
            try:
                item = self._text_queue.get_nowait()
                if item is not _SENTINEL and isinstance(item, tuple):
                    remaining.append(item[0])  # (text, sentence_type, emotion)
            except Empty:
                break
        # Drain audio queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except Empty:
                break
        # Kill currently playing audio
        self._engine.stop()
        # Unblock workers
        self._text_queue.put(_SENTINEL)
        self._audio_queue.put(_SENTINEL)
        return remaining
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tts_stop.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Run full test suite**

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add core/tts.py tests/test_tts_stop.py
git commit -m "feat: TTSPipeline.abort() kills playback + returns remaining sentences"
```

---

## Task 3: Fix _cancel_current + active pipeline + interrupted response

**Files:**
- Modify: `jarvis.py:864-934` (_process_turn streaming section)
- Modify: `jarvis.py:1044-1062` (_cancel_current)
- Modify: `jarvis.py:235-246` (__init__ session state)
- Create: `tests/test_interrupt_integration.py`

- [ ] **Step 1: Write failing tests for resume mechanism**

```python
# tests/test_interrupt_integration.py
"""Tests for interrupt resume and keyword stripping."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import numpy as np
import pytest


class TestInterruptResume:
    """Test the _interrupted_response mechanism."""

    def _make_app(self, tmp_path):
        """Create a minimal JarvisApp with mocks."""
        from tests.test_jarvis import _make_config
        config = _make_config(tmp_path)
        with patch("core.speaker_encoder.SpeakerEncoder"), \
             patch("core.speaker_verifier.SpeakerVerifier"), \
             patch("core.speech_recognizer.SpeechRecognizer"), \
             patch("core.audio_recorder.AudioRecorder"), \
             patch("core.llm.LLMClient"), \
             patch("devices.device_manager.DeviceManager"):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=tmp_path / "config.yaml")
            return app

    def test_interrupted_response_initially_none(self, tmp_path):
        app = self._make_app(tmp_path)
        assert app._interrupted_response is None

    def test_resume_keyword_plays_interrupted_response(self, tmp_path):
        app = self._make_app(tmp_path)
        app._interrupted_response = ["还有小雨。", "建议带伞。"]
        sentences = []
        result = app._process_turn(
            "继续说",
            emotion="",
            session_id="_test",
            output_fn=lambda s: sentences.append(s),
        )
        assert "还有小雨" in result
        assert "建议带伞" in result
        assert len(sentences) == 2
        assert app._interrupted_response is None  # cleared after resume

    def test_resume_keyword_no_interrupted_response_falls_through(self, tmp_path):
        app = self._make_app(tmp_path)
        app._interrupted_response = None
        # "继续说" without interrupted response should go through normal pipeline
        # Mock the LLM to return something
        app.llm.chat_stream = MagicMock(return_value=("好的", []))
        result = app._process_turn(
            "继续说",
            emotion="",
            session_id="_test",
            output_fn=lambda s: None,
        )
        # Should have gone through normal pipeline, not crash
        assert result is not None


class TestKeywordStripping:
    """Test interrupt keyword prefix removal."""

    def test_strip_interrupt_keyword_prefix(self):
        from core.interrupt_monitor import strip_interrupt_prefix
        assert strip_interrupt_prefix("停，改成多伦多的天气") == "改成多伦多的天气"
        assert strip_interrupt_prefix("等一下帮我查下明天") == "帮我查下明天"
        assert strip_interrupt_prefix("停") == ""
        assert strip_interrupt_prefix("明天天气怎么样") == "明天天气怎么样"

    def test_strip_handles_whitespace_and_punctuation(self):
        from core.interrupt_monitor import strip_interrupt_prefix
        assert strip_interrupt_prefix("停 改成多伦多") == "改成多伦多"
        assert strip_interrupt_prefix("等一下，查下天气") == "查下天气"
        assert strip_interrupt_prefix("打住。我要说的是") == "我要说的是"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_interrupt_integration.py -v`
Expected: FAIL — `_interrupted_response` not defined, `strip_interrupt_prefix` not importable

- [ ] **Step 3: Add _interrupted_response and _active_pipeline to JarvisApp.__init__**

In `jarvis.py`, after `self._tts_future: Future | None = None` (line 246), add:

```python
        self._active_pipeline: Any = None  # current TTSPipeline for interrupt abort
        self._interrupted_response: list[str] | None = None
```

- [ ] **Step 4: Add resume check at the top of _process_turn**

In `jarvis.py`, in `_process_turn`, right after `history = self.conversation_store.get_history(session_id)` (line 653), add:

```python
        # Resume from interruption: "继续说" etc
        _RESUME_KEYWORDS = {"继续说", "接着说", "你继续", "继续"}
        if self._interrupted_response and any(kw in text for kw in _RESUME_KEYWORDS):
            sentences = self._interrupted_response
            self._interrupted_response = None
            for s in sentences:
                output_fn(s)
            full_text = "".join(sentences)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": full_text})
            self.conversation_store.replace(session_id, history)
            return full_text
```

- [ ] **Step 5: Store pipeline on self and save remaining on abort**

In `jarvis.py`, in `_process_turn`, where the pipeline is created (line ~870):

Change:
```python
            tts_pipeline = create_tts_pipeline() if create_tts_pipeline else None
```
To:
```python
            tts_pipeline = create_tts_pipeline() if create_tts_pipeline else None
            self._active_pipeline = tts_pipeline
```

In the `finally` block (line ~929), before `tts_pipeline.stop()`, add cleanup:
```python
            finally:
                self._active_pipeline = None
                if tts_pipeline:
                    if sentence_count > 0:
                        tts_pipeline.finish()
                        tts_pipeline.wait_done()
                    tts_pipeline.stop()
```

- [ ] **Step 6: Fix _cancel_current to use pipeline abort and save remaining**

Replace `_cancel_current` (lines 1044–1062):

```python
    def _cancel_current(self) -> None:
        """Cancel current TTS and reset state after user interrupt."""
        # Abort active pipeline — kills playback, returns unplayed sentences
        if self._active_pipeline:
            remaining = self._active_pipeline.abort()
            if remaining:
                self._interrupted_response = remaining
        # Kill non-pipeline TTS (local shortcuts)
        if self._tts_future and not self._tts_future.done():
            self._tts_future.cancel()
        tts = self._get_tts()
        if tts:
            tts.stop()
        # Cancel learning subprocess
        if hasattr(self, "skill_factory"):
            try:
                self.skill_factory.cancel()
            except Exception:
                pass
        self.event_bus.emit("jarvis.state_changed", {"state": "idle"})
```

- [ ] **Step 7: Create stub for strip_interrupt_prefix**

```python
# core/interrupt_monitor.py
"""Full-duplex interrupt monitoring during TTS playback.

Detects voice interrupts via streaming ASR keyword matching and
provides utilities for interrupt content processing.
"""

from __future__ import annotations

import re

INTERRUPT_KEYWORDS = frozenset({
    "等一下", "停", "打住", "暂停", "等等", "你听我说",
    "不对", "你理解错了", "不是这样", "说错了",
})

RESUME_KEYWORDS = frozenset({
    "继续说", "接着说", "你继续", "继续",
})

# Pattern: keyword optionally followed by punctuation/space, then content
_STRIP_RE = re.compile(
    r"^(" + "|".join(re.escape(kw) for kw in sorted(INTERRUPT_KEYWORDS, key=len, reverse=True))
    + r")[，,。.！!？?\s]*",
)


def strip_interrupt_prefix(text: str) -> str:
    """Remove leading interrupt keyword + trailing punctuation from text.

    >>> strip_interrupt_prefix("停，改成多伦多的天气")
    '改成多伦多的天气'
    >>> strip_interrupt_prefix("明天天气怎么样")
    '明天天气怎么样'
    """
    return _STRIP_RE.sub("", text)
```

- [ ] **Step 8: Run tests**

Run: `python -m pytest tests/test_interrupt_integration.py -v`
Expected: PASS

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 9: Commit**

```bash
git add jarvis.py core/interrupt_monitor.py tests/test_interrupt_integration.py
git commit -m "feat: interrupt resume mechanism + keyword stripping + _cancel_current fix"
```

---

## Task 4: Download streaming ASR model

**Files:**
- Create: `scripts/download_streaming_model.sh`
- Modify: `config.yaml`

- [ ] **Step 1: Create download script**

```bash
# scripts/download_streaming_model.sh
#!/usr/bin/env bash
# Download sherpa-onnx streaming zipformer model for interrupt keyword detection.
# Small bilingual zh-en model (~30MB).
set -euo pipefail

MODEL_NAME="sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16"
MODEL_DIR="data/${MODEL_NAME}"

if [ -d "$MODEL_DIR" ]; then
    echo "Model already exists at $MODEL_DIR"
    exit 0
fi

echo "Downloading ${MODEL_NAME}..."
cd data
wget -q "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/${MODEL_NAME}.tar.bz2"
tar xf "${MODEL_NAME}.tar.bz2"
rm "${MODEL_NAME}.tar.bz2"
echo "Done: ${MODEL_DIR}"
```

- [ ] **Step 2: Run the download**

```bash
chmod +x scripts/download_streaming_model.sh
bash scripts/download_streaming_model.sh
```

Expected: `data/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16/` directory with `encoder-epoch-99-avg-1.onnx`, `decoder-epoch-99-avg-1.onnx`, `joiner-epoch-99-avg-1.onnx`, `tokens.txt`.

- [ ] **Step 3: Add config section**

Append to `config.yaml` (at the end, before any trailing comments):

```yaml
# Full-duplex interrupt detection
interrupt:
  enabled: true
  keywords: ["等一下", "停", "打住", "暂停", "等等", "你听我说",
             "不对", "你理解错了", "不是这样", "说错了"]
  resume_keywords: ["继续说", "接着说", "你继续", "继续"]
  streaming_asr:
    model_dir: data/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16
    num_threads: 2
  vad_threshold_during_tts: 0.8
```

- [ ] **Step 4: Add model dir to .gitignore**

Append to `.gitignore`:
```
data/sherpa-onnx-streaming-*
```

- [ ] **Step 5: Commit**

```bash
git add scripts/download_streaming_model.sh config.yaml .gitignore
git commit -m "chore: streaming ASR model download script + interrupt config"
```

---

## Task 5: InterruptMonitor — streaming ASR + keyword detection

**Files:**
- Modify: `core/interrupt_monitor.py`
- Create: `tests/test_interrupt_monitor.py`

- [ ] **Step 1: Write failing tests for InterruptMonitor**

```python
# tests/test_interrupt_monitor.py
"""Tests for InterruptMonitor — streaming ASR keyword detection."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.interrupt_monitor import (
    InterruptMonitor,
    INTERRUPT_KEYWORDS,
    RESUME_KEYWORDS,
    strip_interrupt_prefix,
)


class TestInterruptMonitorKeywordMatch:
    def test_detects_interrupt_keyword_in_partial(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        # Simulate streaming ASR partial result
        monitor._check_partial("停")
        assert len(detected) == 1

    def test_ignores_non_keyword(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("明天天气")
        assert len(detected) == 0

    def test_detects_keyword_as_substring(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        # "停" appears in "停改成多伦多" — should trigger
        monitor._check_partial("停改成多伦多")
        assert len(detected) == 1

    def test_detects_resume_keyword(self):
        resume_detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_resume=lambda: resume_detected.append("resume"),
        )
        monitor._check_partial("继续说")
        assert len(resume_detected) == 1

    def test_fires_only_once_per_session(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        monitor._check_partial("停停停")
        assert len(detected) == 1  # not 2

    def test_reset_allows_new_detection(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: detected.append("interrupt"),
        )
        monitor._check_partial("停")
        assert len(detected) == 1
        monitor.reset()
        monitor._check_partial("停")
        assert len(detected) == 2


class TestInterruptMonitorAudio:
    def test_feed_audio_accepts_float32_array(self):
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        # Should not raise even without a real streaming recognizer
        audio = np.zeros(1600, dtype=np.float32)
        monitor.feed_audio(audio, sample_rate=16000)

    def test_disabled_monitor_does_nothing(self):
        detected = []
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": False}},
            on_interrupt=lambda: detected.append("x"),
        )
        monitor._check_partial("停")
        assert len(detected) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_interrupt_monitor.py -v`
Expected: FAIL — `InterruptMonitor` class not defined

- [ ] **Step 3: Implement InterruptMonitor**

Replace `core/interrupt_monitor.py` with the full implementation:

```python
"""Full-duplex interrupt monitoring during TTS playback.

Detects voice interrupts via streaming ASR keyword matching and
provides utilities for interrupt content processing.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Callable

import numpy as np

LOGGER = logging.getLogger(__name__)

INTERRUPT_KEYWORDS = frozenset({
    "等一下", "停", "打住", "暂停", "等等", "你听我说",
    "不对", "你理解错了", "不是这样", "说错了",
})

RESUME_KEYWORDS = frozenset({
    "继续说", "接着说", "你继续", "继续",
})

# Pattern: keyword optionally followed by punctuation/space, then content
_STRIP_RE = re.compile(
    r"^(" + "|".join(re.escape(kw) for kw in sorted(INTERRUPT_KEYWORDS, key=len, reverse=True))
    + r")[，,。.！!？?\s]*",
)


def strip_interrupt_prefix(text: str) -> str:
    """Remove leading interrupt keyword + trailing punctuation from text.

    >>> strip_interrupt_prefix("停，改成多伦多的天气")
    '改成多伦多的天气'
    >>> strip_interrupt_prefix("明天天气怎么样")
    '明天天气怎么样'
    """
    return _STRIP_RE.sub("", text)


class InterruptMonitor:
    """Monitor audio during TTS playback for interrupt keywords.

    Feeds audio chunks to a streaming ASR recognizer, checks partial
    results against keyword sets, and fires callbacks on detection.

    Args:
        config: Application config dict (reads ``interrupt`` section).
        on_interrupt: Called when an interrupt keyword is detected.
        on_resume: Called when a resume keyword is detected.
    """

    def __init__(
        self,
        config: dict,
        on_interrupt: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        icfg = config.get("interrupt", {})
        self.enabled = bool(icfg.get("enabled", False))
        self._on_interrupt = on_interrupt
        self._on_resume = on_resume
        self._fired = False  # prevent double-fire per session
        self._lock = threading.Lock()

        # Custom keyword sets from config (fallback to defaults)
        kw_list = icfg.get("keywords")
        self._interrupt_kw = frozenset(kw_list) if kw_list else INTERRUPT_KEYWORDS
        resume_list = icfg.get("resume_keywords")
        self._resume_kw = frozenset(resume_list) if resume_list else RESUME_KEYWORDS

        # Streaming ASR recognizer (lazy-loaded)
        self._recognizer: Any = None
        self._stream: Any = None
        self._asr_config = icfg.get("streaming_asr", {})

        # Audio accumulator for post-interrupt re-transcription
        self._audio_chunks: list[np.ndarray] = []
        self._recording = False

    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts."""
        if not self.enabled:
            return
        self._fired = False
        self._audio_chunks = []
        self._recording = True
        self._load_recognizer()
        if self._recognizer:
            self._stream = self._recognizer.create_stream()

    def stop(self) -> np.ndarray | None:
        """End monitoring session. Returns accumulated audio or None."""
        self._recording = False
        if self._stream and self._recognizer:
            # Flush any remaining
            try:
                self._recognizer.decode_stream(self._stream)
            except Exception:
                pass
            self._stream = None
        if self._audio_chunks:
            return np.concatenate(self._audio_chunks)
        return None

    def reset(self) -> None:
        """Reset fired state so new detections can trigger."""
        with self._lock:
            self._fired = False
        if self._recognizer and self._stream is None:
            self._stream = self._recognizer.create_stream()

    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Args:
            audio: Float32 mono audio samples.
            sample_rate: Sample rate (must be 16000).
        """
        if not self.enabled or not self._recording:
            return

        # Accumulate for post-interrupt re-transcription
        self._audio_chunks.append(audio.copy())

        # Feed to streaming ASR
        if self._stream and self._recognizer:
            try:
                self._stream.accept_waveform(sample_rate, audio)
                while self._recognizer.is_ready(self._stream):
                    self._recognizer.decode_stream(self._stream)
                result = self._recognizer.get_result(self._stream)
                if result.text.strip():
                    self._check_partial(result.text.strip())
            except Exception as exc:
                LOGGER.debug("Streaming ASR error: %s", exc)

    def _check_partial(self, text: str) -> None:
        """Check a partial ASR result against keyword sets."""
        if not self.enabled:
            return
        with self._lock:
            if self._fired:
                return
            # Check interrupt keywords
            for kw in self._interrupt_kw:
                if kw in text:
                    self._fired = True
                    if self._on_interrupt:
                        self._on_interrupt()
                    return
            # Check resume keywords
            for kw in self._resume_kw:
                if kw in text:
                    self._fired = True
                    if self._on_resume:
                        self._on_resume()
                    return

    def _load_recognizer(self) -> None:
        """Lazy-load the sherpa-onnx streaming recognizer."""
        if self._recognizer is not None:
            return
        model_dir = self._asr_config.get("model_dir", "")
        if not model_dir:
            LOGGER.info("No streaming ASR model configured; keyword detection disabled")
            return
        try:
            import sherpa_onnx
            from pathlib import Path
            p = Path(model_dir)
            encoder = str(p / "encoder-epoch-99-avg-1.onnx")
            decoder = str(p / "decoder-epoch-99-avg-1.onnx")
            joiner = str(p / "joiner-epoch-99-avg-1.onnx")
            tokens = str(p / "tokens.txt")
            if not Path(encoder).exists():
                LOGGER.warning("Streaming ASR model not found at %s", model_dir)
                return
            num_threads = int(self._asr_config.get("num_threads", 2))
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                encoder=encoder,
                decoder=decoder,
                joiner=joiner,
                tokens=tokens,
                num_threads=num_threads,
                sample_rate=16000,
                feature_dim=80,
            )
            LOGGER.info("Streaming ASR loaded from %s", model_dir)
        except ImportError:
            LOGGER.warning("sherpa-onnx not installed; streaming ASR unavailable")
        except Exception as exc:
            LOGGER.warning("Failed to load streaming ASR: %s", exc)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_interrupt_monitor.py -v`
Expected: PASS (8 tests)

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 5: Commit**

```bash
git add core/interrupt_monitor.py tests/test_interrupt_monitor.py
git commit -m "feat: InterruptMonitor — streaming ASR keyword detection"
```

---

## Task 6: Wire InterruptMonitor into _process_turn

**Files:**
- Modify: `jarvis.py:100-250` (__init__, add monitor)
- Modify: `jarvis.py:864-934` (_process_turn streaming section)
- Modify: `jarvis.py:356-440` (run_always_listening, start mic stream during TTS)

- [ ] **Step 1: Write failing test for interrupt during TTS**

Append to `tests/test_interrupt_integration.py`:

```python
class TestInterruptDuringTTS:
    def _make_app(self, tmp_path):
        from tests.test_jarvis import _make_config
        config = _make_config(tmp_path)
        config["interrupt"] = {"enabled": True}
        with patch("core.speaker_encoder.SpeakerEncoder"), \
             patch("core.speaker_verifier.SpeakerVerifier"), \
             patch("core.speech_recognizer.SpeechRecognizer"), \
             patch("core.audio_recorder.AudioRecorder"), \
             patch("core.llm.LLMClient"), \
             patch("devices.device_manager.DeviceManager"):
            from jarvis import JarvisApp
            app = JarvisApp(config, config_path=tmp_path / "config.yaml")
            return app

    def test_app_has_interrupt_monitor(self, tmp_path):
        app = self._make_app(tmp_path)
        assert hasattr(app, "interrupt_monitor")
        from core.interrupt_monitor import InterruptMonitor
        assert isinstance(app.interrupt_monitor, InterruptMonitor)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_interrupt_integration.py::TestInterruptDuringTTS -v`
Expected: FAIL — `interrupt_monitor` not defined

- [ ] **Step 3: Add InterruptMonitor to JarvisApp.__init__**

In `jarvis.py`, after the learning router section (around line 228), add:

```python
        # --- Interrupt monitor (full-duplex) ---
        from core.interrupt_monitor import InterruptMonitor
        self.interrupt_monitor = InterruptMonitor(
            config=config,
            on_interrupt=self._on_voice_interrupt,
            on_resume=self._on_voice_resume,
        )
```

- [ ] **Step 4: Add interrupt callback methods**

In `jarvis.py`, before `_cancel_current`, add:

```python
    def _on_voice_interrupt(self) -> None:
        """Called by InterruptMonitor when an interrupt keyword is detected."""
        self.logger.info("Voice interrupt detected")
        self._cancel.set()
        self._cancel_current()

    def _on_voice_resume(self) -> None:
        """Called by InterruptMonitor when a resume keyword is detected."""
        self.logger.info("Voice resume detected")
        # Resume is handled in _process_turn via _interrupted_response
        # Just stop TTS so the pipeline returns control
        self._cancel.set()
        self._cancel_current()
```

- [ ] **Step 5: Start/stop InterruptMonitor in _process_turn streaming section**

In `_process_turn`, in the Cloud LLM section, after `tts_pipeline = create_tts_pipeline() ...` and `self._active_pipeline = tts_pipeline`:

```python
            # Start interrupt monitoring during TTS playback
            self.interrupt_monitor.start()
```

In the `finally` block, before `self._active_pipeline = None`:

```python
            finally:
                # Stop interrupt monitor, get accumulated audio
                interrupt_audio = self.interrupt_monitor.stop()
                self._active_pipeline = None
```

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/test_interrupt_integration.py -v`
Expected: PASS

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 7: Commit**

```bash
git add jarvis.py tests/test_interrupt_integration.py
git commit -m "feat: wire InterruptMonitor into _process_turn pipeline"
```

---

## Task 7: Mic listener thread for feeding audio during TTS

**Files:**
- Modify: `core/interrupt_monitor.py` (add `start_mic_listener`/`stop_mic_listener`)
- Modify: `tests/test_interrupt_monitor.py`

This is the piece that connects the actual microphone to the InterruptMonitor. Without XVF3800 AEC, the mic will pick up TTS audio — which is expected and will be filtered by hardware AEC once the XVF3800 is connected.

- [ ] **Step 1: Write failing test**

Append to `tests/test_interrupt_monitor.py`:

```python
class TestMicListener:
    def test_start_stop_mic_listener(self):
        """Mic listener should start/stop without errors (mocked sounddevice)."""
        monitor = InterruptMonitor(
            config={"interrupt": {"enabled": True}},
            on_interrupt=lambda: None,
        )
        mock_stream = MagicMock()
        mock_stream.read.return_value = (np.zeros((1600, 1), dtype="float32"), None)
        with patch("sounddevice.InputStream", return_value=mock_stream):
            monitor.start()
            monitor.start_mic_listener()
            time.sleep(0.2)
            monitor.stop_mic_listener()
            monitor.stop()
            mock_stream.start.assert_called_once()
            mock_stream.stop.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_interrupt_monitor.py::TestMicListener -v`
Expected: FAIL — `start_mic_listener` not defined

- [ ] **Step 3: Implement mic listener**

Add to `InterruptMonitor` class in `core/interrupt_monitor.py`:

```python
    def start_mic_listener(self, sample_rate: int = 16000, block_size: int = 1600) -> None:
        """Open a microphone stream and feed audio to the monitor.

        The stream runs in a background thread until ``stop_mic_listener``
        is called.  Designed for use during TTS playback when the main
        recording pipeline is idle.
        """
        if not self.enabled:
            return
        import sounddevice as sd
        self._mic_stop = threading.Event()
        self._mic_stream = sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block_size,
        )
        self._mic_stream.start()

        def _reader():
            while not self._mic_stop.is_set():
                try:
                    data, _ = self._mic_stream.read(block_size)
                    self.feed_audio(data[:, 0], sample_rate)
                except Exception:
                    break

        self._mic_thread = threading.Thread(target=_reader, daemon=True, name="interrupt-mic")
        self._mic_thread.start()
        LOGGER.debug("Interrupt mic listener started")

    def stop_mic_listener(self) -> None:
        """Stop the background microphone stream."""
        if hasattr(self, "_mic_stop"):
            self._mic_stop.set()
        if hasattr(self, "_mic_stream") and self._mic_stream:
            try:
                self._mic_stream.stop()
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
        if hasattr(self, "_mic_thread") and self._mic_thread:
            self._mic_thread.join(timeout=2)
            self._mic_thread = None
        LOGGER.debug("Interrupt mic listener stopped")
```

- [ ] **Step 4: Wire mic listener into _process_turn**

In `jarvis.py`, in `_process_turn` streaming section, after `self.interrupt_monitor.start()`:

```python
            self.interrupt_monitor.start_mic_listener()
```

In the `finally` block, before `interrupt_audio = self.interrupt_monitor.stop()`:

```python
                self.interrupt_monitor.stop_mic_listener()
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_interrupt_monitor.py -v`
Expected: PASS

Run: `python -m pytest tests/ -q --tb=short 2>&1 | tail -5`
Expected: No new failures

- [ ] **Step 6: Commit**

```bash
git add core/interrupt_monitor.py jarvis.py tests/test_interrupt_monitor.py
git commit -m "feat: mic listener thread for interrupt detection during TTS"
```

---

## Post-Implementation: Hardware Testing Checklist (XVF3800)

When the XVF3800 arrives, test with:

1. **AEC baseline**: Play TTS → speak "停" at normal volume → measure detection rate (target: >80%)
2. **Latency**: Measure time from saying "停" to TTS silence (target: <600ms)
3. **Keyword + content**: Say "停，改成多伦多的天气" → verify full text captured
4. **Resume**: Interrupt mid-response → say "继续说" → verify remaining sentences play
5. **False positives**: Let TTS play without speaking → verify no false interrupts
6. **VAD threshold tuning**: Adjust `vad_threshold_during_tts` if false positive rate is high
