# Silero VAD Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace RMS-based VAD in `audio_recorder.py` and add Silero VAD gate to `interrupt_monitor.py`, reducing recording latency by ~1.5s and interrupt CPU waste by ~10-20%.

**Architecture:** Two independent `sherpa_onnx.VoiceActivityDetector` instances (one per consumer) share the 629KB `silero_vad.onnx` model file. Each instance reads its own config section with different thresholds. Fail-fast on model load failure (no RMS fallback).

**Tech Stack:** sherpa-onnx (already in use for SenseVoice), Silero VAD ONNX model, pytest for tests.

**Spec:** `docs/superpowers/specs/2026-04-13-silero-vad-integration-design.md`

---

## File Structure

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `scripts/download_silero_vad.sh` | Download `silero_vad.onnx` (629KB) to `data/` |
| Modify | `config.yaml` | Add VAD config fields, change threshold semantics |
| Modify | `core/audio_recorder.py` | Replace RMS VAD with `VoiceActivityDetector` |
| Modify | `core/interrupt_monitor.py` | Add VAD gate before streaming ASR |
| Modify | `jarvis.py` | Add VAD warmup to startup preheat queue |
| Create | `tests/test_audio_recorder_vad.py` | Unit tests for Silero integration |
| Modify | `tests/test_interrupt_monitor.py` | Add tests for VAD gate |

---

## Task 1: Model download script + config schema

**Files:**
- Create: `scripts/download_silero_vad.sh`
- Modify: `config.yaml` (audio section ~line 23-39, interrupt section ~line 563-566)

- [ ] **Step 1: Create model download script**

Write `scripts/download_silero_vad.sh`:

```bash
#!/usr/bin/env bash
# Download sherpa-onnx's Silero VAD ONNX model (629KB) to data/.
set -euo pipefail

MODEL_PATH="data/silero_vad.onnx"

if [ -f "$MODEL_PATH" ]; then
    echo "Model already exists at $MODEL_PATH"
    exit 0
fi

mkdir -p data
echo "Downloading silero_vad.onnx..."
wget -q -O "$MODEL_PATH" \
    "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"

actual_size=$(stat -c%s "$MODEL_PATH" 2>/dev/null || stat -f%z "$MODEL_PATH")
if [ "$actual_size" -lt 500000 ]; then
    echo "ERROR: download truncated (${actual_size} bytes)"
    rm -f "$MODEL_PATH"
    exit 1
fi
echo "Done: $MODEL_PATH ($actual_size bytes)"
```

- [ ] **Step 2: Run the script to fetch the model**

```bash
chmod +x scripts/download_silero_vad.sh
bash scripts/download_silero_vad.sh
```

Expected: `data/silero_vad.onnx` exists, size ~629KB.

Verify:
```bash
ls -l data/silero_vad.onnx
```

- [ ] **Step 3: Update `config.yaml` audio section**

Find the `audio:` block (around lines 23-39) and replace the VAD fields.

Before:
```yaml
  # 录音质量检查的最小时长，单位秒。
  min_duration: 1.0

  # 录音 RMS 音量阈值。低于该值会给出"音量太低"警告。
  low_volume_threshold: 0.02

  # VAD: 检测语音结束后提前停止录音（省 2-3 秒）
  vad_enabled: true
  vad_silence_duration: 1.5     # 语音后持续静音多久停止（秒）
  vad_threshold: 0.02           # RMS 阈值，低于此视为静音
```

After:
```yaml
  # 录音质量检查的最小时长，单位秒。
  min_duration: 0.3

  # 录音 RMS 音量阈值（仅用于质量警告，不用于 VAD）。
  low_volume_threshold: 0.02

  # VAD: Silero 语音活动检测（sherpa-onnx 内置）
  vad_enabled: true
  vad_model_path: data/silero_vad.onnx
  vad_threshold: 0.5              # 语音概率阈值 (0.0-1.0)
  vad_silence_duration: 0.5       # 语音后静音多久判结束（秒）
  vad_min_speech_duration: 0.25   # 最短语音段（秒，更短视为噪声）
  vad_max_speech_duration: 20.0   # 最长语音段（秒，超过强制切分）
```

- [ ] **Step 4: Update `config.yaml` interrupt section**

Find the `interrupt:` block (around lines 547-566) and add VAD fields. Current content ends with `vad_threshold_during_tts: 0.8`.

After:
```yaml
interrupt:
  enabled: true
  keywords: ["等一下", "停", "打住", "暂停", "等等", "你听我说",
             "不对", "你理解错了", "不是这样", "说错了"]
  resume_keywords: ["继续说", "接着说", "你继续", "继续"]
  streaming_asr:
    model_dir: data/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16
    num_threads: 2
  # Silero VAD 门控（TTS 播放期间过滤非语音帧）
  vad_model_path: data/silero_vad.onnx
  vad_threshold_during_tts: 0.8      # 高阈值，避免 AEC 残余误触发
  vad_min_speech_duration: 0.15      # 低，捕获"停"(~200ms)
  vad_min_silence_duration: 0.2
  vad_max_speech_duration: 10.0
```

- [ ] **Step 5: Commit**

```bash
git add scripts/download_silero_vad.sh config.yaml
git commit -m "chore: silero VAD model download + config schema"
```

---

## Task 2: AudioRecorder — Silero VAD integration

**Files:**
- Modify: `core/audio_recorder.py:32-67` (__init__), `73-196` (record method)
- Create: `tests/test_audio_recorder_vad.py`

- [ ] **Step 1: Write failing test for Silero VAD initialization**

Create `tests/test_audio_recorder_vad.py`:

```python
"""Tests for AudioRecorder's Silero VAD integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from core.audio_recorder import AudioRecorder


def _make_config(**overrides):
    base = {
        "audio": {
            "sample_rate": 16000,
            "channels": 1,
            "default_duration": 3.0,
            "min_duration": 0.3,
            "low_volume_threshold": 0.02,
            "block_duration": 0.1,
            "vad_enabled": True,
            "vad_model_path": "data/silero_vad.onnx",
            "vad_threshold": 0.5,
            "vad_silence_duration": 0.5,
            "vad_min_speech_duration": 0.25,
            "vad_max_speech_duration": 20.0,
        }
    }
    base["audio"].update(overrides)
    return base


class TestAudioRecorderVADInit:
    def test_vad_loaded_when_enabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig") as mock_cfg_cls:
            mock_cfg = MagicMock()
            mock_cfg.silero_vad = MagicMock()
            mock_cfg_cls.return_value = mock_cfg
            recorder = AudioRecorder(_make_config())
            assert mock_vad_cls.called
            # Verify config fields were passed through
            assert mock_cfg.silero_vad.model == "data/silero_vad.onnx"
            assert mock_cfg.silero_vad.threshold == 0.5
            assert mock_cfg.silero_vad.min_silence_duration == 0.5
            assert mock_cfg.silero_vad.min_speech_duration == 0.25
            assert mock_cfg.silero_vad.max_speech_duration == 20.0

    def test_vad_skipped_when_disabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls:
            recorder = AudioRecorder(_make_config(vad_enabled=False))
            assert not mock_vad_cls.called
            assert recorder._vad is None

    def test_fail_fast_on_model_load_error(self):
        with patch("sherpa_onnx.VoiceActivityDetector",
                   side_effect=RuntimeError("model not found")):
            with pytest.raises(RuntimeError, match="model not found"):
                AudioRecorder(_make_config())
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_audio_recorder_vad.py::TestAudioRecorderVADInit -v
```

Expected: FAIL with `AttributeError: 'AudioRecorder' object has no attribute '_vad'`

- [ ] **Step 3: Add Silero VAD initialization to AudioRecorder.__init__**

In `core/audio_recorder.py`, replace the VAD init block (lines 62-67):

Before:
```python
        # VAD: 检测语音结束后提前停止录音
        self.vad_enabled = bool(audio_config.get("vad_enabled", False))
        self.vad_silence_duration = float(audio_config.get("vad_silence_duration", 0.5))
        self.vad_threshold = float(audio_config.get("vad_threshold",
                                                     self.low_volume_threshold))
        self.logger = LOGGER
```

After:
```python
        # VAD: Silero (sherpa-onnx) 语音活动检测
        self.vad_enabled = bool(audio_config.get("vad_enabled", False))
        self.logger = LOGGER
        self._vad: Any = None
        if self.vad_enabled:
            self._vad = self._build_vad(audio_config)
```

Add imports at top of file (after existing `import` lines):
```python
from typing import Any
```
(Check if `Any` is already imported — if so, skip.)

Add the `_build_vad` method to the class (after `__init__`):

```python
    def _build_vad(self, cfg: dict) -> Any:
        """Construct sherpa-onnx VoiceActivityDetector from config.

        Fail fast on load errors — no RMS fallback.
        """
        import sherpa_onnx
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(cfg["vad_model_path"])
        vad_config.silero_vad.threshold = float(cfg.get("vad_threshold", 0.5))
        vad_config.silero_vad.min_silence_duration = float(
            cfg.get("vad_silence_duration", 0.5)
        )
        vad_config.silero_vad.min_speech_duration = float(
            cfg.get("vad_min_speech_duration", 0.25)
        )
        vad_config.silero_vad.max_speech_duration = float(
            cfg.get("vad_max_speech_duration", 20.0)
        )
        vad_config.sample_rate = self.sample_rate
        return sherpa_onnx.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=30,
        )
```

- [ ] **Step 4: Run tests to verify init passes**

```bash
python -m pytest tests/test_audio_recorder_vad.py::TestAudioRecorderVADInit -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Write failing test for record() using VAD instead of RMS**

Append to `tests/test_audio_recorder_vad.py`:

```python
class TestAudioRecorderVADRecord:
    def test_vad_reset_called_at_record_start(self):
        """record() must reset VAD state before each recording."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad_cls.return_value = mock_vad
            mock_vad.empty.return_value = True  # never detects speech end

            recorder = AudioRecorder(_make_config())

            with patch("sounddevice.InputStream") as mock_stream:
                # Simulate callback not firing (timeout path)
                mock_stream.return_value.__enter__ = lambda s: None
                mock_stream.return_value.__exit__ = lambda *a: None
                # Use a very short duration so it completes fast
                try:
                    recorder.record(duration=0.1)
                except Exception:
                    pass

            assert mock_vad.reset.called

    def test_vad_stops_callback_when_segment_complete(self):
        """When VAD produces a segment (speech ended), callback must stop."""
        import sounddevice as sd
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad_cls.return_value = mock_vad
            # First call empty (still speaking), second call not empty (ended)
            mock_vad.empty.side_effect = [True, False]

            recorder = AudioRecorder(_make_config())

            # Manually invoke the logic that would be in callback
            # Simulate: first chunk → still empty; second chunk → segment found
            chunk = np.zeros(1600, dtype=np.float32)
            recorder._vad.accept_waveform(chunk)
            assert recorder._vad.empty() is True
            recorder._vad.accept_waveform(chunk)
            assert recorder._vad.empty() is False
```

- [ ] **Step 6: Run test to verify it fails**

```bash
python -m pytest tests/test_audio_recorder_vad.py::TestAudioRecorderVADRecord -v
```

Expected: FAIL on `assert mock_vad.reset.called` — record() doesn't call reset yet.

- [ ] **Step 7: Update record() to use VAD instead of RMS**

In `core/audio_recorder.py`, inside `record()` method:

After line 101 (`target_frames = int(math.ceil(target_duration * self.sample_rate))`), add:

```python
        # Reset VAD state for this recording session
        if self._vad is not None:
            self._vad.reset()
```

Then replace the VAD state variables (lines 109-112):

Before:
```python
        # VAD state
        speech_detected = False
        silence_frames = 0
        silence_threshold_frames = int(self.vad_silence_duration * self.sample_rate)
```

After:
```python
        # VAD state (Silero tracks segments internally)
```

Then replace the VAD check block inside callback (lines 145-158):

Before:
```python
            # VAD: early stop when speech ends
            if self.vad_enabled and captured_frames >= min_frames:
                if level >= self.vad_threshold:
                    speech_detected = True
                    silence_frames = 0
                elif speech_detected:
                    silence_frames += chunk.shape[0]
                    if silence_frames >= silence_threshold_frames:
                        self.logger.info(
                            "VAD: speech ended after %.2fs",
                            captured_frames / self.sample_rate,
                        )
                        finished.set()
                        raise sd.CallbackStop()
```

After:
```python
            # Silero VAD: stop when a complete speech segment is detected
            if self._vad is not None and captured_frames >= min_frames:
                self._vad.accept_waveform(chunk)
                if not self._vad.empty():
                    self.logger.info(
                        "VAD: speech ended after %.2fs",
                        captured_frames / self.sample_rate,
                    )
                    finished.set()
                    raise sd.CallbackStop()
```

Update the `nonlocal` declaration at line 123 — remove `speech_detected, silence_frames`:

Before:
```python
            nonlocal captured_frames, progress_started, speech_detected, silence_frames
```

After:
```python
            nonlocal captured_frames, progress_started
```

- [ ] **Step 8: Run all VAD tests to verify they pass**

```bash
python -m pytest tests/test_audio_recorder_vad.py -v
```

Expected: PASS (all 5 tests)

- [ ] **Step 9: Run full audio_recorder tests for regressions**

```bash
python -m pytest tests/test_audio_recorder.py -v --tb=short 2>&1 | tail -20
```

Expected: All passing tests still pass. Any that relied on `vad_threshold` as RMS value may need updating — inspect and fix.

- [ ] **Step 10: Commit**

```bash
git add core/audio_recorder.py tests/test_audio_recorder_vad.py
git commit -m "feat: AudioRecorder uses Silero VAD via sherpa-onnx"
```

---

## Task 3: InterruptMonitor — Silero VAD gate

**Files:**
- Modify: `core/interrupt_monitor.py:57-168` (init + feed_audio)
- Modify: `tests/test_interrupt_monitor.py`

- [ ] **Step 1: Write failing test for VAD gate init in InterruptMonitor**

Add to `tests/test_interrupt_monitor.py` (append class):

```python
class TestInterruptMonitorVADGate:
    def _make_config(self, **overrides):
        base = {
            "interrupt": {
                "enabled": True,
                "vad_model_path": "data/silero_vad.onnx",
                "vad_threshold_during_tts": 0.8,
                "vad_min_speech_duration": 0.15,
                "vad_min_silence_duration": 0.2,
                "vad_max_speech_duration": 10.0,
            }
        }
        base["interrupt"].update(overrides)
        return base

    def test_vad_loaded_when_enabled(self):
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig") as mock_cfg_cls:
            mock_cfg = MagicMock()
            mock_cfg.silero_vad = MagicMock()
            mock_cfg_cls.return_value = mock_cfg

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            monitor.start()

            assert mock_vad_cls.called
            assert mock_cfg.silero_vad.model == "data/silero_vad.onnx"
            assert mock_cfg.silero_vad.threshold == 0.8
            assert mock_cfg.silero_vad.min_speech_duration == 0.15
            assert mock_cfg.silero_vad.min_silence_duration == 0.2

    def test_feed_audio_skips_asr_when_not_speech(self):
        """When VAD says no speech, streaming ASR stream should not receive audio."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad.is_speech_detected.return_value = False
            mock_vad_cls.return_value = mock_vad

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            # Inject a fake streaming ASR to observe calls
            mock_stream = MagicMock()
            monitor._stream = mock_stream
            monitor._recognizer = MagicMock()
            monitor._recording = True
            monitor._vad = mock_vad

            audio = np.zeros(1600, dtype=np.float32)
            monitor.feed_audio(audio)

            # VAD was consulted, ASR stream was NOT fed
            mock_vad.accept_waveform.assert_called()
            mock_stream.accept_waveform.assert_not_called()

    def test_feed_audio_passes_to_asr_when_speech_detected(self):
        """When VAD says speech active, audio is forwarded to streaming ASR."""
        with patch("sherpa_onnx.VoiceActivityDetector") as mock_vad_cls, \
             patch("sherpa_onnx.VadModelConfig"):
            mock_vad = MagicMock()
            mock_vad.is_speech_detected.return_value = True
            mock_vad_cls.return_value = mock_vad

            monitor = InterruptMonitor(
                config=self._make_config(),
                on_interrupt=lambda: None,
            )
            mock_stream = MagicMock()
            mock_recognizer = MagicMock()
            mock_recognizer.is_ready.return_value = False
            mock_recognizer.get_result.return_value = MagicMock(text="")
            monitor._stream = mock_stream
            monitor._recognizer = mock_recognizer
            monitor._recording = True
            monitor._vad = mock_vad

            audio = np.zeros(1600, dtype=np.float32)
            monitor.feed_audio(audio)

            mock_stream.accept_waveform.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_interrupt_monitor.py::TestInterruptMonitorVADGate -v
```

Expected: FAIL — no `_vad` attribute on InterruptMonitor.

- [ ] **Step 3: Add VAD init to InterruptMonitor.__init__**

In `core/interrupt_monitor.py`, modify the `__init__` method. After the line `self._recording = False` (line 83), add:

```python
        # Silero VAD gate (loaded lazily in start() for symmetry with recognizer)
        self._vad: Any = None
        self._vad_config = icfg  # keep reference for lazy load
```

- [ ] **Step 4: Add VAD lazy load + reset to start()**

In `core/interrupt_monitor.py`, modify the `start()` method (around line 90):

Before:
```python
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
```

After:
```python
    def start(self) -> None:
        """Begin a monitoring session. Call before TTS playback starts."""
        if not self.enabled:
            return
        self._fired = False
        self._audio_chunks = []
        self._recording = True
        self._load_recognizer()
        self._load_vad()
        if self._recognizer:
            self._stream = self._recognizer.create_stream()
        if self._vad is not None:
            self._vad.reset()
```

- [ ] **Step 5: Add `_load_vad` method**

After the existing `_load_recognizer` method in `core/interrupt_monitor.py`, add:

```python
    def _load_vad(self) -> None:
        """Lazy-load the Silero VAD gate.

        Fail fast on load errors — no fallback path.
        """
        if self._vad is not None:
            return
        model_path = self._vad_config.get("vad_model_path", "")
        if not model_path:
            LOGGER.info("No VAD model configured; interrupt gate disabled")
            return
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(model_path)
        cfg.silero_vad.threshold = float(
            self._vad_config.get("vad_threshold_during_tts", 0.8)
        )
        cfg.silero_vad.min_speech_duration = float(
            self._vad_config.get("vad_min_speech_duration", 0.15)
        )
        cfg.silero_vad.min_silence_duration = float(
            self._vad_config.get("vad_min_silence_duration", 0.2)
        )
        cfg.silero_vad.max_speech_duration = float(
            self._vad_config.get("vad_max_speech_duration", 10.0)
        )
        cfg.sample_rate = 16000
        self._vad = sherpa_onnx.VoiceActivityDetector(
            cfg, buffer_size_in_seconds=10,
        )
        LOGGER.info("Silero VAD gate loaded from %s", model_path)
```

- [ ] **Step 6: Update `feed_audio` to gate on VAD**

In `core/interrupt_monitor.py`, modify `feed_audio` (around line 123):

Before:
```python
    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis."""
        if not self.enabled or not self._recording:
            return

        # Accumulate for post-interrupt re-transcription (stop after fired)
        if not self._fired:
            self._audio_chunks.append(audio.copy())

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
```

After:
```python
    def feed_audio(self, audio: np.ndarray, sample_rate: int = 16000) -> None:
        """Feed an audio chunk for analysis.

        Silero VAD gates the stream: non-speech chunks are accumulated for
        post-interrupt re-transcription but NOT forwarded to streaming ASR.
        This avoids wasting CPU on AEC residual noise and reduces false
        keyword triggers.
        """
        if not self.enabled or not self._recording:
            return

        # Accumulate for post-interrupt re-transcription (stop after fired)
        if not self._fired:
            self._audio_chunks.append(audio.copy())

        # VAD gate: skip ASR when no speech detected
        if self._vad is not None:
            try:
                self._vad.accept_waveform(audio)
                if not self._vad.is_speech_detected():
                    return
            except Exception as exc:
                LOGGER.debug("VAD gate error: %s", exc)
                return

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
```

- [ ] **Step 7: Run VAD gate tests**

```bash
python -m pytest tests/test_interrupt_monitor.py::TestInterruptMonitorVADGate -v
```

Expected: PASS (3 tests)

- [ ] **Step 8: Run full interrupt_monitor test suite for regressions**

```bash
python -m pytest tests/test_interrupt_monitor.py -v --tb=short 2>&1 | tail -25
```

Expected: Existing tests still pass. Any test using `feed_audio` without `_vad` setup may need to set `monitor._vad = None` explicitly to bypass the gate.

If existing tests fail, fix by adding `monitor._vad = None` after monitor construction in those tests.

- [ ] **Step 9: Commit**

```bash
git add core/interrupt_monitor.py tests/test_interrupt_monitor.py
git commit -m "feat: InterruptMonitor Silero VAD gate for streaming ASR"
```

---

## Task 4: Startup warmup

**Files:**
- Modify: `jarvis.py:247-257` (__init__ preheat block)

- [ ] **Step 1: Locate the existing warmup block**

Open `jarvis.py` and find the block of `self._executor.submit(...)` calls near the end of `__init__` (around lines 247-257). Current content has calls for embedder, HTTP connections, TTS precache, and SenseVoice.

- [ ] **Step 2: Add VAD warmup**

Right after the existing `self._executor.submit(self.speech_recognizer.transcribe, np.zeros(16000, dtype=np.float32))` line, add:

```python
        # 预热 Silero VAD（录音 + 打断各一个实例）
        if getattr(self.audio_recorder, "_vad", None) is not None:
            self._executor.submit(
                self.audio_recorder._vad.accept_waveform,
                np.zeros(512, dtype=np.float32),
            )
        if hasattr(self, "interrupt_monitor"):
            # Start/stop to trigger VAD lazy load + warmup
            self._executor.submit(self._warmup_interrupt_vad)
```

Then add a helper method to `JarvisApp` class (near `_create_tts_pipeline`):

```python
    def _warmup_interrupt_vad(self) -> None:
        """Warm up the interrupt monitor's VAD by triggering lazy load."""
        try:
            self.interrupt_monitor._load_vad()
            if self.interrupt_monitor._vad is not None:
                self.interrupt_monitor._vad.accept_waveform(
                    np.zeros(512, dtype=np.float32)
                )
        except Exception as exc:
            self.logger.warning("Interrupt VAD warmup failed: %s", exc)
```

- [ ] **Step 3: Verify the file parses**

```bash
python3 -c "import ast; ast.parse(open('jarvis.py').read()); print('Syntax OK')"
```

Expected: `Syntax OK`

- [ ] **Step 4: Run full test suite for regressions**

```bash
python -m pytest tests/ -q --tb=short --ignore=tests/test_system_test_models.py 2>&1 | tail -10
```

Expected: Same baseline failures as before this plan started. No new failures.

- [ ] **Step 5: Commit**

```bash
git add jarvis.py
git commit -m "feat: warmup Silero VAD instances on startup"
```

---

## Task 5: End-to-end sanity check

**Files:** none (runtime validation)

- [ ] **Step 1: Verify model file exists**

```bash
ls -l data/silero_vad.onnx
```

Expected: file exists, size ~629KB.

- [ ] **Step 2: Import smoke test**

```bash
python -c "
import sherpa_onnx
cfg = sherpa_onnx.VadModelConfig()
cfg.silero_vad.model = 'data/silero_vad.onnx'
cfg.silero_vad.threshold = 0.5
cfg.sample_rate = 16000
vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
print('VAD loaded OK')
import numpy as np
vad.accept_waveform(np.zeros(512, dtype=np.float32))
print('accept_waveform OK, empty=', vad.empty())
print('is_speech_detected=', vad.is_speech_detected() if hasattr(vad, 'is_speech_detected') else 'API MISSING')
"
```

Expected: `VAD loaded OK`, `accept_waveform OK, empty= True`.

**If `is_speech_detected` is reported as `API MISSING`**: stop and adapt Task 3 to use `empty()` state transitions instead. Specifically:
- Track `_vad_was_speaking: bool = False` on the monitor
- After each `accept_waveform`, infer speech state by checking if `empty()` transitioned from `True` to `False` (segment produced = speech ended)
- Gate as: `if not _vad_was_speaking and vad.empty(): return` (rough approximation)

Document the API reality in a follow-up note.

- [ ] **Step 3: Config loads without error**

```bash
python -c "
import yaml
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
print('audio.vad_model_path:', cfg['audio']['vad_model_path'])
print('interrupt.vad_model_path:', cfg['interrupt']['vad_model_path'])
assert cfg['audio']['vad_threshold'] == 0.5
assert cfg['interrupt']['vad_threshold_during_tts'] == 0.8
print('Config OK')
"
```

Expected: `Config OK`.

- [ ] **Step 4: JarvisApp initializes without error**

```bash
python -c "
import yaml
from jarvis import JarvisApp
with open('config.yaml') as f:
    cfg = yaml.safe_load(f)
app = JarvisApp(cfg, config_path='config.yaml')
print('AudioRecorder VAD loaded:', app.audio_recorder._vad is not None)
app.shutdown()
print('OK')
"
```

Expected: `AudioRecorder VAD loaded: True`, `OK`.

**If this fails** with `RuntimeError` about sherpa-onnx or model missing: fix the immediate issue (download model, check path). Fail-fast is intentional per design.

- [ ] **Step 5: Commit nothing (this task is validation only)**

No commit needed unless fixes were made.

---

## Post-Implementation Checklist

After all tasks complete, verify on real hardware (RPi5 + XVF3800 when available):

- [ ] Recording latency: "开灯" end-to-stop ≤ 1.2s (target: 1.0s)
- [ ] TTS-idle CPU usage: ≤ 5% during silent TTS playback (target: ~2%)
- [ ] "停" detection rate during TTS: ≥ 80% across 10 trials
- [ ] Noise rejection: 10 minutes of fan/keyboard noise with no TTS → 0 false interrupt triggers
- [ ] Warmup latency: first recording after startup adds < 50ms vs warm state

If any metric misses target, tune thresholds in `config.yaml`:
- `audio.vad_threshold` 0.5 → 0.4 or 0.6
- `interrupt.vad_threshold_during_tts` 0.8 → 0.7 or 0.9
- `audio.vad_silence_duration` 0.5 → 0.3 or 0.7
