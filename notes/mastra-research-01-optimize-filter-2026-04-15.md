# Mastra OM 调研 01：`optimizeObservationsForContext` 完整逻辑

**日期：** 2026-04-15
**调研目标：** 复刻 Mastra Observational Memory 的读取过滤机制
**源码位置：** `packages/memory/src/processors/observational-memory/observer-agent.ts` L1440-L1460（共 20 行）
**依赖：** `stripEphemeralAnchorIds` in `./anchor-ids.ts` L84-L90
**Repo：** https://github.com/mastra-ai/mastra (main 分支, 抓取时间 2026-04-15)

---

## 关键认知修正

**原假设"剥离 🟡🟢·保留 🔴✅"不准确。** 该函数是**纯字符串正则替换**，不删除任何行。它只把 🟡🟢 这两个字符本身从字符串中剥掉，整行内容（包括文本、子要点）依然保留。✅ 根本没被提及，原样透传。

完整函数源码（L1440-L1460）：

```typescript
export function optimizeObservationsForContext(observations: string): string {
  let optimized = stripEphemeralAnchorIds(observations);                          // L1441

  // Remove 🟡 and 🟢 emojis (keep 🔴 for critical items)
  optimized = optimized.replace(/🟡\s*/g, '');                                    // L1444
  optimized = optimized.replace(/🟢\s*/g, '');                                    // L1445

  // Remove semantic tags like [label, label] but keep collapsed markers like [72 items collapsed - ID: b1fa]
  optimized = optimized.replace(/\[(?![\d\s]*items collapsed)[^\]]+\]/g, '');     // L1448

  // Remove arrow indicators
  optimized = optimized.replace(/\s*->\s*/g, ' ');                                // L1451

  // Clean up multiple spaces
  optimized = optimized.replace(/  +/g, ' ');                                     // L1454

  // Clean up multiple newlines
  optimized = optimized.replace(/\n{3,}/g, '\n\n');                               // L1457

  return optimized.trim();                                                         // L1459
}
```

文档注释（L1430-L1438）：
```
Optimize observations for token efficiency before presenting to the Actor.

This removes:
- Non-critical emojis (🟡 and 🟢, keeping only 🔴)
- Semantic tags [label, label]
- Arrow indicators (->)
- Extra whitespace

The full format is preserved in storage for analysis.
```

---

## Q1. 过滤粒度：**字符级，不是行级也不是块级**

- **整个函数没有 `split('\n')` / 行遍历 / 树结构解析**。它是对整个 observations 字符串的 5 个全局正则替换。
- `optimized.replace(/🟡\s*/g, '')` (L1444) 匹配 emoji 字符 + 紧随的空白，**不匹配换行符后的任何内容**。所以：

  | 输入 | 输出 |
  |------|------|
  | `🟡 user likes pizza` | `user likes pizza`（**行保留**，只去 emoji）|
  | `🔴 weapons locked\n  * 🟡 ammo low` | `🔴 weapons locked\n  * ammo low`（**子要点保留**）|
  | `🟡 parent\n  * ✅ child` | `parent\n  * ✅ child`（✅ 未被触碰）|

- **子要点挂在 🟡 parent 下怎么办？** —— **照常保留**。没有父子感知逻辑。
- **✅ 子要点挂在 🟡 parent 下怎么办？** —— 同上，✅ 整个没被 regex 覆盖，原样透传。

**结论：不存在"🟡 整行删除"这种行为。原假设的过滤语义在此函数中找不到。**

---

## Q2. 剥离细节

### 2a. `[semantic tags]` 正则（L1448）

```regex
/\[(?![\d\s]*items collapsed)[^\]]+\]/g
```

- 匹配任意 `[...]`，**用 negative lookahead 排除 `[N items collapsed ...]`**（这种折叠标记要保留）。
- `[^\]]+` = 括号内非 `]` 的任意字符（所以不支持嵌套中括号）。
- 例：`[user, preference]` → 删；`[72 items collapsed - ID: b1fa]` → 保留。

### 2b. `->` 箭头规则（L1451）

```regex
/\s*->\s*/g  →  ' '
```

- 箭头两侧任意空白（含换行 `\s` = `[\t\n\r\f\v ]`）连带 `->` 一起替换为**一个单空格**。
- 例：`A -> B` → `A B`；`A\n  -> B` → `A B`（**会吞掉换行**，因为 `\s` 含 `\n`）。⚠️ 这是个隐式副作用。

### 2c. 空白压缩程度

两级压缩：

| 规则 | 位置 | 行为 |
|------|------|------|
| `/  +/g → ' '` | L1454 | **2+ 个空格 → 1 个空格**（单空格保留）|
| `/\n{3,}/g → '\n\n'` | L1457 | **3+ 换行 → 2 换行**（段落结构保留）|
| `.trim()` | L1459 | 首尾空白去除 |

所以：单空格保留、段落（双换行）保留、制表符没处理。

### 2d. Date 头 `Date: Dec 4, 2025` 是否保留

**找不到针对 Date 头的任何特殊处理。** 它是纯文本，没被任何 regex 匹配到，**原样保留**。

### 2e. 附加步骤：`stripEphemeralAnchorIds` (L1441 调用 → anchor-ids.ts L84-L90)

```typescript
export function stripEphemeralAnchorIds(observations: string): string {
  if (!observations) {
    return observations;
  }

  return observations.replace(/(^|\n)([^\S\n]*)\[(O\d+(?:-N\d+)?)\][^\S\n]*/g, '$1$2');
}
```

- 剥离行首的 anchor ID 标记，如 `[O12]` 或 `[O12-N3]`（保留行首缩进 `$2`）。
- 这个步骤在 L1441 最先执行，在 emoji/tag 剥离之前。
- `[^\S\n]` = 非换行的空白字符（即 space/tab），不吞换行。

---

## Q3. Message Boundary 分隔符处理

**找不到 boundary 专属逻辑。**

- 没有 `split` / `filter(chunk => chunk.length > 0)` 这类操作。
- 剥离后若出现空行，只会被 L1457 的 `\n{3,}→\n\n` 规则间接折叠（但只折叠 3 行以上的空行，保留段落分隔）。
- boundary 本身若是纯文本字符串（比如 `---` 或 `=== message ===`），不会被任何 regex 触及，**原样保留**。
- 若 boundary 是 `[boundary]` 这种带方括号形式 → 会被 L1448 删除（除非匹配 `[N items collapsed]` pattern）。

---

## Q4. Fallback 降级逻辑

**找不到。没有 fallback。**

- 函数没有：`count(🔴)` / `if (redCount < N)` / 任何条件分支。
- 纯无状态字符串变换，无统计、无阈值、无回退。
- 即使剥离后 observations 全空 → 返回空字符串（`.trim()` 后）。

---

## 复刻建议（给 Jarvis Python 实现）

1. **不要实现"🟡 整行删除"** —— 原 Mastra 只去 emoji 字符。如果要整行删，是**增强**而非**复刻**，请明确标注。
2. **顺序关键**：`stripAnchorIds` → emoji → tags → arrows → 多空格 → 多换行 → trim。顺序错了会影响结果（例如先压空白再去 tags 会留下多余空格）。
3. **Collapsed 标记例外**：`[N items collapsed]` 必须保留，用 negative lookahead 实现。
4. **⚠️ 注意 `->` 会吞换行**：`\s*->\s*` 中 `\s` 包含 `\n`，跨行箭头会被合并成一行。要不要这个行为自行决定。
5. **✅ 符号原 Mastra 不特殊处理** —— 如果 Jarvis 要赋予 ✅ 特殊语义（"已解决"），需要自己加逻辑。

---

## Python 参考复刻（直译版）

```python
import re

_ANCHOR_ID_RE = re.compile(r'(^|\n)([^\S\n]*)\[(O\d+(?:-N\d+)?)\][^\S\n]*')
_YELLOW_RE = re.compile(r'🟡\s*')
_GREEN_RE = re.compile(r'🟢\s*')
_TAG_RE = re.compile(r'\[(?![\d\s]*items collapsed)[^\]]+\]')
_ARROW_RE = re.compile(r'\s*->\s*')
_MULTI_SPACE_RE = re.compile(r'  +')
_MULTI_NEWLINE_RE = re.compile(r'\n{3,}')


def strip_ephemeral_anchor_ids(observations: str) -> str:
    if not observations:
        return observations
    return _ANCHOR_ID_RE.sub(r'\1\2', observations)


def optimize_observations_for_context(observations: str) -> str:
    """复刻 Mastra observer-agent.ts L1440-L1460."""
    optimized = strip_ephemeral_anchor_ids(observations)
    optimized = _YELLOW_RE.sub('', optimized)
    optimized = _GREEN_RE.sub('', optimized)
    optimized = _TAG_RE.sub('', optimized)
    optimized = _ARROW_RE.sub(' ', optimized)
    optimized = _MULTI_SPACE_RE.sub(' ', optimized)
    optimized = _MULTI_NEWLINE_RE.sub('\n\n', optimized)
    return optimized.strip()
```
