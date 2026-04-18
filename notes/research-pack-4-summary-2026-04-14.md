# Research Pack 4 — 中文长上下文 LLM 实测 (2026-04-14)

**问题**: Llama-3.3-70B / Groq 部署差异 / Grok-4.1-fast / Claude Sonnet 4.6 在中文长上下文的验证数据
**方法**: 3 个并行 agent + 27 个独立信源 (官方 model card/system card、arXiv、SuperCLUE、LongBench、HF、Groq docs)
**置信度**: 中 (官方中文长上下文数据极度稀缺，多数厂商未公开 zh-split)

---

## 核心表格：各模型中文任务准确率 (by context length)

| 模型 | ≤8K (C-Eval/CMMLU/MMLU-ProX zh) | 8-32K | 32-128K | 128K+ | 来源 |
|---|---|---|---|---|---|
| **Llama-3.3-70B (Meta 原版)** | **MMLU-ProX zh 58.4%** | NOT PUBLISHED | LongBench-v2 medium (bilingual) 33.0% | LongBench-v2 long 27.8% | [arXiv 2503.10497](https://arxiv.org/abs/2503.10497) · [longbench2.github.io](https://longbench2.github.io) |
| **Llama-3.3-70B (Groq)** | 推断同原版 ±3-9pp (TruePoint 量化损失) | 同左 | 同左 | 同左 (Groq max 131K input) | [SambaNova 2024 blog](https://sambanova.ai/blog) · [Groq docs](https://console.groq.com/docs/models) |
| **Llama-3.1-70B (proxy for 3.3)** | — | RULER 32K **94.8** (English) | RULER 64K **88.4** / 128K **66.6** (English) | — | [NVIDIA RULER](https://github.com/NVIDIA/RULER) |
| **Grok-4.1-fast reasoning** | ITSoloTime **64.3%** (综合中文) | NOT PUBLISHED | NOT PUBLISHED | **2M 窗口** 但无中文基准 | [ITSoloTime](https://itsolotime.com) · [x.ai model card](https://x.ai/news/grok-4-1) |
| **Grok-4.1-fast non-reasoning** | ITSoloTime **47.6%** (指令-24.3pp) | NOT PUBLISHED | NOT PUBLISHED | — | 同上 |
| **Claude Sonnet 4.6** | **MMMLU 89.3% / GMMLU high-res 91.0%** (中文混入) | MRCR 256K 8-needle **90.6%** (English) | 同左 | MRCR 1M 8-needle **65.1%** (English) | [Anthropic Sonnet 4.6 system card 2026-02-17](https://www.anthropic.com/claude/sonnet) |
| **Qwen3-Max-Thinking** (对比参考) | **SuperCLUE 60.61** (2025 年度) | 中文原生训练 | 中文原生训练 | 同左 | [superclueai.com](https://www.superclueai.com) |
| **Kimi-K2.5-Thinking** (对比参考) | **SuperCLUE 61.50** (2025 年度第一) | 中文原生训练 | 同左 | 同左 | 同上 |

---

## 关键发现

### 1. Llama-3.3-70B 中文是"隐性能力"，非官方支持

- Meta 官方 8 种支持语言：`en/de/fr/it/pt/hi/es/th` — **不含中文**
- 中文表现完全来自预训练数据污染，Meta 从未公开 C-Eval / CMMLU / SuperCLUE 分数
- 唯一 VERIFIED 中文数字：MMLU-ProX Chinese 5-shot CoT **58.4%** (vs English 65.7%，-7.3pp gap)
- 社区 Chinese fine-tune (Shenzhi-Wang Llama3-70B-Chinese-Chat) 基于 **3.0/3.1，非 3.3**
- **长上下文中文数据完全空白**：RULER/LongBench-Chat-zh 均无 3.3 条目，只有 3.1 英文 RULER 可供推断（effective length ~64K，远低于宣称的 128K）

### 2. Groq 部署 ≠ Meta 原版，但差异可控

- **精度**: TruePoint Numerics (专有混合精度) — FP32 attention logits + FP8 activations + 推测 INT8 weights (未官方确认)
- Groq 官方声称 MMLU/HumanEval "无明显损失"，但：
  - SambaNova 2024 测试：Llama-3-8B 全精度 vs Groq **平均高 3.16pp** (15 项，11 项显著)，CoQA 差距 **>9pp**
  - Cerebras 测试：Llama-3.1-70B 10 项中 **9 项胜 Groq**
  - *注：两者均为 Groq 竞品，有偏见可能；无 Llama-3.3-70B 具体对比*
- **上下文窗口**: `llama-3.3-70b-versatile` = **131,072 input / 32,768 output** (VERIFIED from Groq docs)
- **variants 警告**: `llama-3.3-70b-specdec` (8K, 1600 t/s) 已下线 — 若 Jarvis config 仍引用会报错
- **对 Jarvis 意图路由影响**: 预计 <1% (短约束任务对量化鲁棒)，**仅在中文 edge case 可能放大**(社区报告过 Groq Llama 3.1 输出中英文字符交错)

### 3. Grok-4.1-fast — 文档真空

- **xAI 官方 model card (Nov 17 2025, 6 页 PDF) 中文/长上下文基准 = 0 条**
  - 只发布 safety/CBRN/refusal，连 MMLU 都没有
  - Grok 4.1 Fast 在 card 里**只被一笔带过**，没有独立评测
- **唯一独立中文测试** (ITSoloTime, ~15k 题库，2026-03):
  - **reasoning 模式**: 综合 64.3%, rank **#29**, 被腾讯 hunyuan-turbos (65.9%, 4× 更便宜) 击败
  - **non-reasoning 模式**: 47.6% — **指令遵循 -24.3pp vs Grok-3-mini** (红色警告)
- **Grok-4.1-fast ≠ Grok 4.1 蒸馏**: 独立训练的 agentic/tool-calling 分支
  - math/agent: +14-17pp
  - instruction-following / legal / finance: -8-12pp
- **上下文窗口**: **2M tokens** (VERIFIED x.ai/OpenRouter/Oracle；DataStudios 的 128K 说法是错的)
- SuperCLUE 2025 年度榜 / March 月报 / Chatbot Arena Chinese: **均无 Grok 4.x 条目**

### 4. Claude Sonnet 4.6 — 长上下文跃升，但中文颗粒度仍缺

- 官方 system card (Feb 17 2026, 4358 行) **中文数据最详细**:
  - MMMLU (14 非英语含中文): **89.3%**
  - GMMLU high-resource tier (中文归入此组): **91.0%**, 与英文 92.9% 差距 **-1.9pp**
  - 中文 harmless-response: **99.29%** (vs Sonnet 4.5 的 97.27%)
  - 中文 over-refusal: **0.34%**
- **长上下文能力相对 4.5 飞跃**:
  - MRCR v2 256K 8-needle: **90.6%** (Sonnet 4.5 仅 10.8%, **8× 改善**)
  - MRCR v2 1M 8-needle: **65.1%** (Sonnet 4.5: 18.5%)
  - GraphWalks 1M BFS: **68.4%** (Sonnet 4.5: 25.6%)
  - 上下文窗口: **1M tokens (正式版，非 beta)**
- **未发布**: C-Eval / CMMLU / SuperCLUE / Chinese NIAH at 32K/64K/128K/200K
- ⚠️ **已知 bug** (可复现, VERIFIED): 弱 system prompt 下，`你是什么模型？` 可能触发 "我是 DeepSeek-V3" — 非模型替换，是身份锚定失效。**缓解**: system prompt 硬编码身份

---

## 差距清单 (NOT FOUND — 研究边界)

1. C-Eval / CMMLU 对 Llama-3.3-70B **基础版** — 无 (只有 3.0/3.1 fine-tune 数据)
2. SuperCLUE — Llama-3.3、Grok-4.x、Claude Sonnet 4.6 **全部缺席**
3. RULER-zh / LongBench-Chat-zh 对以上 4 个模型 — 无
4. Meta 多语言 MMLU 的**中文分项** — 被归入"non-English"未拆分
5. Groq 的 Llama-3.3-70B **具体位宽** — 未公开，INT8 是推测
6. Chinese needle-in-haystack at 32/64/128/200K — **任何主流闭源模型都未公开**

---

## Jarvis 实战建议

1. **意图路由 (Groq Llama-3.3-70B)**: 保持现状。中文 intent 是短约束任务，预期退化 <1%。如果发现中文 edge case 异常，先用 Groq 的 Llama-3.1-8B 或 Together/Fireworks FP16 部署做 diff。
2. **主 LLM (xAI Grok-4.1-fast)**: ⚠️ **写 20-30 条真实 Jarvis prompt 内测**再定生死。独立数据显示 non-reasoning 指令遵循大幅退化 (-24.3pp) — 对语音命令场景是直接风险。若内测不过，切 Sonnet 4.6 或 Kimi-K2.5。
3. **中文优先备选**: SuperCLUE 2025 年度前三 — Kimi-K2.5-Thinking (61.50) / Qwen3-Max-Thinking (60.61) / DeepSeek-V3.2。国内 API 延迟低，且中文原生训练。
4. **长上下文 (>32K) 需求**: 只有 Sonnet 4.6 有公开 MRCR 数据支持，其他全部靠推断。若 Jarvis 真用到 32K+ 上下文，做一次自建中文 NIAH 基线测试，别信厂商宣称窗口。

---

## 引用汇总 (27 个)

**官方 model/system card**:
- Anthropic Claude Sonnet 4.6 system card (2026-02-17) — anthropic.com
- xAI Grok 4.1 Model Card (2025-11-17) — x.ai/news/grok-4-1
- Meta Llama 3.3 Model Card — llama.com / huggingface.co/meta-llama/Llama-3.3-70B-Instruct
- Groq model docs — console.groq.com/docs/models

**Benchmark 论文/排行榜**:
- MMLU-ProX: arXiv 2503.10497
- LongBench-v2: longbench2.github.io
- NVIDIA RULER: github.com/NVIDIA/RULER
- SuperCLUE 2025 annual: superclueai.com
- C-Eval: cevalbenchmark.com
- ITSoloTime independent Chinese eval: itsolotime.com

**量化/精度**:
- SambaNova 2024 Llama-3 Groq comparison: sambanova.ai/blog
- Cerebras Llama-3.1-70B benchmark: cerebras.net
- Groq TruePoint Numerics docs

**子 agent 输出**:
- `notes/research-pack-4-llama-chinese-2026-04-14.md` (16 verified, 8 gaps)
- `notes/research-pack-4-groq-deployment-2026-04-14.md`
- `notes/research-pack-4-grok-claude-chinese-2026-04-14.md`
