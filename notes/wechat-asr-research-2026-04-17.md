# 微信 ASR 为什么中文超级稳定 —— 深度调研

> 2026-04-17 · 3 路并行调研合成（技术栈 / 五家工程对比 / Jarvis 可迁移）
> 核心问题：为什么用户感知"微信中文语音转文字极稳"？对 Jarvis 本地 ASR（SenseVoice INT8 + sherpa-onnx）可迁移什么？

---

## TL;DR

1. **微信"稳"不是因为 WER 最低**（SpeechIO 2025.01 排第 4，4.64%，落后阿里/微软/喜马拉雅）。稳是 **P95 体验最好**：方言/口音兜底最全 + 前端降噪最强 + 海量真实数据闭环。
2. **技术护城河 3 条**：①150k 小时内部中文语料（Tencent AI Lab 3M 论文确认）；②TEA-PSE 降噪（DNS Challenge 冠军）；③27 方言共享大模型。
3. **业界新趋势**：字节 Seed-ASR 用 LLM decoder 吃掉 VAD/hotword/纠错/中英混读——"模型大了，模块没了"。
4. **Jarvis 本周能落地的最大杠杆**：`sherpa-onnx Homophone Replacer`（拼音 FST 替换）。对 SenseVoice **原生支持**，零延迟，能直接干掉人名/技能名错认。是当前路线下唯一合法的"热词"通道。

---

## 一、微信为什么稳（5 个技术底座）

### 1. 数据规模护城河

- **Tencent AI Lab "3M" 论文（Interspeech 2022）明确披露内部 150,000 小时中文训练语料** [arxiv 2204.03178v2]。这是业界少数到这个量级的团队之一。
- 叠加微信每天亿级语音消息的**闭环反馈流**（SILK 编码 + 云端 ASR + 用户纠错）。
- 数据增广三件套：噪声模拟、混响模拟、语速扰动（智聆白皮书）。

### 2. 模型演进路线（从公开披露拼出来）

| 代际 | 架构 | 来源 |
|---|---|---|
| v1 | HMM/GMM | 智聆白皮书 |
| v2 | HMM/DNN | 智聆白皮书 |
| v3 | RNN-CTC + LF-MMI + RNN-LM rescoring | 智聆白皮书 |
| v4 | CLDNN + Deep CNN | 智聆白皮书 |
| v5 | TLC-BLSTM + Attention + Teacher-Student 蒸馏 | 腾讯云产品页 |
| v6 (AI Lab) | **Conformer CTC/AED + Mixture-of-Experts（3M-ASR / SpeechMoE）** | arxiv 2204.03178 + 2105.03036 |

**关键洞见：MoE 路由让"一个模型覆盖聊天/会议/王者荣耀语音"**。一个大稀疏模型，推理时走不同专家，比"多模型分支"可维护。这是腾讯多场景业务的最优解，不是精度最高的选择。

### 3. 前端信号处理（TEA-PSE）

- **Tencent Ethereal Audio Lab** 的 TEA-PSE 1.0/2.0/3.0 系列是 DNS Challenge 常胜军，2023 年 TEA-PSE 3.0 夺冠（arxiv 2303.07704）。
- TEA-PSE 是否部署到微信 ASR 前端腾讯没明说，但同一支团队 + 同一公司业务 = **高度可能**。
- 对比：Jarvis 现在完全没有 NN 降噪，裸 mic → VAD → ASR。

### 4. 方言覆盖最全

- 腾讯云 ASR 产品文档列出：普通话 + 粤语 + **27 种方言**（上海/四川/武汉/贵阳/昆明/西安/郑州/太原/兰州/银川/西宁/南京/合肥/南昌/长沙/苏州/杭州/济南/天津/石家庄/黑龙江/吉林/辽宁/闽南/客家 …）。
- 2020 年的"**普方英大模型**"——共享声学模型 + 多方言 LM/多任务输出头，单一引擎识别普通话 + 英文 + 多方言混合。
- 对比：讯飞 202 方言（单一统一大模型，更激进）；微信 App C 端实际只开放普通话+粤语+英语，后端能力远超前端暴露。

### 5. 云端架构 + 流式解码

- 微信语音消息管线（腾讯云大学披露）：客户端 **SILK v3 编码 (~2 kbps)** → 最近接入点 TGW → 云端解码识别。
- **处理速度 ≈ 1s 音频 / 0.4s 处理（2.5× 实时）**，流式接口"边录边上传边转写，等待时间固定不随时长增加"。
- 这解释了用户体感"说完即出字"不是端侧跑，是就近接入 + 流式 decoder + 足够快的云。

### 6. LLM 后处理（已在论文，是否线上存疑）

- **Tencent Ethereal Audio Lab ICASSP 2025**："Full-text Error Correction for Chinese ASR with LLM"——ChFT 数据集 + ChatGLM 微调 + `seg_json` prompt，**Mandarin CER 相对下降 22.4%（6.16% → 4.78%）**（arxiv 2409.07790）。
- 2025 年后续 **Chain of Correction (CoC)**：分段 + 多轮 chat 格式，再降 11.7% 到 4.06%（arxiv 2504.01519）。
- 这是同一支支撑微信语音业务的团队，**灰度或专用链路使用的概率很高**，但未官方确认。

### 7. 公开 benchmark 数字

| 场景 | Tencent | 对比 |
|---|---|---|
| SpeechIO 2022.05 整体 CER | 4.06%（排 6） | 阿里云 2.65% (Top 1) |
| SpeechIO 2025.01 非流式 46 场景 | **4.64%（排 4）** | 微软 batch 2.99% / 喜马拉雅 3.35% / 阿里云 3.40% |
| SpeechIO 2025.01 解锁 26 场景 | 3.20% | 讯飞 3.01% / 阿里云 ftasr 1.80% / 百度 7.30% |

**结论：腾讯不是 SOTA，但是"综合最稳"**。用户感知的"超级稳定" ≠ benchmark CER 最低，而是 **场景广度 + 方言 + P95 体验**。

---

## 二、五家工程手段对比矩阵

| 维度 | 微信/腾讯 | 讯飞 | Google | Apple | 字节 Seed-ASR |
|------|-----------|------|--------|-------|------|
| VAD | 自研（未披露） | 前端 VAD+降噪+AEC 级联 | **Unified Endpointer**（EOS token 嵌入 RNN-T） | DNN-HMM + Conformer rescore 两级 | LLM 隐式切分，不显式 VAD |
| 降噪 | TEA-PSE（推测接入） | 远场 AEC + 前置噪抑 | USM 大规模数据鲁棒 | acoustic-FTM 分离噪声 | 数据增广 + 真实大数据分布 |
| 声学模型 | TLC-BLSTM / Conformer+MoE (3M) | Conformer + 星火方言大模型 | Conformer-L (USM 2B) + chunk streaming | **Conformer Transducer 端侧 RTF 0.19** | **AcLLM（Conformer encoder → LLM decoder）** |
| LM | 垂直领域自适应 + 用户词表 | 领域 LM，术语 +20% | **CLAS + Shallow-Fusion WFST 双路径** | Acoustic Model Fusion + Delayed Fusion LLM | LLM 自身即 LM |
| 热词 bias | Hotword API，权重 1-100，权重 100 替换同音候选 | 星火大模型 + 个性化词表 | **CLAS 神经 bias（WER 降 68%）** | Geo-LM（位置锚定 POI） | **对话上下文 prompt 零参数 bias** |
| 错字纠正 | LLM full-text correction（论文层面） | 未披露 | E2E 不做独立纠错 | 混淆矩阵 FST（ITN 路径） | LLM 自带语义纠错 |
| 方言数量 | 27 方言 + 粤+普+英大模型 | **202 方言统一建模** | 100+ 语言（方言≈English variants） | 按 locale 切 | Mandarin + 多方言口音 |
| 数据增广 | Teacher-Student 蒸馏 + MoE 路由 | 用户纠错闭环 30 天 +15% | **Random-Projection Quantization SSL，12M 小时无标注** | 联邦学习 LM | 分阶段 SSL → 监督 → Context Elicit |

### 五家独特做法一览

- **微信/腾讯**：MoE 路由（一个大稀疏 Conformer 覆盖多场景）+ 150k 数据 + TEA-PSE 降噪。
- **讯飞**：202 方言单一大模型（最激进）+ 用户纠错闭环入训练集。
- **Google**：USM RPQ 自监督（12M 小时） + CLAS 神经 bias + Unified Endpointer（VAD 塞进 RNN-T）。
- **Apple**：on-device Conformer（wearable 0.19 RTF） + BiLSTM ITN + Geo-LM + 反其道 Acoustic Model Fusion。
- **字节 Seed-ASR**：**"大模型吃掉所有模块"** —— VAD/hotword/纠错/中英混读全部塌缩成 LLM prompt。论文 Figure 1：第一轮识别成"庞葱"错了，第二轮通过对话上下文自动纠正，零热词参数。

### 5 个通用 trick（中文 ASR 稳定的工程共识）

1. **VAD 端到端化** —— Google Unified Endpointer、Seed 隐式 endpoint 在抛弃外挂 VAD
2. **Contextual biasing 是中文 ASR 最被低估的杠杆** —— CLAS/Shallow-Fusion/LLM-prompt 三条路都能把专有名词错误降 15-60%
3. **ITN 从 FST → 神经 tagger** —— Apple BiLSTM、微软 Transformer-tagger+WFST 混合
4. **自监督预训练是入场券** —— USM RPQ、wav2vec2/HuBERT/WavLM、Seed 的 stage-wise SSL
5. **MoE + 多领域数据** —— 一个大模型覆盖聊天/会议/游戏比多分支可维护

---

## 三、对 Jarvis 的可迁移分析

### SenseVoice 现状重新评估

**强项**
- 非自回归，RTF ≈ 0.007（10s 音频 70ms），比 Whisper-Large 快 15×
- 中文/粤语 WER 优于 Whisper-Large（AISHELL-1/2、WenetSpeech）
- 40 万小时训练，中英日韩粤 5 语 + 情感/事件/LID
- 内置 ITN（`use_itn=1`）

**弱项 / 接口限制（关键）**
- **CTC 架构：sherpa-onnx 的 `hotwords-file` / `ruleFsts` 对 SenseVoice 无效**（sherpa-onnx issue #3373 明确会崩溃）
- 原生非流式；伪流式要切到 `streaming-sensevoice`
- 无 N-best 输出接口
- SenseVoice-Large 未开源

**sherpa-onnx 定制能力矩阵**

| 手段 | SenseVoice 支持？ |
|---|---|
| `hotwords-file` | ❌ 仅 transducer/Qwen3-ASR |
| `ruleFsts` | ❌ 仅 TTS |
| **Homophone Replacer** (`hr-dict-dir` + `hr-lexicon` + `hr-rule-fsts`) | ✅ **所有中文 ASR 模型都支持** |
| `use_itn=1` 内置 ITN | ✅ |
| Silero VAD 管线 | ✅ |

### Top 5 改造（按 ROI 排序）

#### P0-1 · 接入 Homophone Replacer【本周可落地】

- **现状**：SenseVoice 对"小月""小芝""Hue""MiniMax" 等经常错认；GPT-4o-mini 只在 memory extraction 时顺带纠，不是实时
- **目标**：ASR 输出层通过拼音 FST 直接替换，0 额外延迟，0 LLM 成本
- **动作**：
  1. 通讯录/家电/技能名写成 `{pinyin_with_tone → 正字}` 规则
  2. `uv pip install pynini`（Linux/Mac 有 wheel；aarch64 RPi5 可能要源码编译，规避：Mac 编译 `replace.fst` 再 scp 到 RPi，推理端无需 pynini）
  3. 写 `scripts/build_hr_fst.py`
  4. `core/speech_recognizer.py` 的 `OfflineRecognizer.from_sense_voice(...)` 加 `hr_dict_dir / hr_lexicon / hr_rule_fsts` 三参
- **收益**：热词类错认降到 0，立竿见影
- **这是现在路线下唯一合法的"热词"通道，比升级 VAD/上降噪都高 ROI**

#### P0-2 · VAD 升级到 3 态 + pre-roll buffer

- **现状**：2 态 IDLE/ACTIVE（P6 已知 gap），主路径无 pre-roll，首字/短词（"好""嗯"）易漏
- **目标**：3 态 IDLE→ACTIVE→INACTIVE 缓冲态；240-500ms 环形 pre-roll；ACTIVE 触发时把 pre-roll 追加到录音头
- **动作**：参考 Silero 官方 `VADIterator` 的 `speech_pad_ms` + `min_silence_duration_ms`；保留现有双阈值 prob+dBFS；升到 Silero v5/v6（v5 比 v4 快 3×，chunk 严格 512 样本）
- **成本**：1 天

#### P1-1 · Homophone 规则随通讯录自动生成

- **依赖**：P0-1 完成
- **动作**：memory extraction 后台 hook 抽人名/地名 → pypinyin 生成拼音 → 增量重编 `replace.fst`（秒级）→ 运行时 reload recognizer（SenseVoice init ~1s，可接受）
- **收益**：个人词表实时跟上用户生活，不需重启

#### P1-2 · DeepFilterNet3 前置降噪（XVF3800 到货前）

- **现状**：裸 mic → VAD → ASR
- **目标**：DFN3 流式 16kHz，单帧 ~2.6ms，RPi5 RTF ≈ 0.25（Rock-5b/RK3588 实测，Cortex-A76 同代）
- **注意**：`ATTEN_LIM_DB` 调 10-20dB，避免过度去噪伤 ASR 特征；单独线程，不和 ASR 抢核
- **风险**：XVF3800 硬件 AEC/降噪到货后，可能整体降级或跳过此条

#### P2-1 · WeTextProcessing ITN 兜底

- **现状**：只靠 SenseVoice 内置 `use_itn=1`
- **动作**：`uv pip install WeTextProcessing`，在 ASR 输出后加一层 `ZhNormalizer`/`InverseNormalizer`，CPU <10ms
- **收益**：日期/货币/单位规范化，减轻下游 skill 解析负担

### 明确不推荐 / 性价比低

- **在 SenseVoice 上配 `hotwords-file` / `ruleFsts`**：官方 CTC 不支持，会崩溃
- **用 Qwen-0.5B/1.5B 做本地 ASR 后纠错**：ASR-EC Benchmark (EMNLP 2025) 显示 **7B 级别 zero-shot 纠错反而让 WER ↑**（平均 27-34% 劣于 baseline 12-13%）。LoRA 微调需域内数据，Jarvis 没有
- **换 transducer 模型只为热词**：放弃 SenseVoice 的速度和多语言成本太高，Homophone Replacer 已覆盖核心需求
- **SenseVoice 微调**：需几十小时数据，远超 Jarvis 个人助手 scale 可产出
- **RNNoise 替代 DFN3**：RNNoise 对 MCU 有优势（Pico2 22ms），RPi5 算力够 DFN3，质量更高

### 关键依赖和坑

- **pynini aarch64 wheel**：官方只明确 Linux x86。规避：Mac 编译 `.fst` → scp → RPi 只做推理（sherpa-onnx 读 FST 不需 pynini runtime）
- **Python 3.13 兼容性**：sherpa-onnx 和 pynini 都跟 3.13 滞后，落地前先验证 wheel
- **SenseVoice 热加载**：换 HR 规则不需重载 model.onnx，但要销毁重建 recognizer wrapper（init ~1s）
- **SenseVoice 输出里的特殊 token**（`<|HAPPY|><|Speech|>` 等）：HR 规则和下游 parser 都要兼容
- **DFN3 16kHz**：原生 48kHz，16kHz 要走 `eband5ms` 改配

---

## 四、给 Jarvis 的一句话总结

> 微信稳是靠 **150k 小时数据 + TEA-PSE 降噪 + MoE 多场景模型 + 方言兜底 + 海量闭环反馈**——这 5 条 Jarvis 全学不了。
>
> 但业界通用的 **"contextual biasing 是中文 ASR 最大杠杆"** 这条完全可以抄。SenseVoice 路线下，`sherpa-onnx Homophone Replacer` 是唯一合法入口，**本周就能落地，零延迟，对人名/技能名错认立竿见影**。
>
> 其他值得抄的：**VAD 3 态 + pre-roll**（已知 gap，1 天工时）、DFN3 降噪（XVF3800 未到时的过渡方案）、WeTextProcessing ITN。
>
> 不要抄：本地 LLM 后处理纠错（ASR-EC 论文证明小模型反而让 WER ↑）。

---

## 信息缺口

1. 微信 App 当前线上模型**架构/参数量/首末字延迟 P50/P95**（全部未公开）
2. LLM 纠错（ChFT/CoC）**是否已上线微信生产流量**
3. TEA-PSE 降噪**是否/何时**接入微信 ASR 前端
4. pynini aarch64 wheel 能否装 RPi5 Python 3.13（需本地试）
5. DeepFilterNet3 在 RPi5 16kHz 精确 RTF（只有 Rock-5b/RK3588 数据点）
6. `SenseVoiceSmall_hotword`（社区热词微调版）能否转 ONNX 给 sherpa-onnx

---

## 关键来源

### 官方（腾讯/微信直接披露）
- Tencent Cloud ASR — https://www.tencentcloud.com/products/asr
- 腾讯云 ASR 产品文档（方言/引擎） — https://cloud.tencent.com/document/product/1093/35682
- Tencent Cloud Custom Hotword — https://www.tencentcloud.com/document/product/1041/68176
- 微信智聆技术白皮书 — https://ask.qcloudimg.com/draft/1184429/27bjh54d3m.pdf
- 腾讯云大学《语音消息技术实现》程君（GME 团队工程披露） — https://cloud.tencent.com/developer/article/1570333
- 微信开放文档 Cloud API ASR — https://developers.weixin.qq.com/doc/xwei/xiaowei-cloudapi/asr.html

### 学术
- Tencent AI Lab 3M (Interspeech 2022) — https://arxiv.org/pdf/2204.03178v2
- Tencent AI Lab SpeechMoE (Interspeech 2021) — https://arxiv.org/abs/2105.03036
- Tencent Ethereal Audio Lab TEA-PSE 3.0 (ICASSP 2023) — https://arxiv.org/pdf/2303.07704v1
- Tencent Ethereal Audio Lab Full-text Error Correction (ICASSP 2025) — https://arxiv.org/html/2409.07790v2
- Chain of Correction (2025) — https://arxiv.org/html/2504.01519v1
- WenetSpeech 10000h Mandarin corpus — https://arxiv.org/pdf/2110.03370
- ByteDance Seed-ASR — https://arxiv.org/abs/2407.04675 + https://bytedancespeech.github.io/seedasr_tech_report/
- Google USM — https://arxiv.org/abs/2303.01037
- Google Unified ASR+Endpointer (Bijwadia 2022) — https://arxiv.org/pdf/2211.00786
- Google CLAS Shallow-Fusion (Zhao 2019) — https://www.isca-archive.org/interspeech_2019/zhao19d_interspeech.pdf
- Apple Acoustic Model Fusion (ICASSP 2024) — https://machinelearning.apple.com/research/acoustic-model-fusion
- Apple ITN as Labeling — https://machinelearning.apple.com/research/inverse-text-normal
- ASR-EC Benchmark (EMNLP 2025) — https://aclanthology.org/2025.emnlp-industry.110.pdf

### Benchmark
- SpeechColab Leaderboard 2025.01 — https://github.com/SpeechColab/Leaderboard
- SpeechIO 2022.05 — https://mp.weixin.qq.com/s/zKEkpL1R6XeYjA3yIsXAcg

### 工程（sherpa-onnx + SenseVoice 相关，Jarvis 落地需要）
- sherpa-onnx Homophone Replacer — https://k2-fsa.github.io/sherpa/onnx/homophone-replacer/index.html
- sherpa-onnx SenseVoice Python API — https://k2-fsa.github.io/sherpa/onnx/sense-voice/python-api.html
- sherpa-onnx issue #3373（SenseVoice hotwords 不支持） — https://github.com/k2-fsa/sherpa-onnx/issues/3373
- FunAudioLLM/SenseVoice — https://github.com/FunAudioLLM/SenseVoice
- streaming-sensevoice — https://github.com/pengzhendong/streaming-sensevoice
- Silero VAD v5 — https://github.com/snakers4/silero-vad/releases/tag/v5.0
- DeepFilterNet — https://github.com/Rikorose/DeepFilterNet
- DFN on RK3588 实测 — https://github.com/Rikorose/DeepFilterNet/issues/190
- WeTextProcessing — https://github.com/wenet-e2e/WeTextProcessing
