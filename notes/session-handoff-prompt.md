# Session Handoff Prompt — 记忆系统优化全程上下文

复制以下全部内容作为新 session 的第一条消息。

---

我在做小月私人语音管家的记忆系统优化。以下是完整上下文，请仔细阅读后继续。

## 一、第一性原则分析（这是一切的起点）

我最初的要求是："对目前的记忆系统做完整评估，可以借鉴别的人工智能体的记忆系统设计，但需要符合一个私人助手的需求，第一性原则，深度思考"

### 人类记忆的四层认知模型 → 映射到小月

| 认知系统 | 功能 | 小月实现 | 优化前评分 | 优化后评分 |
|---|---|---|---|---|
| **工作记忆** | 当前对话上下文 | ConversationStore 20轮滑窗 | B+ | B+（未改） |
| **语义记忆** | 用户的事实/偏好/身份 | memories 表 6类 + user_profiles | C+ | **A** |
| **情景记忆** | 发生了什么事，什么感受 | episodes 表 + behavior_log | C | **A-** |
| **程序记忆** | 习得的行为模式 | behavior_log 272条原始数据 | **D** | **D**（未消费，下一步重点） |

### 五大失败模式（全部已修复）

- **FM1 记错** → DELETE 主动矛盾检测 + 问句门控 + content dedup
- **FM2 该记没记** → FC 提取 + key 模式映射(25 patterns) + 关系词扩展
- **FM3 无关干扰** → 预算 2000 + 渐进检索 + importance 排序 + 问句门控
- **FM4 不会遗忘** → sweep_expired + backfill_expires + pending 清理
- **FM5 时间错乱** → `date('now','localtime',?)`

### 量化评估结果

| 指标 | 优化前 | 目标 | 最终 |
|---|---|---|---|
| DA 命中率 | 0% | >50% | **95%** |
| Retriever MRR@5 | 未测 | >0.6 | **1.00** |
| 负面拒绝 | 未测 | — | **100%** |
| 测试 | 742 | — | **815** |

## 二、已完成的优化（5 Phase, 15+项改进）

### Phase 1: Bug 修复
- C1: timezone `date('now','localtime',?)`
- C2: DA 阈值 0.75→0.55→多信号 0.35, margin 0.08→0.05, embedder 已是 bge-small-zh-v1.5
- C3: sweep_expired + backfill_expires + pending cleanup
- C4: 注入预算 1200→2000, 使用原则移到 personality.py

### Phase 2: 提取质量（Mem0 启发）
- C5: function calling 提取 + JSON fallback + postprocess(key/expires/importance)
- C6: ADD/UPDATE/DELETE/NONE 四操作 + top-10 候选
- C7: 去重阈值校准（45 对中文实测）same_cat 0.55→0.65

### Phase 3: 检索智能
- C8: 渐进检索（100→20 阈值 + 动态 top_k）
- C9: 冷启动自适应（cosine 0.40→0.60 when all access=0）
- C10: eval_memory.py 评估框架（40 个中文案例）

### Phase 4: 情景+关系
- C11: Episode Jaccard 去重 + episode_digests 周压缩
- C12: ASR 情绪 → episode mood 传导
- C13: memory_relations 表 + regex 实体提取

### Phase 5: DA 提升 + 真实验证
- C14: DA 改用 retriever 多信号评分（60%→85%→95%）
- C15: 真实 LLM 提取测试（41/41 checks passed）
- 关键 bug：relationship 不在 _ANSWERABLE_CATEGORIES, 陈述句触发 DA, LLM 重复提取已有记忆

### 真实语音测试后的综合修复（6项）
- DA 回答 "用户"→"你"
- _is_question 加祈使式（"告诉我/来着/说一下"）
- 关系提取扩展到 identity+关系词
- episode 短摘要跳过 Jaccard
- _derive_key 25 个中文模式映射（"住在"→location 等）
- conversation history tool_result 400 错误修复

## 三、Deep Research 发现（~55 篇来源，两轮研究）

### 第一轮（31 篇）：业界记忆系统对比
- **Mem0**（arXiv 2504.19413）：LOCOMO benchmark 66.9% accuracy, 1.44s p95, ADD/UPDATE/DELETE/NOOP
- **MemGPT/Letta**：分层记忆 + LLM-as-memory-editor, Letta 简单工具达到 74.0% 超过专用系统
- **ChatGPT Memory**：全量注入 system prompt（不用 RAG），~1200 词限制
- **Gemini Memory**：三层（Saved Info + Raw Data + user_context），时间元数据
- **Zep**：双时间模型，三阶段检索，71.2% accuracy
- **bge-small-zh**：原版分布异常（cosine 集中 [0.6,1.0]），v1.5 修复
- **MemoryBank（AAAI 2024）**：Ebbinghaus 遗忘公式 strength = importance * e^(-λ*days) * (1 + recall*0.2)

### 第二轮（~25 篇）：突破天花板研究
详细报告在 `notes/deep-research-breakthrough-2026-04-05.md`，核心发现：

**双 LLM 架构**：
- Letta sleep-time compute：primary agent（快模型，无记忆编辑权）+ sleep-time agent（强模型，独占记忆写权限），生产验证
- MemoRAG（arXiv 2409.05591）：专门的 memory model 生成"记忆线索"→ generation model 只管回答

**持久内心独白**：
- MIRROR 架构（2025）：agent 维护独立"对自己说话"的推理流，跨轮次持久
- Reflexion（NeurIPS 2023）：verbal reinforcement learning, HumanEval 91%（超 GPT-4 的 80%）
- Stanford Generative Agents：反思触发=累计 importance 超阈值 → 归纳高层抽象
- SAGE：Ebbinghaus 遗忘 + 熵驱动优先级，Qwen-1.8B 从 6.8→48.0

**后台认知**：
- Active Dreaming Memory（ADM）：失败 → "做梦"创建规则 → 反事实验证，2x 学习效率
- PAM（2026.2）：时间共现检索 42.1% 跨边界召回，cosine = 0%
- Google Sensible Agent：解决"打扰问题"的主动式 agent

**情绪智能 + 心智理论**：
- Emotional RAG：8 维情绪向量 + cosine 融合，MBTI 准确率 +36.4%
- DAM-LLM：贝叶斯情感 profile，~15 次观察收敛，熵驱动遗忘
- ToM-agent：反事实反思追踪 BDI（Beliefs/Desires/Intentions）
- TheraMind：双循环（intra-session 战术 + cross-session 战略）
- **没有任何主流语音助手实现了情绪加权记忆检索** — 蓝海

**边缘设备**：
- RPi5 上 Qwen2.5:1.5b Q4_K_M = 7-8 tok/s, <2GB RAM
- **但用户实测 3B 模型不能胜任路由，8B 也勉强** → 本地 LLM 做"理解"类任务不可行
- Q4_K_M 是最佳量化（有时比 Q8 好）

## 四、记忆系统 IQ 评估和天花板分析

### 当前 IQ ~85

| 维度 | 估值 | 说明 |
|---|---|---|
| 事实回忆 | ~120 | MRR=1.00, DA=95% |
| 关联推理 | ~70 | 只有 cosine，没有因果/时间共现 |
| 模式识别 | ~50 | behavior_log 有数据零消费 |
| 预测/主动 | ~50 | 纯被动 |
| 情绪智能 | ~60 | 能检测但不影响行为 |

### 四个层级

| 层级 | IQ | 需要什么 |
|---|---|---|
| Level 1 "聪明档案柜" | ~95 | 行为模式消费 + 情绪加权 + 内心独白 |
| Level 2 "有感知的助手" | ~105-110 | 抽象形成 + 预测性召回 + 关系推理 |
| Level 3 "有共情的伙伴" | ~120+ | 心智理论 + 人格建模 + 创造性关联 |
| Level 4 | 不可能 | 真正理解、意识连续性 |

### 硬天花板在 LLM 本身
- 记忆系统是外挂硬盘，LLM 是无状态函数
- LLM 不是"想起来"了，是"读到"了
- 但可以绕过：内心独白创造伪连续意识，背景认知让系统在用户不说话时也在"想"

## 五、下一步方向（已确认）

**不需要本地模型的高价值改进**：

| 方向 | 实现方式 | 成本 |
|---|---|---|
| **内心独白** | inner_monologue.md, 会话结束后云端 LLM 更新 | ~$0.001/次 |
| **行为模式检测** | 纯 Python 统计扫描 behavior_log | 零 |
| **情绪加权检索** | retriever 加第 5 信号 | 零 |
| **Sleep-time 整理** | 空闲时云端 LLM 整理记忆 | ~$0.01/天 |
| **时间共现信号** | retriever 加第 6 信号 | 零 |

优先级：内心独白 > 行为模式 > 情绪加权

## 六、关键文件

- `memory/manager.py` — 核心编排（FC 提取 + postprocess + DELETE + 关系提取）
- `memory/store.py` — SQLite（memories/episodes/episode_digests/memory_relations 4 表）
- `memory/retriever.py` — 4 信号 + 冷启动自适应
- `memory/direct_answer.py` — 多信号评分 + 问句门控 + cosine 安全网
- `scripts/eval_memory.py` — 40 案例评估
- `scripts/calibrate_da.py` / `calibrate_dedup.py` — 校准脚本
- `notes/deep-research-breakthrough-2026-04-05.md` — 突破天花板研究报告（~25 篇来源）
- `.claude/plans/functional-crafting-crab.md` — 初始完整评估方案（31 篇来源）

请读取 progress.md 和 task_plan.md 获取最新状态，读取 notes/deep-research-breakthrough-2026-04-05.md 获取研究细节。准备好后告诉我，我们继续实施内心独白或行为模式检测。
