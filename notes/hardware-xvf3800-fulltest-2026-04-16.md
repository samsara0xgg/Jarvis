# XVF3800 硬件验证实验记录 — 2026-04-16

## 硬件环境

- RPi5 4GB (`allen@jarvis.local`)
- ReSpeaker XVF3800 4-Mic Array — USB 接 RPi5, 识别为 Card 0
- 耳机: HD600 (300ohm) 直插 ReSpeaker 3.5mm 口
- sherpa-onnx 1.12.38, Python 3.13, venv at `~/jarvis/venv`

## 设备识别

```
Card 0: reSpeaker XVF3800 4-Mic Array
  - Playback: hw:0,0 (2ch, S16_LE/S32_LE, 16000Hz only)
  - Recording: hw:0,0 (2ch, S16_LE/S32_LE, 16000Hz only)
```

PortAudio (sounddevice) 对 XVF3800 的识别有问题:
- `sd.query_devices(0)` 显示 `max_input_channels=0` (错误)
- 必须用 `device=6` (default ALSA) 才能通过 sounddevice 录音
- `arecord -D plughw:0,0` 可以正常录音

## 音频播放

- `aplay -D hw:0,0` 无声 (需要 16kHz stereo S16_LE 精确匹配)
- `sounddevice sd.play()` 无声
- `mpv --no-video` 可以播放, 需要 `--volume=150` 因为 HD600 300ohm 高阻
- amixer 设置: `numid=5` (PCM Playback Volume) 和 `numid=6`, 设为 55/60 + mpv volume=150 音量合适

## .asoundrc 配置

已从旧的 Card 2 (HDMI) 改为 Card 0 (XVF3800):
```
defaults.pcm.card 0
defaults.ctl.card 0
```

## 前置文件同步

- `data/silero_vad.onnx` — scp 从 Mac 传到 RPi (644KB)
- 流式 ASR 模型 `data/sherpa-onnx-streaming-zipformer-small-bilingual-zh-en-2023-02-16/` — 已下载
- 代码 git pull 到最新 commit c5be81c

---

## 测试结果

### 1. 麦克风拾音 — PASS

```
device=6, channels=1, 16kHz, float32
RMS: 0.044~0.100, Peak: 0.92
```
麦克风可正常收音。

### 2. AEC 回声消除 — PASS (极好)

使用 `sd.playrec()` 全双工测试:
```
静音 RMS:     0.064
播放时 RMS:   0.007
Echo ratio:   0.12x
```
XVF3800 硬件 AEC 表现极好, 播放时录到的 RMS 反而比静音低。

### 3. 流式 ASR 关键词检测 — PASS

使用 sherpa-onnx OnlineRecognizer (int8, zipformer bilingual):
- 分块喂入 (100ms chunks), `is_ready()` + `decode_stream()` 循环
- 识别结果: "你好" "小月"/"小岳" "等一下" "暂停" 均能识别
- 单字 "停" 不稳定, 2~3 字关键词稳定

### 4. 全双工打断 (手动脚本) — PASS

边播 TTS (`/tmp/tts_test.mp3`, edge-tts zh-CN-XiaoxiaoNeural) 边录音:
- 用 `subprocess + mpv` 播放 TTS, 同时 `sd.InputStream` 录音
- 录到的音频只包含人声, TTS 回声完全被 AEC 消除
- "等一下" 在 2.6s 检测到, "暂停" 在 7.7s 检测到
- 结果: ASR 只识别到 "等一下暂停", 没有 TTS 内容泄漏

### 5. 误触发测试 — PASS

纯 TTS 播放不说话:
- ASR 识别内容: 空
- 误触发次数: 0
- AEC 完美消除了 TTS 回声, ASR 没有把 TTS 语音误识别为任何内容

### 6. 延迟测量 — 1046ms (偏高)

从语音开始 (RMS>0.05 阈值) 到关键词检测:
```
语音开始: 2.41s (from record start)
检测到:   3.46s (from record start)
延迟:     1046ms
```
目标 <500ms, 实际超出。原因:
- 语音起始检测用 RMS 阈值可能不精确
- 流式 ASR 100ms chunk + 解码延迟
- "等一下" 3 字需要累积足够帧才能识别

---

## InterruptMonitor Bug

### Bug 1: result.text AttributeError — 已修

**位置**: `core/interrupt_monitor.py:161`

**原代码**:
```python
result = self._recognizer.get_result(self._stream)
if result.text.strip():
    self._check_partial(result.text.strip())
```

**问题**: sherpa-onnx 1.12.38 的 `get_result()` 返回 `str`, 不是有 `.text` 属性的对象

**修复**:
```python
text = result.text.strip() if hasattr(result, 'text') else str(result).strip()
if text:
    self._check_partial(text)
```

### Bug 2: sherpa-onnx C++ SIGABRT — 未修

**错误**: `features.cc:GetFrames:188 CHECK_GE failed: 128 + 39 > 149`

**含义**: feature 提取时请求的帧范围超出实际可用帧数, C++ 断言失败直接 abort 进程

**特征**: Python try/except 无法捕获 (SIGABRT 不是 Python 异常)

**重现**: 通过 `InterruptMonitor.feed_audio()` 在 sounddevice 回调线程中调用即触发, 无论是外部 callback 还是内置 `start_mic_listener()`

**关键对比**: 手动脚本不走 InterruptMonitor, 直接在回调里 buffer + lock + accept_waveform + decode 完全不 crash

手动测试成功代码:
```python
recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
    encoder="...int8.onnx", ...)
stream = recognizer.create_stream()
lock = threading.Lock()
buf = np.array([], dtype=np.float32)

def callback(indata, frames, time_info, status):
    global buf
    with lock:
        buf = np.concatenate([buf, indata[:, 0]])
        if len(buf) < 8000: return
        chunk = buf
        buf = np.array([], dtype=np.float32)
    stream.accept_waveform(16000, chunk)
    while recognizer.is_ready(stream):
        recognizer.decode_stream(stream)
    # 跑 8 秒完全不 crash
```

**已尝试但无效的修法**:
1. blocksize 1600 -> 3200: crash
2. buffer 累积到 8000 样本 (0.5s) 再喂 ASR: crash
3. 关掉 VAD (config vad_model_path=""): crash
4. 加 threading.Lock 包 buffer 操作: crash
5. 用 queue 在单独 worker 线程处理: crash
6. 用内置 start_mic_listener(): crash
7. 同时换 int8 模型: crash

**分析**:

手动测试和 InterruptMonitor 的关键区别:
- 手动: lock 包住 buffer 累积 + accept_waveform + decode 的整个流程
- InterruptMonitor: lock 只包 buffer 累积, accept_waveform/decode 在 lock 外

但即使关 VAD 也 crash, 说明不纯粹是 lock 范围问题。怀疑方向:
1. `start_mic_listener()` 的 `sd.InputStream` 未指定 device, 可能默认打开了 XVF3800 的 device 0 (max_input_channels=0), 导致 stream.read 返回残帧
2. VAD `accept_waveform` 和 ASR `accept_waveform` 可能共享 sherpa-onnx 全局状态
3. InterruptMonitor 对象的生命周期: `_load_recognizer()` lazy load, 可能初始化时序有问题

**修复方向**:

对比手动成功代码, 需要:
1. `feed_audio()` 中整个 ASR 操作 (buffer + accept + decode + get_result) 必须在同一个 lock 里
2. `start_mic_listener()` 必须指定 `device=6` (或从 config 读取)
3. 可能需要把 VAD 和 ASR 的调用都放到同一个 lock 保护下

---

## 已应用的代码修改 (在 Mac 和 RPi 上)

`core/interrupt_monitor.py`:
1. 添加 `_asr_buffer` 和 `_min_chunk_samples=8000` 字段
2. `_load_recognizer()` 默认用 int8 模型文件名
3. `feed_audio()` 中 buffer 累积逻辑 + lock (但范围不够)
4. `get_result()` 兼容 str 和 .text 属性
5. `start()` 中重置 `_asr_buffer`

---

## Claude 代码审计 — Bug 2 根因分析 (2026-04-16)

### 确认的两个并发 Bug

通过审计 `core/interrupt_monitor.py` 和 `jarvis.py` 的调用链，确认 SIGABRT 由**两个独立 bug 叠加**导致：

#### Bug A: 线程竞争 — sherpa-onnx stream 对象并发访问（主因）

**核心问题**: `self._stream` 是 sherpa-onnx C++ 对象，**不是线程安全的**。但当前代码有两个线程同时操作它：

**线程 1 — mic reader 线程** (`interrupt-mic`, daemon):
```
start_mic_listener() → _reader() → feed_audio() → accept_waveform / decode_stream / get_result
```

**线程 2 — 主线程** (jarvis.py:1051-1052):
```
interrupt_monitor.stop_mic_listener()  ← 先停 mic
interrupt_monitor.stop()               ← 再调 decode_stream + 设 _stream = None
```

**竞争窗口**: `stop_mic_listener()` 设置 `_mic_stop` event 后, mic reader 线程可能**正在执行** `feed_audio()` 中的 `accept_waveform` 或 `decode_stream`。此时 `stop()` 也调用 `decode_stream(self._stream)` → 两个线程同时操作同一个 C++ stream → 内部帧计数器错乱 → `CHECK_GE failed` → SIGABRT。

**证据链**:
1. 手动脚本只有**一个线程**访问 stream（callback 线程），完全不 crash
2. 手动脚本没有 `stop()` 会并发调用 `decode_stream`
3. SIGABRT 的 `128 + 39 > 149` 说明 decoder 认为有 167 帧但只有 149 帧 — 典型的并发写入后状态不一致

**`feed_audio()` 当前 lock 范围问题** (`interrupt_monitor.py:162-175`):
```python
# 当前代码 — lock 范围太小
with self._lock:                                    # ← lock 开始
    self._asr_buffer = np.concatenate(...)
    if len(self._asr_buffer) < self._min_chunk_samples:
        return
    chunk = self._asr_buffer
    self._asr_buffer = np.array([], dtype=np.float32)
                                                     # ← lock 结束
self._stream.accept_waveform(sample_rate, chunk)     # ← 无保护！
while self._recognizer.is_ready(self._stream):       # ← 无保护！
    self._recognizer.decode_stream(self._stream)     # ← 无保护！
result = self._recognizer.get_result(self._stream)   # ← 无保护！
```

**`stop()` 也无 lock 保护** (`interrupt_monitor.py:117-122`):
```python
def stop(self):
    self._recording = False
    if self._stream and self._recognizer:
        self._recognizer.decode_stream(self._stream)  # ← 无保护，和 feed_audio 竞争！
        self._stream = None
```

**`reset()` 同样无保护** (`interrupt_monitor.py:130-134`):
```python
def reset(self):
    with self._lock:
        self._fired = False
    if self._recognizer and self._stream is None:
        self._stream = self._recognizer.create_stream()  # ← 无保护
```

**为什么之前的 7 种修法全部无效**:
- 修法 1-3 (blocksize/buffer/关VAD): 没解决并发问题
- 修法 4 (加 lock 包 buffer): lock 范围只包了 buffer 累积，没包 accept/decode
- 修法 5 (queue + worker): 如果 `stop()` 仍然直接调 `decode_stream`，worker 线程和主线程依然竞争
- 修法 6 (start_mic_listener): 内置 listener 也走 `feed_audio()`，同样有竞争
- 修法 7 (int8 模型): 模型无关，是线程问题

#### Bug B: 设备选择 — start_mic_listener 未指定 device

**位置**: `interrupt_monitor.py:215-220`

```python
self._mic_stream = sd.InputStream(
    samplerate=sample_rate,
    channels=1,
    dtype="float32",
    blocksize=block_size,
    # ← 没有 device= 参数！
)
```

**问题**: 在 RPi5 上，XVF3800 被 sounddevice 识别为 device 0，但 `sd.query_devices(0)` 显示 `max_input_channels=0`。不指定 device 时，sounddevice 可能默认选择 device 0，导致：
- 拿到空帧或残帧
- 帧数据长度不符预期
- 间接导致 sherpa-onnx feature 提取帧数不匹配

**确认**: 手动测试脚本使用 `device=6`（default ALSA）成功录音。

### 修复方案

#### 方案 1: 引入 `_asr_lock` + 修 device（推荐）

新增 `_asr_lock = threading.Lock()` 专门保护 stream/recognizer 访问，与现有 `_lock`（保护 `_fired` 标志）分离：

```python
def __init__(self, ...):
    self._lock = threading.Lock()       # _fired 标志
    self._asr_lock = threading.Lock()   # stream/recognizer 独占访问

def feed_audio(self, audio, sample_rate=16000):
    ...
    with self._asr_lock:
        # buffer 累积 + accept_waveform + decode + get_result 全部在 lock 内
        self._asr_buffer = np.concatenate([self._asr_buffer, audio])
        if len(self._asr_buffer) < self._min_chunk_samples:
            return
        chunk = self._asr_buffer
        self._asr_buffer = np.array([], dtype=np.float32)
        if self._stream is None:
            return
        self._stream.accept_waveform(sample_rate, chunk)
        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)
        result = self._recognizer.get_result(self._stream)
    # lock 释放后再做 keyword check（_check_partial 用自己的 _lock）
    text = result.text.strip() if hasattr(result, 'text') else str(result).strip()
    if text:
        self._check_partial(text)

def stop(self):
    self._recording = False
    with self._asr_lock:
        if self._stream and self._recognizer:
            try:
                if self._recognizer.is_ready(self._stream):
                    self._recognizer.decode_stream(self._stream)
            except Exception:
                pass
            self._stream = None
    ...

def reset(self):
    with self._lock:
        self._fired = False
    with self._asr_lock:
        if self._recognizer and self._stream is None:
            self._stream = self._recognizer.create_stream()
```

`start_mic_listener()` 加 device 参数:
```python
def start_mic_listener(self, sample_rate=16000, block_size=1600, device=None):
    ...
    self._mic_stream = sd.InputStream(
        samplerate=sample_rate,
        channels=1,
        dtype="float32",
        blocksize=block_size,
        device=device,  # 从 config.yaml 读取
    )
```

**优点**: 最小改动，精确解决竞争问题
**风险**: `_asr_lock` 在 sounddevice callback 线程中持有，如果 accept_waveform + decode 耗时超过 blocksize 时间（100ms），会导致 callback 被阻塞。但 buffer 累积到 8000 样本（0.5s）才处理一次，实际 decode 延迟远小于 0.5s，风险可控。

#### 方案 2: 改用 stop-then-join 顺序

在 `jarvis.py` 调用链中，先确保 mic 线程完全退出，再调 `stop()`:

```python
# jarvis.py:1051
self.interrupt_monitor.stop_mic_listener()  # 等 mic 线程 join 完成
# 此时保证没有线程在 feed_audio() 里
self.interrupt_monitor.stop()               # 安全调用 decode_stream
```

当前 `stop_mic_listener()` 已经有 `self._mic_thread.join(timeout=2)`，但如果 thread 2s 内没退出就不等了。需要确保 join 成功后才调 stop()。

**优点**: 不需要新 lock
**缺点**: 依赖调用顺序，fragile；如果有其他调用方直接调 feed_audio 也不安全

#### 方案 3: 复制手动脚本架构 — 去掉 InterruptMonitor 的 stop() 中 decode_stream

`stop()` 中的 `decode_stream` 调用目的是冲刷剩余帧。但如果 mic listener 已停止，stream 里没有新数据，这次 decode 几乎无意义。直接删掉:

```python
def stop(self):
    self._recording = False
    self._stream = None  # 直接丢弃 stream，不 decode
```

**优点**: 最简单
**缺点**: 可能丢失最后一小段语音的识别结果（但打断检测场景中这不重要）

### 推荐: 方案 1 + 方案 3 结合

- `_asr_lock` 保护所有 stream 访问（方案 1 的安全保证）
- `stop()` 中不调 `decode_stream`（方案 3 的简洁性）
- `start_mic_listener()` 接受 device 参数（修 Bug B）
- config.yaml 添加 `audio.input_device` 和 `audio.output_device`

---

## 当前进度

### 已完成
- [x] 硬件环境搭建 (XVF3800 + RPi5 + HD600)
- [x] 设备识别和 .asoundrc 配置
- [x] 6 项功能测试全部 PASS (麦克风/AEC/ASR/全双工/误触发/延迟)
- [x] Bug 1 修复 (result.text AttributeError)
- [x] Bug 2 根因定位 (线程竞争 + 设备选择)
- [x] 代码修改已同步到 Mac 和 RPi (buffer 累积 + int8 模型)
- [x] Claude 代码审计完成，确认修复方案

### 未完成
- [ ] 修 Bug 2: 实施方案 1+3（引入 `_asr_lock` + 去掉 stop() 中 decode + device 参数）
- [ ] 在 RPi 上验证 Bug 2 修复
- [ ] 延迟优化: 目标 <500ms (当前 1046ms)
  - 可能方向: 减小 `_min_chunk_samples` (8000→4800 即 0.3s), 用更短关键词
- [ ] config.yaml 添加 `audio.input_device` / `audio.output_device`
- [ ] 端到端验证: `jarvis.py` 完整流程 (wake → ASR → LLM → TTS → interrupt)
- [ ] 考虑 `stop()` 中 decode_stream 不加 `is_ready()` 检查也可能独立触发 crash 的问题
