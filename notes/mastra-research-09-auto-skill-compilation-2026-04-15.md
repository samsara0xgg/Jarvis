# 调研 3: 从 Trace 自动编译 Skill 的先例
*Generated: 2026-04-15 | Sources: 45+ | 7 parallel research agents*

## Executive Summary

"从 execution trace 自动编译 declarative skill" 是一个 2023 年由 Voyager 开创、2026 年进入实用期的研究方向。核心发现：

- **成功案例存在**：Voyager（JS 函数）→ LATM（Python tool）→ Trace2Skill/EvoSkill（声明式 SKILL.md）
- **Hotspot detection** 没有统一算法：频率统计、embedding 聚类、失败分析三条路线并存
- **Shadow → live 渐进部署** 只有 selftune（MIT 开源）做到了完整 canary + auto-rollback
- **Generative Agents 的 reflection 不是 skill 编译**，但其 "importance 累积 → 触发抽象" 机制可借鉴
- **对 Jarvis 最直接可借鉴**：LATM 的双模型分工 + Trace2Skill 的批量蒸馏 + selftune 的 canary

---

## 1. Voyager (MineDojo, 2023)

[Paper](https://arxiv.org/abs/2305.16291) · [GitHub](https://github.com/MineDojo/Voyager)

### Skill Library 积累机制

循环结构：`CurriculumAgent 提任务 → ActionAgent 写代码 → 环境执行 → CriticAgent 判定 → 成功则入库`

```python
# voyager/voyager.py — 核心循环
if info["success"]:
    self.skill_manager.add_new_skill(info)
```

触发条件极其简单：**Critic 返回 `success: true` 就入库**。没有频率阈值，没有 novelty 过滤，没有 A/B 测试。

### Skill 编译 Prompt（Verbatim）

**`skill.txt`** — 将 JS 代码转换为自然语言描述（用于 vector 检索）：

```
You are a helpful assistant that writes a description of the given function
written in Mineflayer javascript code.

1) Do not mention the function name.
2) Do not mention anything about `bot.chat` or helper functions.
3) There might be some helper functions before the main function, but you
   only need to describe the main function.
4) Try to summarize the function in no more than 6 sentences.
5) Your response should be a single line of text.

For example, if the function is:

async function mineCobblestone(bot) {
  let woodenPickaxe = bot.inventory.findInventoryItem(
    mcData.itemsByName["wooden_pickaxe"].id);
  if (!woodenPickaxe) {
    bot.chat("Crafting a wooden pickaxe.");
    await craftWoodenPickaxe(bot);
    ...
  }
  await mineBlock(bot, "stone", 8);
}

The main function is `mineCobblestone`.

Then you would write:

The function is about mining 8 cobblestones using a wooden pickaxe.
First check if a wooden pickaxe is in the inventory. If not, craft one.
If the wooden pickaxe is available, equip the wooden pickaxe in the hand.
Next, explore the environment until finding a stone block. Once a stone
block is found, mine a total of 8 cobblestone blocks using the wooden pickaxe.
```

**组装流程**（`skill.py` → `generate_skill_description()`）：

1. System message = `skill.txt` 全文
2. Human message = 新完成函数的 JS 源码 + `"\n\nThe main function is \`{name}\`."`
3. LLM 返回一行自然语言描述
4. 包装成 stub：`async function {name}(bot) { // {description} }`
5. Stub 文本 → OpenAI embedding → 存入 Chroma vector DB
6. 检索时（`retrieve_skills(query)`）→ cosine similarity top-5 → 取回**原始 JS 代码**注入 action prompt

**`action_template.txt`** — 生成新代码的 system prompt（节选关键规则）：

```
You are a helpful assistant that writes Mineflayer javascript code to
complete any Minecraft task specified by me.

Here are some useful programs written with Mineflayer APIs.
{programs}    ← 这里注入检索到的 skill 代码

...
3) Your function will be reused for building more complex functions.
   Therefore, you should make it generic and reusable.
...
11) Name your function in a meaningful way (can infer the task from name).
```

**`critic.txt`** — 判定成功/失败：

```
You are required to evaluate if I have met the task requirements.
Exceeding the task requirements is also considered a success while
failing to meet them requires you to provide critique to help me improve.

Response JSON:
{ "reasoning": "...", "success": boolean, "critique": "..." }
```

**`curriculum.txt`** — 提出下一个任务：

```
My ultimate goal is to discover as many diverse things as possible,
accomplish as many diverse tasks as possible and become the best
Minecraft player in the world.

Criteria:
- Novel and interesting — seek rare resources, upgrade gear, discover new things
- Not too hard — match current resources and skills
- Single concise phrase: "Mine [qty] [block]", "Craft [qty] [item]"
```

### Skill Discovery 触发条件

| 条件 | 机制 |
|------|------|
| **什么时候尝试新 task** | CurriculumAgent 基于 novelty 压力 + 当前状态提出 |
| **什么时候写代码** | 每个 task 都写，ActionAgent 总是生成新 JS 函数 |
| **什么时候入库** | CriticAgent `success: true` |
| **什么时候复用** | 下次 task 的 action prompt 里注入 top-5 相似 skill |

**关键洞察**：Voyager 没有 "hotspot detection"。它不观察历史发现重复，而是**每次成功都存**，让 vector 检索自然去重（语义相近的 skill 会被同一 query 检索到，新版覆盖旧版）。

---

## 2. JARVIS / HuggingGPT (Microsoft)

[Paper](https://arxiv.org/abs/2303.17580) · [GitHub](https://github.com/microsoft/JARVIS)

### Task → Model 路由

两步 prompt-based 选择：
1. **Task-type filter** — 只保留匹配任务类型的模型（如 image-to-text 只看 caption 模型）
2. **Download-count top-K** — 按 HuggingFace 下载量排序取 top-K
3. **LLM 单选** — ChatGPT 读 model description 选最佳

没有 embedding 检索、没有 learned ranking，纯 NL 推理。

### "学会新 task" 的机制

**完全没有。** 每次请求从头走 4 阶段 pipeline。论文自认：
> *"Efficiency poses a common challenge… requires multiple interactions with LLMs throughout the whole workflow."*

无 plan cache、无 workflow compilation、无 skill 积累。唯一 "记忆" 是同 session 的 chat history。

**可借鉴**：follow-up 工作 [EasyTool](https://arxiv.org/abs/2401.06201) 压缩 tool description 为统一格式提升 selection 准确率 — 对 Jarvis 的 skill 检索有参考价值。

---

## 3. AutoGPT / BabyAGI / MetaGPT

### AutoGPT — 无 skill 积累
手动 workflow 组合平台。Agent Blocks 是人工包装，非运行时发现。

### BabyAGI — 原始的 on-demand 函数积累
- `functionz` 框架：新任务 → embedding 检索已有函数 → 无匹配则 LLM 生成 → Python 函数存入 SQLite
- 无显式正确性测试，仅 embedding similarity 阈值
- **最简单可参考的实现**

### MetaGPT AFlow (ICLR 2025) — 离线 MCTS workflow 搜索
- 把 workflow 优化建模为搜索问题，MCTS 迭代生成/评估/精炼
- 验证用 held-out validation set
- 结果：+5.7%，小模型超 GPT-4o（推理成本 4.55%）
- **离线优化工具，非运行时 feature**

---

## 4. Generative Agents (Stanford, 2023)

[Paper](https://arxiv.org/abs/2304.03442) · [Code](https://github.com/joonspk-research/generative_agents)

### Reflection 机制（pattern 类似但不是 skill 编译）

**Memory Stream**：每个观察事件存入带以下元数据的时序数据库：
- Timestamp
- Subject-predicate-object triple
- **Poignancy score**（1-10，LLM 单次调用评分）
- Sentence embedding

**检索公式**：
```
score = recency × 0.5 + importance × 2.0 + relevance × 3.0
```
- Recency = `e^(-λ·Δt)` 指数衰减
- Importance = 存储时的 poignancy score
- Relevance = 与 focal point 的 cosine similarity

Top-30 进入 LLM context。

**Reflection 触发**：
> **当近期观察的 importance 累积和超过阈值（~150）时触发**

不是固定时间表，约每模拟日 2-3 次。

**Reflection 过程**：
1. 从近期高 importance 记忆中选 3 个 focal points
2. 每个 focal point 检索关联记忆
3. LLM 生成 3-5 条高层洞察
4. **洞察作为 `[Reflection]` 节点存回 memory stream** — 形成递归抽象树

**Planning 管线**：
```
每天早晨：Reflections + 近期记忆 → LLM 生成日计划 → 分解为小时级 → 分钟级行为
显著事件：触发 re-planning
```

### 有没有 "编译重复 pattern 为可复用模板"？

**没有。** Reflection 产生的是**信念**（如 "我倾向于每天早上去图书馆"），不是**可调用的 skill**。每次 planning 都重新 LLM 推理，没有 plan cache 或 skill library。

**但 "importance 累积 → 触发抽象" 这个模式可以直接映射到 Jarvis**：
- Jarvis trace 里每条 intent 有 importance → 累积到阈值 → 触发 skill 编译
- 这比简单的频率统计更能捕获 "低频但重要" 的 pattern

---

## 5. ToolBench / ToolLLM (清华/OpenBMB)

[Paper](https://arxiv.org/abs/2307.16789) · [GitHub](https://github.com/OpenBMB/ToolBench) · ICLR 2024 Spotlight

### Tool Definition 格式

从 RapidAPI 半自动抓取 16,464 个 API，结构化为：
```json
{
  "tool_description": "Return hello world.",
  "tool_name": "hello_world",
  "api_list": [{
    "name": "get_hello_world",
    "description": "To get 'hello world'.",
    "method": "GET",
    "required_parameters": [],
    "optional_parameters": []
  }]
}
```

自定义 API：写 `.json` + `api.py` 放入 `data/toolenv/tools/{category}/`。

### DFSDT 算法

**解决 CoT/ReACT 的单路径问题**：
- 每个节点 = (state=对话历史, action=一次 API call + 结果)
- LLM 每步输出 `{Thought, API_name, Parameters}`
- 两个终止函数：`Finish with Final Answer(answer)` / `Finish by Giving Up()`
- DFS 搜索（不是 BFS），找到任一有效路径即可
- 深度限制 ~5 级，max actions = 200

**Pass rate**：DFSDT 54.5-75% vs ReACT 22-46.5%（ChatGPT）

### 有没有把成功路径编译成 tool？

**没有。** 126K+ 条 (instruction, solution path) 是训练数据，不是运行时可调用的 compiled skill。

**API-Bank**（Alibaba DAMO, EMNLP 2023）同样没有。73 个 API + 3 级评估，无 skill 编译。

---

## 6. 学术论文：直接做 "trace → reusable skill" 的工作

### 按技术成熟度排序

#### Tier 1 — 已验证、可借鉴

| 论文 | 年份/会议 | Skill 格式 | 触发条件 | pass_rate 数据 |
|------|----------|-----------|----------|---------------|
| **LATM** | 2023 / ICLR 2024 | Python 函数（缓存复用） | 新问题类型出现 | Big-Bench 上 GPT-4 做 tool → GPT-3.5 用，准确率持平直接 GPT-4 |
| **CRAFT** | 2023 / ICLR 2024 | Python 函数（vector 检索库） | 检索置信度低 | 沙盒 held-out 通过才入库 |
| **Voyager** | 2023 | JS 函数（持续增长） | Critic success | 3.3x 更多 unique items vs baselines |
| **Trace2Skill** | 2026 | 声明式文本 skill 文件 | 批量轨迹收集 | **35B evolve 的 skill 让 122B +57.65pp** |
| **EvoSkill** | 2026 | SKILL.md 目录 | 失败分析 per cluster | OfficeQA +7.3%, SealQA +12.1% |

#### Tier 2 — 有意思但偏学术

| 论文 | 年份 | 要点 |
|------|------|------|
| **CREATOR** | 2023 / EMNLP | 临时生成 Python tool，不持久化 |
| **AutoRefine** | 2026 / Alibaba | 双形态 skill（指南 + subagent 配置），batch 比 sequential 好 8.9x |
| **SkillX** | 2026 / 浙大 | 三层层级 skill（Planning/Functional/Atomic），跨 agent 迁移 |
| **EXIF** | 2025 / KAIST | Alice 探索 → Bob 学习 → Alice 评估 Bob 失败 → 迭代 |
| **PAE** | 2024 / ICML 2025 | RL 策略权重（隐式 skill），VLM evaluator |

#### Tier 3 — 相关但不直接

| 论文 | 年份 | 要点 |
|------|------|------|
| AgentTuning | 2023 | SFT baked into weights，不是显式 skill |
| AFlow | 2025 / ICLR | MCTS workflow 搜索，离线优化 |
| ToolLLM | 2023 / ICLR | API 检索 + DFSDT，无 skill 编译 |
| AutoSkill | 2026 | Lifelong skill self-evolution，细节较少 |

---

## Q&A 综合回答

### Q1. 有没有从 trace/demonstration 自动编译 declarative skill 的成功案例？

**有，且 2026 年进入实用期**：

| 系统 | 输入 | 输出 | 验证 | 迁移 |
|------|------|------|------|------|
| **Trace2Skill** (ETH/Alibaba 2026) | 批量执行轨迹 T+/T- | 声明式文本 skill 文件 | OOD held-out eval | 35B → 122B 跨规模 |
| **EvoSkill** (Sentient AGI 2026) | 失败分析 per task cluster | SKILL.md 目录 | Pareto 前沿筛选 | 跨 benchmark |
| **selftune** (OSS 2026) | Claude Code session traces | SKILL.md | 3-gate + >5% lift + canary | 跨 session |
| **Letta Skill Learning** (2025) | Agent 执行轨迹 | .md skill files | Terminal Bench eval | 同 agent |
| **LATM** (DeepMind 2023) | 几个问题示例 | Python 函数 | Functional tests | 同问题类型 |

**答案：是的，成功案例存在，且声明式文本（非代码）是 2026 年的共识格式。**

### Q2. Voyager 的 Skill 编译 Prompt（Verbatim）

见上方 §1 完整贴出。核心是 `skill.txt`（JS → NL 描述）+ `action_template.txt`（注入已有 skill 代码 + 生成新代码的 system prompt）。

关键设计：**描述用于检索（embedding similarity），代码用于执行**。两份表示解耦。

### Q3. 编译出的 Skill 质量验证

| 系统 | 验证机制 | pass_rate 数据 |
|------|---------|---------------|
| **Voyager** | Critic agent 二元判定 | 3.3x unique items vs no-skill baseline |
| **LATM** | Tool maker 生成 functional tests | Big-Bench: GPT-3.5+tool ≈ GPT-4 direct |
| **CRAFT** | 沙盒执行 held-out test cases | 通过才入库 |
| **Trace2Skill** | OOD held-out eval | 35B skill → 122B agent +57.65pp absolute |
| **EvoSkill** | Pareto 前沿 performance scoring | OfficeQA +7.3%, SealQA +12.1% |
| **selftune** | 3-gate 验证 + >5% lift 门槛 | 部署后 trigger rate 监控 |
| **Letta** | Terminal Bench 2.0 | +36.8% relative, 成本 -15.7% |

**最硬的数据**：Trace2Skill 的 +57.65pp 是绝对值提升，且是跨模型规模的。

### Q4. Hotspot Detection 用什么算法？

**三条路线并存，没有统一标准：**

| 策略 | 代表 | 算法 | 优缺点 |
|------|------|------|--------|
| **频率统计** | EarthPilot | 计数 ≥2 次相似请求 | 简单直接，但忽略低频高价值 |
| **Embedding 聚类** | EvoSkill, Trace2Skill | LLM-based task clustering + parallel batch | 更精确，但需要批量轨迹 |
| **失败分析** | EvoSkill, EXIF | 执行失败 → 分析缺失能力 → 创建 skill | 针对性强，但需要先失败 |
| **检索失败** | CRAFT, BabyAGI | 已有 skill 库无法匹配 → 创建新 skill | 自然增长，但依赖检索质量 |
| **Importance 累积** | Generative Agents | poignancy score 累积超阈值 (~150) 触发 | 能捕获低频高价值，需要评分模型 |

**对 Jarvis 推荐**：**混合策略**
```
Level 1: 频率统计（trace 里 ≥3 次相似 intent，embedding cosine >0.85）
Level 2: Importance 加权（用户明确说 "每次都要这样" = importance 10）
Level 3: 失败触发（用户重试/纠正 = 现有 skill 不足的信号）
```

### Q5. 有没有 Shadow → Live 渐进部署机制？

| 系统 | Shadow | Canary | Rollback |
|------|--------|--------|----------|
| **selftune** | 否（直接 evolve） | **是**（`watch` 监控 trigger rate） | **是**（自动 rollback） |
| **Temporal** | N/A | **是**（workflow versioning，新旧共存） | 是 |
| 所有学术论文 | 否 | 否 | 否 |
| 所有商业 SaaS | 否 | 否 | 否 |

**真正的 shadow → live 只有 selftune 做了一半**（有 canary 无 shadow）。完整的 shadow → canary → promote 流程在现有系统中**不存在**。

**这是你的 Jarvis SkillFactory 可以做出差异化的地方。**

### Q6. 对 Jarvis 的适用性评估

#### 直接可借鉴（拿来就能用）

| 来源 | 借鉴什么 | 怎么用 |
|------|---------|--------|
| **LATM 双模型分工** | 强模型造 skill，弱模型用 skill | Grok 编译 YAML skill → Groq/Llama 执行 |
| **Voyager 的描述/代码分离** | 检索用 NL embedding，执行用原始定义 | Skill 的 `description` 做 embedding 检索，`parameters` + `execute` 做执行 |
| **Trace2Skill 批量蒸馏** | 不要逐条在线更新，夜批收集后批量分析 | 你已经计划的夜批 pipeline 完全正确 |
| **EvoSkill Pareto 前沿** | 平衡 specificity vs generality | 太 specific 的 skill（只有 1 个用户/1 个场景）不晋升 |
| **Generative Agents importance** | 重要度累积触发而非纯频率 | intent 的 importance score（用户语气、执行复杂度）纳入触发判定 |
| **selftune 3-gate + canary** | 验证 + 部署后监控 + 自动 rollback | Shadow 7 天 → canary trigger rate 监控 → auto-rollback |
| **CRAFT 检索失败触发** | 现有 skill 匹配不了 = 需要新 skill | 夜批分析中标记 "intent 无 skill 命中" 的高频 cluster |

#### 过度学术（不要照搬）

| 来源 | 为什么不适用 |
|------|-------------|
| **Voyager 的 Critic agent** | 需要环境反馈（游戏状态），语音助手没有结构化 ground truth |
| **PAE 的 RL 训练** | 需要大量 trajectory + GPU 训练，你是 API-call agent |
| **AgentTuning 的 SFT** | Baked into weights，你需要显式可编辑的 skill |
| **AFlow 的 MCTS** | 离线 workflow 搜索需要大量评估预算，夜批时间不够 |
| **SkillX 三层层级** | 过度工程化，Jarvis 的 skill 粒度单一（用户意图 → 执行），不需要 Planning/Functional/Atomic 分层 |
| **EXIF Alice-Bob** | 需要两个独立 agent 交替，Jarvis 是单 agent |

---

## 推荐的 Jarvis SkillFactory 架构

基于以上调研，融合最佳实践：

```
┌─────────────────────────────────────────────┐
│  Night Batch Pipeline (每日 3:00 AM)          │
│                                               │
│  1. Trace Collection                          │
│     └─ 收集过去 24h 的 (intent, skill_match,  │
│        success, user_correction) 四元组        │
│                                               │
│  2. Hotspot Detection (混合策略)               │
│     ├─ 频率聚类: embedding cosine >0.85,      │
│     │   count ≥3                              │
│     ├─ Importance 加权: 用户纠正/重试 = ×3    │
│     └─ 检索失败: intent 无 skill 命中的 cluster│
│                                               │
│  3. Skill Compilation (Grok)                  │
│     ├─ 输入: cluster 的 representative traces │
│     ├─ Prompt: 类似 Voyager skill.txt 但输出  │
│     │   YAML 而非 JS                          │
│     └─ 输出: draft YAML skill                 │
│         name / description / parameters /     │
│         intent_patterns / execute_template     │
│                                               │
│  4. Validation Gate (3-gate, 借鉴 selftune)   │
│     ├─ Gate 1: YAML schema 校验               │
│     ├─ Gate 2: 历史 trace replay (≥80% match) │
│     └─ Gate 3: 与现有 skill 去重              │
│         (embedding similarity <0.9)            │
│                                               │
│  5. Shadow Deployment (7 天)                   │
│     └─ 记录 would-have-triggered 但不执行      │
│        计算 precision / recall / false_positive│
│                                               │
│  6. Promotion Gate                             │
│     ├─ Shadow precision >70%                   │
│     ├─ 无 regression (现有 skill 命中率不降)   │
│     └─ 用户 approve (TTS 播报 + 确认)         │
│                                               │
│  7. Live + Canary                              │
│     ├─ 上线后 48h 监控 trigger rate            │
│     └─ trigger rate 下降 >20% → auto-rollback  │
│                                               │
│  8. Fallback                                   │
│     └─ Skill 执行失败 → 退回 raw LLM pipeline │
│        (LATM 式 graceful degradation)          │
└─────────────────────────────────────────────┘
```

### Skill Compilation Prompt（参考 Voyager skill.txt 风格）

```
你是一个 skill 编译器。给定一组用户与助手的对话 trace，
你需要提取可复用的 skill 定义。

规则：
1) Skill 必须是声明式 YAML，不包含可执行代码
2) description 用一句话概括（用于 embedding 检索）
3) intent_patterns 列出 3-5 种用户可能的表达方式
4) parameters 从 trace 中提取变量化的部分
5) execute_template 描述执行步骤（自然语言）
6) 不要编造 trace 中没有的功能

示例输入 trace:
  用户: "把客厅的灯关了"  → success, skill=hue_control
  用户: "关掉卧室的灯"    → success, skill=hue_control
  用户: "灯全关了吧"      → success, skill=hue_control

示例输出:
  name: turn_off_lights
  description: 关闭指定房间或全部房间的灯
  intent_patterns:
    - "把{room}的灯关了"
    - "关掉{room}灯"
    - "灯全关了"
  parameters:
    room: { type: string, default: "all", examples: ["客厅","卧室"] }
  execute_template: |
    调用 hue_control skill，action=off，target={room}
```

---

## Sources

### 论文（按时间线）
1. [Voyager](https://arxiv.org/abs/2305.16291) — Wang et al. 2023, skill library 开创
2. [CREATOR](https://arxiv.org/abs/2305.14318) — Qian et al. 2023, EMNLP
3. [LATM](https://arxiv.org/abs/2305.17126) — Cai et al. 2023, ICLR 2024
4. [Generative Agents](https://arxiv.org/abs/2304.03442) — Park et al. 2023, UIST
5. [CRAFT](https://arxiv.org/abs/2309.17428) — Yuan et al. 2023, ICLR 2024
6. [ToolLLM/ToolBench](https://arxiv.org/abs/2307.16789) — Qin et al. 2023, ICLR 2024
7. [API-Bank](https://arxiv.org/abs/2304.08244) — Li et al. 2023, EMNLP
8. [HuggingGPT/JARVIS](https://arxiv.org/abs/2303.17580) — Shen et al. 2023
9. [EasyTool](https://arxiv.org/abs/2401.06201) — JARVIS follow-up
10. [AFlow](https://arxiv.org/abs/2410.10762) — MetaGPT, ICLR 2025
11. [PAE](https://arxiv.org/abs/2412.13194) — Zhou et al. 2024, ICML 2025
12. [EXIF](https://arxiv.org/abs/2506.04287) — Yang et al. 2025, KAIST
13. [AutoRefine](https://arxiv.org/abs/2601.22758) — Qiu et al. 2026, Alibaba
14. [EvoSkill](https://arxiv.org/abs/2603.02766) — Alzubi et al. 2026
15. [Trace2Skill](https://arxiv.org/abs/2603.25158) — Ni et al. 2026, ETH/Alibaba
16. [SkillX](https://arxiv.org/abs/2604.04804) — Wang et al. 2026, 浙大

### 开源工具
17. [selftune](https://selftune.dev/) — MIT, Claude Code skill evolution + canary
18. [Letta Skill Learning](https://www.letta.com/blog/skill-learning) — +36.8% Terminal Bench
19. [EarthPilot Meta-Skills](https://earthpilot.ai/metaskills/) — frequency-triggered auto-creation
20. [BabyAGI functionz](https://babyagi.org/) — SQLite Python function accumulation

### GitHub
21. [MineDojo/Voyager](https://github.com/MineDojo/Voyager) — prompts 全文
22. [joonspk-research/generative_agents](https://github.com/joonspk-research/generative_agents) — reflection 代码
23. [OpenBMB/ToolBench](https://github.com/OpenBMB/ToolBench) — DFSDT 实现
24. [sentient-agi/EvoSkill](https://github.com/sentient-agi/EvoSkill)
25. [zjunlp/SkillX](https://github.com/zjunlp/SkillX)

## Methodology

7 parallel research agents (2 rounds). Round 1: Voyager deep-dive, JARVIS, AutoGPT/BabyAGI/MetaGPT, 12+ academic papers, 11 commercial products. Round 2: Voyager prompt verbatim extraction, Generative Agents + ToolBench. Total: 45+ sources via Exa web search + crawling.
