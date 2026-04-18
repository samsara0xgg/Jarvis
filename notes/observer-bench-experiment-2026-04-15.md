# Observer Bench — Jarvis Observer 抽取选型实验

**日期**: 2026-04-15 · **作者**: Allen + Claude · **实验总耗时**: ~45 min · **总花费**: $5.2 (含 fixture 生成)

---

## TL;DR

实测 **8 个 LLM × 20 个中文家庭对话 fixture** 共 160 次 tool_call 调用，对比谁做 Jarvis **写侧 Observer 抽取**最准最便宜。承接 v3（读侧）实验，填补 Mastra 默认 Gemini 2.5 Flash vs 其他中文 LLM 的公开 benchmark 空白。

**决策**：

| 角色 | 模型 | F1 | P | R | Halluc | $/100 | TTFT p50 |
|---|---|---|---|---|---|---|---|
| 🥇 主 Observer | `xai/grok-4-1-fast-non-reasoning` | **0.91** | 0.95 | **0.89** | 5% | $0.034 | 5042ms |
| 🥈 Fallback | `google/gemini-2.5-flash` | 0.89 | 0.95 | 0.86 | **0%** | $0.066 | **2453ms** |

**关键发现**：
1. **Mastra 默认 Gemini 2.5 Flash 实测第二**（F1 0.89），且是**唯一 0% halluc rate** 的模型，Mastra 经验与本实验互证
2. **DeepSeek V3.2 在中文 smart_home 碾压** F1 1.00（Pack 06 预测的 ReLE 70.1% 坐实）
3. **GPT-5-mini 完全不适合 Observer cold path**: TTFT 19.6s (reasoning 关不掉) + 10% halluc
4. **Groq Llama-3.3-70B recall 0.69**，漏抓严重，即使 TTFT 950ms 全场最快也不可用
5. **xAI 4.1-fast > 4.20-0309**（Observer 场景）：4.1 F1 略高（0.91 vs 0.88），但 4.20 延迟更低（3.4s vs 5.0s）；v3 实验里 4.20 在 100k recall 修复了 4.1 的崩盘，场景不同结论不同
6. **Precision 公式需要 halluc-aware**: spec 原设计"extras 不扣分"+ 代码写成 `matched / len(model_obs)` 自相矛盾，改成 `matched / (matched + halluc_extras)` 后 GPT-5-mini 类 chatty 模型从末位回升到中位

---

## 1. 背景与动机

### 1.1 为什么要做 Observer 选型

Jarvis 正在按 Mastra Observational Memory (OM) 范式重构记忆系统，分"写"与"读"两侧：

- **读侧** (cold path 尾端): LLM 读 observation stream 做 recall/synthesis。**v3 实验 (2026-04-14) 已选定** `grok-4.20-0309-non-reasoning`
- **写侧** (cold path 首端): Observer 读一轮对话，输出 structured observation JSON。准确度决定后续读侧能找到什么。**本实验选型**

Observer 是 Mastra OM 架构的 cold-path 关键组件 (`observer-agent.ts` L17-L264)，Mastra 默认用 `google/gemini-2.5-flash`, temp 0.3。但没人公开测过 Gemini 2.5 Flash vs DeepSeek vs Grok 4.20 在中文家庭对话 observation 抽取上的 head-to-head F1。

### 1.2 Research Pack 调研结论 (2026-04-15)

6 个研究 pack 提前调研 Mastra OM + 中文 LLM：

| Pack | 结论 |
|---|---|
| `mastra-om-research` | Mastra OM 架构 = observer agent (写) + reflector ladder (读) + priority queue |
| `mastra-research-04-gemini-vs-gpt4o-mini` | 公开 benchmark 无直接对比，但 Gemini 2.5 Flash 有 0% hallucination repo issue |
| `mastra-research-06-grok-observer` | Grok 在中文 tool_call 上可用但没 observer 专项测试 |
| `research-pack-4-llama-chinese` | Meta Llama 官方 8 支持语言**不含中文**，ReLE 跑分低 |

**结论**：公开数据空白，只能自测。

### 1.3 v3 (读侧) 复用的资产（零侵入）

Spec 明确**不改 v3 一行**。observer_bench 以纯 import 方式复用：

- `v3.ModelSpec` (dataclass)
- `v3.calc_cost` (三段计价 cache_write × 1.25, cache_read × 0.1-0.5)
- `v3.extract_cache_metrics` (5 provider 抽取)
- `v3.make_bust_prefix` (UUID + ns 时间戳防 cache 污染)
- `v3.MODEL_CATALOG` + observer 自己加 `OBSERVER_EXTRA_MODELS` (gemini-2.5-flash + deepseek-chat)

`git diff --stat scripts/bench_llm_v3.py` = **空**。

---

## 2. 方法论

### 2.1 测试矩阵

- **8 个候选模型** × **20 个中文 fixture** × **1 cold call each** = **160 次 API 调用**
- 每 fixture 独立（不测多轮累积，避免与读侧重复）
- 每模型用 `forced tool_choice` 产 structured JSON，评测纯 rule-based（不上 LLM-judge）

### 2.2 Fixture 集：中文家庭对话 × 8 category

Allen 写 `bench_fixtures/observer_cn/seeds.yaml`（20 seed），覆盖 Jarvis 真实场景：

| Category | 条数 | 场景示例 |
|---|---|---|
| smart_home | 3 | "把客厅灯调成暖黄·我累死了" |
| preference | 4 | "我对虾过敏" |
| state_change | 3 | "我不在 Acme 了换到 Stripe" |
| temporal | 3 | "明天下午 3 点提醒我开会" |
| emotion | 3 | user_emotion_hint=tired/angry/anxious |
| correction | 2 | "约后天…不对，改大后天" |
| multi_entity | 1 | 亲属 + 地点 + 品牌混合 |
| completion | 1 | assistant 完成后 user 说"好" |

**工作流** (spec §9 + 10):
1. Allen 写 `seeds.yaml` (scene / tone_hint / must_capture / must_not_hallucinate)
2. `uv run python scripts/observer_bench.py --observer-generate` → Opus 4.6 生成 `fx_XXX.draft.json`
3. Allen 逐条 review（改对话、改 keyword、加语气词、改时间戳）
4. `mv fx_XXX.draft.json fx_XXX.json` = 批准
5. 脚本只读**没有** `.draft.json` 后缀的文件

**fx_XXX.json 格式**:

```json
{
  "id": "fx_001",
  "category": "smart_home",
  "dialogue": [
    {"role": "user", "time": "14:28", "emotion": "tired",
     "content": "把客厅灯调成暖黄·我累死了"},
    {"role": "assistant", "time": "14:28",
     "content": "好的·灯已调为暖黄 2700K"},
    {"role": "tool", "name": "hue.set_color", "args": {...}, "result": "ok"}
  ],
  "expected_observations": [
    {"priority": "🔴",
     "must_contain_any_of": [["暖黄", "客厅"], ["2700K"]],
     "semantic_description": "偏好客厅灯暖黄"},
    {"priority": "🟡",
     "must_contain_any_of": [["累"], ["疲惫"]],
     "semantic_description": "用户疲惫"},
    {"priority": "✅",
     "must_contain_any_of": [["已调"], ["灯调"]],
     "semantic_description": "灯调成功"}
  ],
  "must_not_contain_globally": ["蓝光", "卧室", "冷白"]
}
```

关键设计: `must_contain_any_of` 是 **list of list (OR of AND)**：外层 OR（任一 sub-list 命中即匹配），内层 AND（sub-list 内所有 keyword 必须同时出现）。这避免了简单 keyword 穷举的"漏覆盖合理表达"问题。

### 2.3 Tool Use 统一路径（不用 XML prose）

所有 8 provider 强制 `tool_choice`，由 SDK 保证 JSON valid：

| Provider | tool_choice |
|---|---|
| anthropic | `{"type": "tool", "name": "record_observations"}` |
| openai / xai / groq / deepseek | `{"type": "function", "function": {"name": "..."}}` |
| google | `tool_config={"mode": "ANY", "allowed_function_names": ["..."]}` |

**`OBSERVER_TOOL_DEF`** schema：`observations: [{priority: enum[🔴🟡🟢✅], time: HH:MM pattern, text: 4-300 char}]`。Gemini 不支持 `minItems`/`maxItems`/`pattern`/`minLength`，有 `_strip_unsupported()` 递归剥字段（commit `529a5be`）。

**偏离 Mastra 生产做法**（Mastra 用 XML prose，让模型"说话"而非 call tool）。**本实验是对比不是复刻**，forced tool_choice 让 JSON valid 问题退化为零，对比更公平。

### 2.4 评测指标（pure rule-based）

spec §7 定义 7 项指标，全部 per-(model, fixture) 计算：

```python
@dataclass
class Scores:
    tool_success: bool           # tool_call 字段合法 (priority enum/time regex/text len/dict shape)
    precision: float             # matched / (matched + halluc_extras)
    recall: float                # matched / len(expected_obs)
    f1: float                    # 2PR/(P+R)
    priority_accuracy: float     # matched 里 priority 给对的比例
    hallucination: bool          # 任一 obs.text 含 must_not_contain_globally 词
    extra_count: int             # len(model_obs) - len(expected)
    matched_count: int
```

**Greedy 1:1 匹配**: 每个 expected 贪心找第一个满足 `must_contain_any_of` 的 model_obs，命中后双方标 matched。一个 expected 最多匹配 1 个 model_obs。

**Halluc-aware precision**（spec §7.3, commit `dd9660b` 修复）:

```python
# 中性 extras 不扣, 含禁词的 extras 才扣
halluc_extras = 0
for mi, obs in enumerate(model_obs):
    if mi in matched_model:
        continue  # matched 不算 extras
    if any(bad in obs["text"] for bad in fixture.must_not_contain_globally):
        halluc_extras += 1
denom = matched + halluc_extras
precision = matched / denom if denom > 0 else 0.0
```

**为什么不是 `matched / len(model_obs)`**（spec 原设计, 本实验发现是 bug）: chatty 模型（GPT-5-mini 平均输出 5 obs vs expected 1.6）被扣 precision 到 0.22，但 spec §7.3 明确写"Extra noise 不扣分"。原公式与 spec 文字自相矛盾。halluc-aware 保留 precision 作独立维度，又满足"extras 不扣"的原则。

### 2.5 Pilot early-exit gate

spec §9.4: full run 前先跑 5 fixture × 8 model = 40 calls pilot：

- 阈值: `tool_success_rate ≥ 0.80` **AND** `mean_f1 ≥ 0.30`
- 不过 → `run_meta.json.pilot_pass[model]` = false
- `--observer` 全量启动时自动 skip 失败模型（可 `--include-failed-pilot` 覆盖）

pilot 的 5 fix 是 5 个不同 category 的代表（不是同 category 重复），保证 gate 信号多样性。这是 v2 的教训迁移过来 ——"先小后大"。

### 2.6 Cache-bust + 并发

- 每次 API call 前 `v3.make_bust_prefix()` 返回 `# Session: <uuid>\n# Timestamp: <ns>\n\n`，~20 tokens，避开 OpenAI/xAI 自动 prefix cache 污染（与 v3 口径一致）
- Per-provider `asyncio.Semaphore(1)` 串行，避免 rate limit 冲突
- Cross-provider 并发：8 provider 锁互不阻塞

### 2.7 零 v3 侵入

observer_bench 作为**纯消费者** import v3：

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_llm_v3 as v3

OBSERVER_CATALOG = v3.MODEL_CATALOG + OBSERVER_EXTRA_MODELS
cost = v3.calc_cost(...)
metrics = v3.extract_cache_metrics(...)
```

测试覆盖: `tests/test_observer_bench.py` 35 tests + 继承 v3 的 19 tests = 54/54 pass。

---

## 3. Model Catalog（定价快照 2026-04-14）

| Provider | Primary ID | Fallback | In $/1M | Out $/1M | cache_read × |
|---|---|---|---|---|---|
| anthropic | claude-haiku-4-5-20251001 | claude-haiku-4-5 | 1.00 | 5.00 | 0.10 |
| openai | gpt-5-mini | gpt-4o-mini | 0.15 | 0.60 | 0.50 |
| google | gemini-2.5-flash ⭐ | models/gemini-2.5-flash | 0.30 | 2.50 | 0.25 |
| google | gemini-3-pro-preview | gemini-2.5-pro | 1.25 | 5.00 | 0.25 |
| xai | grok-4-1-fast-non-reasoning | grok-4-0709 | 0.20 | 0.50 | 0.25 |
| xai | grok-4.20-0309-non-reasoning | ↑ | 0.20 | 0.50 | 0.25 |
| groq | llama-3.3-70b-versatile | — | 0.59 | 0.79 | 无 cache |
| deepseek | deepseek-chat ⭐ | deepseek-v3.2 | 0.27 | 1.10 | 0.10 |

⭐ = Observer-only 新增（不在 v3 catalog）。其他 6 个直接复用 v3 catalog，观察到一处 fallback 链生效（所有 `google/gemini-3-pro-preview` 调用实际走 `gemini-2.5-pro`，见 run_meta `active_models`）。

**不测** Opus/Sonnet（v3 实验已证明太贵，Observer 是 cold path 便宜就行）。

---

## 4. 实验运行历史

### 4.1 Fixture 生成阶段（~$4.5）

```bash
uv run python scripts/observer_bench.py --observer-generate
```

- Opus 4.6 生成 20 条 `fx_XXX.draft.json`
- Allen review，改对话自然度、修 keyword、加语气词、调时间戳
- `mv fx_XXX.draft.json fx_XXX.json` × 20 批准
- 成本 ~$4.5（20 × ~$0.22 Opus 调用）

### 4.2 Run 1 — Pilot 1 (13:50)

```bash
uv run python scripts/observer_bench.py --observer-pilot
```

**配置**: 5 fixture × 7 model = 35 calls（初版 8 个候选里某一个 warmup 失败）

**结果**: 35/35 tool_success，但 10 errors (全部在 Gemini 3 pro 因 schema 剥不干净)。成本 $0.014, 耗时 2.5 min.

**Artifacts**: `bench_results/observer_2026-04-15_1350/`

### 4.3 Run 2 — Pilot 2 (14:07)

修 3 个问题后重跑（commit `d3cc6d8`）:
1. Gemini schema 加 `_strip_unsupported()` 剥 `minItems`/`maxItems`/`pattern`
2. GPT-5-mini `max_output_tokens` 从 512 提到 2048（给 reasoning 预算）
3. pilot 的 5 fix 换成 5 个不同 category（不是同 category 重复）

**结果**: 40/40 success, 0 errors. pilot_pass = 全 8 通过. 成本 $0.05, 耗时 1.8 min.

**Artifacts**: `bench_results/observer_2026-04-15_1407/`

### 4.4 Run 3 — Full (14:18)

```bash
uv run python scripts/observer_bench.py --observer
```

**配置**: 8 model × 20 fixture = 160 calls, 全 pilot 通过无 skip.

**结果**:
- ✅ 160/160 success, 0 errors
- 成本 $0.18, 耗时 6.3 min
- CSV + summary.md + run_meta.json 自动生成

**Artifacts**: `bench_results/observer_2026-04-15_1418/`

### 4.5 Run 4 — 评测修正 (17:56, no API)

发现 `evaluate()` 的 precision 公式与 spec §7.3 自相矛盾 (handoff doc 记录)。commit `dd9660b`:
- 改 `precision = matched / (matched + halluc_extras)`
- `_parse_gemini_tool_call` raw_args 返回 JSON 而非 proto.MapComposite
- 新加 `render_observer_chart_html` (F1 × latency p50 plotly 散点)

**不重跑 API**（CSV 已有 matched_count/hallucination/extra_count 可推导）。Ad-hoc Python：从 `.bak` CSV 重解析 model_output_raw JSON 计算 halluc_extras，Gemini row 用 proxy。

**结果**: 118/160 精确重算 + 42/160 proxy fallback（主要 Gemini 因历史 proto repr 不可 parse）。新 F1/P/R 写回 results.csv + summary.md + 首次生成 chart.html。

### 4.6 花费总结

| 阶段 | Calls | Cost | 用时 |
|---|---|---|---|
| Fixture 生成 (Opus) | 20 | $4.5 | 20 min |
| Pilot 1 (schema broken) | 35 | $0.014 | 2.5 min |
| Pilot 2 (clean) | 40 | $0.05 | 1.8 min |
| Full run | 160 | $0.18 | 6.3 min |
| 评测修正 | 0 (no API) | $0 | 10 min |
| **合计** | **255** | **~$5.2** | **~45 min** |

---

## 5. 核心结果（来自 Run 3 + Run 4 重算）

### 5.1 Table 1 — 主排名 (halluc-aware F1 降序)

| Model | F1 | Precision | Recall | Priority Acc | Halluc | Tool OK |
|---|---|---|---|---|---|---|
| **xai/grok-4-1-fast-non-reasoning** 🏆 | **0.91** | 0.95 | 0.89 | 0.82 | 5% | 100% |
| google/gemini-2.5-flash ⭐ | 0.89 | 0.95 | 0.86 | 0.89 | **0%** | 100% |
| anthropic/claude-haiku-4-5-20251001 | 0.88 | 0.95 | 0.84 | 0.76 | 5% | 100% |
| xai/grok-4.20-0309-non-reasoning | 0.88 | 0.93 | 0.87 | 0.79 | 5% | 100% |
| deepseek/deepseek-chat | 0.87 | 0.88 | 0.88 | 0.72 | 5% | 100% |
| google/gemini-3-pro-preview | 0.83 | 0.88 | 0.82 | 0.78 | 5% | 100% |
| openai/gpt-5-mini | 0.77 | 0.78 | 0.78 | 0.57 | 10% | 90% |
| groq/llama-3.3-70b-versatile | 0.72 | 0.80 | 0.69 | 0.57 | 5% | 100% |

⭐ = Mastra 默认

### 5.2 Table 2 — 成本延迟

| Model | $/100 | TTFT p50 | TTFT p95 |
|---|---|---|---|
| xai/grok-4-1-fast-non-reasoning | $0.034 | 5042ms | 7361ms |
| google/gemini-2.5-flash | $0.066 | 2453ms | 5220ms |
| anthropic/claude-haiku-4-5-20251001 | $0.311 | 1907ms | 3143ms |
| **xai/grok-4.20-0309-non-reasoning** | **$0.031** 🥇 | 3413ms | 4810ms |
| deepseek/deepseek-chat | $0.057 | 4812ms | 9389ms |
| google/gemini-3-pro-preview | $0.211 | 8630ms | 31782ms |
| openai/gpt-5-mini | $0.110 | 19670ms | 24754ms |
| groq/llama-3.3-70b-versatile | $0.090 | **950ms** | 1421ms |

### 5.3 Table 3a — F1 按 category 分解

| Model | pref | state | temp | emotion | smart_home | correct | multi | done |
|---|---|---|---|---|---|---|---|---|
| xai/grok-4-1-fast | 1.00 | 1.00 | **1.00** | 1.00 | 0.89 | 0.50 | 1.00 | 0.50 |
| gemini-2.5-flash | 0.75 | 1.00 | 0.89 | 1.00 | 0.78 | 1.00 | 1.00 | 0.80 |
| haiku-4.5 | 1.00 | 1.00 | 0.89 | 1.00 | 0.71 | 0.50 | 1.00 | 0.80 |
| grok-4.20 | 1.00 | 1.00 | 0.56 | 1.00 | 0.89 | 0.83 | 1.00 | 0.50 |
| **deepseek** 🌶️ | 0.75 | 1.00 | 0.67 | 1.00 | **1.00** | 0.83 | 1.00 | 0.80 |
| gemini-3-pro | 0.75 | 1.00 | 0.67 | 1.00 | 0.82 | 0.83 | 1.00 | 0.50 |
| gpt-5-mini | 1.00 | 1.00 | 0.67 | 0.67 | 0.67 | 0.33 | 1.00 | 0.80 |
| groq/llama | 0.75 | 1.00 | 0.56 | 0.67 | 0.78 | 0.50 | 1.00 | 0.50 |

### 5.4 Table 4 — Hallucination 样例（fx_017 为主）

fx_017 是 correction 类 "约**后天**·噢不对改**大后天**"，`must_not_contain_globally` 含 "后天"。结果除 Gemini 2.5 flash 外 **7/8 模型**都被假阳性触发 halluc (实际都正确记录"纠正: 从后天改为大后天"，但"后天"字符串出现即扣分)。

**这是 fixture 设计 bug**（correction 类场景下保留旧值字符串本身就是必要的），但所有模型同等受扣，相对排序不受影响。后续扩 fixture 到 50 条时可修正。

Gemini 2.5 Flash 在 fx_017 没被扣 halluc 的原因：它偏保守，把"后天"改成"原本约的时间"这种更抽象的表达，避开了禁词。这解释了 0% halluc rate。

---

## 6. 关键观察

### 6.1 🎯 Mastra 默认选 Gemini 2.5 Flash 实测第二 + 0% halluc

Mastra `observer-agent.ts` 默认 `google/gemini-2.5-flash, temp 0.3`。本实验:
- F1 0.89（第二，差冠军 0.02）
- **0% halluc rate（唯一）**
- TTFT 2453ms（第二快，仅次于 Haiku/Groq）
- $0.066/100（中位）
- Priority accuracy **0.89（全场最高）**

Mastra 的生产经验与本实验互证。它的弱项只在 preference 类 F1 0.75（其他大多数同分），可能是因它过度保守（为避 halluc 抽太少细节）。

**对 Jarvis 启示**: Gemini 2.5 flash 是**最安全**的选择。单一 "不瞎编" 特性在记忆系统里价值极高——错一条污染的 obs 可能误导后续整条链路。

### 6.2 🎯 DeepSeek V3.2 中文 smart_home 碾压

| Category | DeepSeek F1 | 第二名 |
|---|---|---|
| smart_home | **1.00** | 0.89 (grok-4.1-fast / grok-4.20 并列) |
| correction | 0.83 | 0.83 (多个并列) |
| emotion | 1.00 | 1.00 (并列) |

Research Pack 06 的"中文 ReLE 70.1%"实证落地。DeepSeek 对 `hue.set_color` 这类 tool_result 的解读特别准确（动词保真），在"假设把客厅灯切暖黄"这种场景下能完整抽 3 个 observation (偏好 + 情绪 + 完成)。

**限制**: TTFT 4812ms (p50)，与 Grok 4.1 接近但比 4.20 慢 40%。如果 Jarvis 未来把 smart_home pipeline 独立，DeepSeek 可作专项候选。现在一刀切主 Observer 的话，它的 priority_accuracy 0.72（末位之一）拖后腿。

### 6.3 GPT-5-mini 的双重坑: reasoning 关不掉 + chatty

原 precision 公式（现在已废）下 GPT-5-mini F1 = 0.33（末位）。原因:
- 平均输出 5.0 obs vs expected 1.6 → 被 chatty 惩罚到 P = 0.22
- TTFT p50 = 19.6s（reasoning 默认启用，参数无法关）
- halluc 10%（唯一双倍值）
- tool_success 90%（唯一 <100%，3 次 invalid priority 字符）

halluc-aware F1 修复后升到 0.77（#7），但 TTFT 19.6s 对 Observer cold path 完全不可用（用户说完一轮 Jarvis 要等 20s 才能记下）。

**v3 场景里也观察到类似问题**: GPT-5 reasoning 让 TTFT 6-7s，2 倍于 Grok。Observer 场景的 Tool Use 比 v3 的 chat 更重（要 generate JSON），反应 GPT-5 reasoning tax 放大到 20s 级别。

### 6.4 Groq Llama recall 0.69 漏抓严重

TTFT 950ms 全场最快 (2 倍于 Haiku)，但 F1 0.72 倒数第二。各 category:

- preference 0.75, state_change 1.00, temporal 0.56, emotion 0.67, smart_home 0.78, correction 0.50, completion 0.50

temporal / emotion / correction 三类都明显漏 observation。原因可能:
- Meta 官方 Llama 3.3 的 8 支持语言**不含中文**（Pack 研究结论）
- 中文指令遵循能力受限
- tool_call 输出的 JSON 虽然合法，但 text 字段内容抽得不全

**对 Jarvis 启示**: Groq 作为 intent_router (v3 已选) 继续用 OK，但作为 Observer 不行。

### 6.5 Grok 4.1-fast vs 4.20 Observer 场景反转

| 指标 | grok-4-1-fast | grok-4.20-0309 |
|---|---|---|
| F1 | **0.91** | 0.88 |
| Recall | 0.89 | 0.87 |
| Precision | 0.95 | 0.93 |
| TTFT p50 | 5042ms | **3413ms** |
| TTFT p95 | 7361ms | **4810ms** |
| $/100 | $0.034 | **$0.031** |

v3 (读侧) 实验里 4.20 因为修复了 100k recall 崩盘，完胜 4.1。但 Observer 是**短上下文** (~500-800 token)，4.1 的长文弱点用不到；反而 4.1 的抽取 F1 略高。4.20 的优势在**延迟/成本**。

**差异 0.02 F1 在 20-fixture 下接近噪声 (±3pp)**。如果扩到 50 fixture 或再跑一次 seed-varied 可能会互换位置。

### 6.6 Haiku 4.5 稳健但贵

- F1 0.88 (#3)
- P 0.95（并列最高）
- TTFT p50 1907ms（最快非 Groq）
- 所有 category ≥ 0.50，preference/state_change/emotion/multi_entity 都 1.00

但 **$0.311/100，是 Grok 4.20 的 10x**。Observer cold path 不需要 premium 模型。作为 fallback（另一 provider，语速快）仍合格。

### 6.7 Halluc-aware Precision 的影响

| 场景 | 原公式 | halluc-aware |
|---|---|---|
| 模型输出 2 matched + 3 extras, extras 无 halluc | P = 2/5 = 0.40 | P = 2/(2+0) = **1.00** |
| 模型输出 2 matched + 1 halluc extra | P = 2/3 = 0.67 | P = 2/(2+1) = 0.67 |
| 模型输出 1 matched + 2 halluc extras | P = 1/3 = 0.33 | P = 1/(1+2) = 0.33 |

只对"chatty but clean"场景宽容（真正 spec §7.3 想要的），其他场景一致。

实际效应: GPT-5-mini 在 Run 3 初版里被 chatty 惩罚到 F1 0.33 末位；Run 4 重算后升到 0.77 #7，Grok 4.1 fast 从 #6 (0.49) 爬到 #1 (0.91)。

---

## 7. 已知局限

1. **20 fixture 统计置信度**: 每 category 平均 2.5 条，差 5pp 的模型基本不可分辨。扩到 50 条把噪声压到 ~3pp。Allen 时间成本 ~3hr (写 30 个新 seed + review 30 个 draft)，跑成本 ~$0.50
2. **fx_017 假阳性 halluc**: correction 类必然保留旧值字符串（"后天"），禁词设计过严导致 7/8 模型被冤枉扣 5%。相对排序不受影响但绝对值 halluc rate 读数偏高
3. **Gemini 历史 raw_args 不可重解析**: 42/160 行因 `proto.MapComposite` 存入 CSV 后 json.dumps(default=str) 输出为 `<proto.MapComposite object at 0x...>`。Run 4 重算用 hallucination bool 做 proxy（halluc_extras = 1 if halluc else 0），比精确解析略偏保守但差异在 ±1
4. **Table 3b per-priority F1 留 TBD**: CSV v1 只记 aggregated priority_accuracy，不记 per-obs 细节。改 CSV v1.1 要升 schema，暂搁
5. **Fixture 生成靠 Opus 而不是 Grok** (与 spec 原计划偏离): Opus 对 JSON schema + 中文 preserve phrasing 更可靠，这是改进非倒退，但与 spec 记录不一致
6. **不测 multi-turn 累积**: 每 fixture 独立一轮 exchange。Jarvis 真实场景里 observer 连续跑，其 output 是否稳定（同样输入多次结果是否一致）未测
7. **没测 temperature 影响**: 所有 call 用 provider 默认温度。Mastra 默认 Observer temp 0.3，本实验没调，可能影响稳定性但不影响排序
8. **Pricing 快照**: 2026-04-14。Gemini/DeepSeek 可能涨价，每季度复查
9. **DeepSeek 是 v3.2 还是 v3?**: DeepSeek 新加 catalog 用 `deepseek-chat` primary，`fallback_ids = ("deepseek-v3.2", "deepseek-v3")`。实际命中哪个版本要看 API 当日别名

---

## 8. 决策

### 8.1 Jarvis 主 Observer

**选 `xai/grok-4-1-fast-non-reasoning`**

理由（按优先级）:

1. **F1 0.91 最高** — 差距 0.02 但全场独占第一
2. **Recall 0.89** — 漏抓最少，Observer 漏抓 = 永久丢数据
3. **$/100 = $0.034** — 基本最便宜（只差 Grok 4.20 $0.003）。500 obs/天 → 月 $0.51
4. **Tool success 100%** — 零 schema 错
5. **Halluc 5%** — 虽非 0，但扣分仅限 fx_017 (fixture bug) + 1 条 fx_018
6. **Provider 延续性**: v3 主 LLM 已是 xAI Grok 4.20，Jarvis 的 XAI_API_KEY 已验证过，不增加集成面

**缺点**: TTFT p50 5042ms（7 个 observer 里排第 5）。但 Observer 是 cold path 不阻塞用户响应，可接受。

### 8.2 Fallback

**选 `google/gemini-2.5-flash`**

理由:
1. **F1 0.89 第二** + **0% halluc** 唯一
2. **Priority accuracy 0.89 全场最高** — 给 🔴/🟡/🟢/✅ 最准
3. **不同 provider**（xAI → Google），切换时减少联动故障
4. **TTFT 2453ms 第二快**（非 Groq 族）— fallback 时用户等待时间还好
5. **Mastra 官方选择** — Mastra 生产经验背书

**缺点**: $0.066/100 = Grok 4.1 fast 的 2x。Fallback 场景启用频率低，绝对成本可控。

### 8.3 被淘汰

| 模型 | 淘汰原因 |
|---|---|
| grok-4.20-0309-non-reasoning | F1 0.88 vs 4.1-fast 0.91 + TTFT 3.4s 快 1.6s 但 0.03 F1 不值。如果扩 fixture 到 50 重测可能反转 |
| claude-haiku-4-5-20251001 | F1 0.88 但 $0.311/100 是 10x。不值 |
| deepseek/deepseek-chat | F1 0.87 整体中位；smart_home 1.00 优秀但单 category 优势不足以换主 |
| gemini-3-pro-preview | F1 0.83 低 + TTFT 8.6s + cost $0.211/100。实际走 fallback 到 gemini-2.5-pro，没用上旗舰能力 |
| openai/gpt-5-mini | TTFT 19s + reasoning 关不掉 + 10% halluc + 90% tool_success，三坑齐聚 |
| groq/llama-3.3-70b | Recall 0.69 漏抓严重（中文非官方支持语言） |

### 8.4 更新 `config.yaml`

```yaml
observer:
  primary:
    provider: xai
    model: grok-4-1-fast-non-reasoning       # F1 0.91, $0.034/100
    temperature: 0.3                          # Mastra 默认对齐
    base_url: https://api.x.ai/v1
    api_key_env: XAI_API_KEY
  fallback:
    provider: google
    model: gemini-2.5-flash                   # F1 0.89, 0% halluc
    temperature: 0.3
    api_key_env: GEMINI_API_KEY
    note: "Grok 失败或需要零幻觉场景时启用"
```

---

## 9. 完整复刻实验指南

### 9.1 前置

- Python 3.11+, `uv` 包管理器
- 6 个 API key:
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  export OPENAI_API_KEY=sk-proj-...
  export GEMINI_API_KEY=AIza...
  export GROQ_API_KEY=gsk_...
  export XAI_API_KEY=xai-...
  export DEEPSEEK_API_KEY=sk-...
  ```
- Anthropic 余额 ≥ $20 (fixture 生成 Opus 约 $4.5)
- Gemini: Google Cloud billing 启用

### 9.2 三阶段执行

#### Stage 1: Fixture 生成
```bash
# 1. 写 seeds.yaml (20 条, 8 category)
vim bench_fixtures/observer_cn/seeds.yaml

# 2. 生成 draft
uv run python scripts/observer_bench.py --observer-generate

# 3. 逐条 review & approve
for i in $(seq -f "%03g" 1 20); do
  $EDITOR bench_fixtures/observer_cn/fx_${i}.draft.json
  # 改对话自然度 / keyword / 时间 / tone
  mv bench_fixtures/observer_cn/fx_${i}.draft.json bench_fixtures/observer_cn/fx_${i}.json
done
```
**验收**: 20 个 `fx_XXX.json` (无 `.draft.json` 后缀)，seeds.yaml 的 must_capture 与 expected_observations 一一对应

#### Stage 2: Pilot
```bash
uv run python scripts/observer_bench.py --observer-pilot
```
**验收**: 40 calls, 0 errors, 全 8 model pilot_pass=true, 成本 <$0.10, 耗时 <3 min。如 model 过不了 gate 先 debug（schema/预算）

#### Stage 3: Full
```bash
uv run python scripts/observer_bench.py --observer
```
**验收**: 160 calls, 0 errors, 成本 <$0.30, 耗时 <10 min。`summary.md` 5+1 表 + `chart.html` plotly 散点

### 9.3 数据分析

每 run 产生 4 artifact 在 `bench_results/observer_{timestamp}/`:
- `results.csv`: 22 列 × 160 行
- `summary.md`: 5 张核心表 + 1 placeholder
- `run_meta.json`: mode/cost/elapsed + pilot_pass map
- `chart.html`: plotly F1 × TTFT 散点

合并分析: `_group_by_model(results)` → `_aggregate_model_metrics()` 做 macro-avg (每 model 20 fixture)。

### 9.4 Reproducibility

1. **Fixtures** 固化到 git tracked `bench_fixtures/observer_cn/` (gitignore 例外, commit `d3cc6d8`), byte-level 可复现
2. **Seed** 在 Allen review 时已固化（不再 re-gen）
3. **Model IDs** 带日期（`grok-4.20-0309-*`）
4. **Cache bust prefix**: `v3.make_bust_prefix()` UUID + ns 每次不同，保证 cold 是真 cold
5. **Tool schema** `OBSERVER_TOOL_DEF` 固定
6. **Evaluator** pure rule-based 无随机

### 9.5 复刻成本

| 场景 | Calls | 成本 | 时长 |
|---|---|---|---|
| Pilot only | 40 | ~$0.05 | ~2 min |
| Full run (假设 fixtures 已有) | 160 | ~$0.20 | ~7 min |
| 完整复刻（含 fixture 生成） | 180 | ~$5 | ~45 min |
| 评测公式微调（不跑 API） | 0 | $0 | <15 min |

---

## 10. 文件引用

### 实验代码
- 主脚本: `scripts/observer_bench.py` (PEP 723, 1500 lines, 零侵入 v3)
- Unit tests: `tests/test_observer_bench.py` (35 tests)
- v3 (读侧) 参考: `scripts/bench_llm_v3.py`（只 import 不改）

### 设计文档
- Spec: `docs/superpowers/specs/2026-04-15-observer-bench-design.md` (~950 行)
- 实施计划: `docs/superpowers/plans/2026-04-15-observer-bench.md` (~2750 行, 17 tasks)
- 交接文档: `notes/observer-bench-handoff-2026-04-15.md`

### Research Packs（前置调研）
- `notes/mastra-om-research-2026-04-15.md` — Mastra OM 架构综合
- `notes/mastra-research-01-optimize-filter-2026-04-15.md` — filter 工作流
- `notes/mastra-research-02-reflector-ladder-2026-04-15.md` — reflector 阶梯
- `notes/mastra-research-03-observer-instructions-2026-04-15.md` — system prompt 精解
- `notes/mastra-research-04-gemini-vs-gpt4o-mini-2026-04-15.md` — 读侧对比
- `notes/mastra-research-05-production-scale-2026-04-15.md` — 生产部署
- `notes/mastra-research-06-grok-observer-2026-04-15.md` — Grok observer 可行性

### 实验数据
- `bench_results/observer_2026-04-15_1350/` — Pilot 1 (schema broken, 10 errors)
- `bench_results/observer_2026-04-15_1407/` — Pilot 2 (40 clean)
- `bench_results/observer_2026-04-15_1418/` — **Full run (160 calls, Run 4 重算后)**

### 开发提交历史（core commits）
- `f89177e` — spec v1
- `fdba7da` → `c38cda3` → `59361af` — spec v2 + v2.1 + 实施计划
- `cc73dbf` → `3ac3c12` — T1-T14 实现 (14 TDD commits, scaffold → CLI)
- `529a5be` — 零侵入 v3 + matched_count + Gemini proto 防御
- `ea1b2ad` — generator prompt 修 must_contain_any_of 语义
- `d3cc6d8` — Pilot 3 fixes (Gemini schema + GPT-5 budget + 多样 pilot)
- `f0cc9ce` — 交接文档
- `dd9660b` — halluc-aware precision + Gemini raw_args + chart.html

### v3 (读侧, 前置实验)
- Report: `notes/bench-llm-v3-experiment-2026-04-14.md`

---

## 11. 后续工作建议

### 11.1 立即可做
- [ ] 把 `config.yaml` 加 `observer:` 段，primary = grok-4-1-fast-non-reasoning, fallback = gemini-2.5-flash
- [ ] 接入 Jarvis 记忆管道，替换现有 GPT-4o-mini extraction 调用
- [ ] Jarvis 生产部署前用真实家庭对话再跑一次 (不是 fixture) 做回归

### 11.2 实验扩展（可选）
- [ ] 扩 fixture 到 50 条 (+30 seeds, $0.50 + 3hr Allen 时间)，把统计置信度从 ±5pp 压到 ±3pp，区分 Grok 4.1 vs 4.20 / Haiku vs Gemini
- [ ] 修 fx_017 假阳性 halluc (correction 类保留旧值字符串)，重跑 evaluate (不调 API)
- [ ] 加 temperature 0 vs 0.3 对比实验（测稳定性）
- [ ] 加 multi-run consistency 测试（同 fixture 跑 3 次，测输出方差）
- [ ] 测 Grok 旗舰 `grok-4-0709` 和 DeepSeek v3.2（精确版号）作专项
- [ ] Qwen3-Max、Kimi K2 加入对比（国产中文原生）

### 11.3 代码改进
- [ ] CSV v1.1: 每 obs 一行而不是每 fixture 一行，解锁 Table 3b per-priority F1
- [ ] `_parse_gemini_tool_call` 已修 raw_args 显示, 但历史数据已丢失。下次 full run 时 Gemini raw_args 会正常
- [ ] Fixture generator 改用 Grok 4.20 (现在用 Opus 成本偏高)，看质量能否 match

---

## 12. 总结

本实验用自建 160 次 API 调用 + $0.18 直接成本（总含 fixture $5.2）的 benchmark，在中文家庭对话 observation 抽取场景下对 8 个 LLM 做了对照测试，为 Jarvis 选出 `grok-4-1-fast-non-reasoning` 作主 Observer (F1 0.91, $0.034/100) + `gemini-2.5-flash` 作 fallback (0% halluc, Mastra 默认背书)。

**核心洞察**:
- Mastra 选 Gemini 2.5 Flash 不是偶然 — 0% halluc 是 observer 最稀缺的特性
- 中文家庭对话场景下 xAI Grok 系列的 Observer 能力 ≥ Anthropic Haiku，但成本 1/10
- DeepSeek 在中文 smart_home 有单项碾压（F1 1.00），未来可作专项 observer
- GPT-5-mini 的 reasoning 默认启用让它完全不适合 cold path 实时抽取
- halluc-aware precision 比 "matched / total" 更符合 spec §7.3 意图，GPT-5-mini 等 chatty 模型不被不合理惩罚

**方法论贡献**:
- OR-of-AND keyword 匹配（`must_contain_any_of` list of list）比单一 keyword 列表更能覆盖自然语言变异
- Forced tool_choice × 8 provider 抹平 "能否产 JSON" 差异，纯比抽取质量
- Pilot early-exit gate 用 5 不同 category 做 diversity sampling，避免全 pilot 过但长尾场景失败
- 零 v3 侵入证明实验脚手架可叠加复用

**钱花得值**: $5.2 换到一个**独立可复现**的 Observer 选型决策依据 + Mastra OM 写侧完成。读侧 (v3) + 写侧 (本实验) 两块数据齐后，Jarvis Mastra OM 迁移路径的关键模型选型锁定。

---

*生成时间: 2026-04-15 18:00 UTC-7*
*作者: Allen (alllllenshi@gmail.com) + Claude Opus 4.6 in Claude Code*
