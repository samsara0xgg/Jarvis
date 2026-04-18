# Mastra OM 深度调研报告

**调研日期**：2026-04-15
**目的**：为 Jarvis 记忆系统重新设计提供一手实现依据

**⚠️ 术语纠正**：用户称 "Observation Model (OM)"，Mastra 官方名称是 **Observational Memory (OM)**。缩写一致，但概念是"可观察式记忆"。

**调研基础**：4 个官方权威源 + GitHub 源码（commit `a179a1dbb3c` on `main`）+ 3 篇第三方分析 + podcast 页（无 transcript）。

---

## Q1. Observation 的实际数据结构

**重要发现：Observation 不是一个结构化对象，而是一段 LLM-enforced 的格式化文本。** 没有 `type Observation`，没有 Zod schema，没有字段枚举。

**单条 observation 的"语法"**（由 Observer 的 prompt 强制约束，不在代码里 parse）：

```
* <emoji> (HH:MM) <text>
  * -> <sub-bullet>
  * ✅ <completion>
```

按 day 分组：

```
Date: Dec 4, 2025
* 🔴 (14:30) User prefers direct answers
* 🔴 (14:31) Working on feature X
* 🟡 (14:32) User might prefer dark mode
```

**分类字段（emoji 作为 priority）**：
- 🔴 High — explicit user facts, preferences, unresolved goals, critical context
- 🟡 Medium — project details, learned info, tool results
- 🟢 Low — minor/uncertain
- ✅ Completed — task finished, question answered, issue resolved

**emoji 是 stored（不是渲染时加的）** — 由 Observer LLM 直接输出到文本流并入库。

**时间戳**：
- Day 级：`Date: Dec 4, 2025`（日期分组头）
- 分钟级：`(HH:MM)` 24 小时制
- 官方 research 页强调 "three-date model"（observation date / referenced date / relative date），但这是**语义层** — 在 prompt 里让 LLM 用人话写进来，不是独立字段

**整段 Observer 产出的结构化包装**（`types.ts#L255-L261`）：

```ts
export interface ObserverResult {
  observations: string;        // 整段带日期分组的 markdown 文本
  currentTask?: string;
  suggestedContinuation?: string;
  threadTitle?: string;
  rawOutput?: string;
  degenerate?: boolean;
}
```

**硬约束**：每行最多 10,000 字符（`MAX_OBSERVATION_LINE_CHARS`, `observer-agent.ts#L1333`）。

**来源**：
- `packages/memory/src/processors/observational-memory/observer-agent.ts#L276-L327` (format template)
- `packages/memory/src/processors/observational-memory/types.ts#L255-L261`
- 官方 mdx 文档 [observational-memory.mdx](https://github.com/mastra-ai/mastra/blob/main/docs/src/content/en/docs/memory/observational-memory.mdx)

---

## Q2. 写入 prompt

**Extractor model**：默认 `'google/gemini-2.5-flash'`, `temperature: 0.3`, `maxOutputTokens: 100_000`, `thinkingBudget: 215`（[`constants.ts#L4-L41`](https://github.com/mastra-ai/mastra/blob/main/packages/memory/src/processors/observational-memory/constants.ts)）。**Anthropic Claude 目前不支持当 Observer/Reflector**（第三方确认，来自 the-decoder）。

**Observer system prompt 骨架**（`observer-agent.ts#L356-L459`，`buildObserverSystemPrompt` 函数）：

```
You are the memory consciousness of an AI assistant. Your observations will be
the ONLY information the assistant has about past interactions with this user.

Extract observations that will help the assistant remember:

<OBSERVER_EXTRACTION_INSTRUCTIONS>  ← ~250 行，~6KB

=== OUTPUT FORMAT ===
Your output MUST use XML tags...
<observations>...</observations>
<current-task>...</current-task>
<suggested-response>...</suggested-response>

=== GUIDELINES ===
<OBSERVER_GUIDELINES>

=== IMPORTANT: THREAD ATTRIBUTION ===
Do NOT add thread identifiers...

User messages are extremely important. If the user asks a question or gives a
new task, make it clear in <current-task> that this is the priority.
```

**关键 Instructions 章节（observer-agent.ts#L17-L264）**：
- `CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS`
- `STATE CHANGES AND UPDATES`
- `USER ASSERTIONS ARE AUTHORITATIVE`
- `TEMPORAL ANCHORING`
- `PRESERVE UNUSUAL PHRASING`
- `USE PRECISE ACTION VERBS`
- `PRESERVING DETAILS IN ASSISTANT-GENERATED CONTENT`
- `COMPLETION TRACKING` (✅ 的使用规则)

**GUIDELINES 全文**（`observer-agent.ts#L333-L349`，verbatim）：

```
- Be specific enough for the assistant to act on
- Add 1 to 5 observations per exchange
- Use terse language to save tokens.
- Do not add repetitive observations...
- Group repeated similar actions under a single parent with sub-bullets
- Make sure you start each observation with a priority emoji (🔴, 🟡, 🟢) or ✅
- Capture the user's words closely — short/medium near-verbatim, long summarized
- Treat ✅ as a memory signal that tells the assistant something is finished
- Prefer concrete resolved outcomes over meta-level workflow
- Observe WHAT the agent did and WHAT it means
```

**Per-turn task prompt**（`observer-agent.ts#L1201-L1215`）：

```
## New Message History to Observe
<formatted messages>

## Previous Observations     ← 仅当有时
<existing observations>

Do not repeat these existing observations. Your new observations will be
appended to the existing observations.

## Your Task
Extract new observations from the message history above...
```

**输出格式**：**XML 标签包裹的文本**，不是 JSON schema，不是 function call。由 regex 解析 `<observations>`, `<current-task>`, `<suggested-response>`。

---

## Q3. 读取时怎么拼 prompt

**全部源码证据在 `observational-memory.ts#L1418-L1483`（`formatObservationsForContext`）+ `constants.ts#L61-L121`。**

**位置**：observations 作为**独立的 system-level messages**（数组），不是塞 system prompt 也不是 user message。顺序是：
1. Preamble + instructions（一条）
2. 可选的"其他 thread 对话"块（仅 resource scope）
3. 字面量 `<observations>` 开标签
4. 多段 observation chunks（中间有 boundary 分隔）
5. `<current-task>` 独立消息
6. `<suggested-response>` 独立消息

**注意**：没有闭合 `</observations>` 标签 — 只有开标签作为 marker。

**Preamble**（`constants.ts#L61`）：
```
The following observations block contains your memory of past conversations
with this user.
```

**Instructions（跟在 `<observations>` 前）**（`constants.ts#L67-L75`，verbatim 重要摘录）：
```
IMPORTANT: When responding, reference specific details from these observations.
Do not give generic advice...

KNOWLEDGE UPDATES: ...always prefer the MOST RECENT information. Observations
include dates - if you see conflicting information, the newer observation
supersedes the older one...

PLANNED ACTIONS: If the user stated they planned to do something...assume they
completed the action unless there's evidence they didn't.

MOST RECENT USER INPUT: Treat the most recent user message as the
highest-priority signal...
```

**排序**：**chronological ASCENDING**（老的在前）。Observer 按日期分组输出，Reflector 被明确指示 *"Condense older observations more aggressively, retain more detail for recent ones"*。

**分隔符**：多段 observation chunks 之间用
```
--- message boundary (2026-04-15T10:06:00.000Z) ---
```
分隔（`observational-memory.ts#L1464-L1474`），每次 Observer 成功跑完插一个。

**过滤（关键）**：**注入时跑 `optimizeObservationsForContext()`（`observer-agent.ts#L1440-L1460`）**：
- **剥离 🟡 和 🟢** — 只有 🔴 进入 Actor 上下文
- 剥离 `[semantic tags]`
- 剥离 `->` 箭头
- 压缩空白

**所以：中低优先级 observation 存进数据库但不进 Actor prompt。** 这是个重要的发现。

**其他过滤**：没有 top-k、没有按 type 过滤（因为没有 type 字段）。全塞 🔴 进去，Reflector 在 40k token 时才压缩。

---

## Q4. "高准确率"来源

**Benchmark**：LongMemEval（公开 benchmark）。

**数字**（Mastra research 页 + 三方多源确认）：

| Model (Overall) | Score |
|---|---|
| gpt-5-mini | **94.87%** |
| gemini-3-pro-preview | 93.27% |
| gemini-3-flash-preview | 89.20% |
| gpt-4o | 84.23% |

**对照组数据**（research 页）：
- OM `gpt-4o` **84.23%** vs full-context baseline **60.2%**（+24 分）
- OM `gpt-4o` **84.23%** vs LongMemEval **"oracle"** 82.4%（+2 分，超 oracle）
- OM **vs Mastra 自家 RAG baseline**：84.23% vs 80.05%（gpt-4o，第三方 techbuddies 引用）
- OM vs Zep gpt-4o：84.23% vs 71.2%
- OM vs Supermemory gpt-4o：84.23% vs 81.6%
- OM vs Hindsight gemini-3-pro：93.27% vs 91.4%

**可靠性来源的分解**：
1. **Prompt 工程**（主力）— 250 行 extraction instruction 教 LLM 如何选/压观察
2. **Schema 设计**（辅助）— emoji 优先级 + 日期分组 = 稀疏检索
3. **长 context LLM**（放大器）— 从 gpt-4o → gemini-3-pro 涨 9 分，Mastra 明确强调 "scaling advantage"
4. **Prompt caching 对齐**（成本，不是准确率）— 第三方 techbuddies 称 "4–10x cache 折扣"

**Per-category 突破（gpt-5-mini）**：
- knowledge-update 96.2% / single-session-preference 100% / temporal-reasoning 95.5% / multi-session 87.2%

**注意事项**：LongMemEval 是记忆专项 benchmark，不代表其他任务。Mastra 自家 RAG baseline 在同套 benchmark 上也达 80%，所以 +4 分的提升不是数量级的。

**来源**：
- [research/observational-memory](https://mastra.ai/research/observational-memory)
- [the-decoder 三方报道](https://the-decoder.com/mastras-open-source-ai-memory-uses-traffic-light-emojis-for-more-efficient-compression/)
- [techbuddies 技术分析](https://www.techbuddies.io/2026/02/12/how-mastras-observational-memory-beats-rag-for-long-running-ai-agents/)

---

## Q5. 污染控制 / soft-delete / 反思

**关键发现：Mastra OM 没有 soft-delete 或单条编辑 API。**

**Grep 证据**（全库搜索）：`deleteObservation` / `removeObservation` / `hideObservation` / `setObservations` / `updateObservations` / `rewriteObservations` / `injectObservation` / `editObservation` / `correctObservation` — **全部 0 hits**。

**仅有的修改 API**（`observational-memory.ts#L3292-L3347`）：
1. `clear(threadId, resourceId?)` — 核选项，整 thread 抹掉
2. `updateRecordConfig(...)` — 只改 token threshold，不改内容
3. `getRecord` / `getHistory` — 只读

**实际纠错机制（隐式，三层）**：
1. **用户跟 Actor 说**（natural language correction）→ 下一轮 Observer 把"更正"记为新 observation（带日期）
2. **Actor-side instruction**（`constants.ts#L67-L75`）已植入规则："newer observation supersedes the older one"，所以老错误 observation 不会被信任
3. **Reflector 重写**（`reflector-agent.ts#L33-L274`）→ 40k token 时跑，把老+新合并成新状态。老 observation 实际上被"遗忘"（不再出现在 reflection 输出里）

**反思机制（Reflector）是独立 agent，不是独立表**：
- 写回到同一个 observation stream，覆盖性重写
- 默认 `'google/gemini-2.5-flash'`, `temperature: 0`, `thinkingBudget: 1024`
- 有 5 级压缩重试（`reflector-agent.ts#L154-L226`）：如果压缩结果不小于输入，Level 1→4 越压越狠，Level 4 目标 "2/10 detail"
- 还有 degenerate-output 检测（滑窗 dedup + 行长检查，`observer-agent.ts#L1359-L1391`），防 Gemini Flash 重复循环

**限制推论**：如果用户多次被误记"过敏花生"，Observer 每轮记一次错，Reflector 每轮压一次但**无法验证事实**。唯一"擦除"方式：用户显式纠正 → Actor 读到 → 下一轮 Observer 写"更正" observation → Reflector 合并后老错误消失。**有 lag，lag = 40k observation token 的间隔**。

**来源**：`observational-memory.ts`, `reflector-agent.ts`, `observer-agent.ts` + GitHub 全库 grep

---

## Q6. 规模上限

**官方 reference 文档明确不给数字**（[reference/memory/observational-memory](https://mastra.ai/reference/memory/observational-memory)）：
> *"No explicit guidance on recommended scale limits per user or thread."*

**实际是 token-based，没有 observation count 硬上限**：

| 阈值 | 默认值 | 行为 |
|---|---|---|
| `observation.messageTokens` | 30,000 | 触发 Observer |
| `reflection.observationTokens` | 40,000 | 触发 Reflector |
| `MAX_OBSERVATION_LINE_CHARS` | 10,000 | 单行硬上限 |
| `maxTokensPerBatch` | 10,000 | Observer 单批消耗 |

**超过后怎么办**：
- **压缩，不归档，不分窗**
- Reflector 每次把 40k observation 压到更小（level 1→4 retry ladder 保证每次必变小）
- 被 Reflector "忘掉"的老 observation 就是事实上的 GC
- 没有显式的"归档到冷存储"机制

**理论上限**：不限 — Mastra 压缩式设计让 observation stream 的 token 量通过 reflection 保持在 ~40k 量级。但**无官方生产规模证据**（podcast 无 transcript，博客无用户数据）。

**第三方声称**：techbuddies 称 "reduce token costs by up to an order of magnitude"，但没给测试时长或 observation 总数。

**存储适配器限制（间接提示规模定位）**：只支持 `@mastra/pg`、`@mastra/libsql`、`@mastra/mongodb` — 说明他们预期是"中等持久规模"（数据库，不是文件，不是专用向量库）。

---

## 置信度评估

| 问题 | 置信度 | 理由 |
|---|---|---|
| Q1. 数据结构 | **HIGH** | 源码 line-cited，types.ts + observer-agent.ts 验证一致 |
| Q2. 写入 prompt | **HIGH** | 完整 prompt 模板 verbatim 贴出，buildObserverSystemPrompt 函数定位 |
| Q3. 读取拼装 | **HIGH** | formatObservationsForContext 完整代码 + constants.ts 所有提示文本 |
| Q4. 准确率来源 | **HIGH** | research 页完整数字表 + 三方交叉验证 + 子分类分解 |
| Q5. 污染控制 | **HIGH** | 负面结论 — 全库 grep 确认无 API，机制链条清晰 |
| Q6. 规模上限 | **MEDIUM** | 机制清晰（token-based 无限压缩），**但无官方生产规模数据或用户报告** |

---

## 对 Jarvis 设计的关键启示

1. **不要过度建模 schema** — Mastra 把 observation 当 markdown 文本流，靠 prompt 约束格式，解析只做 XML tag
2. **emoji 是真·字段** — 🔴🟡🟢 作为 priority，入库时保留，读取时 `optimizeObservationsForContext` 只放 🔴 进 context。这招能把 context 砍掉 2/3
3. **Reflection > Soft-delete** — 用 40k token 阈值的重写式 GC 而不是标记删除，纠错靠用户自然语言 + Reflector 合并
4. **prompt caching 是一等公民** — append-only + 稳定前缀 + 仅在阈值时重写，是为了匹配 Anthropic/OpenAI 的缓存 TTL 设计
5. **Jarvis 小心点**：
   - Mastra 默认用 Gemini 2.5 Flash 当 Observer/Reflector — 你的架构（Grok main + Groq Llama router）需要决策谁来当 Observer。**不要用 Claude**（官方说 Anthropic 不工作）
   - Reflector level 4 极限压缩会丢细节，小月场景（家庭对话，有大量敏感偏好）可能需要把阈值调低让压缩更温和
   - 纠错 lag = ~40k tokens，对家庭语音助手（低 turn 密度）可能是几天 — 需要更激进的机制

---

## Sources

- [Observational Memory docs](https://mastra.ai/docs/memory/observational-memory)
- [Research: 95% on LongMemEval](https://mastra.ai/research/observational-memory)
- [Announcement blog](https://mastra.ai/blog/observational-memory)
- [mastra-ai/mastra GitHub](https://github.com/mastra-ai/mastra) — `packages/memory/src/processors/observational-memory/`
- [Reference docs](https://mastra.ai/reference/memory/observational-memory)
- [the-decoder analysis](https://the-decoder.com/mastras-open-source-ai-memory-uses-traffic-light-emojis-for-more-efficient-compression/)
- [techbuddies.io OM vs RAG](https://www.techbuddies.io/2026/02/12/how-mastras-observational-memory-beats-rag-for-long-running-ai-agents/)
- [Tyler Barnes podcast](https://mastra.ai/podcasts/observational-memory-the-human-inspired-memory-system-for-ai-agents-with-tyler-barnes) — transcript 未发布
