# Silero VAD Integration Design

**Date**: 2026-04-13
**Status**: Ready for implementation plan (待 XVF3800 硬件到位实测)
**Related**: `notes/full-duplex-interrupt-design-2026-04-13.md`, `docs/superpowers/plans/2026-04-13-full-duplex-interrupt.md`

## Goal

用 Silero VAD 替换 `audio_recorder.py` 的 RMS 音量判断，同时给 `interrupt_monitor.py` 的 streaming ASR 加门控。目标：录音延迟省 1.5 秒、打断 CPU 省 10-20%、打断误触发大幅降低。

## Background

当前两个 VAD 问题：

1. **`core/audio_recorder.py:146-158`** — RMS 音量阈值（`vad_threshold=0.02`）。门关声、风扇、咳嗽都误触发；安静起始的说话漏检；`vad_silence_duration=1.5s` 是为防 RMS 误判中间停顿。

2. **`core/interrupt_monitor.py:feed_audio`** — 无 VAD 门控。TTS 播放期间 mic 每一帧都喂给 streaming ASR，CPU 浪费 + XVF3800 AEC 残余可能被幻觉识别成关键词触发打断。

## Architecture

**零新依赖** — sherpa-onnx（已在用于 SenseVoice）内置 Silero VAD。只需下载 629KB 模型文件 `silero_vad.onnx`。

**两个 `VoiceActivityDetector` 实例，独立配置，共享模型文件**：
- 录音实例：`core/audio_recorder.py` 内部创建，检测说话结束
- 打断实例：`core/interrupt_monitor.py` 内部创建，门控 streaming ASR

**代码组织**：直接使用（approach A）。两个消费者各自 `import sherpa_onnx` 创建 VAD，不抽 `core/vad.py` 服务层。参数从各自 config section 读取。

**失败策略**：Fail fast。Silero 模型加载失败 → 启动报错退出。无 RMS fallback 路径。

## Config Schema Changes

```yaml
audio:
  min_duration: 0.3                    # 从 1.0 降（Silero 不需要启动保护窗口）
  vad_enabled: true
  vad_model_path: data/silero_vad.onnx # 新
  vad_threshold: 0.5                   # 语义变化：RMS 音量 → 语音概率
  vad_silence_duration: 0.5            # 从 1.5 降
  vad_min_speech_duration: 0.25        # 新
  vad_max_speech_duration: 20.0        # 新

interrupt:
  # ... 保留现有 keywords, resume_keywords, streaming_asr 等
  vad_model_path: data/silero_vad.onnx # 新（同一文件）
  vad_threshold_during_tts: 0.8        # 已存在，继续用作 silero threshold
  vad_min_speech_duration: 0.15        # 新（低，捕获"停"）
  vad_min_silence_duration: 0.2        # 新（低，快速响应）
  vad_max_speech_duration: 10.0        # 新
```

**向后兼容性**：无。`vad_threshold` 从 0.02（RMS 音量）变成 0.5（概率）。现有用户必须更新 config.yaml。因选 fail fast，不保留 RMS 路径。

## Component 1: audio_recorder.py 改造

### 当前逻辑

```python
# 回调内 (line 146-158)
if level >= self.vad_threshold:          # RMS
    speech_detected = True
elif speech_detected:
    silence_frames += chunk.shape[0]
    if silence_frames >= silence_threshold_frames:
        finished.set()
        raise sd.CallbackStop()
```

### 改造后

在 `__init__` 读 config，创建 `VoiceActivityDetector`：

```python
from sherpa_onnx import VoiceActivityDetector, VadModelConfig

vad_config = VadModelConfig()
vad_config.silero_vad.model = cfg["vad_model_path"]
vad_config.silero_vad.threshold = float(cfg["vad_threshold"])
vad_config.silero_vad.min_silence_duration = float(cfg["vad_silence_duration"])
vad_config.silero_vad.min_speech_duration = float(cfg["vad_min_speech_duration"])
vad_config.silero_vad.max_speech_duration = float(cfg["vad_max_speech_duration"])
vad_config.sample_rate = self.sample_rate
self._vad = VoiceActivityDetector(vad_config, buffer_size_in_seconds=30)
```

`record()` 开始时调 `self._vad.reset()` 清除上次状态。

回调内：

```python
self._vad.accept_waveform(chunk)
if not self._vad.empty():
    # VAD 已检测到完整语音段（说完 + min_silence 静音）
    finished.set()
    raise sd.CallbackStop()
```

- 不再手动跟踪 `speech_detected` / `silence_frames`
- `min_silence_duration` 作为 VAD 内部参数，不在外层循环
- 外层 `target_duration` 硬上限保留作保底

### 预期效果

用户说 "开灯" (~500ms)：

| 时刻 | 当前（RMS） | 改后（Silero） |
|------|-----------|-------------|
| 0.0s | 录音开始 | 录音开始 |
| 0.5s | 说完 | 说完 |
| 1.0s | min_duration 满，开始累计静音 | VAD 已累计 0.5s 静音 |
| 1.0s | — | VAD 产出段，停 |
| 2.5s | 1.5s 静音累计满，停 | — |

**总时长 2.5s → 1.0s，省 1.5s**。

## Component 2: interrupt_monitor.py 改造

### 当前逻辑

```python
def feed_audio(self, audio, sample_rate=16000):
    if not self._recording:
        return
    self._audio_chunks.append(audio.copy())
    if self._stream and self._recognizer:
        self._stream.accept_waveform(sample_rate, audio)  # 所有帧都喂
        ...
```

### 改造后

`__init__` 创建第二个 VAD 实例（参数不同）：

```python
icfg = config["interrupt"]
vad_config = VadModelConfig()
vad_config.silero_vad.model = icfg["vad_model_path"]
vad_config.silero_vad.threshold = float(icfg["vad_threshold_during_tts"])   # 0.8
vad_config.silero_vad.min_speech_duration = float(icfg["vad_min_speech_duration"])   # 0.15
vad_config.silero_vad.min_silence_duration = float(icfg["vad_min_silence_duration"]) # 0.2
vad_config.silero_vad.max_speech_duration = float(icfg["vad_max_speech_duration"])   # 10
vad_config.sample_rate = 16000
self._vad = VoiceActivityDetector(vad_config, buffer_size_in_seconds=10)
```

`start()` 每次 `self._vad.reset()`。

`feed_audio`：

```python
def feed_audio(self, audio, sample_rate=16000):
    if not self._recording:
        return
    self._audio_chunks.append(audio.copy())   # 累计给 SenseVoice 后处理

    self._vad.accept_waveform(audio)
    if not self._vad.is_speech_detected():
        return                                 # ← 门控：非语音帧不喂 ASR

    self._stream.accept_waveform(sample_rate, audio)
    while self._recognizer.is_ready(self._stream):
        self._recognizer.decode_stream(self._stream)
    result = self._recognizer.get_result(self._stream)
    if result.text.strip():
        self._check_partial(result.text.strip())
```

### 预期效果

| 场景 | 当前 | 改后 |
|------|------|------|
| 用户沉默 | streaming ASR 持续跑，10-20% CPU | VAD 门控，~2% CPU |
| AEC 残余噪声 | 可能被 ASR 幻觉识别成关键词 | VAD 过滤，不进 ASR |
| 用户说"停" | ~350ms 检测到 | 同样 ~350ms（加 VAD 推理 +1ms，忽略不计） |

## Model Loading & Startup

下载模型（建议加到 `scripts/download_streaming_model.sh` 或新脚本）：

```bash
cd data
wget -q https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx
```

**启动预热**：`jarvis.py:247-257` 那一堆后台预热调用（SenseVoice、embedding、TTS）里加一行：

```python
self._executor.submit(lambda: self.audio_recorder._vad.accept_waveform(
    np.zeros(512, dtype=np.float32)
))
```

给 ONNX session 首次推理机会暖身，避免第一次录音时多等 10-100ms。

## API 假设

本 spec 假设 sherpa-onnx Python 绑定暴露：
- `VoiceActivityDetector.accept_waveform(samples)` — 喂音频
- `VoiceActivityDetector.empty()` — 段队列空否（audio_recorder 用）
- `VoiceActivityDetector.is_speech_detected()` — 当前是否处于说话状态（interrupt_monitor 用）
- `VoiceActivityDetector.reset()` — 清空内部状态

实现阶段需验证 API 名称。若 `is_speech_detected()` 不存在，备用方案：通过 `accept_waveform` 后检查 `empty()` 状态变化推断说话开始/结束。

## Testing Strategy

### 单元测试（无硬件）

- `tests/test_audio_recorder.py` 加用例：mock `VoiceActivityDetector`，验证 VAD 产段时 callback 停止
- `tests/test_interrupt_monitor.py` 加用例：mock VAD 返回 `is_speech_detected = False`，验证 `_stream.accept_waveform` 不被调用
- 测试参数正确从 config 传到 VoiceActivityDetector

### 集成测试（需硬件）

XVF3800 到位后：
- 正常录音延迟基线测量（目标：说完话到录音停 ≤ 500ms）
- TTS 播放 + 沉默时 CPU 占用（目标：< 5%）
- TTS 播放 + "停" 关键词检测率（目标：> 80%）
- 噪声环境（风扇、键盘声）误触发率（目标：< 1 次/小时）

### 调参

所有阈值可能需要按真实硬件重调：
- `audio.vad_threshold`：0.5 太严或太松
- `interrupt.vad_threshold_during_tts`：0.8 可能需要根据 AEC 残余调整
- `min_speech_duration`：0.15 是否足够捕获"停"

## Non-Goals

- 不做 WebRTC VAD 混合方案（sherpa-onnx Silero 对 RPi5 性能足够）
- 不做自适应阈值（先用固定值，真实数据出来后再决定）
- 不做 VAD 服务层抽象（YAGNI，两个直接用户足够简单）
- 不做运行时 engine 切换（Silero 固定，无 fallback）

## Risks

1. **sherpa-onnx API 可能不如假设**：`is_speech_detected()` 未必存在。需要实现阶段查 Python 绑定实际 API，必要时用 `empty()` 状态变化推断。
2. **模型加载失败导致启动崩溃**：按设计，fail fast。首次部署需要先下载模型。
3. **硬件未到调参靠猜**：所有阈值是保守起点，真实硬件到位后必然调整。
4. **min_speech_duration=0.15 可能漏掉极短命令**：如 "嗯" (100ms) 仍会被过滤。此为已知限制。

## Dependencies

- Silero VAD 模型文件下载（629KB）
- sherpa-onnx Python 绑定支持 `VoiceActivityDetector`（已在使用）
- XVF3800 硬件到位后完成真实调参
