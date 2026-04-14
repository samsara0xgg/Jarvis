# 全双工打断系统设计 — 2026-04-13

## 核心原则

半双工 → 全双工。听觉系统持续开放，语义驱动打断，不随意被打断。

## 硬件

XVF3800 (XMOS 硬件 AEC) + 固定 3D 打印外壳。
2-Mic/4-Mic HAT 无硬件 AEC，不可用。

## 架构

```
持续监听 (XVF3800 AEC)
  → VAD 触发（TTS 期间抬高阈值）
    → streaming ASR (zipformer-streaming) partial result
      → 命中 INTERRUPT_KEYWORDS → 停 TTS，继续录音到用户说完
      → 命中 RESUME_KEYWORDS + 有断点 → 播放剩余句子
      → 无命中 → 忽略
```

## 关键词

```python
INTERRUPT_KEYWORDS = {"等一下", "停", "打住", "暂停", "等等", "你听我说",
                      "不对", "你理解错了", "不是这样", "说错了"}
RESUME_KEYWORDS = {"继续说", "接着说", "你继续", "继续"}
```

一个关键词集合，一个动作（停 TTS，听用户说）。
不做 backchannel（"嗯"/"对"），误判代价大于收益。
不分 STOP vs CORRECT，动作相同，语义理解交给后续 _process_turn。

## 打断后内容拾取

录完整段音频（含关键词 + 后续内容）→ SenseVoice 转录全段 → 去掉关键词前缀 → 进 _process_turn。
不切割音频，不追求精确时间点。

## 断点恢复 (L5)

- `self._interrupted_response: list[str] | None` — 存未播句子
- 只保留最近一次，新打断覆盖旧的
- 不设过期时间，session 结束自然消失
- resume 关键词命中 + 有断点 → 直接播剩余句子
- ~20 行代码

## 延迟预估

- 关键词检测：~350ms（一个 streaming ASR chunk）
- TTS 实际停止：再 +150-200ms（进程终止 + 声卡缓冲）
- 总计用户感知：~500ms，多听半句

## ASR 双模型

- streaming ASR (zipformer-streaming)：TTS 期间运行，做打断检测
- SenseVoice INT8 (offline)：安静时运行，做高精度最终转录
- 两者不同时全速运行，RPi5 4 核跑得动

## 依赖

- XVF3800 硬件到位（ETA 2026-04-14）
- sherpa-onnx streaming 模型下载
- TTSEngine 加 stop() 方法（kill 播放子进程）
- TTSPipeline 加 pause()/resume() 支持

## 不做

- Backchannel 检测
- TTS 播放时非打断自言自语捕获（后续阶段）
- 持续 wake word 检测（改用句间间隙 + VAD）
- 音频精确切割
