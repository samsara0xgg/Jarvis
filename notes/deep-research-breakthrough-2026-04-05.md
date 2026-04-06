# 突破记忆系统天花板：Deep Research 报告

*2026-04-05 | 3 个 Opus 研究 agent | ~25 篇来源*

---

## Executive Summary

现有研究证实了三个打破天花板的方向都有成熟的学术和工程基础。最关键的发现：

1. **Letta 的 sleep-time compute** 已经在生产中验证了双 agent 架构——记忆 agent 在空闲时用强模型整理记忆，响应 agent 用快模型回答。这不是理论，是在跑的系统。
2. **MIRROR 架构** 明确实现了"持久内心独白"——agent 在对话轮次之间维护独立的内部推理流。
3. **RPi5 能跑 1-1.5B 模型达到 7-8 tok/s**（Q4_K_M via llama.cpp），足以做本地记忆推理。
4. **没有任何主流语音助手实现了情绪加权记忆检索**——这是真正的蓝海。

---

## 1. 双 LLM 架构

### Letta Sleep-Time Compute（最成熟的实现）

- **Primary Agent**: 处理用户对话，用快模型（gpt-4o-mini），**没有记忆编辑工具**
- **Sleep-Time Agent**: 管理记忆，在空闲时运行，用强模型（gpt-4.1/Sonnet），**独占记忆写权限**
- **共享状态**: Memory Blocks（有 block_id、label、value、size_limit），持久化在数据库中
- **核心洞见**: "The primary agent should never trade off response quality for memory management"
- **Benchmark**: AIME/GSM 上实现帕累托改进——更好的质量 AND 更低延迟

### MemoRAG（学术清晰版）

- **Memory Model**（轻量 LLM）: 处理全部上下文，压缩为"memory tokens"，生成检索线索
- **Generation Model**（重量 LLM）: 只看 memory model 的输出+检索结果，生成回答
- 压缩比 2x-16x，Qwen2 变体处理 200 万 tokens
- Memory model 参数冻结，只训练新增权重矩阵
- 训练管线: pre-train → SFT → RLGF（用 generation model 的答案质量反馈训练 memory model）

### 小模型可行性

| 模型 | 参数 | RPi5 推理速度 | RAM |
|---|---|---|---|
| Gemma3:1b | 1B | 最高 | 低 |
| Qwen2.5:1.5b | 1.5B | ~7-8 tok/s | 低 |
| BitNet B1.58 2B 4T | 2B | ~8 tok/s | <2.5GB |
| Qwen2.5:3b | 3B | ~5 tok/s | 较高 |

Q4_K_M 是最佳量化——有时精度比 Q8 还好（减少过拟合），能耗比 Q3 低（Q3 的反量化开销大）。

---

## 2. 持久内心独白

### MIRROR 架构（2025，最直接相关）

- **Thinker**: 在用户可见回复之前生成内部推理
- **Inner Monologue Manager**: 维护一个独立的对话流——"assistant 只对自己说话"
- **Cognitive Controller**: 将内部推理整合到外部对话

内心独白是跨轮次持久化的，追踪用户偏好演变、沟通模式、推理状态。

### Reflexion（NeurIPS 2023）

- Actor → Evaluator → Self-Reflection → 存入 episodic memory buffer
- "Verbal reinforcement learning"——用自然语言反思代替梯度更新
- 保留最近 3 条自我反思
- 结果: HumanEval 91%（vs GPT-4 的 80%）

### Stanford Generative Agents 的反思触发

- **触发条件**: 近期事件的累计 importance 超过阈值
- **过程**: 查最近 100 条记忆 → 生成候选反思问题 → 回答 → 存为高层抽象
- 反思本身也被存回记忆流，可以被再次检索和反思

### SAGE（2024）

- 短期记忆（快速更新）+ 长期记忆（关键反思）
- **Ebbinghaus 遗忘曲线**: R = e^(-t/S)，双阈值决定保留/转移/丢弃
- **熵驱动优先级**: 高信息量保留，冗余丢弃
- 结果: memory optimization 让 Qwen-1.8B 从 6.8 提到 48.0

### Quiet-STaR（Stanford 2024）

- 训练模型在每个输出 token 前生成内部推理 token（"thoughts"）
- 常识推理准确率 +33%，零样本数学准确率翻倍
- 说明内部推理可以 baked into 模型本身

### Narrative Continuity Test（2025）

评估 AI 身份持久性的 5 个维度：
1. Situated Memory（上下文保持）
2. Goal Persistence（目标跨交互保持）
3. Autonomous Self-Correction（自主修正）
4. Stylistic Stability（风格一致性）
5. Persona Continuity（角色持续性）

---

## 3. 后台认知

### Active Dreaming Memory（ADM，2025）

- Agent 失败 → 生成执行轨迹 → "做梦"阶段创建候选规则
- **反事实验证**: "这个规则在类似但不同的场景中是否有帮助？" → 只有验证通过的才存入长期记忆
- 结果: 2x 首次学习效率，83% 成功率，显著超过 Reflexion/MemGPT/Self-RAG

### 主动式 Agent

Google Sensible Agent（UIST 2025）解决"打扰问题"——用实时多模态上下文感知决定何时介入。

四种触发类型：
1. **时间型**: 定时播报
2. **模式型**: 检测到行为规律
3. **事件型**: 外部事件触发
4. **行为信号型**: 用户行为模式变化

### 行为模式挖掘

- Sequential pattern mining（PrefixSpan, SPADE）
- 用户行为聚类 → 按频率+时近性排序
- 实用管线: 收集 → 预处理 → 模式发现 → 行动

### 关联发现（超越 cosine）

**PAM（2026.2）核心发现**: cosine 在跨边界召回上 = **0%**，PAM 达到 **42.1%**

机制: Inward JEPA 从时间共现中学习关联，不需要表示相似。"楼梯"和"滑倒"语义不相似但时间共现强关联。

---

## 4. 情绪智能 + 心智理论

### Emotional RAG（2024）

- 8 维情绪向量（joy/acceptance/fear/surprise/sadness/disgust/anger/anticipation），1-10 分
- 检索融合: semantic cosine + emotional cosine
- 结果: MBTI 准确率 +36.4%

### DAM-LLM（2025）

- 每条记忆存概率情感 profile（positive/negative/neutral 置信度）
- 贝叶斯更新: `C_new = (C * W + S * P) / (W + S)`
- **熵驱动遗忘**: 信念熵 H > 1.4 且权重低 → 剪枝
- 500 轮后减少 63-70% 记忆，同时提升性能
- ~15 次观察后情感 profile 收敛

### ToM-agent（2025，心智理论）

- 追踪不可观察的 BDI（Beliefs, Desires, Intentions）
- **反事实反思**: 预测用户下一句话 → 对比实际 → "如果我推断的 BDI 错了呢？"
- 结果: F1 0.45（beliefs）, 0.59（intentions），任务成功率 44-55%

### TheraMind（2025，纵向情绪理解）

- **双循环**: intra-session（战术，逐轮）+ cross-session（战略，长期轨迹）
- Reaction Classifier: (emotion, intensity[0,1], attitude{Cooperative,Resistant})
- 会话后评估+策略选择
- 结果: 95.59% 情绪识别，75% 人类偏好

### PersonaX（2025，用户建模）

- **层次聚类** 将交互历史分为语义一致的行为组
- **预算分配** 按比例（非均匀），防止多数兴趣主导
- 只用 30-50% 历史就达到完整性能

### 行业现状

**没有任何主流语音助手实现了情绪加权记忆检索**。
- Alexa（2025）加了跨会话记忆但明确说"未来可能集成情感分析"
- MARIA Voice 有 6 层记忆含 Emotional Pattern 层（最接近），但很新
- Hume AI EVI 3 做情绪生成（输出端）而非情绪理解（输入端）

---

## 5. 对小月的具体启发

### 可以现在做（用现有代码库）

| 改进 | 来源 | 实现方式 | 成本 |
|---|---|---|---|
| 情绪加权检索 | Emotional RAG | retriever 加第 5 信号（emotion_match）| 低 |
| 跨会话反思 | TheraMind/RMM | 会话结束后后台 LLM 更新 inner_monologue.md | 每次 ~$0.001 |
| 行为模式检测 | behavior_log 消费 | cron/idle 时扫描时间模式 | 零（本地计算）|
| 时间共现信号 | PAM | retriever 第 6 信号（co_occurrence）| 低 |
| 内心独白注入 | MIRROR | 加 inner_monologue.md，注入 system prompt | 低 |

### 中期做（需要新组件）

| 改进 | 来源 | 实现方式 | 成本 |
|---|---|---|---|
| Sleep-time agent | Letta | 空闲时 GPT-4o 整理记忆 | ~$0.01/day |
| "做梦"过程 | ADM | 每晚回顾失败→反事实验证→存规则 | ~$0.005/night |
| 本地记忆推理 | MemoRAG | Qwen2.5:1.5b Q4_K_M on RPi5 | 一次性（模型下载）|
| 用户 BDI 追踪 | ToM-agent | 预测用户下句→对比→更新模型 | 中 |

### 长期做（需要微调）

| 改进 | 来源 | 实现方式 |
|---|---|---|
| 专属记忆 LLM | MemoRAG | 用用户对话数据微调 1.5B 模型 |
| 情感 profile | DAM-LLM | 每条记忆的贝叶斯情感分数 |
| 人格面向建模 | PersonaX | 层次聚类现有记忆为多面向 |

---

## 来源（~25 篇）

### 双 LLM / Sleep-Time
- [Letta: Sleep-time Compute](https://www.letta.com/blog/sleep-time-compute)
- [Letta: Memory Blocks](https://www.letta.com/blog/memory-blocks)
- [MemoRAG (arXiv 2409.05591)](https://arxiv.org/abs/2409.05591)
- [Best Open-Source SLMs 2026](https://www.bentoml.com/blog/the-best-open-source-small-language-models)

### 持久内心独白 / 认知架构
- [MIRROR: Cognitive Inner Monologue (arXiv)](https://arxiv.org/pdf/2506.00430)
- [Reflexion (NeurIPS 2023)](https://arxiv.org/abs/2303.11366)
- [Generative Agents (UIST 2023)](https://arxiv.org/abs/2304.03442)
- [SAGE: Self-evolving Agents (arXiv)](https://arxiv.org/html/2409.00872v2)
- [Quiet-STaR (Stanford 2024)](https://www.pymnts.com/artificial-intelligence-2/2024/new-ai-training-method-boosts-reasoning-skills-by-encouraging-inner-monologue/)
- [Narrative Continuity Test (arXiv)](https://arxiv.org/abs/2510.24831)
- [Cognitive Design Patterns for LLM Agents (arXiv)](https://arxiv.org/html/2505.07087v2)
- [LLM-ACTR (arXiv)](https://arxiv.org/abs/2408.09176)

### 后台认知 / 关联发现
- [Active Dreaming Memory (ResearchGate)](https://www.researchgate.net/publication/398306877)
- [Sensible Agent (Google Research)](https://research.google/blog/sensible-agent/)
- [PAM: Predictive Associative Memory (arXiv)](https://arxiv.org/html/2602.11322)
- [Beyond Cosine Similarity (arXiv)](https://arxiv.org/html/2602.05266v1)
- [User Behavior Mining (Springer)](https://link.springer.com/article/10.1007/s12599-023-00848-1)

### 边缘设备
- [RPi5 LLM Benchmarks (Stratosphere Lab)](https://www.stratosphereips.org/blog/2025/6/5/how-well-do-llms-perform-on-a-raspberry-pi-5)
- [Sustainable LLM Inference (arXiv 2504.03360)](https://arxiv.org/html/2504.03360v1)
- [llama.cpp Optimization Guide](https://ohyaan.github.io/tips/local_llm_optimization_with_llama.cpp_-_on-device_ai/)

### 情绪 / 心智理论
- [Emotional RAG (arXiv 2410.23041)](https://arxiv.org/html/2410.23041v1)
- [DAM-LLM (arXiv 2510.27418)](https://arxiv.org/html/2510.27418v1)
- [MemEmo Benchmark (arXiv 2602.23944)](https://arxiv.org/html/2602.23944v1)
- [ToM-agent (arXiv 2501.15355)](https://arxiv.org/html/2501.15355v1)
- [TheraMind (arXiv 2510.25758)](https://arxiv.org/html/2510.25758v2)
- [PersonaX (arXiv 2503.02398)](https://arxiv.org/html/2503.02398v1)
- [Reflective Memory Management (ACL 2025)](http://arxiv.org/abs/2503.08026v2)
- [MARIA Voice Architecture](https://os.maria-code.ai/blog/maria-voice-agi-assistant-architecture)
