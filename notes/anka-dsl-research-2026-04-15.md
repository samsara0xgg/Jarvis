# Anka DSL 深度调研报告
*Generated: 2026-04-15 | Sources: 3 (GitHub, arXiv, Argmin AI) | Confidence: High*

## Executive Summary

Anka 是一个专为 **LLM 代码生成** 设计的 DSL，领域限定在 **数据转换 pipeline**（filter/map/aggregate 表格数据）。论文声称的 +40pp 优势真实但高度受限：仅在 multi-step pipeline（3-5 步）类别中成立，overall 只 +4.6pp。整个项目由一位 UW-Madison 学生独立完成，6 星 repo，benchmark 100 题自建。**核心设计理念（canonical forms + explicit naming + structural scaffolding）高度可借鉴**，但 Anka 本身的实现和 primitives 和我们的 Compose DSL 方向几乎零重合。

---

## Q1. Anka DSL 完整 Primitive 列表

### Data Operations (18 种)

| 类别 | Primitives |
|------|-----------|
| Selection | `FILTER ... WHERE ... INTO`, `SELECT`, `DISTINCT` |
| Transformation | `MAP ... WITH`, `RENAME`, `DROP`, `ADD_COLUMN` |
| Aggregation | `AGGREGATE ... GROUP_BY ... COMPUTE` (SUM, AVG, COUNT, MIN, MAX) |
| Ordering | `SORT` (ASC/DESC), `LIMIT`, `SKIP`, `SLICE` |
| Combination | `JOIN`, `LEFT_JOIN`, `UNION` |
| I/O | `READ` (JSON/CSV), `WRITE` (JSON/CSV), `FETCH` (HTTP GET), `POST` (HTTP POST) |

### Control Flow

| Construct | 说明 |
|-----------|------|
| `IF/ELSE` | 条件执行 |
| `FOR_EACH` | 遍历 |
| `WHILE` | 条件循环 |
| `MATCH/CASE` | 模式匹配 |
| `TRY/ON_ERROR` | 错误处理 |

### 结构元素

- `PIPELINE <name>:` — 顶层声明
- `INPUT <name>: TABLE[field: TYPE, ...]` — 类型化输入
- `STEP <name>:` — 命名步骤块
- `INTO <name>` — 显式命名中间结果
- `OUTPUT <name>` — 声明输出

### 类型系统

`INT`, `STRING`, `DECIMAL`, `BOOL`, `DATE`, `DATETIME`

---

## Q2. 与我们 Compose DSL 的重合度

### 我们提出的 ~10 个 Primitive（Phase 4, synthesis.md:268）

```
http_get, jmespath, filter, map, compose, if, slot_fill, call_skill, say, remember
```

### 重合分析

| 我们的 Primitive | Anka 对应 | 重合？ |
|-----------------|-----------|--------|
| `http_get` | `FETCH` | ✅ 部分（Anka 也有 `POST`） |
| `jmespath` | 无 | ❌ |
| `filter` | `FILTER` | ✅ 语义相同，但 Anka 操作表格行，我们操作 JSON/dict |
| `map` | `MAP` | ✅ 同上 |
| `compose` | `PIPELINE` + `STEP` | 🟡 概念相似，实现不同 |
| `if` | `IF/ELSE` | ✅ |
| `slot_fill` | 无 | ❌ 对话交互概念 |
| `call_skill` | 无 | ❌ 技能编排概念 |
| `say` | 无 | ❌ TTS/输出概念 |
| `remember` | 无 | ❌ 记忆概念 |

**结论：语义重合 ~30%（filter/map/if/http），但目标域完全不同。**

Anka = 数据转换 pipeline（类 SQL/pandas）
我们 = 语音助手技能编排（API 调用 + 对话 + 设备控制）

---

## Q3. 解释器规模

| 指标 | 数值 |
|------|------|
| **总代码行数** | ~6,400 行（README 写 ~5,000，论文写 ~6,400） |
| **实现语言** | Python（97.2%）+ TypeScript（2.5%）+ TeX（0.2%） |
| **解析器** | Lark（Python PEG parser） |
| **Grammar 规则** | 98 条 production rules |
| **AST 节点类型** | 68 种（不可变 dataclass + 源码位置追踪） |
| **解释器类型** | tree-walking interpreter |
| **单元测试** | 322 个 |
| **Contributors** | **1 人**（BleBlo） |
| **活跃度** | 创建 2025-12-24, 最后 push 2025-12-25（**仅 2 天开发**） |
| **Stars** | 6 |

---

## Q4. +40pp 的具体实验设置

### Baseline
- **对比语言**: Python（假设 LLM 已有 pandas 知识，即利用训练数据优势）
- **Anka 侧**: 通过 prompt 注入 ~100 行语法指南，zero-shot 学习（无 fine-tuning）

### 模型
| 模型 | Multi-step (Anka) | Multi-step (Python) | Δ |
|------|-------------------|---------------------|---|
| Claude 3.5 Haiku | 100.0% | 60.0% | **+40.0 pp** |
| GPT-4o-mini | 86.7% | 60.0% | **+26.7 pp** |
| **平均** | | | **+33.4 pp** |

### Benchmark 详情
- **100 个任务**，8 个类别
- multi_step 类别仅 **10 个任务**（所以 +40pp = Anka 做对 10/10，Python 做对 6/10）
- 每任务每语言生成 **10 个 samples**，temperature 0.3
- Task Accuracy = ≥50% samples 正确即算通过
- **Overall**: Anka 95.8% vs Python 91.2%（+4.6pp，没那么惊人）

### Python 失败原因分析
| 错误类型 | 占比 |
|---------|------|
| Variable shadowing（变量遮蔽） | 42% |
| Operation sequencing（操作顺序错误） | 31% |
| Chaining confusion（链式调用混乱） | 27% |

### 复杂度与优势关系
- 1-2 步操作: **0% 优势**
- 3-4 步操作: **+5% 优势**
- 5+ 步操作: **+40% 优势**

---

## Q5. 作者的 Known Limitations

论文 Section 7 自述的局限：

1. **Benchmark Scope** — 仅覆盖数据转换 pipeline，**未验证其他编程任务的泛化性**
2. **Model Coverage** — 仅测了 2 个模型（Claude 3.5 Haiku + GPT-4o-mini），未测 Gemini/Llama/等
3. **No Fine-Tuning Comparison** — 只比了 prompt-based Anka vs pre-trained Python，未比 Anka-fine-tuned 模型（上限未知）
4. **No User Study** — 人类是否觉得 Anka 代码可读，完全未评估
5. **Single Benchmark Suite** — 100 题自建，可能存在对 Anka 有利的偏见

### Argmin AI 的独立评估补充
- Evidence Strength: 0.80 / Confidence: 0.80
- 标记 **"Needs Validation"**
- Production Readiness: 0.70（注意这是 Argmin 对"能否直接用在生产"的打分）
- Novelty: 0.60（概念不新，执行有值）
- 0 citations（截至 2026-04）

---

## 对我们的可借鉴性评估

### ✅ 值得借鉴的设计理念（与实现无关）

| Anka 原则 | 我们的应用 |
|-----------|-----------|
| **One canonical form** — 每个操作只有一种写法 | Compose DSL 的每个 primitive 应该语法固定，不允许 alias |
| **Explicit INTO naming** — 强制命名中间结果 | 每个 step 产出必须有 `result_name`，后续 step 引用它 |
| **STEP scaffolding** — 命名步骤块引导 LLM 序列生成 | 我们的 YAML 已有 `steps:` 列表，与此吻合 |
| **Verbose keywords > symbols** — LLM 更擅长自然语言关键词 | 用 `http_get` 不用 `→`，用 `filter_where` 不用 `|` |
| **Typed inputs in prompt** — 类型信息作为文档 | skill YAML 的 parameters 应该包含类型声明 |

### ❌ 不值得直接复用的部分

| 理由 | 详情 |
|------|------|
| **域不同** | Anka = 表格数据转换，我们 = 语音助手技能编排 |
| **Primitives 不重合** | 我们需要 `call_skill`, `say`, `remember`, `slot_fill` — Anka 没有 |
| **解释器太重** | 6,400 行 + Lark 解析器 vs 我们只需 ~200-300 行 YAML 解释器 |
| **Benchmark 不适用** | 100 个数据转换题和语音助手场景无关 |
| **社区活跃度** | 6 stars, 1 人, 2 天开发，无后续 |

### 🟡 风险提醒

Anka 的 +40pp 数据听起来很猛，但：
- 基于 **10 个** multi-step 任务（统计显著性存疑）
- Overall 只 +4.6pp
- 仅测了 2 个小模型（Haiku + 4o-mini），未测 Opus/Sonnet/GPT-4o 等强模型
- 强模型可能本身就不犯 variable shadowing 的错，优势可能消失

---

## 底线建议

**不要用 Anka 的代码，偷它的思想。**

我们 Phase 3 的 YAML spec 和 Phase 4 的 Compose DSL 应该遵循 Anka 验证的 4 条设计原则：
1. Canonical forms（每个 primitive 一种写法）
2. Explicit naming（每步结果必须命名）
3. Step scaffolding（步骤结构明确）
4. Verbose keywords（用词而非符号）

但 primitives、解释器、benchmark 全部自建，因为域完全不同。

---

## Sources

1. [BleBlo/Anka GitHub](https://github.com/BleBlo/Anka) — MIT, 6 stars, Python, 6400 LOC
2. [arXiv:2512.23214v1](https://arxiv.org/html/2512.23214v1) — Saif Al Mazrouei (UW-Madison), 2025-12-29
3. [Argmin AI Review](https://app.argminai.com/arxiv-dashboard/papers/2512.23214v1) — 独立评估，Evidence 0.80, 0 citations

## Methodology

搜索 3 条 query（exa web search），深度阅读 3 个源（GitHub README、arXiv 全文、Argmin 评估），交叉引用 skillfactory-research 的 Phase 4 Compose DSL 设计。
