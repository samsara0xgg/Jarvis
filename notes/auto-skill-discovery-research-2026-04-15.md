# Auto Skill Discovery Research
*Generated: 2026-04-15 | Sources: 40+ | Parallel agents: 5*

## Executive Summary

"从 trace 自动发现 hotspot → 编译成 skill" 不是幻想，但也不是任何成熟商业产品的核心功能。现状：

- **学术界**（2023-2026）有 12+ 篇论文，从 Voyager 的 JS 函数库到 Trace2Skill 的声明式 skill 文件，技术路线已经清晰
- **开源工具** selftune（MIT，Claude Code 生态）和 Letta Skill Learning 是最接近生产可用的实现
- **商业 SaaS**（Zapier/Make/n8n/Lindy/Dust）全部是 "用户描述 → AI 生成 workflow"，**零**自动 pattern mining
- 最大转折：2026 年论文（EvoSkill/Trace2Skill/SkillX）全部转向**声明式 SKILL.md 文本格式**，抛弃了 2023 年的 Python/JS 代码格式 —— 这与你计划的 YAML skill 完全对齐

---

## 1. Voyager (MineDojo) — 开创性 Skill Library

[GitHub](https://github.com/MineDojo/Voyager) · [Paper](https://arxiv.org/abs/2305.16291) · [Site](https://voyager.minedojo.org/)

### Skill 发现触发条件

**纯粹的 success-gated**。`learn()` 循环里：
```python
if info["success"]:
    self.skill_manager.add_new_skill(info)
```
没有频率阈值、没有 novelty 过滤。任务由 CurriculumAgent 提出（内置 "discover diverse things" 压力），但 skill 是否入库只看 Critic agent 的 `success: true/false` 判定。

### Skill 表示

**JavaScript async 函数 + GPT 生成的自然语言 description**：
```json
{
  "mineWoodLog": {
    "code": "async function mineWoodLog(bot) { ... }",
    "description": "async function mineWoodLog(bot) {\n    // Mines one oak log by ...\n}"
  }
}
```
存储：`ckpt/skill/code/{name}.js` + `ckpt/skill/description/{name}.txt` + Chroma vector DB（OpenAI embeddings）。检索时 `similarity_search_with_score` 取 top_k=5 注入 prompt。

### 验证/晋升机制

三阶段 gauntlet：
1. **Babel parse** — 语法校验，必须包含 `async function (bot)`，失败重试 3 次
2. **环境反馈循环** — 执行后观察 runtime errors、inventory delta、health，最多 4 轮迭代
3. **Critic agent** — 独立 GPT-4 判定 `{reasoning, success, critique}`，只有 `success: true` 才入库

### 失败路径

- Parse 失败 → 消耗 retry budget，不入库
- Critic 拒绝 → critique 文本反馈给下轮，最多 4 次
- 异常 → hard reset 环境，标记 failed，curriculum 记录避免重复

### 已知失败模式

1. **Critic 幻觉** — 错误标记失败任务为成功，buggy skill 永久入库
2. **静默覆盖** — 同名 skill 直接替换，无质量对比
3. **无运行时失效检测** — 入库后永不测试/移除，环境变化导致静默错误
4. **硬编码 retry=4** — 需要 5 次迭代的任务永远放弃
5. **Vector DB 同步断裂** — 中断写入后 assert 崩溃

---

## 2. JARVIS / HuggingGPT (Microsoft)

[GitHub](https://github.com/microsoft/JARVIS) · [Paper](https://arxiv.org/abs/2303.17580)

**结论：没有 skill 积累机制。**

- 四阶段固定管线：Task Planning → Model Selection → Task Execution → Response Generation
- Tool selection = task-type 过滤 + Hugging Face 下载量排序 top-K + LLM 单选
- 无 plan cache、无 result memoization、无 workflow compilation
- 每次请求从头走完整 LLM pipeline
- 论文自认效率问题：*"requires multiple interactions with LLMs throughout the whole workflow"*

**可借鉴**：EasyTool ([arxiv:2401.06201](https://arxiv.org/abs/2401.06201)) 的思路 — 压缩 tool description 为统一简洁格式，提升 selection 准确率

---

## 3. AutoGPT / BabyAGI / MetaGPT

### AutoGPT — 无 skill 积累
手动 workflow 组合平台。Agent Blocks 是人工包装，非运行时发现。格式为 JSON workflow graph + Python block class。

### BabyAGI — 原始但真实的 skill 积累
**最接近你想要的简单实现**：
- `functionz` 框架：SQLite 存储 Python 函数 + embedding 索引
- 新任务到来 → embedding 检索已有函数 → 无匹配则 LLM 生成新函数 → 持久化
- `self_build` 模式可批量积累
- 无显式正确性测试，仅 embedding similarity 阈值门控

### MetaGPT base — 无 skill 积累
静态 `Action` 类 + 手写 prompt 模板

### MetaGPT AFlow (ICLR 2025) — MCTS 驱动的 workflow 搜索
- 把 workflow 优化建模为搜索问题
- MCTS 迭代生成/评估/精炼 LLM node graph
- 成功 workflow 存入 tree-structured experience log
- 验证：held-out validation set
- 结果：比手写 baseline +5.7%，小模型超 GPT-4o 且推理成本仅 4.55%
- **但是离线优化工具，非运行时 feature**

---

## 4. 学术论文综述（12 篇）

### 按时间线的技术演进

| 年份 | 论文 | Skill 格式 | 触发条件 | 验证 | 跨任务迁移 |
|------|------|-----------|----------|------|-----------|
| 2023 | **CREATOR** | Python 函数（临时） | 问题需精确计算 | 执行 + LLM 自检 | 无 |
| 2023 | **LATM** | Python 函数（缓存） | 新问题类型出现 | Tool maker 生成测试 | 同类型 |
| 2023 | **CRAFT** | Python 函数（库） | 检索置信度低 | 沙盒执行 held-out | 同领域 |
| 2023 | **Voyager** | JS 函数（持续增长） | 任务成功 | 环境反馈 + 自验证 | Minecraft 内 |
| 2023 | **ToolLLM** | REST API（预定义） | 固定语料 | DFSDT + LLM judge | 有限 |
| 2024 | **PAE** | 隐式（RL 策略权重） | 自动任务提议 | VLM evaluator reward | Web 导航 |
| 2025 | **EXIF** | NL 指令数据集 | 探索 + 失败反馈 | Alice 评估 Bob | 同环境 |
| 2026 | **AutoRefine** | 指南/代码 + subagent 配置 | 轨迹批量提取 | 效用评分 + 裁剪 | 多领域 |
| 2026 | **EvoSkill** | **SKILL.md 目录** | 失败分析 per cluster | Pareto 前沿筛选 | 2 benchmark |
| 2026 | **Trace2Skill** | **声明式文本 skill 文件** | 批量轨迹收集 | OOD eval + 跨规模 | **强** |
| 2026 | **SkillX** | **三层层级文本** | Rollout + 迭代精炼 | Benchmark + 扩展验证 | 跨 agent |

### 关键论文详解

#### LATM (Google DeepMind, ICLR 2024)
[arxiv:2305.17126](https://arxiv.org/abs/2305.17126)

**两阶段双模型分工**：
- Phase 1 (Tool Making): GPT-4 看几个示例 → 写通用 Python tool
- Phase 2 (Tool Using): GPT-3.5 用预制 tool 回答新实例
- 验证：tool maker 生成 functional tests
- 失败时 fallback 到直接 LLM 推理
- **与你的 Grok + Groq 双模型架构天然对齐**

#### CRAFT (UIUC, ICLR 2024)
[arxiv:2309.17428](https://arxiv.org/abs/2309.17428)

- 从领域任务语料批量生成 Python 函数 → 存入 vector-indexed 检索库
- 推理时检索最相关 tools 注入 context
- 新 tool 添加条件：检索置信度低 + 已有 tool 解不了
- 沙盒执行 held-out test cases 验证
- **"检索置信度低才创建" 是很实用的启发式**

#### EvoSkill (Sentient AGI, 2026)
[arxiv:2603.02766](https://arxiv.org/abs/2603.02766) · [GitHub](https://github.com/sentient-agi/EvoSkill)

**迭代失败驱动 skill 进化**：
1. LLM 聚类任务
2. 每 cluster 运行 agent，分析执行失败
3. 提出新 skill 或编辑已有 skill
4. **Pareto 前沿**平衡 performance vs generality — 太 task-specific 的 skill 淘汰

格式：文件系统目录，每个 skill 一个 folder 包含 `SKILL.md` + helper scripts，遵循 [agentskills.io specification](https://agentskills.io/specification)。

#### Trace2Skill (ETH/Alibaba, 2026)
[arxiv:2603.25158](https://arxiv.org/abs/2603.25158)

**并行批量蒸馏（非在线逐条）**：
1. Frozen agent 并行跑 diverse task pool → 生成 T+ 和 T- 轨迹
2. 多个 analyst sub-agent 独立从个别轨迹提出 skill patches
3. **Conflict-free consolidation** — 层级归纳合并所有 patches 为单一 evolved skill
4. 支持 deepening（精炼已有 skill）和 creation（从零创建）

关键发现：**35B 模型 evolve 的 skill 让 122B 模型提升 57.65 绝对点** — 声明式 skill 携带真正可泛化的知识。

#### SkillX (浙大, 2026)
[arxiv:2604.04804](https://arxiv.org/abs/2604.04804) · [GitHub](https://github.com/zjunlp/SkillX)

**三层层级 skill 知识库**：
- **Planning Skills** — 高层策略
- **Functional Skills** — 可复用 tool-based 子程序
- **Atomic Skills** — 原子操作

迭代精炼 + 扩展验证 + 跨 agent 迁移。最新（2026-04），代码已开源。

---

## 5. 商业产品

### Tier 1 — 真正的 Auto-Discovery

| 产品 | 自动检测 | 编译方式 | 触发 | 格式 | 验证 | Canary |
|------|---------|---------|------|------|------|--------|
| **selftune** (OSS) | 是（语言 mismatch） | 半自动（`selftune evolve`） | 持续监控 | SKILL.md | 3-gate + >5% lift | **是（auto-rollback）** |
| **Letta Skill Learning** | 是（轨迹反思） | 半自动（`/skill` 命令） | 用户触发 | .md files | Benchmark eval | 否 |
| **EarthPilot Meta-Skills** | 是（频率 2+） | 全自动 | 2+ 重复 | SKILL.md | Quality check | 否 |

#### selftune — 最生产就绪
[selftune.dev](https://selftune.dev/) · [GitHub](https://github.com/selftune-dev/selftune) · MIT · 533 weekly npm downloads

- Hook 捕获每次用户查询 + 哪些 skill 被触发
- 检测 "skill 应该触发但没触发" 的 language mismatch
- `selftune evolve` 改写 skill description + body
- **3-gate 验证管线 + >5% improvement 门槛**
- `selftune watch` 部署后监控 trigger rate，**自动 rollback**

#### Letta Skill Learning
[letta.com/blog/skill-learning](https://www.letta.com/blog/skill-learning)

- Agent 分析过去轨迹，识别可抽象的重复 pattern
- `/skill` 命令触发两阶段：Reflection → Creation
- Terminal Bench 2.0 上 **+36.8% relative improvement**，成本降 15.7%

### Tier 2 — 用户描述，AI 生成（无 Pattern Mining）

| 产品 | 自动检测 | 备注 |
|------|---------|------|
| Zapier Copilot | **否** | 用户 NL → Zap DAG |
| Make.com AI Agents | **否** | 用户 NL → visual scenario |
| n8n AI Workflow Builder | **否** | 用户 NL → JSON workflow |
| Retool Generate with AI | **否** | 用户 NL → workflow blocks |
| Lindy.ai | **否** | 模板 + 手动配置 |
| Dust.tt | **部分** | Tracker 监控文档 staleness，非行为频率 |
| Relevance AI | **否** | 手动 tool builder |

---

## Q&A 综合回答

### Q1. 怎么判断 "这个 pattern 值得编译成 skill"？

**没有统一标准，但已知四种策略：**

| 策略 | 代表系统 | 机制 |
|------|---------|------|
| **成功门控** | Voyager, CREATOR | 执行成功就入库，简单粗暴 |
| **频率阈值** | EarthPilot | 2+ 次相似请求触发 |
| **检索失败** | CRAFT, BabyAGI | 已有 skill 库无法匹配当前需求 → 创建新 skill |
| **失败分析** | EvoSkill, Trace2Skill, EXIF | 从失败轨迹中提取缺失能力 → 创建/修补 skill |

**对你的 Jarvis 最实用的组合**：频率阈值（trace 里 ≥3 次相似 intent）+ 成功率门控（成功率 >80% 的 pattern 才晋升）+ 用户确认（shadow 期满后 prompt 用户 approve/reject）。

### Q2. 编译后的 skill 是什么格式？

**2023 → 2026 的明确趋势：从代码到声明式文本**

| 时期 | 格式 | 代表 |
|------|------|------|
| 2023 | Python/JS 可执行代码 | Voyager, CREATOR, LATM, CRAFT |
| 2024-2025 | 隐式（模型权重/RL） | AgentTuning, PAE |
| 2026 | **声明式 SKILL.md / 文本 skill 文件** | EvoSkill, Trace2Skill, SkillX, selftune, Letta |

2026 年的共识：**声明式文本 > 可执行代码**，因为：
- 跨模型/跨 agent 可迁移
- 不依赖特定运行时
- 人类可读可审计
- 更鲁棒（环境变化不会让代码 break）

**你的 YAML skill 路线完全正确**。

### Q3. 有没有 shadow / differential testing 验证机制？

| 系统 | 机制 |
|------|------|
| **selftune** | **最完整**：3-gate 验证 + >5% lift 门槛 + 部署后 `watch` 监控 trigger rate + 自动 rollback |
| **LATM** | Tool maker 生成 functional tests |
| **CRAFT** | 沙盒执行 held-out test cases |
| **EvoSkill** | Pareto 前沿 + benchmark 评分 |
| **Trace2Skill** | OOD held-out eval + 跨规模 transfer 测试 |
| **Voyager** | 无部署后验证 |
| 所有商业 SaaS | 无 |

**真正的 shadow/canary 只有 selftune 做了**。大多数学术系统只有 "入库前验证"，无 "部署后监控"。

### Q4. 有没有失败/退化降级路径？

| 系统 | 降级路径 |
|------|---------|
| **LATM** | Tool 执行失败 → fallback 到直接 LLM 推理 |
| **Voyager** | Critic 拒绝 → 最多 4 次 retry → 放弃，不入库 |
| **CRAFT** | 失败 tool 丢弃 → fallback 到无 tool 的 LLM 生成 |
| **selftune** | 部署后 trigger rate 下降 → 自动 rollback 到上一版本 |
| **AutoRefine** | 效用评分低 → 裁剪 |
| **EvoSkill** | Pareto 前沿淘汰 task-specific skill |

**对 Jarvis 的建议**：LATM 的 "tool 失败 → fallback 到 raw LLM" 最适合你的场景。

### Q5. 最大的已知失败模式是什么？

1. **Critic/Evaluator 幻觉** (Voyager, PAE) — 错误标记为成功，buggy skill 入库污染
2. **Skill 库膨胀无去重** (Voyager, CRAFT, BabyAGI) — 语义重复的 skill 越积越多，检索噪声增大
3. **在线逐条更新 vs 批量蒸馏** (AutoRefine 论文数据) — sequential-only 比 batch 差 8.9x，轨迹局部过拟合是最大陷阱
4. **环境/API 漂移** (Voyager, ToolLLM) — 入库后的 skill 永不重验，环境变化导致静默失败
5. **跨任务泛化失败** (CREATOR, 早期 LATM) — 为特定问题生成的 tool 对变体无效
6. **两阶段模型依赖** (LATM) — tool maker 质量上限决定整个系统天花板

---

## 对 Jarvis SkillFactory 的设计建议

基于以上调研，你的 "夜批从 trace 发现 hotspot → 编译 YAML skill" 方案可以借鉴：

### 推荐架构

```
Night Batch Pipeline:
1. Trace Clustering (embedding similarity on intent)
   → 借鉴 EvoSkill 的 LLM-based clustering
2. Hotspot Detection (frequency ≥3 + success_rate >80%)
   → 借鉴 EarthPilot 的频率阈值 + Voyager 的成功门控
3. Skill Compilation (Grok → YAML skill draft)
   → 借鉴 Trace2Skill 的声明式文本格式
   → 借鉴 LATM 的 "强模型造 tool，弱模型用 tool" 分工
4. Shadow Deployment (7 天 shadow 期，记录 would-have-triggered)
   → 借鉴 selftune 的 watch + auto-rollback
5. Promotion Gate (shadow 命中率 >70% + 无 regression + 用户 approve)
   → 借鉴 selftune 的 3-gate + >5% lift
6. Fallback: skill 执行失败 → 退回 raw LLM pipeline
   → 借鉴 LATM 的 graceful degradation
```

### 关键设计决策

| 决策 | 推荐 | 理由 |
|------|------|------|
| Skill 格式 | YAML（声明式） | 2026 学术共识：声明式 > 代码，可读可审计可迁移 |
| 去重策略 | 入库前 embedding 相似度检查 | 避免 Voyager 式库膨胀 |
| 验证 | Shadow 期 + 自动 rollback | selftune 是唯一做了完整 canary 的系统 |
| 批量 vs 在线 | 夜批（batch） | AutoRefine 数据证明 batch 比 sequential 好 8.9x |
| 失败处理 | Pareto 前沿 + TTL 过期 | EvoSkill 避免过拟合 + 定期清理过时 skill |

---

## Sources

### 论文
1. [Voyager](https://arxiv.org/abs/2305.16291) — Wang et al. 2023, NVIDIA/Caltech
2. [CREATOR](https://arxiv.org/abs/2305.14318) — Qian et al. 2023, EMNLP 2023 Findings
3. [LATM](https://arxiv.org/abs/2305.17126) — Cai et al. 2023, ICLR 2024
4. [CRAFT](https://arxiv.org/abs/2309.17428) — Yuan et al. 2023, ICLR 2024
5. [HuggingGPT/JARVIS](https://arxiv.org/abs/2303.17580) — Shen et al. 2023, NeurIPS 2023
6. [ToolLLM](https://arxiv.org/abs/2307.16789) — Qin et al. 2023, ICLR 2024 Spotlight
7. [EasyTool](https://arxiv.org/abs/2401.06201) — JARVIS follow-up
8. [AgentTuning](https://arxiv.org/abs/2310.12823) — Zeng et al. 2023, ACL 2024
9. [AFlow](https://arxiv.org/abs/2410.10762) — MetaGPT team, ICLR 2025
10. [PAE](https://arxiv.org/abs/2412.13194) — Zhou et al. 2024, ICML 2025
11. [EXIF](https://arxiv.org/abs/2506.04287) — Yang et al. 2025, KAIST
12. [AutoRefine](https://arxiv.org/abs/2601.22758) — Qiu et al. 2026, Alibaba
13. [EvoSkill](https://arxiv.org/abs/2603.02766) — Alzubi et al. 2026, Sentient AGI
14. [Trace2Skill](https://arxiv.org/abs/2603.25158) — Ni et al. 2026, ETH/Alibaba
15. [SkillX](https://arxiv.org/abs/2604.04804) — Wang et al. 2026, 浙大
16. [AutoSkill](https://arxiv.org/abs/2603.01145) — 2026

### 项目/产品
17. [Voyager GitHub](https://github.com/MineDojo/Voyager)
18. [JARVIS GitHub](https://github.com/microsoft/JARVIS)
19. [selftune](https://selftune.dev/) — MIT, Claude Code skill evolution
20. [Letta Skill Learning](https://www.letta.com/blog/skill-learning)
21. [EarthPilot Meta-Skills](https://earthpilot.ai/metaskills/)
22. [BabyAGI functionz](https://babyagi.org/)
23. [EvoSkill GitHub](https://github.com/sentient-agi/EvoSkill)
24. [SkillX GitHub](https://github.com/zjunlp/SkillX)
25. [agentskills.io specification](https://agentskills.io/specification)

### 商业平台（均无自动 pattern mining）
26. Zapier Copilot · 27. Make.com AI Agents · 28. n8n AI Workflow Builder
29. Retool Generate with AI · 30. Lindy.ai · 31. Dust.tt · 32. Relevance AI

## Methodology

5 parallel research agents covering: Voyager deep-dive, JARVIS/HuggingGPT, AutoGPT/BabyAGI/MetaGPT, 12+ academic papers, 11 commercial products. Each agent performed 8-17 web searches + source crawls via Exa MCP. Total: 40+ unique sources analyzed.
