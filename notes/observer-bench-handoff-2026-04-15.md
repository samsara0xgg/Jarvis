# Observer Bench 交接文档

**日期**: 2026-04-15
**接手者**: 下一个 Claude session
**当前状态**: ⚠️ 全量数据已跑完，但发现 2 个评测 bug 待修，等用户拍板修复方案
**总花费**: ~$5（含 fixture 生成 + pilot + full run）

---

## 一句话目的

为 Jarvis 中文语音助手的 **Observer 抽取组件** 选型 —— 8 个 LLM 候选，对中文家庭对话→ structured observation 抽取的能力做对照测试，输出可直接拍板的 F1/Recall/cost/latency 表。

**Observer 的地位**: 是 Mastra Observational Memory (OM) 范式的 **cold-path 关键组件**，准确度决定后续 LLM 读侧（v3 已测）能查到什么。

---

## 上下文（必读）

### Jarvis 项目背景
- Allen 的中文私人语音助手，部署 RPi5
- 主 LLM 已经由 v3 实验选定 = **`grok-4.20-0309-non-reasoning`** + Haiku fallback
  - 实验记录: `notes/bench-llm-v3-experiment-2026-04-14.md`
- 现在做 **写侧** Observer 选型，与读侧互补

### Mastra OM 调研已做透
- 6 个研究 pack 在 `notes/mastra-research-0[1-6]-2026-04-15.md`
- 结论: Mastra 默认 Observer = `google/gemini-2.5-flash`, temp 0.3
- **公共数据空白**: Gemini 2.5 Flash vs DeepSeek vs Grok 4.20 中文抽取无 head-to-head benchmark → 这次实验填这个空

### v3 复用的资产（零侵入）
- `scripts/bench_llm_v3.py` — 不修改
- observer_bench import v3 作纯 helper:
  - `v3.ModelSpec` (dataclass)
  - `v3.calc_cost` (三段计价)
  - `v3.extract_cache_metrics` (5 provider 抽取)
  - `v3.make_bust_prefix` (UUID + ns 时间戳防 cache 污染)
- v3 的 `MODEL_CATALOG` 不动，observer 自己加 `OBSERVER_EXTRA_MODELS` (Gemini 2.5 Flash + DeepSeek)
- 合并: `OBSERVER_CATALOG = v3.MODEL_CATALOG + OBSERVER_EXTRA_MODELS` = 14 models, 8 用作候选

---

## 设计核心决策（已锁定，不要回头改）

### 1. Tool Use / Function Call 统一路径（不用 prose / 不用 XML）
- spec §6.4: 所有 8 provider 都用 forced tool_choice
- per-provider 映射: Anthropic `tool_choice={"type":"tool"}`, OpenAI-compat `tool_choice={"type":"function"}`, Gemini `tool_config={"mode":"ANY"}`
- **理由**: 让 SDK 保证 JSON valid，对比公平不偏袒任何 provider
- **代价**: 偏离 Mastra 生产做法（XML prose），但本实验是**对比**不是**复刻**

### 2. Tool schema (`OBSERVER_TOOL_DEF`)
```python
{
    "name": "record_observations",
    "parameters": {
        "type": "object",
        "properties": {"observations": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": ["🔴", "🟡", "🟢", "✅"]},
                "time": {"type": "string", "pattern": "^[0-2][0-9]:[0-5][0-9]$"},
                "text": {"type": "string", "minLength": 4, "maxLength": 300}
            },
            "required": ["priority", "time", "text"]
        }}}
    }
}
```
**Gemini 不支持 `minItems`/`maxItems`/`pattern`/`minLength`/`maxLength`**: 已有 `_strip_unsupported()` 函数递归剥这些字段（commit `d3cc6d8`）。

### 3. 评测算法 (pure rule-based, no LLM-judge)
- `must_contain_any_of`: list of list, **OR of AND**
  - 外层 OR (任一 sub-list 命中即匹配)
  - 内层 AND (sub-list 内所有 keyword 必须出现在 obs.text)
- `must_not_contain_globally`: 全局禁词，任一 obs.text 含即 halluc=True
- **Greedy match**: 每个 expected 最多匹配 1 个 model_obs
- `tool_success`: 4 字段验证 (priority enum/time regex/text len ≥4/dict shape)

### 4. Pilot early-exit gate
- 5 fixtures × 8 models = 40 calls
- 如果某模型 `tool_success<80%` OR `F1<0.3` → 写入 `run_meta.json` 的 `pilot_pass: false`
- `--observer` 全量启动时自动 skip 这些模型，可 `--include-failed-pilot` 覆盖

### 5. Fixture 工作流
- Allen 写 `bench_fixtures/observer_cn/seeds.yaml` (剧本提纲)
- `--observer-generate` 调 Opus 生成 `fx_XXX.draft.json`
- Allen review + 编辑 → `mv fx_XXX.draft.json fx_XXX.json` 表示批准
- 脚本只读非 `.draft.json` 的文件
- **`bench_fixtures/observer_cn/` git tracked** (`.gitignore` 例外, commit `d3cc6d8`)

---

## 实际跑出来的数据（2026-04-15 14:18）

### Full run: 160 calls, 0 errors, $0.18, 6 min

**Top-line F1 (precision-penalized, see 🔴 BUG #1)**:

| Rank | Model | F1 | Precision | Recall | TTFT p50 | $/100 |
|---|---|---|---|---|---|---|
| 1 | claude-haiku-4-5-20251001 | 0.56 | 0.47 | 0.84 | 1907ms | $0.31 |
| 2 | grok-4.20-0309-non-reasoning | 0.55 | 0.42 | 0.87 | 3413ms | **$0.031** |
| 3 | deepseek-chat | 0.54 | 0.41 | 0.88 | 4812ms | $0.057 |
| 4 | gemini-2.5-flash | 0.51 | 0.41 | 0.86 | 2453ms | $0.066 |
| 5 | gemini-3-pro-preview | 0.51 | 0.40 | 0.82 | 8630ms | $0.211 |
| 6 | grok-4-1-fast-non-reasoning | 0.49 | 0.36 | 0.89 | 5042ms | $0.034 |
| 7 | groq llama-3.3-70b-versatile | 0.40 | 0.30 | 0.69 | 950ms | $0.090 |
| 8 | gpt-5-mini | 0.33 | 0.22 | 0.78 | 19670ms | $0.110 |

**按 Recall（实际更可信）**:
1. grok-4-1-fast 0.89  ← cheapest!
2. deepseek 0.88
3. grok-4.20 0.87
4. gemini-2.5-flash 0.86 (Mastra 默认)
5. haiku 0.84

### 各 category F1 王者
- **DeepSeek 中文 smart_home 0.82 碾压** ← Pack 06 中文 70.1% 实证
- DeepSeek correction 0.67 第一
- Haiku preference/temporal/emotion/multi_entity 4 类领先
- Grok 4.20 state_change/smart_home 第二
- Gemini 2.5 Flash state_change 0.63 意外强

### 数据文件
- CSV: `bench_results/observer_2026-04-15_1418/results.csv`
- Summary: `bench_results/observer_2026-04-15_1418/summary.md`
- Meta: `bench_results/observer_2026-04-15_1418/run_meta.json`

---

## 🔴 待修的问题（这是当前阻塞点）

### Bug #1: F1 precision-penalty 违反 spec（真 bug）
**位置**: `scripts/observer_bench.py` line ~857 (evaluate function)
```python
precision = len(matched_model) / len(model_obs) if model_obs else 0.0
```
**问题**: spec §7.3 写 "Extra noise 不扣分"，但代码用 `len(model_obs)` 作分母，extras 直接扣 precision，进而拉低 F1。

**症状**: 所有模型 Recall 0.69-0.89 都好，Precision 0.22-0.47 都低。GPT-5-mini avg 输出 5.0 obs vs expected 1.6 → precision 0.22。本质是"模型 chatty"被惩罚，但 spec 想说 chatty 是中性的。

**修法选项**:
- Option A: 接受现状 + 用 Recall 主导分析（不改代码，最快）
- Option B: 改 evaluate, 加 `f1_extras_neutral` 用 `precision = matched / max(matched, expected)` 不扣 extras（10 min, 不重跑数据，重渲染 summary）
- Option C: B + 同时修 fx_017 fixture 的 must_not_contain_globally（30 min）
- Option D: B + cosmetic 修 Gemini raw_args 显示

**用户没拍板**。最后讨论倾向 B+D。

### Bug #2: fx_017 假 hallucination（fixture 设计缺陷）
- fx_017 是 correction 类: "约后天 → 改大后天"
- `must_not_contain_globally` 含 "后天"
- 但模型正确记录纠正必然写 "**从后天**改到大后天"
- → 8 个模型几乎都被假阳性扣 5% halluc

**修法**: fx_017 从禁词移除"后天"（接受纠正必然提旧值），重新跑 evaluate（不重跑 API）。

### "Bug #3" Gemini proto raw_args（不是真 bug，cosmetic）
- CSV `model_output_raw` 列 Gemini 显示 `[<proto.MapComposite at 0x...>]`
- evaluate 实际用的是 `obs_list`（已转 dict），所以 F1/Recall 数据**正确**
- 只是 debug 阅读体验差
- 修法: `_parse_gemini_tool_call` 返回时把 obs_list 转 JSON 当 raw_args（不影响数据，纯显示）

---

## 当前推荐（如果不修 bug 直接拍板）

### 🥇 主 Observer: `xai/grok-4.20-0309-non-reasoning`
- F1 0.55, Recall 0.87, **$0.031/100**, TTFT 3.4s
- Recall 高 + 最便宜 + 没明显短板
- 月成本估算: 500 obs/天 = $5/月

### 🥈 Fallback: `anthropic/claude-haiku-4-5-20251001`
- F1 0.56 (第一), TTFT 1.9s (最快非 Groq)
- 不同 provider, xAI 故障时切
- 缺点: 贵 11x

### 🌶️ 黑马候选: DeepSeek V3.2
- F1 0.54, smart_home 0.82 (碾压)
- 国产, 中文 ReLE 70.1%
- 如果将来 Jarvis 把 smart_home pipeline 独立可考虑

### ❌ 出局
- **gpt-5-mini**: TTFT 19s + reasoning 关不掉，cold path 不可用
- **groq llama**: F1 0.40, recall 0.69 严重漏抓

---

## 关键 commits（代码考古用）

| SHA | 内容 |
|---|---|
| `f89177e` | spec v1 |
| `fdba7da` → `c38cda3` | spec v2 12 项 review 修复 → final |
| `59361af` | 实施计划 (17 tasks) |
| `cc73dbf` → `3ac3c12` | T1-T14 实现 (14 commits) |
| `529a5be` | 零侵入 v3 + matched_count + Gemini proto 防御 |
| `ea1b2ad` | generator prompt 修 must_contain_any_of 语义 |
| `d3cc6d8` | Pilot 3 fixes (Gemini schema strip + GPT-5 token budget + 多样 pilot) |

## 关键文件清单

```
docs/superpowers/specs/2026-04-15-observer-bench-design.md   ← spec (961 lines)
docs/superpowers/plans/2026-04-15-observer-bench.md          ← plan (2750 lines, 17 tasks)
scripts/observer_bench.py                                     ← 主代码 (~1500 lines)
tests/test_observer_bench.py                                  ← 34 tests passing
bench_fixtures/observer_cn/seeds.yaml                         ← 20 seeds (Allen 写的)
bench_fixtures/observer_cn/fx_001.json ~ fx_020.json          ← Allen 批准的 fixture
bench_results/observer_2026-04-15_1418/                       ← 全量数据 (CSV+summary+meta)
notes/observer-bench-handoff-2026-04-15.md                    ← 本文档
notes/mastra-research-0[1-6]-2026-04-15.md                    ← 6 个前置研究 pack
notes/bench-llm-v3-experiment-2026-04-14.md                   ← v3 (读侧) 实验记录
```

---

## 下一步该做的（优先级降序）

### 1. 让用户拍板修 bug 方案（推荐 B + D，10 min, 不花钱）
完成后:
- evaluate 加 extras-neutral F1
- summary.md 重新渲染
- 决策表用新 F1，可能换冠军（Grok 4.1 fast 因 recall 0.89 + 最便宜可能反超）

### 2. 写实验报告（参考 v3 的 `notes/bench-llm-v3-experiment-2026-04-14.md` 风格）
保存到 `notes/observer-bench-experiment-2026-04-15.md`，含:
- 方法论 (fixture 工作流, tool use, 评测算法)
- 8 模型完整数据
- 各 category 王者分析（特别 DeepSeek 在中文 smart_home 的发现）
- 复刻指南
- 决策建议 + config.yaml diff

### 3. 更新 Jarvis `config.yaml` 接入 Observer
- `observer.primary` = grok-4.20-0309-non-reasoning (或修完后的赢家)
- `observer.fallback` = haiku-4-5-20251001
- 需要 `XAI_API_KEY` (已有) + `ANTHROPIC_API_KEY`

### 4.（后续，不阻塞）扩展 fixture 集到 50 条
- 当前 20 条统计置信度有限（差 5pp 的模型基本不可分辨）
- 50 条把统计噪声压到 ~3pp
- Allen 时间成本: ~3 hr (写 30 个新 seed + review 30 个 draft)
- 跑成本: ~$0.50

---

## Allen 沟通风格（重要！）

从过往交流总结:
1. **速度 > 完美**: 别铺垫太多，别问太多问题，给 ABC 选项让他选
2. **诚实直接**: 不要对冲。"这是真 bug" / "这是假阳性" 直说
3. **中文混 English 自然**: 文件名/code 用英文，分析用中文
4. **强势反推**: 如果他错了直接说"实际查证后..."并贴数据，但最终接受他的决定
5. **每个动作给具体 cost**: "$0.18" 比"很便宜"好
6. **Skill 用法**: 喜欢 superpowers 流程 (brainstorm → plan → subagent-driven)
7. **不要主动建议 commit**: 只有用户明确说才 commit。push 永远不要

记忆系统位置: `/Users/alllllenshi/.claude/projects/-Users-alllllenshi-Projects-jarvis/memory/`

---

## API key 状态（截至 2026-04-15）

| Provider | 状态 | 备注 |
|---|---|---|
| ANTHROPIC | ✓ 有 | $20 余额，已用 ~$10 (v3 + observer fixture 生成) |
| OPENAI | ✓ 有 | sk-proj-*  |
| GEMINI | ✓ 有 | Google AI Studio key, billing 已开 ($250 cap) |
| GROQ | ✓ 有 | 免费 tier |
| XAI | ✓ 有 | $25 trial credits |
| DEEPSEEK | ✓ 有 | 这次新加，平台 platform.deepseek.com |

注意: Anthropic 中途出过 key 轮换问题，要重新 export。Gemini 出过 billing 切 project 的坑。

---

## 一句话状态

实验数据齐全，pilot+全量都跑通，需要修一个 evaluate 公式 bug 后 summary.md 重新生成，然后写报告 + 改 config.yaml 收尾。**全程不需要再调 API**（除非用户决定扩 fixture 到 50）。

下个 session 接手时：先看本文档 → 看 `bench_results/observer_2026-04-15_1418/summary.md` → 问用户 "Bug #1 选 B+D 修吗" → 修 → 写报告 → 改 config。
