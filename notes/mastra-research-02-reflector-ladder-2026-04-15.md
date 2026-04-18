# Mastra Reflector 5 级压缩梯度 — 一手源码调研

**调研日期**：2026-04-15
**目的**：评估 Mastra Reflector 的 retry ladder 对"敏感偏好保留度"的影响，决定 Jarvis Reflector 是否抄这套梯度、level 4 会不会丢硬偏好
**源**：`mastra-ai/mastra` @ `main` 分支
- `packages/memory/src/processors/observational-memory/reflector-agent.ts`（380 行）
- `packages/memory/src/processors/observational-memory/reflector-runner.ts`（821 行）
- `packages/memory/src/processors/observational-memory/observer-agent.ts`（1460 行，含 degenerate 检测）
- `packages/memory/src/processors/observational-memory/observational-memory.ts`（3423 行，含存储调用）
- `packages/core/src/storage/domains/memory/inmemory.ts`（`createReflectionGeneration` 实现）

---

## Q1. 5 个 Level 的完整 Prompt 文本（verbatim）

**位置**：`reflector-agent.ts` L154–L226，导出为 `COMPRESSION_GUIDANCE: Record<CompressionLevel, string>`。

**每次 retry 重新构造完整 prompt**（`reflector-agent.ts` L236–L274 `buildReflectorPrompt`）：system prompt 不变（L33–L129 `buildReflectorSystemPrompt`，始终共享），user prompt = 观察原文 + guidance 段落。level 之间 **只有 user prompt 里的 guidance 段落不同**，system 100% 共享。

### Level 0 — 无 guidance（首次尝试）

```ts
// reflector-agent.ts L155
0: '',
```

`buildReflectorPrompt` 里 `if (guidance)` 分支直接跳过，prompt 只包含 observations + manualPrompt（如有）+ continuation hint。**这是 regular reflection 的起点**，不是 retry。

### Level 1 — Gentle（8/10 detail）

```
## COMPRESSION REQUIRED

Your previous reflection was the same size or larger than the original observations.

Please re-process with slightly more compression:
- Towards the beginning, condense more observations into higher-level reflections
- Closer to the end, retain more fine details (recent context matters more)
- Memory is getting long - use a more condensed style throughout
- Combine related items more aggressively but do not lose important specific details of names, places, events, and people
- Combine repeated similar tool calls (e.g. multiple file views, searches, or edits in the same area) into a single summary line describing what was explored/changed and the outcome
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done

Aim for a 8/10 detail level.
```

源：`reflector-agent.ts` L156–L171。导出为 `COMPRESSION_RETRY_PROMPT`（L231，历史兼容别名）。

### Level 2 — Aggressive（6/10 detail）

```
## AGGRESSIVE COMPRESSION REQUIRED

Your previous reflection was still too large after compression guidance.

Please re-process with much more aggressive compression:
- Towards the beginning, heavily condense observations into high-level summaries
- Closer to the end, retain fine details (recent context matters more)
- Memory is getting very long - use a significantly more condensed style throughout
- Combine related items aggressively but do not lose important specific details of names, places, events, and people
- Combine repeated similar tool calls (e.g. multiple file views, searches, or edits in the same area) into a single summary line describing what was explored/changed and the outcome
- If the same file or module is mentioned across many observations, merge into one entry covering the full arc
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done
- Remove redundant information and merge overlapping observations

Aim for a 6/10 detail level.
```

源：`reflector-agent.ts` L172–L189。

### Level 3 — Critical（4/10 detail）

```
## CRITICAL COMPRESSION REQUIRED

Your previous reflections have failed to compress sufficiently after multiple attempts.

Please re-process with maximum compression:
- Summarize the oldest observations (first 50-70%) into brief high-level paragraphs — only key facts, decisions, and outcomes
- For the most recent observations (last 30-50%), retain important details but still use a condensed style
- Ruthlessly merge related observations — if 10 observations are about the same topic, combine into 1-2 lines
- Combine all tool call sequences (file views, searches, edits, builds) into outcome-only summaries — drop individual steps entirely
- Drop procedural details (tool calls, retries, intermediate steps) — keep only final outcomes
- Drop observations that are no longer relevant or have been superseded by newer information
- Preserve ✅ completion markers — they are memory signals that tell the assistant what is already resolved and help prevent repeated work
- Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done
- Preserve: names, dates, decisions, errors, user preferences, and architectural choices

Aim for a 4/10 detail level.
```

源：`reflector-agent.ts` L190–L207。

### Level 4 — Extreme（2/10 detail）

```
## EXTREME COMPRESSION REQUIRED

Multiple compression attempts have failed. The content may already be dense from a prior reflection.

You MUST dramatically reduce the number of observations while keeping the standard observation format (date groups with bullet points and priority emojis):
- Tool call observations are the biggest source of bloat. Collapse ALL tool call sequences into outcome-only observations — e.g. 10 observations about viewing/searching/editing files become 1 observation about what was actually learned or achieved (e.g. "Investigated auth module and found token validation was skipping expiry check")
- Never preserve individual tool calls (viewed file X, searched for Y, ran build) — only preserve what was discovered or accomplished
- Consolidate many related observations into single, more generic observations
- Merge all same-day date groups into at most 2-3 date groups per day
- For older content, each topic or task should be at most 1-2 observations capturing the key outcome
- For recent content, retain more detail but still merge related items aggressively
- If multiple observations describe incremental progress on the same task, keep only the final state
- Preserve ✅ completion markers and their outcomes but merge related completions into fewer lines
- Preserve: user preferences, key decisions, architectural choices, and unresolved issues

Aim for a 2/10 detail level. Fewer, more generic observations are better than many specific ones that exceed the budget.
```

源：`reflector-agent.ts` L208–L225。

### Detail Level 对照表

| Level | 名称 | Detail | 关键动作 |
|-------|------|--------|----------|
| 0 | — | —/10 | 首次无 guidance |
| 1 | COMPRESSION REQUIRED | 8/10 | "slightly more compression" |
| 2 | AGGRESSIVE | 6/10 | "much more aggressive" |
| 3 | CRITICAL | 4/10 | 老观察合并为段落·drop 程序性细节 |
| 4 | EXTREME | 2/10 | 同日 ≤2-3 组·老话题 ≤1-2 行 |

### Level 间共享 vs 差异

**共享（100%，system prompt + user prompt 顶部）**：
- System：`buildReflectorSystemPrompt` 整串（L33–L129）——"memory consciousness"角色、观察提取规则内嵌、thread attribution 规则、XML 输出格式、"USER ASSERTIONS TAKE PRECEDENCE"
- User prompt 顶部：`## OBSERVATIONS TO REFLECT ON` + 原文 + "produce a refined, condensed version that will become the assistant's entire memory going forward"（L246–L252）
- 每 level 都显式保留 ✅ 标记

**差异（只有 guidance 段）**：
- L1→L4 压缩强度递增（8→6→4→2 /10）
- L3 起引入"drop procedural details"
- L4 起显式"Never preserve individual tool calls"
- L3/L4 明确 Preserve 列表（L204、L222）

---

## Q2. 触发到 Level N 的判定条件

**位置**：`reflector-runner.ts` L195–L329 主 loop；`reflector-agent.ts` L377–L380 `validateCompression`。

### 核心判定函数

```ts
// reflector-agent.ts L377-L380
export function validateCompression(reflectedTokens: number, targetThreshold: number): boolean {
  // Reflection should be below the target threshold
  return reflectedTokens < targetThreshold;
}
```

**判定条件：reflected output token 必须 < `targetThreshold`（严格小于）。**

### 关键发现：**不是"比输入小 X%"，而是"小于 target threshold 的绝对值"**

- `targetThreshold` 来自 `observationTokensThreshold` 参数，默认 `getMaxThreshold(reflectionConfig.observationTokens)`（`reflector-runner.ts` L184）。
- **没有比例阈值**。不是"output < 80% input"或"output < input"，而是"output < 预设 reflection 触发阈值"。
- 言下之意：如果输入已经很大，reflector 必须压到 threshold 之下才算成功，否则 retry 升级。

### Retry 升级逻辑

```ts
// reflector-runner.ts L188-L195
const startLevel: CompressionLevel = compressionStartLevel ?? 0;
let currentLevel: CompressionLevel = startLevel;
const maxLevel: CompressionLevel = Math.min(MAX_COMPRESSION_LEVEL, startLevel + 3) as CompressionLevel;
// ...
while (currentLevel <= maxLevel) {
```

- `startLevel` 默认 0（regular reflection）
- `maxLevel = min(4, startLevel + 3)` → 默认 `maxLevel = 3`
- **这意味着 regular reflection 默认只跑 level 0→1→2→3，不自动升到 level 4**
- **只有 caller 显式传 `compressionStartLevel=1`（或更高）才可能到达 level 4**（例如 L1 start → maxLevel=4）

### 退出条件（L290–L297）

```ts
if (!parsed.degenerate && (validateCompression(reflectedTokens, targetThreshold) || currentLevel >= maxLevel)) {
  break;
}

if (parsed.degenerate && currentLevel >= maxLevel) {
  omDebug(`[OM:callReflector] degenerate output persists at maxLevel=${maxLevel}, breaking`);
  break;
}
```

- 压缩成功 **或** 已到 maxLevel → 退出（接受当前输出）
- degenerate 输出 + 已到 maxLevel → 退出（接受 degenerate）
- 否则 `currentLevel = min(currentLevel + 1, maxLevel)`（L328），每次失败 **升一级**

### 失败几次进入下一 level？

**每次失败升一级，不是"累计失败 N 次才升"**。每个 level 只尝试一次。总尝试次数 = `maxLevel - startLevel + 1` = 最多 4 次（startLevel=0 时）。

### degenerate 的特殊处理（L278–L285）

```ts
if (parsed.degenerate) {
  reflectedTokens = originalTokens;  // 强制标记为未压缩
}
```

degenerate 被当作"未压缩"处理，`validateCompression` 返回 false，继续 retry。

---

## Q3. 压缩时的保留规则

### 明确保留的信号（**所有 level 都包含**）

| 信号 | 保留规则 | 源 |
|------|----------|-----|
| ✅ 完成标记 | 5 个 level 全部显式"Preserve ✅ completion markers — they are memory signals..." | L64、L167、L184、L202、L221 |
| ✅ 的具体结果 | "Preserve the concrete resolved outcome captured by ✅ markers" | L65、L168、L185、L203 |
| Date 头（时间戳） | "Preserve and include dates/times when present (temporal context is critical)" — system prompt L61 | 所有 level 继承 |
| 最新观察 | "retain more fine details (recent context matters more)" — L1/L2 | L160、L179 |
| User assertions | "USER ASSERTIONS TAKE PRECEDENCE"，在 user stated vs user asked 冲突时保留 stated | L68–L74 |

### 🔴🟡🟢 emoji 的处理

**源码里 Reflector prompt 完全没提"扔 🟡🟢、留 🔴"**。

- Reflector system prompt L110–L113 仅说"Put all consolidated observations here using the date-grouped format with **priority emojis (🔴, 🟡, 🟢)**" —— 3 色都保留。
- L4 prompt L213 明确"keeping the standard observation format (date groups with bullet points and priority emojis)" —— 仍保留 emoji 格式。
- **把 🟡🟢 剥掉发生在下游**（`observer-agent.ts` L1440–L1459 `optimizeObservationsForContext`），**只在把观察送给 Actor 时才 strip**，reflection 存储层保留完整 emoji。
- 含义：Reflector 不会"扔 🟡🟢 留 🔴"，而是对 3 种 emoji 按文本保留/合并。判优先级主要靠 Reflector 自己的语义理解，不靠 emoji 硬规则。

### "硬偏好不可压"——**没有**

**源码里没有任何关于食物过敏、健康数据、生命安全数据的"不可压"硬规则**。

- L3 的 Preserve 清单（L204）：`names, dates, decisions, errors, user preferences, and architectural choices`
- L4 的 Preserve 清单（L222）：`user preferences, key decisions, architectural choices, and unresolved issues`

**"user preferences" 是唯一涉及偏好的兜底**，但：
- 没有分类（没有"critical preferences"子类）
- 没有"不可合并"标记（L4 明确可以把"many related observations"合并成 generic 的）
- 完全依赖 LLM 理解什么是 "preference" 以及是否足够重要

**风险点对 Jarvis**：Level 4 prompt 说"Consolidate many related observations into single, more generic observations"、"each topic or task should be at most 1-2 observations capturing the key outcome"——如果"用户对花生过敏"被 LLM 归类为"饮食偏好"子话题，可能被合入 generic "dietary preferences" 里，丢细节。**没有白名单兜底**。

### ✅ 处理

不只是保留标记，**要保留"具体结果"**。"Preserve the concrete resolved outcome captured by ✅ markers so the assistant knows what exactly is done"（L65/L168/L185/L203）。L4 允许"merge related completions into fewer lines"（L221），可合并同类完成但不丢结果。

---

## Q4. Degenerate 检测逻辑

**位置**：`observer-agent.ts` L1359–L1391 `detectDegenerateRepetition`；调用点 `reflector-agent.ts` L282（parser 里）。

### 前置门槛

```ts
// L1360
if (!text || text.length < 2000) return false;
```

**< 2000 字符的文本直接判定不 degenerate**，不走检测。

### 滑窗 dedup 算法（Strategy 1，L1362–L1381）

```ts
const windowSize = 200;
const step = Math.max(1, Math.floor(text.length / 50)); // 采样 ~50 个窗口
const seen = new Map<string, number>();
let duplicateWindows = 0;
let totalWindows = 0;

for (let i = 0; i + windowSize <= text.length; i += step) {
  const window = text.slice(i, i + windowSize);
  totalWindows++;
  const count = (seen.get(window) ?? 0) + 1;
  seen.set(window, count);
  if (count > 1) duplicateWindows++;
}

if (totalWindows > 5 && duplicateWindows / totalWindows > 0.4) {
  return true;
}
```

**参数**：
- 窗口大小：固定 200 字符
- 步长：`floor(len/50)`，确保 ~50 个采样窗口
- 相似度阈值：**完全相等字符串哈希**（`Map<string, number>` key 相等，不是模糊相似度）
- 触发条件：`totalWindows > 5` **且** `duplicateWindows / totalWindows > 0.4`（40% 的采样窗口是重复的）

**注意**：不是语义相似，是字面完全相等。这只抓"复读机 bug"（如 Gemini Flash loop），抓不到近义改写。

### 行长检查（Strategy 2，L1385–L1388）

```ts
const lines = text.split('\n');
for (const line of lines) {
  if (line.length > 50_000) return true;
}
```

**阈值**：**单行 > 50,000 字符** → 视为 degenerate 枚举。

（这和 `sanitizeObservationLines` 的 `MAX_OBSERVATION_LINE_CHARS = 10_000`（L1333）是两回事：sanitize 在 < 10k 时不动，10k–50k 之间会被截断到 10k+"… [truncated]"，> 50k 直接判 degenerate。）

### 触发后的恢复

```ts
// reflector-agent.ts L282-L287 (parseReflectorOutput)
if (detectDegenerateRepetition(output)) {
  return {
    observations: '',
    degenerate: true,
  };
}
```

- Parser 直接返回空 observations + `degenerate: true` flag
- `reflector-runner.ts` L278–L285：`reflectedTokens = originalTokens`（强制判"未压缩"）
- 走正常 retry 升级路径（currentLevel + 1）
- **recovery = 升级 level 重新调 LLM**。没有"丢 LLM 直接回退到上一代 observations"的路径。
- 极端：`currentLevel >= maxLevel && degenerate` → 接受空字符串作为最终 observations（L294–L297）。这意味着 **持续 degenerate 会把记忆清空**（一个已知风险）。

---

## Q5. Reflector 跑完后：替换还是追加？

**位置**：`observational-memory.ts` L3240–L3263；存储 adapter `inmemory.ts` L1072–L1103 `createReflectionGeneration`。

### 调用链

```ts
// observational-memory.ts L3240-L3258
const reflectResult = await this.reflector.call(record.activeObservations, ...);
const reflectionTokenCount = this.tokenCounter.countObservations(reflectResult.observations);

await this.storage.createReflectionGeneration({
  currentRecord: record,
  reflection: reflectResult.observations,
  tokenCount: reflectionTokenCount,
});
```

### 存储语义（inmemory adapter）

```ts
// inmemory.ts L1072-L1103
async createReflectionGeneration(input: CreateReflectionGenerationInput): Promise<ObservationalMemoryRecord> {
  const { currentRecord, reflection, tokenCount } = input;
  const key = this.getObservationalMemoryKey(currentRecord.threadId, currentRecord.resourceId);
  const now = new Date();

  const newRecord: ObservationalMemoryRecord = {
    id: crypto.randomUUID(),                          // ← 新 record ID
    scope: currentRecord.scope,
    threadId: currentRecord.threadId,
    resourceId: currentRecord.resourceId,
    createdAt: now,
    updatedAt: now,
    lastObservedAt: currentRecord.lastObservedAt ?? now,
    originType: 'reflection',                         // ← 标识来源
    generationCount: currentRecord.generationCount + 1,  // ← 代数递增
    activeObservations: reflection,                   // ← 用 reflection 替换
    // ...
  };
  // (后续代码将 newRecord 推入 history 数组)
}
```

### 结论：**新增一代（generation），不是 in-place 替换**

- 每次 reflection 生成 **新 record（新 UUID）**，`generationCount++`
- 新 record 的 `activeObservations = reflection 输出`（在"当前"维度确实替换了老的）
- **老 record 仍保留在数据库**（`types.ts` 有 `getObservationalMemoryHistory` API，可按 threadId/resourceId 拉全部历史代）
- `originType` 字段区分是 `'observation'` 还是 `'reflection'`
- **无"soft delete"标记**（没有 `deletedAt`），老代直接作为历史代保留
- **无物理删除代码路径**（本次调研未发现任何 `DELETE` / `remove` / `purge` 调用针对老 generation）

### 对 Jarvis 的含义

- "记错"问题 **理论上可回溯**：reflection 丢失的细节，上一代 record 还在（如果你实现 `getHistory` 查询路径）
- 但 Actor 实际只读 "当前活跃" record 的 `activeObservations`（L3279–L3280）—— **历史代不自动 inject context**，需要业务主动调 history API
- 如果 Jarvis 抄这套，可以做一个"可疑 reflection → 回滚到 G-1"机制（Mastra 本身没做）

---

## 核心结论（对 Jarvis 敏感偏好保留问题）

1. **5 级梯度"漂亮"但有漏洞**：Level 4 允许把"many related observations"合并成 generic 观察，**没有任何"硬偏好白名单"保护**（过敏、健康、安全信息和普通偏好一视同仁）。
2. **regular reflection 默认不到 Level 4**：`maxLevel = startLevel + 3`，startLevel=0 时最高只到 L3。L4 是"已经压缩过一次的老 reflection 再次压缩"的场景（caller 显式传 start=1+）。
3. **判定条件是绝对值 threshold，不是比例**：reflected < `targetThreshold` 字面严格小于。没有"输出必须比输入小 20%"之类的比例要求。
4. **degenerate 检测只抓字面复读**，不抓"压缩丢细节"。Jarvis 若关心语义保留，需要 **独立加一层"关键事实 diff 检查"**（Mastra 没做）。
5. **存储层对 rollback 友好**：每次 reflection 是新 generation，老代保留。Jarvis 可以抄这个设计，加一层"新 reflection 经 diff 审核，未通过则 stay on G-1"的门控，填补 Mastra 缺失的安全网。

### 建议 Jarvis 抄袭 / 改造清单

- ✅ 抄：5 级 ladder + validateCompression 退出条件
- ✅ 抄：每 reflection 创建新 generation（存储层 rollback 友好）
- ⚠️ 改：在 L3/L4 prompt 的 "Preserve" 清单里 **显式列出"过敏、疾病、生命安全、金额阈值、亲属关系"等硬偏好类**（比 Mastra 笼统的 "user preferences" 更强）
- ⚠️ 加：独立"关键事实集合"白名单，reflection 后跑 diff，白名单条目缺失则拒绝 swap（Mastra 缺失）
- ⚠️ 加：degenerate 除了字面复读，再加"reflection token < input × 5%" 异常值保护（防过度压缩）
