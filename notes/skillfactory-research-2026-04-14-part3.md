# SkillFactory 调研 Part 3 — 替代栈 + 声明式架构 + 数据飞轮

*Date: 2026-04-14 · 接 Part 1 + Part 2*

**这批回答的问题**：除了 "让 CC 写更好的 Python" 以外，有没有根本不同的架构？

---

## TL;DR — 三条颠覆性发现

### 1. ★★★★★ **"80% 的 skill 根本不应该是 Python"**

这是整个调研里**最大的数值发现**。

**Anka 论文（arXiv:2512.23214, Dec 2025）**：专为 LLM 设计的 DSL，在多步管道任务上**比 Python 高 40 个百分点**（Anka 100% vs Python 60%）。Claude 3.5 Haiku 在 Anka 上 99.9% 解析成功率、95.8% 任务准确率 —— **零训练暴露，只靠 in-context prompt 100 行文档**。GPT-4o-mini 确认：多步任务 +26.7 pp。

**Bassamzadeh (Microsoft, 2024)** 700 API 的 DSL：hallucinated API 参数名 **降 ~20 pp**，API 名降 6 pp。

**机制显式**：Python 的灵活性 —— 做每步的方式有多种、隐式状态 —— **就是 error 的来源**。DSL 的 STEP 块、命名中间变量、规范形式消除选择，这就是降失败率的原因。

**翻译到 Jarvis**：你的 skill 里有多少是"调 API → 塞参数 → 转格式 → 返回字符串"？这些根本不需要 Python，写 YAML 声明就够。剩下 20% 真正需要代码的，再落 Python 模板。

### 2. ★★★★ **单用户规模别 fine-tune**

**家庭场景数据飞轮不值得建**。Viqus 2026 实用指南直说：fine-tune 需要 **1000-5000 高质量样本**作起点。你一年可能生成 100 skill，corrections 20 条。**<500 样本下 fine-tune 几乎总是败给 retrieval+prompt**。

多租户才玩得起：Airbnb AITL 每周重训 agent (+11.7% recall)、Cursor Tab-RL 400M predictions/day checkpoints/天、GitHub Copilot 企业级自训模型。**没有单用户数据飞轮成功的公开案例**。

**正确做法**：cascade routing（便宜本地 7B 先试 → Opus 兜底）+ retrieval few-shot（相似 skill 作 in-context 示例）。RouteLLM 数据：**保持 95% frontier 质量，85% 请求走便宜模型，省 45-85% 成本**。

### 3. ★★★ **Voyager 式 skill 自积累在编程领域还是原创空白**

没有任何主流 coding agent（Aider、Cursor、Devin、OpenHands、Codex、Cline）真正做 Voyager-style 自动 skill 积累。最接近的只是**格式**：Claude Code Skills + OpenHands Skills 提供了目录+frontmatter 标准。**但"生成 → 验证 → 入库 → 索引 → 复用"的闭环**本身还没人在代码里做出来。

这意味着：
- 你没有现成 playbook 可抄（不像 Part 2 有 compound-engineering-plugin）
- 但也意味着这**真的是新方向**，值得做

OpenHands V1 (Nov 2025) 的 skill system（3 种触发器：always-on / keyword / task）+ event-sourced state + LLMSummarizingCondenser（2x 成本降低）是**最接近的架构参考**。

---

## Part 3.1 — 替代 AI Coding Stacks（Agent G）

### 系统特性对照

| 系统 | Architect+Editor | 持久 skill 记忆 | Auto 测试 | 上下文管理 | 生产证据 |
|---|---|---|---|---|---|
| **Aider** | ✅ Native，SOTA claim | ❌（每次重建 repo map）| `--auto-test` + 自修 | PageRank repo map over tree-sitter | 43K★，自测 |
| **SWE-agent** | ❌ | ❌ | linter-gated edits | 100 行 file viewer + 精简 grep | NeurIPS 2024 |
| **mini-swe-agent** | ❌ | ❌ | 裸 shell | 无（100 LOC）| **>74% SWE-bench Verified** |
| **OpenHands V1** | ❌（可选 RouterLLM）| ✅ **AgentContext + Skills（3 触发器）** | via tools | **LLMSummarizingCondenser（2x 成本降）** | 71K★ |
| **Cursor Composer 2** | ✅（"Apply" 小模型）| 部分（`.cursorrules`, AGENTS.md）| 非架构级 | 自总结 + MoE | 闭源 |
| **Devin v3** | ❌（单 Sonnet + 长计划）| 计划+情景 notes（Cognition 自己说"不够全"）| ✅ 模型自发测试 | 手工压缩 + "1M beta 封顶 200k" | 云端闭源 |
| **Codex CLI** | ❌（GPT-5-codex 单模型）| **AGENTS.md 级联 + Skills 注入** | 非架构级 | stateless + `/responses/compact` | 开源 Rust |
| **Cline** | ❌ | ❌ | ❌ | 简单头尾截断 | 58K★，HITL |

### Aider 的 architect+editor 真实价值

**有效，但增益小（3-10 pp）**。Aider 自家 benchmark（不是 SWE-bench）：

| Architect → Editor | Pass rate | vs solo baseline |
|---|---|---|
| o1-preview → o1-mini/DeepSeek (whole) | **85.0%** | +5.3 pp 对 o1-preview 单独 |
| o1-preview → Sonnet (diff) | 82.7% | +3.0 |
| Sonnet → Sonnet (diff) | 80.5% | +3.1 对 Sonnet 单独 |
| o1-mini → DeepSeek | 71.4% | +10.3 |
| gpt-4o-mini → gpt-4o-mini | 60.2% | +4.6 |

**增益的真实来源**：(a) 推理模型不擅长发出 valid diff 格式、(b) 注意力被"干什么"和"怎么格式化 patch"分裂。**同模型跑两遍也有效** —— 第二遍只操心格式就更准。

**注意**：这是一个 benchmark 的一次结果，SWE-bench Verified 上没人复现。但它处理的失败模式（推理模型把 diff 语法写错）是真的。

### SWE-agent ACI 的 4 条设计铁律（对 SkillFactory 可迁移）

1. **Linter-gated edits** —— 编辑结果语法无效 → 拒绝，下一 turn 看不到
2. **有界 file viewer（每 turn 100 行）** —— `cat` 淹没 context；分页 viewer 带 scroll/search 有效
3. **精简 search 输出** —— `grep` 只列有匹配的文件名，**"每条匹配的上下文会让模型混乱"**
4. **"Your command succeeded and produced no output"** —— 否则模型以为静默=失败

**对 SkillFactory 的迁移**：当你生成 skill 时，对 skill 自身接口也用这套哲学。**narrow typed inputs 胜过 free-form**，列举允许状态，违反 schema 直接 loud fail。

**mini-swe-agent 的颠覆性结果**：100 LOC agent 只用 `bash` 没自定 tool，**>74% SWE-bench Verified**。**"SWE-agent ACI 的大部分收益可以用好 prompt 在现代 frontier 模型上直接恢复"**。file viewer trick 在弱模型上还有用。**在 Opus 4.6 上，你可能根本不需要复杂 ACI**。

### OpenHands V1 — 最诚实的开源 Devin

**Nov 2025 SDK 论文（arxiv:2511.03690）是这个领域最清晰的架构文档**。真实发现：

- **Event-sourced ConversationState**：append-only 日志 + 确定性 replay。**整个系统里唯一的 mutable object**
- **LLMSummarizingCondenser (default)**："reduces API costs up to 2× with no degradation in agent performance"
- **Skills system 3 种触发器**：
  - **always-on**（repo skills，如 `AGENTS.md`）
  - **keyword-triggered**（knowledge skills）
  - **task-triggered**（workflow skills，声明 input fields）
  
  **这是所有 coding tool 里最接近一等公民持久 skill library 的**，文档 `docs.openhands.dev/sdk/arch/skill` 值得读
- **RouterLLM**：子类按请求选模型（图片走多模态、文本走便宜）—— 这是**结构级 architect/editor**，比 Aider 的硬编码两调用更通用

### Cursor Composer 2 的公开模式

- **Apply 小模型专管 patch** —— 大模型出伪代码，小快模型翻译成行级精确 edit。和 Aider architect+editor 同理念，Cursor 默默 ship
- **自总结 for 长任务** —— Composer 2 显式训练了自总结能力（不靠 prompt engineering）
- **Multi-agent 并行 via git worktrees** —— 一次 prompt 起 8 个 agent
- **AGENTS.md / `.cursorrules`** —— 现在是跨厂商标准（OpenHands/Codex/Cursor/Gemini/Copilot 都读）

### 持久 skill 积累 —— 几乎没人做

**诚实回答：主流 coding tool 里几乎没有做真正的 Voyager 式积累**。最接近的：

- **Claude Code Skills**（Anthropic Oct 2025）—— SKILL.md + YAML frontmatter，关键词自动触发，progressive disclosure（只 metadata 常驻）
- **OpenHands Skills** —— 同形态 + MCP tool bundling
- **Codex AGENTS.md + Skills** —— 路径级联，32 KiB 上限**强制分布不是集中**

**Aider、SWE-agent、Devin、Cursor 每个任务都从零**。Devin 会中途生成总结 notes 但 Cognition 自己说"不够全面"，后来加了显式 memory management。

**SkillFactory 的定位**：你要做的闭环（生成 → 验证 → 入库 → 索引 → 复用）**在编程代理里是原创空白**。Claude Code Skills 提供了**格式**；curation loop 你得自己发明。

### Context 管理技术

- **Aider PageRank repo map** (`aider/repomap.py`)：tree-sitter 抽 symbol → SQLite 缓存 → personalized PageRank over symbol-reference graph → 1024 tokens 内选 definitions。**这是"哪些文件对此任务重要"的 SOTA**。Nous Research 的 Hermes 明说抄的（issue #535）
- **OpenHands LLMSummarizingCondenser** —— append-only event log + 渲染时 stateless summary 替换被忘窗口。2× 成本降，质量不降
- **Codex stateless + `/responses/compact`** —— 每请求全历史 + server-side 压缩 endpoint，返回不透明 `encrypted_content` 保留 latent reasoning

**Claude Code 已经有智能压缩 + 并行 file read**，**相对这些系统最缺的是"技能库的结构化符号图"**（Aider repo-map 等价物，但索引 skill 定义/触发器而不是代码符号）。

### SkillFactory 可迁移模式（排序）

1. **SWE-agent ACI 纪律用在 skill schema** —— typed inputs、linter-gated writes、精简 error。坏 ACI → 坏 skill 使用，不管什么模型
2. **Aider PageRank repo-map 应用到 skill library** —— skill 构成图（一个 skill 引用另一个、触发器重叠）。rank-select top-k 按当前请求相关度，不要整个 dump
3. **OpenHands event sourcing 应用到 skill 生成 loop** —— 每次 skill 创建 = append event。确定性 replay = 能 diff "agent 以为自己在做的" vs "skill 实际做的"
4. **Aider architect+editor** —— 生成 skill 时分开 reasoning（写什么 skill / 测试）和 mechanics（写 Python / YAML / glue）。Opus 可以都做，但便宜 editor 处理 boilerplate 省钱
5. **Codex 32 KiB AGENTS.md 级联上限** —— 强制分布不是集中。永远全载的 skill library 比能索引的更差

### CC 对比其他系统的独家优势

- **Skills with progressive disclosure**（metadata-only 直到触发）—— 只有 OpenHands 格式匹配，只有 CC 有策展 marketplace
- **真并行 tool execution** —— Cline/Devin/Codex 都是顺序；只 Cursor Composer 2 和 CC 一 turn 多 tool_use
- **17 个 lifecycle hooks** —— "灵活但用户负责"（Codex 团队自己的说法）；Codex 明确不匹配因为 kernel sandboxing 更安全。**对 SkillFactory 这是 feature**：hooks 提供 test-verify-commit loop 的接缝
- **Subagent Task tool** —— OpenHands V1（Nov 2025）刚加等价物；Devin 团队说"Sonnet 4.5 上 works well 但要仔细管状态"

### 证据漏洞（要说透）

- Devin 2024 claim 的 13.86% SWE-bench 没人独立复现；SWE-bench 加了 "Verified" 后公开 agent 都 70%+（Augment Auggie 在同模型 Opus 4.5 上比 Claude Code 多解 17 个 issue —— **scaffolding 差异大于模型差异**）
- Aider 85% architect+editor 是**它自己的 benchmark（133 Python 练习）**，不是 SWE-bench。但 isolate 了 edit-format 失败模式是有信息量的
- **Voyager 式 skill 积累在编程里还是 unproven**。每个系统都说"想做"（包括 Devin 的 notes），**没有一个在 benchmark 上证明 task-over-task 性能提升**

---

## Part 3.2 — 声明式 / 混合架构（Agent H）

### 代码生成光谱

```
纯 Python         模板填空         Skill = code+md        声明式 DSL          纯组合
(Jarvis 现在)     (Jinja 槽位)     (Anthropic Skills)    (Anka, Zapier YAML) (LangGraph/DSPy)
    │                │                   │                    │                   │
表达力最高                                                                     表达力最低
失败面最大                                                                     失败面最小
```

**每往右一步掉表达力，也掉失败模式**。真正的问题不是"选哪个"，而是"愿意付哪种失败代价"。

### 模板填空：证据与收益

**SAFIM benchmark**（17,720 个执行校验的 fill-in-the-middle 任务）：**syntax-constrained FIM 系统性胜过自由生成** 对 localized task。"Alignment with FIM for Enhancing Code Generation"（EMNLP 2025）报告从 prefix/suffix 锁定让模型不能产生无效 import / 错基类 / 缺方法的可测增益。

**对 SkillFactory 的映射**：保留 `class MySkill(Skill): name/description/parameters` 脚手架固定，LLM 只填 `execute()` body + `parameters` dict。对**结构性 bug 类**（错继承、缺方法、坏 import）—— 基于 SAFIM/Anka delta 预计一次成功率跳 **20-30 pp**。

### 声明式 skill + 解释器：Microsoft 的证据

**Bassamzadeh & Methani (Microsoft, arXiv:2407.02742)**：700 API 的 DSL，1000 test flows。Fine-tuned Codex + 优化 RAG：
- **Hallucinated API 参数名降 ~20 pp**
- **Hallucinated API 名降 ~6 pp**
- Parsing error 低因为 DSL 语法小（大约：function-name + params + 条件）

**已存在的生产例子**：

- **Liman**：`load_openapi(url)` → 自动生成 tools via YAML LLMNode/ToolNode 规范
- **`openapi-llm`、`openapi-llm-tools`、`rig-openapi-tools`**：OpenAPI spec → LLM tool definitions，零代码
- **LangChain `StructuredTool.from_function`**：Pydantic schema + callable，无模板
- **MCP servers**：声明 tool schema，server 实现

**对 SkillFactory**：skill 是 YAML（name、description、JSON-Schema params、type 之一 {http, shell, python_snippet, compose}）。解释器 ~300 行永不变。LLM 只填 YAML。

### 组合（LangGraph、DSPy、运行时 planner）

**DSPy 论点**（"programming, not prompting"）：停止生成 prompt 字符串，声明 module（Predict、ChainOfThought、ReAct），让 compiler 优化。**LangGraph 建模 agent 为 primitive node 图 + 类型化 state transition**。

对语音助手：用户说"查明天航班" → planner 输出 `compose([search_email(query=flight), http_get(url=airline.checkin(confirmation))])` 作为 JSON。**零 Python 生成**。

适用：**简单多步查询好**，novel stateful logic 差（如"当股票一周内跌两次 5% 时提醒我"）。LangGraph issue #763 明说"cannot add nodes/edges truly dynamically before runtime" —— 组合灵活但 node 集合固定。

### API-spec-based skills — 语音助手的**最大价值机械解**

**"学会查航班 / 读天气 / 查股票 / 控 Hue" 的大部分本质就是：找 API → auth → 映射 slot**。Liman 演示直接从 FastAPI OpenAPI spec 生成 6 个 tool，**零自定 Python**。现役项目：

- **`openapi-llm`** (vblagoje)：OpenAPI → OpenAI/Anthropic/Cohere tool definitions
- **`agentspec.tools`**：粘贴 spec，得 LangChain/MCP/OpenAI tool schemas
- **Zapier AI Actions**：**7,000 apps / 30,000 actions** 作 LLM-callable actions，自然语言 slot filling（LangChain 2023 集成）

如果目标 skill 是"调这个 API"，**写 Python 是 100% 浪费**。**OpenAPI registry + runtime tool construction 消除整个代码生成失败类** —— 没生成代码，就是 runtime HTTP call + validated params。

### DSL + 无代码链：**最强的数值证据**

**Anka（arXiv:2512.23214, Dec 2025）是整个 Part 3 最震撼的数据**：

- **专为 LLM 生成设计的 DSL，在多步管道任务上比 Python 高 40 pp**（Anka 100% vs Python 60%）
- **Claude 3.5 Haiku**：99.9% 解析成功率、95.8% 任务准确率，**零训练暴露**（只 100 行 in-context prompt docs）
- **GPT-4o-mini 确认**：多步任务 +26.7 pp

**机制显式**：Python 多种有效写法 + 隐式状态 = error 来源。**Anka 的 STEP 块、命名中间变量、规范形式消除选择**，降失败率。

**多窄才算太窄？**
- **Zapier**（7K apps、30K actions）—— 多步 Zaps 表达力对 "连接两服务" 任务够用
- **IFTTT**（trigger→action，一个 filter）—— 对语音助手太窄
- **Sweet spot ≈ Anka 或 Zapier 多步**：~10 个 primitive（http_get、jmespath、filter、map、compose、if、slot-fill、call_llm、say、remember）

### 混合 — 代码在 skeleton 里（Anthropic Skills 模式）

审查 `anthropics/skills` 仓库得到的**真实比例**：

- **所有 skills**：100% 有 SKILL.md（markdown，不是代码）
- **文档类 skills**（pdf/docx/pptx/xlsx）：bundle 小的、预写的、手工审过的 Python script（`fill_form.py`、`create_chart.py`）—— **Claude 调用它们，不是生成它们**
- **创作/企业类 skills**：常 **0% 代码**，只 prose

**Anthropic 的 design essentially：写指令一次、bundle 确定性 script 一次、绝不每次用时生成代码**。这是 "skills" 最佳文档化的生产模式，**70-90% 是 config/prose，10-30% 是预写代码**。skill 是目录，不是被生成的。

### 每种 skill 类型最佳方案（对语音助手）

| Skill 类型 | 最佳方案 | 原因 |
|---|---|---|
| API 查询（weather、flights、stocks、Hue）| **OpenAPI → tool** | 已解决；0% 代码生成 |
| 记忆/召回（"提醒我..."）| **声明式 spec + 解释器** | 模式重复；模板参数类型 |
| 控制流/例行（"早晨例程"）| **DSL compose** | Anka +40 pp 直接适用 |
| 系统操作（音量、关机）| **预建库，无生成** | 确定性，审过一次 |
| 创作/生成（"讲个故事"）| **不需要 skill** —— 直接 prompt | Skill 是错抽象 |
| 罕见的新颖 stateful logic | **skeleton 内代码生成** | 唯一真需 Python |

### SkillFactory 推荐光谱位置

**把用户从"纯 Python 代码生成"挪到 Anthropic Skills + OpenAPI auto-tools 位置附近**：

1. **默认（目标 ~80% 新 skill）：声明式 YAML spec**。`type: http | compose | recall | say`。LLM 填槽位。解释器是审过一次的固定 Python。**整个代码生成失败类消除**
2. **API skills：OpenAPI registry**。用户预注册 spec（或用 Zapier/RapidAPI registry）。"学会查航班" → "找匹配 endpoint，填 auth + slot"。**0% 生成代码**
3. **控制流 skills：Anka 风格 compose DSL**，~10 个 primitive。证据：**+40 pp 多步准确率胜过 Python**
4. **逃生口（~5-10% skills）：模板填 Python**。固定 `class MySkill(Skill)` skeleton，LLM 只填 `execute()` body，pre/post AST 验证。覆盖真正新颖逻辑

**为啥这解决可靠性痛点**：每种失败模式根因不同 —— (a) 错继承/import、(b) hallucinated API 名/参数、(c) 错步骤顺序、(d) 语法错。以上用最便宜的技术针对每个，而不是用全 Python 代码生成当万能锤。**Anka 40 pp delta 就是多步顺序失败**；Bassamzadeh 20 pp 参数 hallucination 降低就是 API call 失败；SAFIM FIM 增益就是结构 bug 类。

**不修复的**：真正新颖的 per-skill 逻辑（语音助手罕见）。这些用模板填，别自由生成。**底线：纯 Python 代码生成被当锤子打螺丝钉问题，研究给出了每个替代方案的具体节省数字。把 80%+ skill 搬离代码生成是正确方向**。

---

## Part 3.3 — 数据飞轮 + Fine-Tuning（Agent I）

### Fine-tune 小模型在小规模 —— 有效但 N=50 不行

证据清晰：领域专用 fine-tune **能**胜过 frontier LLM，但要真数据量：

- **LoRA Land**（Predibase, 2024）：25 个 Mistral-7B LoRAs 比 GPT-4 强 4-15% 在窄任务。每个 adapter 用**数千**样本
- **Fine-tune-SLM 论文**（KDD 2025）：Fine-tuned Mistral-Nemo-12B 在 low-code workflow 生成上胜过 GPT-4o、Gemini-2.0-Flash、o3-mini。训练集**数千**curated workflows，不是 50
- **Viqus 2026 指南**明说实用门槛："fine-tune when you have **1,000–5,000 高质量样本**"；以下"invest in better prompts and evaluation instead"
- **成本**（DeployBase）：LoRA 7B fine-tune = **~$1-8 on spot RTX 4090**；13B = ~$3-12。算力便宜到 trivial。**数据是瓶颈**

**对 SkillFactory 的判决**：50-100 skills 时，fine-tune 几乎肯定败给 Claude-Opus + retrieval。**交叉点约 1K curated 对**（按当前速度 10+ 年）。

### 生产数据飞轮 —— 多租户才玩得起

- **Airbnb AITL**（Oct 2025, arxiv:2510.06674）：live agent feedback（pairwise preferences、adoption、missing-knowledge flag）每周重训。+11.7% recall@75、+8.4% helpfulness、+4.5% adoption。重训周期从月到周
- **Cursor Tab-RL**（Sep 2025）：400M+ predictions/day 在线 RL，checkpoints **一天多次 ship**。结果：suggestions 数降 21%、accept rate 升 28%
- **GitHub Copilot 自定模型**：企业-only，且 GitHub 自己的 "next edit suggestions" 自训模型是内部努力 —— 不是客户 fine-tune

**共性：flywheel 要多租户容量**（Airbnb、Cursor、GitHub 规模）。**没有记录的单用户数据飞轮成功案例**。

### 级联 / 主动学习 escalation —— **最高 ROI 的模式**

**很实用，不需要 fine-tune**：

- **Cascading survey**（arxiv:2603.04445）+ **ETH unified routing**（2410.10347）：便宜模型先试，低置信升级
- **RouteLLM 结果**：**保持 95% frontier 质量，85% 请求走便宜，45-85% 成本降**
- **关键 caveat**：token 级 self-confidence 校准差（tianpan.co/llm-routing）。好信号：abstention training、retrieval-quality 耦合、在自己数据上经验校准的阈值

**对 SkillFactory 的判决**：**最高 ROI 模式 —— 便宜本地 7B 先试，Opus 在不确定/失败时兜底**。你已经有需要的 logging。

### RLHF / DPO 在单用户规模 —— 不实用

- OpenAI 自己的 DPO 指南：**数千偏好对**作起点
- "What Matters in Data for DPO"（arxiv:2508.18312）：多样性 + edge coverage，不只是原始数量
- Cursor Tab-RL 只因为有 400M preference signals/day 才 work
- 你 100 skills/year × 20 corrections/year = **20 对/年** —— 这不是 DPO 数据集，是噪音

### Fine-tune vs Retrieval（few-shot）vs Prompting 决策矩阵

基于 retrieval-vs-finetune paper（arxiv:2512.04106）+ Viqus crossover：

| 数据量 | 任务稳定性 | 最佳方案 |
|---|---|---|
| <100 样本 | 任何 | **Prompt + retrieval few-shot from skill library** |
| 100-1K | 稳定 schema | Retrieval few-shot；只在格式极死板时考虑 small fine-tune |
| 1K-5K | 稳定 >6 周 | **Fine-tune 开始付本** |
| >5K + >500K req/month | 成本驱动 | Fine-tune + 小模型主导 |

**arxiv:2512.04106 发现**："retrieval-augmented prompting achieves near plug-and-play capabilities with zero cost and zero training time"，在大多数 shot 数（up to ~10）上胜过 fine-tuned CodeBERT。

### 日志 pipeline 实用清单

每次 skill 生成 + 执行记录：

**必记**：
- `skill_id`、`user_prompt`、`intent_classification`、`generated_code`、`model_version`、`retrieval_context`（prompt 里用了哪些过去 skill）
- `execution_outcome`：`succeeded | threw | wrong_output | user_corrected | user_rejected`
- `tests_passed`（auto-run smoke tests）、`latency_ms`、`tokens_in/out`
- 用户修正时：generated vs 修正的 diff

**隐式信号**：
- Skill 7 天内再次被调用？
- 同用户错误模式重复？
- 静默成功？

**显式信号**：可选 thumbs-up/down **只在模糊 case**（别每 skill 都问 —— Viqus 称之为 "annoying feedback UX" 失败模式）

### SkillFactory 推荐策略（按规模分阶段）

- **0-20 skills**：纯 prompting + Claude Opus。**无飞轮**。只做结构化日志
- **20-200 skills**：加 **retrieval few-shot + cascade**。每次新请求 embed 它，拉 top-3 成功过的相似 skill 当示例。组合 cascade：local 7B 先试 → Opus 低置信或测试失败时升级。**最大质量跳在这阶段，零训练成本**
- **200-1000 skills**：加 test-passing 作 reward 信号。自动重生成失败 skill 用失败作 in-context hint（"previous attempt failed because X"）。**仍无权重训练**
- **>1000 稳定类别 skills**：考虑 LoRA on Qwen2.5-7B **仅对最常见 skill 家族**（如 "Hue 灯控"、"MQTT publish"）。通用生成继续 Claude

### Anti-patterns

- **过早 fine-tune**：N<500 高质量对几乎总是败给 few-shot retrieval
- **用 Claude 生成的 skill 自训不做人工验证**："Training on model-generated outputs means the model learns to imitate itself"（Viqus）。先按实际执行成功过滤
- **微规模 preference 数据**：20 thumbs/year 是噪音不是 DPO 数据集
- **Catastrophic forgetting**：别 full fine-tune；非用不可时用 OPLoRA 式正交投影（arxiv:2510.13003）并在 held-out general task 上评估
- **Annoying UX**：永远别每 skill 问反馈。收集隐式信号（复用率、执行成功、修正）。显式反馈只在真有歧义时
- **无校准的 confidence-cascade**：LLM self-confidence 校准差；在自己 domain 数据上校准才可信

---

## Sources（Part 3，~35 个）

**Agent G — 替代栈**：
- aider.chat/2024/09/26/architect.html（benchmark 完整结果）
- aider "Chat modes"、"Scripting aider"、`aider/repomap.py`
- Yang et al. SWE-agent NeurIPS 2024（arxiv:2405.15793）、swe-agent.com/background/aci
- SWE-agent/mini-swe-agent README
- Wang et al. OpenHands V1 SDK（arxiv:2511.03690, Nov 2025）
- docs.openhands.dev/sdk/arch/skill
- Bolin "Unrolling the Codex agent loop"（OpenAI, 2026-01-23）
- yage.ai Codex CLI 内部调研（2026-03-14）
- Cognition "Rebuilding Devin for Claude Sonnet 4.5"（2025-09-29）
- Cursor "Composer 2 Technical Report"（2026-03）、Cursor 2.0 changelog
- Squid Club "Reverse Engineering Cline vs Claude Code"（2025-07）
- Ganhotra "From 73% to 11%: discriminative subsets of SWE-Bench"（2025-06）
- Anthropic Claude Code Skills docs + Shilkov "Inside Claude Code Skills"（2025-10）

**Agent H — 声明式 / 混合**：
- Al Mazrouei **Anka** arXiv:2512.23214（Dec 2025）**★**
- Bassamzadeh & Methani (Microsoft) arXiv:2407.02742
- Gong et al. SAFIM ICML 2024 arXiv:2403.04814
- Ren et al. "Alignment with FIM" EMNLP 2025
- Anthropic "Equipping agents for the real world with Agent Skills"（Oct 2025）
- `anthropics/skills` 仓库源码
- Bokum Liman 博客
- vblagoje `openapi-llm`
- DSPy 文档
- LangChain + Zapier NLA 集成
- LangGraph dynamic graph creation 讨论
- Argmin AI Anka 总结 + benchmark 表

**Agent I — 数据飞轮**：
- Airbnb AITL arxiv:2510.06674
- Cursor Tab-RL blog
- GitHub Copilot 自训模型 blog
- Fine-Tune SLM vs Prompt LLM KDD 2025（arxiv:2505.24189）
- Retrieval-aug vs fine-tune 2025 arxiv:2512.04106
- LoRA Land（Predibase）
- "What Matters in Data for DPO" arxiv:2508.18312
- Model routing/cascading survey arxiv:2603.04445
- OPLoRA catastrophic forgetting arxiv:2510.13003
- DeployBase fine-tune cost 指南
- Viqus "Fine-tuning vs Prompting 2026 指南"
- tianpan.co LLM routing/cascades
- NVIDIA data flywheel blueprint
