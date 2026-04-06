# ASR 方案调研笔记 — 2026-04-01

## 背景

小月端到端延迟 ~10-25s，ASR（本地 Whisper base）是最大瓶颈之一（3-7s）。
目标：找到中文 ASR 最优解，需要在 RPi5 4GB 上运行。

## 结论

**推荐：sherpa-onnx + SenseVoice-Small INT8 + Silero VAD**

- 推理延迟：~75ms（1.5s 音频，Cortex-A76 四线程）
- 中文准确率：AISHELL-1 CER 2.96%
- 模型大小：228MB（INT8 量化）
- 内存占用：~350MB
- 免费，本地运行，零网络依赖

## 流式 vs 非流式

流式对本项目几乎无收益：
- VAD + 非流式：说完后 ~0.7s 得到结果
- 流式：说完后 ~0.5s 得到结果
- 差 0.2s，不值得增加复杂度

## 全方案对比（已验证数据）

### 本地方案（RPi5 4GB 可用）

| 方案 | AISHELL-1 CER | RPi5 推理(1.5s音频) | 模型大小(INT8) | 特点 |
|------|:---:|:---:|:---:|------|
| **SenseVoice-Small** | **2.96%** | **~75ms** | 228MB | 中英粤日韩 + 情绪识别 |
| **Paraformer-Large** | **1.95%** | ~220ms | 217MB | 纯中文最准 |
| Paraformer-Small | ~3-5%(估) | ~115ms | 79MB | 轻量备选 |
| Whisper base (whisper.cpp) | ~8-12%(估) | ~1.5s | 142MB | 太慢，中文差 |
| Moonshine | 36.1%(CV) | 很快 | ~26MB | 中文太差 |

### 本地方案（RPi5 跑不动）

| 方案 | AISHELL-1 CER | 参数量 | 说明 |
|------|:---:|:---:|------|
| FireRedASR-AED | **0.55%** | 1.1B | SOTA 但太大 |
| Belle-Whisper-turbo-zh | 3.07% | 800M | Whisper 中文微调版 |
| SenseVoice-Large | 2.09% | 1.6B | **未开源** |

### 云端方案

| 方案 | 中文 CER | 延迟 | 可靠性 | 价格 |
|------|:---:|:---:|:---:|:---:|
| Groq Whisper | ~5.14% | 300ms-**60s+** | **不稳定**，社区确认延迟波动 | $0.02-0.11/hr |
| Google Chirp 2 | **不公开** | 200-500ms | 稳定 | $0.016/min |
| Azure Speech | ~6-10% | 700ms-5s | 冷启动 3-5s | $0.017/min |
| OpenAI Whisper | ~5.14% | 3-10s | 稳定但慢 | $0.006/min |
| Deepgram | 中文差 | <300ms | — | $0.004/min |
| AssemblyAI | >10% WER | — | — | — |

## 关键发现

1. **所有云端方案中文准确率都不如本地 SenseVoice/Paraformer**
2. **Groq Whisper 延迟不可靠** — 社区报告 300ms 到 60s+ 波动
3. **Google/Azure 不公开中文 CER 数据**，无法验证
4. **SenseVoice-Small 是速度/精度最优平衡** — 75ms + 2.96% CER
5. **Paraformer 纯中文最准** — 1.95% CER，但速度稍慢（220ms）

## SenseVoice 详细评估

### 能力
- ASR：中英粤日韩五语种
- 情绪识别：7 种情绪（开心/悲伤/愤怒/中性/恐惧/厌恶/惊讶）
- 声音事件：8 种（笑声/咳嗽/掌声/背景音乐等）
- 标点恢复 + 逆文本正则化
- 语种自动检测

### 限制
- 不支持流式（非自回归架构）
- 30 秒输入上限（需 VAD 切段）
- 英文只是 Whisper-Small 水平
- SenseVoice-Large 未开源
- 微调容易灾难性遗忘

### 部署方式
```bash
pip install sherpa-onnx
# 下载 INT8 模型
wget https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
```

sherpa-onnx 自带 VAD + ASR 集成示例：`vad-with-non-streaming-asr.py`

## 推荐架构

```
麦克风 → Silero VAD 检测说话结束(~0.3s)
  → [Speaker verify + SenseVoice INT8 并行](~0.075s)
  → 意图路由(~0.3s) → 执行 → TTS
用户说完话到得到结果：~0.7s
```

## 数据来源

- FunAudioLLM 论文 (arxiv 2407.04051) — SenseVoice 基准数据
- FireRedASR 论文 (arxiv 2501.14350) — 独立验证 + 更强模型基准
- sherpa-onnx 官方文档 — RTF 基准（RK3588 Cortex-A76）
- Groq 社区论坛 — 延迟不稳定问题
- Microsoft Q&A — Azure 冷启动问题
- Belle-Whisper HuggingFace 模型卡 — CER 数据

## 选 SenseVoice 而非 Paraformer 的理由

1. 推理快 3 倍（75ms vs 220ms）
2. 支持中英粤日韩（Paraformer 仅中文）
3. 自带情绪识别 — 小月人格系统可用
4. 2.96% vs 1.95% 的差距对语音指令场景影响极小
