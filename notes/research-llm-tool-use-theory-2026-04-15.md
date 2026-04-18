# LLM Tool-Use Emergence: 学术/理论基础

*Generated: 2026-04-15 | Sources: 30+ papers | Confidence: High*

## Executive Summary

学术界已经从三个方向确认了"少量原子工具 + LLM 编排 = 开放式任务完成"的可行性：

1. **LATM 范式成立** — GPT-3.5 用 GPT-4 造的工具在 6 个推理任务上追平甚至超过 GPT-4 直接 CoT，推理成本降低 ~15x
2. **组合泛化是潜在能力** — SKiC、STEPS 等论文证明预训练 LLM 已具备组合工具的能力，瓶颈在激活方式和训练数据覆盖
3. **工具数量的真正瓶颈是检索而非推理** — 模型在 ~10 个工具的上下文中表现良好；扩展到 16k+ 工具时，IR 检索（nDCG@10 仅 ~34）才是性能崩塌的主因

---

## 1. 涌现式工具组合 (Emergent Tool Composition)

### 1.1 Chameleon — 异构模块组合推理
- **论文:** Chameleon: Plug-and-Play Compositional Reasoning with Large Language Models
- **时间:** 2023-04 (NeurIPS 2023)
- **作者:** Pan Lu et al. (UCLA / Microsoft Research)
- **核心发现:** LLM planner 组合视觉模型、搜索、Python、其他 LLM 等异构模块解决多模态推理，ScienceQA +11.37%, TabMWP +17%。GPT-4 planner 展现一致的、约束感知的工具选择
- **评估:** 真实任务 (ScienceQA, TabMWP)
- **链接:** https://arxiv.org/abs/2304.09842

### 1.2 SKiC — 上下文中的技能组合
- **论文:** Skills-in-Context Prompting: Unlocking Compositionality in Large Language Models
- **时间:** 2023-08 (修订 2024-07)
- **作者:** Jiaao Chen et al. (Tencent AI Lab)
- **核心发现:** 在 prompt 中同时展示基础技能和组合示例，LLM 即可泛化到更难的未见问题。仅 2 个示例即可实现近完美的系统性泛化。**关键论点：组合能力是预训练 LLM 的潜在能力，通过正确的 prompt 结构被"解锁"而非从零训练**
- **评估:** 混合（数学推理、符号任务、组合 QA）
- **链接:** https://arxiv.org/abs/2308.00304

### 1.3 ToolNet — 图结构工具导航
- **论文:** ToolNet: Connecting Large Language Models with Massive Tools via Tool Graph
- **时间:** 2024-02
- **作者:** Xukun Liu et al. (Northwestern / MSRA)
- **核心发现:** 把工具组织为有向加权图，LLM 通过图遍历选择工具序列，可扩展到数千工具并保持组合正确性
- **评估:** 合成基准
- **链接:** https://arxiv.org/abs/2403.00839

### 1.4 GenTool — 零到一、弱到强工具泛化
- **论文:** GenTool: Enhancing Tool Generalization in Language Models
- **时间:** 2025-02
- **作者:** Jie He et al. (Edinburgh / Microsoft)
- **核心发现:** 训练框架模拟"零到一"（从未见过的工具）和"弱到强"（简单训练、复杂组合使用）两个泛化维度，在 ToolBench 16k+ 真实 API 上显著提升组合能力
- **评估:** 真实 REST API
- **链接:** https://arxiv.org/abs/2502.18990

### 1.5 ToolOrchestra — RL 编排器超越 GPT-5
- **论文:** ToolOrchestra: Elevating Intelligence via Efficient Model and Tool Orchestration
- **时间:** 2025-11
- **作者:** Hongjin Su et al. (NVIDIA / HKU)
- **核心发现:** 8B 编排模型通过 RL 学习组合工具，在 Humanity's Last Exam 达 37.1%（GPT-5 为 35.1%），成本降低 2.5x，且能泛化到训练时未见的工具组合。**最强的真实世界涌现组合证据**
- **评估:** 真实任务 (HLE, tau2-Bench, FRAMES)
- **链接:** https://arxiv.org/abs/2511.21689

### 1.6 ToolOmni — 开放世界工具使用
- **论文:** ToolOmni: Enabling Open-World Tool Use via Agentic Learning
- **时间:** 2026-04 (最新)
- **作者:** Shouzheng Huang et al. (HIT)
- **核心发现:** 模型学到"通用元技能"可迁移到全新工具类别，Category Generalization 55.95% vs ChatGPT 42.10%。ToolBench 端到端成功率 +10.8%
- **评估:** 真实 REST API
- **链接:** https://arxiv.org/abs/2604.13787

### 1.7 STEPS — 长尾组合数据的理论解释
- **论文:** Towards Compositional Generalization of LLMs via Skill Taxonomy Guided Data Synthesis
- **时间:** 2026-01
- **作者:** Yifan Wei et al. (Beihang / BAAI)
- **核心发现:** 复杂技能组合遵循长尾幂律分布，这是组合泛化的数据瓶颈。用信息论方法构建层次技能分类、合成最大信息量训练数据可解决
- **评估:** 指令跟随 + agent 基准
- **链接:** https://arxiv.org/abs/2601.03676

### 1.8 综述 — 工具使用的演化
- **论文:** The Evolution of Tool Use in LLM Agents: From Single-Tool Call to Multi-Tool Orchestration
- **时间:** 2026-03
- **作者:** Haoyuan Xu et al.
- **核心发现:** 领域从单工具调用演化到长时序多工具编排，识别六个核心研究维度。"组合空间覆盖"和"RL 涌现自调节"是前沿方向
- **链接:** https://arxiv.org/abs/2603.22862

---

## 2. LATM (LLM-as-Tool-Maker) 及后续工作

### 2.1 LATM — 核心论文
- **论文:** Large Language Models as Tool Makers
- **时间:** 2023-05 (ICLR 2024)
- **作者:** Tianle Cai et al. (DeepMind / Princeton / Stanford)
- **机制:** 两阶段闭环 — (1) 强模型"造工具"（GPT-4 从 k=3 示例生成 Python 函数）→ 验证 → 封装; (2) 弱模型"用工具"（GPT-3.5 调用缓存工具）; (3) 轻量分发器路由到已有工具或触发新造
- **量化结果:**

| 任务 | GPT-3.5 CoT | GPT-3.5 + LATM | GPT-4 CoT |
|---|---|---|---|
| Logical Deduction (5) | 66.4% | **79.7%** (+13.3) | 88.8% |
| Tracking Shuffled Objects | 61.6% | **99.6%** (+38.0) | 100.0% |
| Dyck Language | 20.4% | **92.2%** (+71.8) | 63.6% |
| Word Sorting | 59.2% | **98.3%** (+39.1) | 90.9% |
| Chinese Remainder Theorem | 0.0% | **100.0%** (+100) | 0.0% |
| Schedule Meeting | 18.9% | **100.0%** (+81.1) | 55.6% |

- **关键结论:** GPT-3.5 + LATM 工具在几乎所有任务上追平或超越 GPT-4 CoT，推理成本 ~15x 更低
- **分发器准确率:** 95%±2% 识别正确缓存工具，96%±3% 判断何时需造新工具
- **局限:** GPT-3.5 作为造工具者在难任务上 0/5 成功（需要强模型造工具）；所有评估任务为 BIG-Bench 结构化推理
- **评估:** 主要为合成基准
- **链接:** https://arxiv.org/abs/2305.17126

### 2.2 CREATOR — 抽象与具体推理分离
- **论文:** CREATOR: Tool Creation for Disentangling Abstract and Concrete Reasoning
- **时间:** 2023-05 (EMNLP 2023)
- **作者:** Cheng Qian et al. (Tsinghua / UIUC)
- **核心发现:** 将工具创建（写泛化函数）和决策执行（传具体参数）分离为四阶段，在 MATH 和 TabMWP 上超越 CoT/PoT 基线。合并两步的版本（Entangled）表现更差，确认分离是关键
- **评估:** 结构化基准
- **链接:** https://arxiv.org/abs/2305.14318

### 2.3 ToolMaker — 从论文自动生成工具
- **论文:** LLM Agents Making Agent Tools
- **时间:** 2025-02 (ACL 2025)
- **作者:** Georg Wölflein et al. (TU Dresden / St Andrews)
- **核心发现:** 自动将 GitHub 仓库 + 论文转化为 LLM 可调用的 Python 工具，通过闭环自修正实现 80% 任务完成率，超越 OpenHands
- **评估:** 真实科学任务（生物信息、病理学等）
- **链接:** https://arxiv.org/abs/2502.11705

### 2.4 AgentFactory — 可执行子代理累积
- **论文:** AgentFactory: A Self-Evolving Framework Through Executable Subagent Accumulation and Reuse
- **时间:** 2026-03
- **作者:** Zhang Zhang et al. (PKU / BAAI)
- **核心发现:** 三阶段生命周期（安装→自进化→部署），成功方案保存为可执行 Python 子代理（非文本反思），跨运行进化。Claude Opus 4.6: 2,971 tokens/task vs ReAct 7,022 (2.4x 效率提升)
- **评估:** 30 个真实任务（爬虫、可视化、会议预订等）— **最真实的评估**
- **链接:** https://arxiv.org/abs/2603.18000

### 2.5 Tool-Genesis — 自主工具创建基准
- **论文:** Tool-Genesis: A Task-Driven Tool Creation Benchmark
- **时间:** 2026-03 (ICML 2026)
- **作者:** Bowei Xia et al.
- **核心发现:** 即使 SOTA 模型也无法一次生成正确的工具接口；闭环修复（执行反馈调试）大幅改善结果，但收益依赖模型规模
- **评估:** 真实 MCP 工具构建任务
- **链接:** https://arxiv.org/abs/2603.05578

### LATM 范式总结

**成立吗？** 是的，在其领域内令人信服。核心发现：
- 造工具 + 编排 > 端到端推理（成本低 15x，准确率同等或更高）
- **但需要强模型造工具** — 弱模型造工具在难任务完全失败
- **但主要验证在结构化任务** — LATM 本身未测试开放式真实场景
- AgentFactory (2026) 将范式扩展到真实多步骤任务，验证了 LATM 假设在更大规模上的成立

---

## 3. 原子动作 / 原始动作方法 (Primitive/Atomic Actions)

### 3.1 Scaling Coding Agents via Atomic Skills
- **时间:** 2026-04
- **作者:** Yue Liu et al.
- **核心发现:** 将软件工程分解为 5 个原子技能（代码定位、编辑、单元测试生成、问题复现、代码审查）作为"基向量"，联合 RL 训练在原子和未见组合基准上平均提升 18.7%
- **评估:** 真实任务 (SWE-bench 级)
- **链接:** https://arxiv.org/abs/2604.05013

### 3.2 SayCan — 机器人领域的经典
- **论文:** Do As I Can, Not As I Say: Grounding Language in Robotic Affordances
- **时间:** 2022-04 (CoRL 2023)
- **作者:** Michael Ahn et al. (Google Robotics)
- **核心发现:** LLM 评分哪个预训练低级技能（拿、放、找等）最可能推进高级指令；affordance 函数将 LLM 接地到现实可行性。固定原语库 → 长时序指令执行
- **评估:** 真实世界（101 任务，移动厨房机器人）
- **链接:** https://arxiv.org/abs/2204.01691

### 3.3 RH20T-P — 原语级机器人数据集
- **论文:** RH20T-P: A Primitive-Level Robotic Dataset Towards Composable Generalization Agents
- **时间:** 2024-03
- **作者:** Zeren Chen et al. (Shanghai AI Lab)
- **核心发现:** 标准化层次原语技能集 + 38k 标注片段，VLM plan-execute 范式展示对未见任务的组合泛化
- **评估:** 真实机器人操作（67 任务）
- **链接:** https://arxiv.org/abs/2403.19622

### 3.4 Husky — 统一动作本体
- **论文:** Husky: A Unified, Open-Source Language Agent for Multi-Step Reasoning
- **时间:** 2024-06
- **作者:** Joongwon Kim et al. (UW / Meta AI / AI2)
- **核心发现:** 定义 4 种动作类型（代码、数学、搜索、常识）作为完整原语集，7B 模型在 14 个评估数据集上匹配或超越 GPT-4
- **评估:** 基准任务（真实世界邻近）
- **链接:** https://arxiv.org/abs/2406.06469

### 3.5 CompWoB — 组合泛化的警示
- **论文:** Exposing Limitations of LM Agents in Sequential-Task Compositions on the Web
- **时间:** 2023-11
- **作者:** Hiroki Furuta et al. (U Tokyo / Google)
- **核心发现:** **GPT-4 在基础任务 94% → 组合任务 24.9%**。微调迁移模型退化更少。**原子任务精通不自动等于组合泛化**
- **评估:** Web 自动化基准
- **链接:** https://arxiv.org/abs/2311.18751

### 3.6 HERAKLES — 开放式技能编译
- **论文:** HERAKLES: Hierarchical Skill Compilation for Open-ended LLM Agents
- **时间:** 2025-08
- **作者:** Thomas Carta et al. (INRIA / Sorbonne)
- **核心发现:** 两级代理持续将掌握的目标编译为快速低级神经策略，动态扩展 LLM 高级控制器的子目标空间，实现无预设子目标的开放式学习
- **评估:** 合成 (Crafter 环境)
- **链接:** https://arxiv.org/abs/2508.14751

### 3.7 Greedy Is Enough — 理论基础
- **论文:** Greedy Is Enough: Sparse Action Discovery in Agentic LLMs
- **时间:** 2026-01
- **作者:** Angshul Majumdar
- **核心发现:** **理论证明**（块稀疏恢复 / OMP）任何部署上下文中只需对数级小子集的完整动作空间。首次为"小动作集足以完成开放任务"提供数学基础
- **评估:** 纯理论
- **链接:** https://arxiv.org/abs/2601.08280

### 原子动作总结

- **最深入的工作在机器人领域** — SayCan, RAPS, RH20T-P 在物理机器人上证明固定小原语库可组合出数千任务变体
- **LLM agent 社区正趋同** — Husky (4 动作), Scaling Coding Agents (5 原子技能) 都证明预定义动作词汇 + 在该层级训练/组合 > 端到端复合任务训练
- **理论基础刚刚建立 (2026)** — "Greedy Is Enough" 首次形式化证明稀疏动作集的充分必要性
- **关键警示 (CompWoB):** 原子任务精通 ≠ 组合泛化，差距仍是核心研究前沿

---

## 4. 工具数量 vs 准确率：基准测试分析

### 4.1 Toolformer
- **时间:** 2023-02 | **作者:** Timo Schick et al. (Meta AI)
- **工具数:** 仅 5 个工具
- **结果:** 计算器工具在 SVAMP 上 29.4% vs 基线 5.2% (6x); 搜索工具在 T-REx 上 53.5% vs 31.9%
- **关键发现:** 工具使用能力在 **~775M 参数**以上才涌现；未研究工具数量扩展
- **评估:** 学术基准 | **链接:** https://arxiv.org/abs/2302.04761

### 4.2 Gorilla (APIBench, 1,645 API)
- **时间:** 2023-05 | **作者:** Shishir G. Patil et al. (UC Berkeley / MSR)
- **结果:**

| 模型 | TorchHub (94 API) | HuggingFace (925 API) | TensorHub (626 API) |
|---|---|---|---|
| GPT-4 zero-shot | 38.7% | 19.8% | 18.2% |
| GPT-3.5 zero-shot | 48.4% | 16.8% | 41.75% |
| Gorilla zero-shot | **59.1%** | **71.7%** | **83.8%** |
| Gorilla + Oracle | 67.2% | 91.3% | 94.2% |

- **关键发现:** 非 Oracle 检索器（BM25）将 Gorilla 从 59.1% 降到 40.3%（降 19pp）；约束选择再降 ~15-20pp
- **幻觉:** LLaMA zero-shot 100% 幻觉；GPT-4 37-79% 幻觉；Gorilla fine-tuned 5-11%
- **评估:** 真实 ML API | **链接:** https://arxiv.org/abs/2305.15334

### 4.3 ToolLLM / ToolBench (16,464 API)
- **时间:** 2023-07 (ICLR 2024) | **作者:** Yujia Qin et al. (Tsinghua)
- **机制:** 每个任务通过神经检索器从 16k API 缩小到 ~10 个候选
- **结果:** ToolLLaMA 在 I1/I2 场景匹配 ChatGPT (pass rate ~47-60%)；DFSDT 比 CoT 提升 10-20pp
- **关键发现:** **实际上下文中始终是 ~10 个工具**，16k 只是检索池。没有检索的 16k 工具不可行
- **评估:** 真实 REST API | **链接:** https://arxiv.org/abs/2307.16789

### 4.4 API-Bank (73 API, 三层能力测试)
- **时间:** 2023-04 (EMNLP 2023) | **作者:** Minghao Li et al. (Alibaba)

| 模型 | Call (已知 ~3 API) | Retrieve+Call (73 API) | Plan+Retrieve+Call | 总计 |
|---|---|---|---|---|
| GPT-3.5 | 59.4% | 38.5% | 22.0% | 47.2% |
| GPT-4 | 63.7% | 37.0% | **70.0%** | 60.2% |

- **关键发现:** 从"给定 API"到"检索 API"，GPT-3.5 降 **21pp**；再加规划降 17pp。GPT-4 例外：规划层反而提升（70% vs 37%）
- **API 幻觉**是微调模型的主要错误（61%）
- **评估:** 可执行真实 API | **链接:** https://arxiv.org/abs/2304.08244

### 4.5 ToolRet — 检索是真正瓶颈
- **论文:** Retrieval Models Aren't Tool-Savvy
- **时间:** 2025-03 | **作者:** Zhengliang Shi et al.
- **规模:** 43,215 工具语料库
- **关键发现:** 最佳 IR 模型 nDCG@10 仅 **33.83**。Recall@10 与下游任务通过率直接正相关。用 ToolRet 数据集训练检索器可显著改善
- **评估:** 聚合自 34 个真实基准 | **链接:** https://arxiv.org/abs/2503.01763

### 4.6 WildToolBench — 真实世界难度
- **时间:** 2026-02
- **关键发现:** 57 个 LLM 中无一超过 15% session accuracy。对话中指令转换增加到 3 次时性能下降达 30%
- **评估:** 真实用户行为 | **链接:** https://arxiv.org/abs/2604.06185

### 准确率 vs 工具数量总结表

| 工具数量级 | 表现 | 关键瓶颈 | 来源 |
|---|---|---|---|
| **1-5 (全部在上下文中)** | 良好。Toolformer +21pp factual, +24pp math | 模型规模 (≥775M) | Toolformer |
| **10-20 (预选在上下文中)** | 可用。GPT-4 38-64%, fine-tuned 60-84% | 工具描述质量 | Gorilla, API-Bank, ToolBench |
| **73-1,645 (需检索)** | 明显退化。检索引入 -17~21pp | 检索器质量 | API-Bank, Gorilla |
| **16k-43k (大规模)** | 仅通过检索缩小到 ~10 可行。IR nDCG@10 ~34 | IR 模型能力 | ToolLLM, ToolRet |
| **真实世界/野外** | <15% session accuracy | 组合复杂度 + 用户行为变化 | WildToolBench |

**核心规律:** 准确率退化不是工具数量的简单函数，而是**检索难度**的函数。模型在 ~10 个正确工具的上下文中表现良好。真正的瓶颈是从数千工具中找到那 10 个。

---

## 5. 关键启示 (Key Takeaways)

### 对 Jarvis 设计的启示

1. **10-30 个精选工具是甜蜜区间** — 学术证据一致表明这是 LLM 工具使用的高效区间，无需检索层
2. **原子工具 + 编排 > 复合工具** — LATM 和 Scaling Coding Agents 都证明：分解为原子操作后组合 >> 端到端复杂工具
3. **组合能力已内置于预训练模型** — 不需要专门训练工具组合，需要的是正确的 prompt 结构（SKiC）和足够好的工具描述
4. **CompWoB 警示适用** — 单个 skill 测试通过不等于组合场景可靠，需要组合级别的测试
5. **工具描述质量 >> 工具数量** — Gorilla 证明 fine-tuning on domain data 将幻觉从 37-79% 降到 5-11%
6. **闭环修复是关键** — Tool-Genesis 显示一次性生成失败率高，但执行反馈 + 修复循环大幅改善结果

### 开放问题

- 没有论文提供组合涌现在 transformer 权重中**如何发生**的机制解释
- 真实世界 session accuracy 仍然很低 (<15%)，学术基准已饱和但实际应用未解决
- 工具检索（从大池中选小集）仍是最薄弱环节

---

## Sources (按出现顺序)

1. [Chameleon](https://arxiv.org/abs/2304.09842) — NeurIPS 2023, 异构模块组合推理
2. [SKiC](https://arxiv.org/abs/2308.00304) — 上下文技能组合解锁 LLM 组合泛化
3. [ToolNet](https://arxiv.org/abs/2403.00839) — 图结构工具导航
4. [GenTool](https://arxiv.org/abs/2502.18990) — 零到一/弱到强工具泛化
5. [ToolOrchestra](https://arxiv.org/abs/2511.21689) — 8B RL 编排器超越 GPT-5
6. [ToolOmni](https://arxiv.org/abs/2604.13787) — 开放世界工具元技能
7. [STEPS](https://arxiv.org/abs/2601.03676) — 长尾组合数据的理论解释
8. [Tool-Use Survey](https://arxiv.org/abs/2603.22862) — 工具使用演化综述
9. [LATM](https://arxiv.org/abs/2305.17126) — ICLR 2024, LLM 作为工具制造者
10. [CREATOR](https://arxiv.org/abs/2305.14318) — EMNLP 2023, 抽象/具体推理分离
11. [ToolMaker](https://arxiv.org/abs/2502.11705) — ACL 2025, 从论文自动造工具
12. [AgentFactory](https://arxiv.org/abs/2603.18000) — 可执行子代理累积与复用
13. [Tool-Genesis](https://arxiv.org/abs/2603.05578) — ICML 2026, 自主工具创建基准
14. [Scaling Coding Agents](https://arxiv.org/abs/2604.05013) — 5 原子技能 + 联合 RL
15. [SayCan](https://arxiv.org/abs/2204.01691) — CoRL 2023, 机器人原语 affordance
16. [RH20T-P](https://arxiv.org/abs/2403.19622) — 原语级机器人数据集
17. [Husky](https://arxiv.org/abs/2406.06469) — 4 动作统一推理代理
18. [CompWoB](https://arxiv.org/abs/2311.18751) — 组合 web 任务泛化警示
19. [HERAKLES](https://arxiv.org/abs/2508.14751) — 开放式技能编译
20. [Greedy Is Enough](https://arxiv.org/abs/2601.08280) — 稀疏动作发现理论
21. [Toolformer](https://arxiv.org/abs/2302.04761) — Meta AI, 自学工具使用
22. [Gorilla](https://arxiv.org/abs/2305.15334) — 1,645 API 基准
23. [ToolLLM](https://arxiv.org/abs/2307.16789) — ICLR 2024, 16,464 API
24. [API-Bank](https://arxiv.org/abs/2304.08244) — EMNLP 2023, 73 API 三层测试
25. [ToolRet](https://arxiv.org/abs/2503.01763) — 43k 工具检索瓶颈
26. [WildToolBench](https://arxiv.org/abs/2604.06185) — 真实世界工具使用基准
27. [StableToolBench](https://arxiv.org/abs/2403.07714) — 稳定可复现的 16k API 评估
28. [MetaTool](https://openreview.net/forum?id=R0c2qtalgG) — ICLR 2024, 工具意识基准
29. [Chain-of-Tools](https://arxiv.org/abs/2503.16779) — 冻结 LLM 的动态工具激活
30. [Boids for Emergent Tool-Building](https://openreview.net/forum?id=46LJ81Yqm2) — 多代理涌现工具创建

*Methodology: 4 parallel research agents searched exa across arxiv, semanticscholar, openreview. 30+ papers crawled and analyzed.*
