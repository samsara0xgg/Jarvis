# SkillFactory 重设计调研 Batch 2 — 聚焦可靠性 + Refine + 质量

*Date: 2026-04-14 · Status: 3/9 agents 完成 · 其余 6 个跑着（D 多-CC 编排 / E CC CLI 多轮 / F 多-agent debate / G 替代栈 / H 声明式架构 / I 数据飞轮）*

**背景**：第一批调研（`skillfactory-research-2026-04-13.md`）被用户批评过于偏向"换 CC Skill 格式"。重新以用户真实目标重做：
- 一次成功率 = 北极星（**不是**格式/延迟）
- 后台 refine 到收敛是明确的架构倾向
- 不预设用任何具体格式

---

## TL;DR — 三条颠覆性发现

### 1. 一次成功率的最高杠杆**不是**模板也**不是**多-agent，而是"测试 + 执行验证 + 参考注入"

Anthropic 官方 best-practices 把 **"give Claude a way to verify its work"** 列为 *"single highest-leverage thing you can do"*。AlphaCodium 论文证明：spec → 测试 → 代码的 3-call 流程，pass@5 从 19% → 44%（GPT-4，CodeContests）。**核心信号只有一个：执行 = 真理**。LLM-as-judge 对代码判断不可靠；跑代码 + 跑测试才可靠。

**ChatDev/MetaGPT 的 4+ agent 管线 = 论文烟雾弹**。MetaGPT 比 GPT-4 直接调用在 HumanEval 上只多 ~4pp，但 token 成本 5-10 倍。Anthropic 自己推荐的模式是最简单的 2-agent：**Writer + Reviewer 各一个 session**。Anthropic 拿 49% SWE-bench 靠 *"minimalist scaffolding"* 不靠复杂编排。

### 2. "后台无限 refine 到收敛" = 神话

研究界没有一个生产系统做"timer-based 后台重写 skill 库"。全部真实 refine 都是：
- **同步 2-4 轮最多**（Voyager 硬限 4，AlphaCodium 每个 sub-loop 4，Reflexion 3）
- **仅由外部信号触发**（测试失败、异常、用户负反馈、遥测回归），不是定时任务
- **有硬预算**（"5 LLM 调用/skill/week max" 是合理上限）

**Olausson 2024 (ICLR, "Is Self-Repair a Silver Bullet?")** 把铁棺材钉死：成本调整后，self-repair 相对于 i.i.d. 重采样只有 **0.97x–1.09x** 收益。Self-Refine 在代码正确性上**经常倒退**。**Huang 2024 (ICLR)**：没有外部信号的纯自我批评，GPT-4 改正确答案变错的概率比修错答案的概率更高。

**Voyager 自己也不做后台 refine**。它的"skill 替换"只有在 curriculum 恰好重新提议同一个任务时才触发 —— 被动替换，不主动刷新库存。Lethain（Stripe CTO）描述真实生产 agent：人工 triage + Notion 手工改 prompt + MCP 看日志，不是 LLM 后台循环。

### 3. 风格 / 格式收敛靠**检索 few-shot**，不靠模板

Schall & de Melo (RANLP 2025) 在 11 个模型上证明：约束解码 / 严格 schema **降低模型质量 5-20%**，因为模型被迫离开高置信度自然模式进入低置信度结构化模式。

Anthropic 自己的 `anthropics/skills` 仓库（112K stars）观察：17 个 skill **body 风格高度发散**（标题深度、代码块密度、markdown 结构都不一样），唯一共享的是 YAML frontmatter 这个极简接口契约。**"Convergence on interface, not content"** —— 收敛在接口上，不强求 body 风格统一。

Voyager 的 `skill_library/trial1/skill/code/` 里 30+ 个生成技能**极其风格一致** —— 但他们**没有用模板**，机制是：每次生成时检索 top-5 相似 skill 作为 in-context few-shot exemplars。**风格从示例涌现**，不靠规则。

---

## Part 1 — 一次成功率（Agent A 精华）

### 前三条（如果只做 3 件事）

**① 测试驱动 + 执行验证 loop**
- Cheap LLM（Groq Llama-70B）按 skill spec 生成 3-5 pytest
- 跑 v1 skill 过所有测试
- 过不了 → 反馈 {input, expected, actual, trace} 再生成
- 跑过 → 收工

**② Reference code injection（RAG for gen）**
- 用 FastEmbed（你栈内已有）按 description 相似度拉 top 2-3 既有 skill
- 作为 full-file few-shot 放 prompt 里
- 成本：+3-5k tokens / 次生成
- 预期收益：大于任何单一 prompt 技巧（因为教模型你的 `Skill` ABC / docstring / config 惯例）

**③ Spec-first 结构化 prompt（3-call 流）**
- Call 1: YAML spec — `{intent, inputs, outputs, edge_cases, error_modes, dependencies}`
- Call 2: 从 spec 出测试
- Call 3: 过 spec + tests 的代码
- 捕获 AlphaCodium ~70% 收益，只花 3 call 而不是 20

### Best-of-N + 执行过滤（次优先）

- N=5-10 sweet spot（再多边际收益递减）
- 选法：**执行过滤 > 共识投票 > LLM-judge**（对代码 LLM-judge 垃圾）
- 你已经有执行信号（pytest），用 best-of-3 temperature 0.7 加上执行过滤 = 几乎免费的 +5-10pp

### 其他有据可依的改进

- **YAML 输出胜过 JSON** 对代码（转义少、嵌套代码友好）
- **IMPORTANT / YOU MUST emphasis** Anthropic 官方确认有效，不是民间传说
- **Extended thinking** ~8k budget 只给 spec/planning 步，别给代码输出步，也别飙到 32k
- **Thinking 对代码收益小**（tool use 收益大）

### 跳过

- ❌ MetaGPT / ChatDev 4+ agent 管线
- ❌ LLM-as-judge 代替执行验证
- ❌ 严格 JSON schema 套在代码 body 外面
- ❌ 给 Claude Opus thinking 32k+ budget

---

## Part 2 — 迭代 Refine（Agent B 精华）

### Voyager 的真实 loop 机制

```
for round in range(4):  # 硬 cap
    code = llm.generate(task, context, critique_so_far)
    events = env.execute(code)                    # Minecraft 环境
    success, critique = critic_llm(events, task)  # 读状态判定
    if success: break
if success: skill_lib.add(code)  # 只有成功才入库
```

关键事实：
- Critic 读的是**环境状态**（inventory / biome），不是代码质量
- **4 轮硬限**，不行就放弃重排队
- 入库**只在成功时**
- 同名 skill 会被覆盖（vectordb 替换），但**这不是主动 refine**，只是 curriculum 偶然又提了同任务
- 消融：去掉 critic → 探索覆盖掉 73%（单点最重要组件）

### 4 种 refine 方法对代码的适配性

| 方法 | 反馈来源 | 对代码效果 | SkillFactory 实用？ |
|---|---|---|---|
| **Reflexion** | 自生成测试 + 执行 | HumanEval 91% pass@1（vs GPT-4 80%）| ✅ 如果你有测试 |
| **Self-Refine** | 同模型自批评 | **常常倒退**（Huang 2024） | ❌ 别用 |
| **CRITIC** | 外部工具（代码解释器） | 对 Python 最合适 | ✅ **推荐这个** |
| **LATS** | MCTS 树搜 | HumanEval 92.7% | ❌ 对 narrow skill 过度设计 |

**铁律：纯自我批评无外部信号 = break-even 或倒退**（Olausson ICLR 2024）。refine 的 "feedback" 必须来自执行 / 测试 / 真实错误，不能是"同一个模型换个 prompt 看一遍"。

### 推荐的 Refine 架构（opinionated）

**同步阶段**（用户说"学会 X"之后，后台 0-30s 完成 v1）：

1. 生成 v1（Grok-4.1-fast）
2. AST 静态检查 + 签名匹配
3. Groq Llama-70B 生成 6-8 个 pytest（YAML 结构化）
4. Sandbox 跑 v1
5. **全过 → 直接入库收工**，别再 refine
6. 有失败 → CRITIC loop：
   - 喂 `{code, failing_input, actual, expected, trace}`
   - 再生成
   - 跑**所有**测试（anchor 语义 = 之前过的不能退）
   - 最多 3 轮
7. 3 轮还失败 → 标 `experimental` 入库，等真实失败信号

**异步阶段**（可选，opt-in per skill，严格预算）：

**只由真实失败触发**（异常 / 用户负反馈 / 遥测 success rate 跌）：

1. 收集失败上下文
2. 加一条复现失败的新测试
3. 跑同步 refine loop（3 轮 max）
4. v2 必须在 shadow 模式下 24h **优于** v1 才晋升（A/B）
5. 不优于 → 丢弃 v2 保留 v1（**绝不降级**）
6. 预算：5 LLM 调用 / skill / week 硬顶

**绝不做**：定时"每晚 refine 所有 skill"（烧预算、静默回归），refine 已经全过的代码（负收益），LATS 树搜。

### Regression 防护

1. **Test anchors**：每个已过测试锁定，refine 后必须全过
2. **依赖索引**：refine A 之前找谁 import A，跑他们的测试
3. **Interface freezing**：签名禁止改，只允许内部重构
4. **版本化 + Canary**：v1 产线 / v2 shadow，通过才晋升
5. **Hypothesis 属性测试**：每个 skill 20-50 随机输入，v1/v2 diff >5% 打回

---

## Part 3 — 质量收敛（Agent C 精华）

### 核心反直觉：Anthropic 的做法是"接口严格 / body 放飞"

inspect 了 `anthropics/skills` 实际 17 个 skill：
- **共享**：YAML frontmatter（`name`, `description`），progressive disclosure 结构
- **发散**：markdown body 风格、代码块密度、章节深度全都不同

Anthropic 的 `skill-creator` meta-skill 描述流程是"写草稿 → 测 prompt → 跑 eval → 根据 eval 改写 → 再跑 → 描述优化脚本"。**用 eval 收敛**而不是用模板。

### Voyager 的"无模板却高度一致"秘密

机制：**每次生成都把 top-5 相似既有 skill 作为 in-context exemplars 塞进 prompt**。风格从示例传染，不从规则施加。论文 §2.2 明确。

这跟 Anthropic 的 best practice "Reference an existing pattern in the codebase" 是同一件事 —— **RAG 作为风格 anchor**。

### 实用工具栈（按适用度排名）

**用于代码重写**：
1. **LibCST** (Meta) — 无损 CST，保 comment/格式，写 codemod 首选
2. **Bowler** (Meta) — LibCST 高层 API，简单 rename/重命名
3. **Ruff `--fix`** — 700+ 规则，亚秒级，事实标准
4. **pyupgrade / isort** — 窄功能修复器，链式组合
5. **Sourcery** — AST-based refactor 建议（已转做 PR review）

**用于质量评估**：
1. **Radon MI ≥ 65**（可维护性指数，标准阈值）
2. **Radon CC grade ≤ B**（圈复杂度）
3. **Bandit** 零 high-severity
4. **interrogate** docstring 覆盖 ≥ 80%
5. **description ↔ body 向量相似度 ≥ 0.75**（用你的 FastEmbed），低了说明"描述偏移 body"

### LLM 驱动的"standardize this" 真的有用吗？

**CodeQUEST** (JPMorgan arXiv:2502.07399, 2025)：42 个 Python/JS 例子，10 维 Evaluator + Optimizer 循环：
- **52.6% mean 相对改进**，41/42 改善
- **但 80% 的改进在第 1 轮完成**，iter 2 +20%，iter 3 +5%，iter 4+ 可忽略
- 与 Pylint/Radon/Bandit 相关系数 0.53（vs baseline 0.27）

**结论**：LLM standardize **单轮有用，多轮白烧 token**。

### Codemod 何时值得？

满足**三全**：≥20 skills + 系统性 AST 级改动 + 可表达为 AST pattern → 写 LibCST codemod。否则 1-shot LLM 更便宜。

关键不变量：**幂等性**（codemod 跑自己的输出 → AST 相同）。不幂等就不安全。

### 重写 vs 重生成

Meta CQS（5000+ 工程师，60% 周 helpfulness）：**识别-批评-DPO 调 feedback，不重生成**。

Voyager：**小 skill (20-40 LOC) 失败就重生成**，比 refactor 便宜。

SkillFactory 经验法则：
- Skill < 200 LOC + spec 清晰 → **重生成**
- Skill 带学习状态 / 用户定制 → **codemod refactor**

---

## 合成推荐的 SkillFactory 流程骨架

等剩下 6 个 agent 回来会更精细。当前基于 A/B/C 的骨架：

```
用户说"学会查汇率"
   ↓
[1] Intent router 检测 learn_create
   ↓
[2] 生成 SPEC（YAML，~500 tokens）
    - intent, inputs, outputs, edge_cases, error_modes, dependencies
   ↓
[3] RAG：FastEmbed 找 top 2-3 相似既有 skill 作 few-shot
   ↓
[4] 并行：
    - Claude Opus 按 SPEC + 示例 生成 code
    - Groq Llama-70B 按 SPEC 生成 6-8 pytest
   ↓
[5] Sandbox 跑 pytest
   ↓ 全过         ↓ 有失败
[6a] 入库         [6b] CRITIC refine loop (3 轮 max)
    静态 gate        每轮喂 {failing, trace}
    (ruff/bandit/    跑全部测试 (anchor)
     radon MI/
     描述相似度)
   ↓
[7] commit 元数据：{code, signature, tests, pass_rate, v=1}
   ↓
[8] 语音回复"学会了"

[后台]
监听失败信号 (exception / 负反馈 / 遥测回归)
  ↓ 真触发才跑（非定时）
加失败复现测试 → refine v2 → shadow 24h → 通过晋升 / 否则丢弃
预算：5 LLM 调用 / skill / week
```

**关键取舍**：
- 不追"无限 refine"幻想（研究否定）
- 风格统一靠 RAG 示例，不靠模板（研究确认）
- 执行验证是真理（不是 LLM-as-judge）
- v1 快速 ship，失败再补（不追完美主义）

---

## 还没调研的面（6 个 agent 跑着）

会补充但尚未确定的关键问题：

- **D 多-CC 编排**：hamelsmu/claude-review-loop、crit、claude-flow 真实架构
- **E CC CLI 多轮机制**：`--resume` / `--session-id` / JSON 输入输出
- **F 多-agent debate**：Claude-on-Claude 是否有效，偏差抑制
- **G 替代栈**：Aider architect+editor、SWE-agent ACI 模式的可迁移启示
- **H 声明式架构**：不生成 Python 而是生成 spec/DSL/OpenAPI 是否更可靠
- **I 数据飞轮**：usage log → fine-tune 小模型 在单用户规模下是否 ROI 正

---

## Sources（Batch 2，~40 个）

**Agent A**:
- AlphaCodium, arXiv:2401.08500 · CodeT, arXiv:2207.10397 · Top Pass, arXiv:2408.05715 · CodeRAG-Bench NAACL 2025 · Anthropic best-practices · Anthropic "think" tool · MetaGPT arXiv:2308.00352 · RANLP 2025 "Hidden Cost of Structure"

**Agent B**:
- Voyager arXiv:2305.16291（含 critic.txt, action_template.txt, skill.py 源码阅读）
- Reflexion arXiv:2303.11366 · Self-Refine arXiv:2303.17651 · CRITIC arXiv:2305.11738
- LATS arXiv:2310.04406 · Huang 2024 ICLR（"LLMs Cannot Self-Correct Reasoning Yet"）
- Olausson 2024 ICLR（"Is Self-Repair a Silver Bullet?"）
- OS-Copilot/FRIDAY arXiv:2402.07456 · AlphaCodium config.toml 源码
- Lethain "Iterative prompt and skill refinement" (2026)
- Cognition Devin 2025 Performance Review

**Agent C**:
- `anthropics/skills` 仓库源码（17 skills inspected）
- Voyager `skill_library/trial1/` 源码（30+ skills inspected）
- Cummins "Don't Transform the Code" arXiv:2410.08806 (NeurIPS 2024)
- CodeQUEST arXiv:2502.07399 · Meta CQS (NeurIPS 2025 DL4C)
- Molison arXiv:2508.00700 · NVIDIA Code Consistency arXiv:2502.00611
- Aider linting (2024) · LibCST docs · DebuggAI Codemods playbook (Sep 2025)
- OpenRewrite · Sourcery wiki
