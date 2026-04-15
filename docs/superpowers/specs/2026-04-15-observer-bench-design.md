# Observer Bench — Jarvis Observer 抽取基准设计规格

**日期**: 2026-04-15
**作者**: Allen + Claude (brainstorm)
**依赖**: `scripts/bench_llm_v3.py`（读侧实验，2026-04-14 完成）
**目标**: 扩展 v3 测写侧——中文家庭对话 → structured observation 抽取能力
**背景**: Jarvis 基于 Mastra Observational Memory (OM) 范式重设计记忆系统，Observer 是 cold-path 关键组件，准确度决定后续读侧质量

---

## 1. 动机

### 1.1 Jarvis Observer 在做什么
Observer 读一段对话（user + assistant + tool_calls），输出 structured observation JSON。这些 observation 进入 memory 作为**将来 LLM 读取的唯一信息源**。

**范式参考**: Mastra OM (`mastra-ai/mastra` 源码，commit `a179a1dbb3c`), 默认 Observer = `google/gemini-2.5-flash @ temp 0.3`。

### 1.2 为什么要自测
已有研究结论（`notes/mastra-research-*.md`）:
- **Pack 06**: Grok-4.1-fast 中文抽取 47.6% 垫底，但 **Grok 4.20 / Gemini 2.5 Flash 中文 Observer 能力无公开 head-to-head**
- **Pack 04**: Gemini 2.5 Flash vs GPT-4o-mini 中文 structured extraction 无直接对比
- **Pack 03**: Mastra 官方说 Claude 不支持做 Observer，但未提供量化证据
- **Mastra LongMemEval 84%**: 基于英文，中文表现未知

**结论**: 公共数据无法支持 Jarvis Observer 选型决策，必须自测。

### 1.3 与 v3 的区别
- v3 测**读侧**: LLM 在长 notes 里找针（recall）
- 本实验测**写侧**: LLM 从对话抽 observation（extraction）
- 两侧互补，合在一起画出 Jarvis 记忆系统的完整 LLM 能力图

---

## 2. 目标 & 非目标

### 2.1 目标
- 输出一张可直接拍板的表：**哪个模型做中文 Observer 的 F1 最高 / 延迟可接受 / 成本最低**
- 覆盖 7 个候选 Observer 模型（fast/cheap 档位）
- 使用统一 Tool Use 调用路径，消除 API feature gap 带来的不公平
- 总耗时 <30 min 总成本 <$6

### 2.2 非目标（YAGNI）
- ❌ LLM-as-judge 评分（引入评测方差，不可复现）
- ❌ Multi-turn 累积测试（每 fixture 独立，单轮 extraction）
- ❌ Reflector / Compressor 测试（本实验聚焦 Observer 一个角色）
- ❌ 英文 fixture（Jarvis 用户全中文）
- ❌ 长 context observation stream 测试（这是 v3 读侧场景，不重复）
- ❌ 复现 Mastra 论文数字（我们用 function call，Mastra 用 XML prose；方法不同，对比无意义）

---

## 3. 文件结构

```
~/Projects/jarvis/
├── scripts/
│   ├── bench_llm_v3.py                    # 现有 1390 行，不改
│   └── observer_bench.py                  # 新增 ~600 行，import v3 的 providers/cost/retry
│
├── bench_fixtures/
│   ├── fake_notes_*.txt                   # v3 读侧 fixtures (.gitignore)
│   └── observer_cn/                       # ★ 新增，git tracked
│       ├── seeds.yaml                     # Allen 写的主题清单
│       ├── fx_001.json                    # Allen 批准的 fixture
│       ├── fx_002.json
│       └── ...
│
└── bench_results/
    └── observer_<timestamp>/
        ├── results.csv                    # 每 (model, fixture) 一行
        ├── summary.md                     # 5 张评测表
        ├── run_meta.json                  # mode/cost/elapsed
        └── chart.html (可选)              # F1 vs latency scatter
```

**关键**: `observer_cn/` **全部 git tracked**，跟 `fake_notes_*.txt` 不同。fixture 是跨机器复现的资产，必须入库。

`scripts/observer_bench.py` **不修改** v3，只 `import bench_llm_v3 as v3` 复用 providers / cost / retry。所有 Observer-specific 代码在新文件里。

---

## 4. 架构总览

```
observer_bench.py
├── Fixture 管理
│   ├── Seed (dataclass)
│   ├── Fixture (dataclass)
│   ├── load_seeds(path: Path) → list[Seed]
│   ├── generate_fixture_from_seed(seed, opus_client) → Fixture
│   │   └── 调 claude-opus-4-6 生成 dialogue + draft ground truth,
│   │       stdout 打印给 Allen review, Allen 改完手动写入 fx_XXX.json
│   └── load_approved_fixtures(dir: Path) → list[Fixture]
│
├── Observer 调用（复用 v3）
│   ├── OBSERVER_SYSTEM_PROMPT (str, 英文骨架 + 中文输出要求)
│   ├── OBSERVER_TOOL_DEF (dict, JSON schema for record_observations)
│   ├── build_observer_prompt(fixture) → (system, user_message)
│   ├── build_tool_call_kwargs(provider) → dict (per-provider tool_choice)
│   └── call_observer(spec, fixture) → ObserverCall
│       └── 基于 v3 的 call_api_with_retry, 但强制 tool_choice
│
├── 评测（pure rule-based, no LLM-judge）
│   ├── Scores (dataclass)
│   ├── evaluate(observer_output, fixture) → Scores
│   └── 包含: precision, recall, f1, priority_accuracy,
│           hallucination, tool_success, extra_count
│
├── 输出
│   ├── ObserverResult (dataclass, CSV 行)
│   ├── write_observer_csv(results, dir)
│   ├── render_observer_summary(results, dir) → 5 张表
│   └── 可选: render_f1_latency_scatter (复用 v3 plotly)
│
└── CLI
    ├── --observer             (全量, 读 observer_cn/*.json)
    ├── --observer-pilot       (只读 observer_cn/fx_00{1..5}.json)
    ├── --observer-generate    (读 seeds.yaml, 调 Opus 生成草稿到 stdout)
    ├── --model <id>           (单模型, 跟 v3 行为一致)
    └── --dry-run / --output-dir / --with-chart
```

---

## 5. Fixture Schema

### 5.1 seeds.yaml（Allen 编写 → git 入库）

```yaml
# bench_fixtures/observer_cn/seeds.yaml
#
# 20 条 seed 分布要求（pilot 先写 5 条 cover 主要类别）:
# · 偏好 (preference): 4 条 (食物/颜色/风格/品牌)
# · 状态变更 (state change): 3 条 (工作/住址/关系)
# · 时间锚定 (temporal): 3 条 (提醒/约定/deadline)
# · 情感 (emotion): 3 条 (疲惫/愤怒/开心/焦虑)
# · 智能家居 (smart home + tool): 3 条
# · 纠正/覆盖旧信息 (correction): 2 条
# · 多实体 (亲属/地点/品牌): 1 条
# · 任务完成信号 ✅: 1 条

- id: fx_001
  scene: "智能家居 + 疲惫语气"
  user_emotion_hint: tired
  tone_hint: "口语化·带抱怨·短句·允许粗口"
  dialogue_length_hint: "3-4 turns"
  must_capture:
    - "偏好: 客厅灯暖黄色 (🔴)"
    - "情绪: 用户疲惫 (🟡)"
    - "完成: 灯调节任务 (✅)"
  must_not_hallucinate:
    - "蓝光"
    - "冷白"
    - "卧室"

- id: fx_002
  scene: "食物过敏声明"
  user_emotion_hint: neutral
  tone_hint: "平静陈述·可能伴随上下文（菜谱、点餐）"
  dialogue_length_hint: "2-3 turns"
  must_capture:
    - "过敏: 虾 (🔴, 不可变)"
  must_not_hallucinate:
    - "喜欢虾"
    - "不喜欢"  # 过敏≠不喜欢, 语义必须准
    - "鸡蛋"  # 不相关的过敏

# ... fx_003 ~ fx_020
```

**字段语义**:
- `tone_hint`: ★ 关键防污染字段 — 让 Opus 别用教科书普通话写"我感到十分疲惫"，要写"我累死了"
- `dialogue_length_hint`: 给 Opus 参考，最终实际可能 ±1 轮
- `must_capture`: Allen 的设计意图，review 时作为"观察员还该抓什么"的 checklist
- `must_not_hallucinate`: 全局禁用词，模型输出里**任何** observation 出现即触发 halluc=True

### 5.2 fx_XXX.json（Opus 生成 → Allen 改 → git 入库）

```json
{
  "id": "fx_001",
  "seed_id": "fx_001",
  "generated_by": "claude-opus-4-6",
  "generated_at": "2026-04-15T10:30:00Z",
  "approved_by": "allen",
  "approved_at": "2026-04-15T11:00:00Z",
  "dialogue": [
    {
      "role": "user",
      "time": "14:28",
      "emotion": "tired",
      "content": "把客厅灯调成暖黄·我累死了"
    },
    {
      "role": "assistant",
      "time": "14:28",
      "content": "好的·灯已调为暖黄 2700K"
    },
    {
      "role": "tool",
      "name": "hue.set_color",
      "args": {"room": "living", "color": "#FFB36B", "kelvin": 2700},
      "result": "ok"
    }
  ],
  "expected_observations": [
    {
      "priority": "🔴",
      "must_contain_any_of": [
        ["暖黄", "客厅"],
        ["客厅", "暖色"],
        ["偏好", "暖黄"]
      ],
      "semantic_description": "用户偏好客厅灯暖黄色"
    },
    {
      "priority": "🟡",
      "must_contain_any_of": [
        ["累"],
        ["疲惫"],
        ["撑不住"]
      ],
      "semantic_description": "用户语气疲惫"
    },
    {
      "priority": "✅",
      "must_contain_any_of": [
        ["已调", "灯"],
        ["灯", "完成"],
        ["灯光", "设置完毕"]
      ],
      "semantic_description": "灯调节任务完成"
    }
  ],
  "must_not_contain_globally": ["蓝光", "冷白", "卧室"]
}
```

**Schema 关键解读**:

- `must_contain_any_of`: **list of list** — 外层 OR, 内层 AND
  - 只要模型 observation.text 命中**任何一个子 list 的全部关键词**就算匹配该 expected
  - 例: `[["暖黄","客厅"], ["偏好"]]` = "暖黄 AND 客厅" OR "偏好"
- `must_not_contain_globally`: **integer OR**（任一词出现即触发），针对模型的**全部** observation 输出，不限于某一条
- `semantic_description`: **仅供 Allen 人审核 borderline 情况**，代码**不使用**（no LLM-judge）
- `generated_by` / `approved_by`: 可追溯，后续换 Opus 版本或 Allen 改 fixture 时方便 diff

### 5.3 Fixture 生成工作流

```
Step 1 · Allen 写 seeds.yaml (pilot 阶段 5 条)          [30 min]
Step 2 · uv run python scripts/observer_bench.py --observer-generate
         → 对每条 seed 调 claude-opus-4-6
         → 生成 dialogue + draft expected_observations
         → stdout 打印 JSON, Allen 复制审核                 [5 min 生成 + 30 min review]
Step 3 · Allen 改完 dialogue/ground truth, 手动写入
         bench_fixtures/observer_cn/fx_XXX.json          [上一步内]
Step 4 · uv run python scripts/observer_bench.py --observer-pilot
         → 跑 7 models × 5 fixtures = 35 calls
         → 看 summary.md 评测口径                         [5 min run + 15 min review]
         → 不对 → 改评测算法 → 回 step 4
         → 对 → step 5
Step 5 · Allen 扩 seeds.yaml 到 20 条                    [1 hr]
Step 6 · 重复 step 2-3 生成 fx_006~fx_020                [10 min + 1 hr review]
Step 7 · uv run python scripts/observer_bench.py --observer
         → 7 × 20 = 140 calls, ~$1.50                    [15 min]
Step 8 · Allen 分析 summary.md, 拍板 Observer 模型        [30 min]

Allen 时间总计: ~3.25 hr (分两天完成)
```

---

## 6. Observer 调用（Tool Use）

### 6.1 OBSERVER_SYSTEM_PROMPT

精简自 Mastra `observer-agent.ts` L17-L264（原 ~250 行），保留核心语义约束，英文骨架 + 中文输出要求。

```
You are the memory consciousness of an AI assistant.
Your observations will be the ONLY information the assistant has about past interactions.

## YOUR JOB
Extract structured observations from the conversation below.
Call the `record_observations` tool with your results.
ALWAYS respond in Chinese (中文). English output will be rejected.

## PRIORITY EMOJI
- 🔴 HIGH: explicit user facts/preferences, unresolved goals, critical context
- 🟡 MEDIUM: learned info, tool results, mild observations, user emotions
- 🟢 LOW: minor, uncertain, speculative
- ✅ DONE: task completed, question answered, issue resolved

## FORMAT RULES
- Each observation MUST have: priority (emoji), time (HH:MM 24h), text (中文)
- text field: 用中文撰写, 第三人称描述, 简洁 (10-50 字理想)
- Use the TIME from the message that triggered this observation

## CRITICAL: DISTINGUISH USER ASSERTIONS FROM QUESTIONS
- "我对虾过敏" → 🔴 assertion: 用户声明对虾过敏
- "虾过敏严重吗？" → question, 不要当作断言

## STATE CHANGES
If user indicates change, frame as state change that supersedes:
- "我不在 Acme 了换到 Stripe" → 🔴 用户从 Acme 换到 Stripe (不再在 Acme)
  - ❌ BAD: 用户在 Stripe 工作 (丢失了 "从 Acme 换过来" 的语义)
  - ✅ GOOD: 用户从 Acme 换到 Stripe

## PRESERVE UNUSUAL PHRASING
- 用户说 "累死了" → observation 写 "用户说累死了" 或 "用户疲惫 (原话: 累死了)"
- 不要"洗成"教科书普通话

## EMOTION DETECTION
If user message has emotion hint (tired/angry/happy/...) → add 🟡 observation

## AUTHORITY
User assertions are authoritative. If user said X earlier and now asks about X,
the assertion is the ground truth, the question doesn't invalidate it.

## OUTPUT
Call tool `record_observations` ONLY. Do not output free text.
```

**规模**: ~1500 tokens（不到 Mastra 原版 6KB 的 25%，留下 room 给中文扩展）。

### 6.2 OBSERVER_TOOL_DEF

```python
OBSERVER_TOOL_DEF = {
    "name": "record_observations",
    "description": "Record observations extracted from the conversation above.",
    "parameters": {
        "type": "object",
        "properties": {
            "observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "priority": {
                            "type": "string",
                            "enum": ["🔴", "🟡", "🟢", "✅"],
                            "description": "Priority emoji"
                        },
                        "time": {
                            "type": "string",
                            "pattern": "^[0-2][0-9]:[0-5][0-9]$",
                            "description": "HH:MM 24h format"
                        },
                        "text": {
                            "type": "string",
                            "minLength": 4,
                            "maxLength": 300,
                            "description": "Observation text in Chinese"
                        }
                    },
                    "required": ["priority", "time", "text"]
                },
                "minItems": 0,
                "maxItems": 10
            }
        },
        "required": ["observations"]
    }
}
```

### 6.3 build_observer_prompt

将 fixture.dialogue 渲染成人类可读的文本（不用原生 chat messages，避免 Observer 把 past conversation 当成 "me talking now"）:

```python
def build_observer_prompt(fixture: Fixture) -> tuple[str, str]:
    """Returns (system, user_message)"""
    system = OBSERVER_SYSTEM_PROMPT
    
    lines = ["以下是一段对话，抽取 observation 并调用 record_observations：\n"]
    for turn in fixture.dialogue:
        if turn["role"] == "user":
            emo_suffix = f" [情绪: {turn['emotion']}]" if turn.get("emotion") else ""
            lines.append(f"USER ({turn['time']}){emo_suffix}: {turn['content']}")
        elif turn["role"] == "assistant":
            lines.append(f"ASSISTANT ({turn['time']}): {turn['content']}")
        elif turn["role"] == "tool":
            args_str = json.dumps(turn['args'], ensure_ascii=False)
            lines.append(f"TOOL_CALL {turn['name']}({args_str}) → {turn['result']}")
    lines.append("\n请调用 record_observations 工具。")
    return system, "\n".join(lines)
```

### 6.4 Per-provider tool_choice 映射

```python
def build_tool_call_kwargs(provider: str) -> dict:
    if provider == "anthropic":
        return {
            "tools": [{
                "name": OBSERVER_TOOL_DEF["name"],
                "description": OBSERVER_TOOL_DEF["description"],
                "input_schema": OBSERVER_TOOL_DEF["parameters"],
            }],
            "tool_choice": {"type": "tool", "name": "record_observations"},
        }
    elif provider == "google":
        # Gemini function_declarations shape
        return {
            "tools": [{"function_declarations": [{
                "name": OBSERVER_TOOL_DEF["name"],
                "description": OBSERVER_TOOL_DEF["description"],
                "parameters": OBSERVER_TOOL_DEF["parameters"],
            }]}],
            "tool_config": {"function_calling_config": {
                "mode": "ANY",
                "allowed_function_names": ["record_observations"],
            }},
        }
    else:  # openai / xai / groq (OpenAI-compat)
        return {
            "tools": [{"type": "function", "function": OBSERVER_TOOL_DEF}],
            "tool_choice": {"type": "function", "function": {"name": "record_observations"}},
        }
```

### 6.5 Observer call 复用 v3

```python
def call_observer(spec: v3.ModelSpec, fixture: Fixture) -> ObserverCall:
    """Single call to Observer. Returns raw response + parsed observations."""
    system, user_msg = build_observer_prompt(fixture)
    tool_kwargs = build_tool_call_kwargs(spec.provider)
    
    # 调 v3 的 provider-specific call 函数, 但注入 tool 参数
    # 需要小改 v3 的 call_* 函数签名, 接受可选 extra_kwargs
    #
    # Alternatively: 在 observer_bench.py 里重写一个 call_with_tools(...)
    # 复用 v3 的 extract_cache_metrics + retry 逻辑
```

**v3 侵入改动**: v3 的 `call_anthropic` / `call_openai_compat` / `call_gemini` 签名加可选 `extra_kwargs: dict | None = None`，当前默认 `None` 不改行为。Observer bench 通过此参数注入 `tools` + `tool_choice`。

```python
# v3 signature 变动 (反向兼容)
async def call_anthropic(cs: CallSpec, extra_kwargs: dict | None = None) -> dict: ...
async def call_openai_compat(cs: CallSpec, base_url: str, api_key: str,
                             extra_kwargs: dict | None = None) -> dict: ...
async def call_gemini(cs: CallSpec, extra_kwargs: dict | None = None) -> dict: ...
```

v3 的现有测试不受影响（默认参数）。observer_bench 调用时传入 `build_tool_call_kwargs(provider)`。

**响应解析**: tool_call 参数提取也在 observer_bench.py 里做（不进 v3）。

---

## 7. 评测算法

### 7.1 per-fixture 打分

```python
@dataclass
class Scores:
    tool_success: bool           # tool_call 成功且字段合法
    precision: float             # matched / len(model_obs)
    recall: float                # matched / len(expected_obs)
    f1: float                    # 2PR/(P+R)
    priority_accuracy: float     # matched 中 priority 对的比例
    hallucination: bool          # 任意 obs.text 含 must_not_contain_globally
    extra_count: int             # len(model_obs) - len(expected_obs), 负数记 0

def evaluate(model_obs: list[dict], fixture: Fixture) -> Scores:
    # 1. Tool call 成功率
    tool_success = (
        isinstance(model_obs, list)
        and all(
            isinstance(o, dict) and
            o.get("priority") in {"🔴", "🟡", "🟢", "✅"} and
            isinstance(o.get("time"), str) and re.match(r"^[0-2]\d:[0-5]\d$", o["time"]) and
            isinstance(o.get("text"), str) and len(o["text"]) >= 4
            for o in model_obs
        )
    )
    
    # 2. 匹配（贪心）
    matched_expected = set()    # index into expected_observations
    matched_model = set()       # index into model_obs
    priority_correct = 0
    
    for ei, exp in enumerate(fixture.expected_observations):
        for mi, obs in enumerate(model_obs):
            if mi in matched_model: continue
            # must_contain_any_of: OR of (AND of keywords)
            if any(
                all(kw in obs["text"] for kw in keyword_list)
                for keyword_list in exp["must_contain_any_of"]
            ):
                matched_expected.add(ei)
                matched_model.add(mi)
                if obs["priority"] == exp["priority"]:
                    priority_correct += 1
                break  # 一个 expected 最多匹配一个 model obs
    
    recall = len(matched_expected) / len(fixture.expected_observations) if fixture.expected_observations else 0.0
    precision = len(matched_model) / len(model_obs) if model_obs else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    priority_acc = priority_correct / len(matched_expected) if matched_expected else 0.0
    
    # 3. Hallucination（全局禁用词）
    halluc = any(
        any(bad in obs.get("text", "") for bad in fixture.must_not_contain_globally)
        for obs in model_obs
    )
    
    # 4. Extra noise
    extra = max(0, len(model_obs) - len(fixture.expected_observations))
    
    return Scores(tool_success, precision, recall, f1, priority_acc, halluc, extra)
```

### 7.2 per-model 汇总

```
across 20 fixtures:
  F1 = macro avg (F1_per_fixture)
  Precision = macro avg
  Recall = macro avg
  Priority accuracy = macro avg (skip fixtures with 0 matched)
  Hallucination rate = count(halluc=True) / 20
  Tool success rate = count(tool_success=True) / 20
  Avg observer_latency_ms = median across 20
  Cost per 100 = avg cost × 100
```

### 7.3 评测口径的已知边界

- **Priority 严格匹配**: 🔴 vs 🟡 的判定本身有 subjective 成分，扣 priority_accuracy 分但不影响 recall/precision（这是对的，语义抓对优先于 priority 分级）
- **关键词穷举风险**: fixture schema 里 `must_contain_any_of` 的穷举可能漏掉合理表达。**缓解**: pilot 5 条跑完 Allen 看哪些 "语义对但关键词没覆盖" 的情况，补充进 fixture
- **Extra noise 不扣分**: 模型输出多余 observation（如 `🟢 用户住在温哥华`，fixture 里没标）不影响 F1，但记录在 CSV（`extra_count` 列）供分析

---

## 8. Model Catalog 增补

**新加 Gemini 2.5 Flash**（Mastra 默认 Observer）到 v3 的 `MODEL_CATALOG`:

```python
# 追加到 MODEL_CATALOG (v3)
ModelSpec("google", "gemini-2.5-flash",
          ("models/gemini-2.5-flash", "gemini-flash-latest"),
          0.30, 2.50,      # $0.30/1M in, $2.50/1M out (2026-04)
          1.00, 0.25,      # 无 write 费, 0.25x read
          4096)
```

**Observer 候选过滤**（observer_bench.py 里的常量，从 MODEL_CATALOG 筛选）:

```python
OBSERVER_CANDIDATES: tuple[str, ...] = (
    "gemini-2.5-flash",                   # Mastra 默认
    "gemini-3-pro-preview",
    "gpt-5-mini",
    "grok-4-1-fast-non-reasoning",        # 旧版基线
    "grok-4.20-0309-non-reasoning",       # 新版空白
    "llama-3.3-70b-versatile",
    "claude-haiku-4-5-20251001",
)
# 全量运行时: active_specs = [s for s in MODEL_CATALOG if s.primary_id in OBSERVER_CANDIDATES]
```

**候选 Observer 模型详细（7 个）**:

| # | Model ID | Provider | 理由 |
|---|---|---|---|
| 1 | `gemini-2.5-flash` | google | **Mastra 默认**，Observer 标杆 |
| 2 | `gemini-3-pro-preview` | google | 最新旗舰 preview，中文 72.5% |
| 3 | `gpt-5-mini` | openai | OpenAI 快速档 |
| 4 | `grok-4-1-fast-non-reasoning` | xai | 旧版基线（Pack 06 警告中文差） |
| 5 | `grok-4.20-0309-non-reasoning` | xai | **新版，空白区，要填** |
| 6 | `llama-3.3-70b-versatile` | groq | 零 cache 但 TTFT 低 |
| 7 | `claude-haiku-4-5-20251001` | anthropic | 验证 Mastra "Claude 不行" 声明 |

**不测**:
- Opus / Sonnet（Observer 是 cold path 不值）
- Grok 旗舰 `grok-4-0709`（对 Observer 场景浪费，且 $3/M 输入）
- `gpt-5`（默认 reasoning 5-7s TTFT，不适合 cold-path）

---

## 9. CLI 设计

### 9.1 命令

```bash
# 生成 fixture (对 seeds.yaml 里还没对应 fx_*.json 的 seed 调 Opus 生成)
uv run python scripts/observer_bench.py --observer-generate
  # stdout 打印 draft JSON, Allen 手动 review + 写入 fx_XXX.json

# Pilot: 只跑 fx_001~fx_005 × 7 models = 35 calls
uv run python scripts/observer_bench.py --observer-pilot

# 全量: 跑所有 observer_cn/fx_*.json 文件
uv run python scripts/observer_bench.py --observer

# 单模型: 跟 v3 行为一致
uv run python scripts/observer_bench.py --observer --model gemini-2.5-flash

# Dry run: 仅估算
uv run python scripts/observer_bench.py --observer --dry-run
```

### 9.2 Args

```python
parser.add_argument("--observer", action="store_true", help="Run full observer bench")
parser.add_argument("--observer-pilot", action="store_true", help="Run pilot (5 fixtures)")
parser.add_argument("--observer-generate", action="store_true",
                    help="Generate draft fixtures from seeds.yaml via Opus")
parser.add_argument("--model", type=str, help="Single model override")
parser.add_argument("--dry-run", action="store_true")
parser.add_argument("--output-dir", type=str, default=None)
parser.add_argument("--fixtures-dir", type=str, default="bench_fixtures/observer_cn")
parser.add_argument("--with-chart", action="store_true")
```

### 9.3 安全设施（继承 v3）

- Warmup（隔离 payload，零前缀污染）
- Exponential backoff + fallback_ids
- Dry-run 估算成本 + 打印 plan
- 缺 API key 的 provider 自动 skip

**不继承**: v3 的 smoke gate（`--quick` → `--standard` 强制流程不适用 Observer，因为 Observer pilot 就是 smoke gate）。

---

## 10. 输出

### 10.1 CSV 字段

```python
CSV_FIELDS = [
    "timestamp",                # ISO 8601
    "model",                    # primary_id or fallback_id
    "model_is_fallback",        # bool
    "provider",
    "fixture_id",               # fx_001
    "fixture_category",         # preference/state_change/temporal/emotion/smart_home/correction/multi_entity/completion
    "tool_success",             # bool
    "precision",                # float [0,1]
    "recall",                   # float
    "f1",                       # float
    "priority_accuracy",        # float
    "hallucination",            # bool
    "extra_count",              # int (多余 observation 数)
    "expected_count",           # int (fixture 的 expected_observations 条数)
    "matched_count",            # int
    "observer_latency_ms",      # float (完整 call total_ms, 包含 tool_call 返回)
    "actual_input_tokens_api",  # int (复用 v3 字段命名)
    "output_tokens",            # int
    "cost_usd",                 # float (三段计价, 复用 v3 calc_cost)
    "model_output_raw",         # str (tool_call arguments JSON, 截断到 1000 字)
    "error",                    # str (empty 表示成功)
]
```

**字段语义**:
- `fixture_category`: 从 `seeds.yaml` 的 scene 字段映射而来，供 Table 3 按类别 breakdown 用
- `actual_input_tokens_api`: 保持与 v3 CSV 同名，方便跨实验分析
- `model_output_raw`: 失败排查用，人眼看模型输出了啥
```

### 10.2 summary.md 的 5 张表

**Table 1 — 主排名表**（这是 Allen 拍板用的一张）:
```markdown
| Model | F1 | Precision | Recall | Priority Acc | Halluc Rate | Tool Success |
|---|---|---|---|---|---|---|
| google/gemini-2.5-flash | 0.85 | 0.88 | 0.82 | 0.75 | 5% | 100% |
| google/gemini-3-pro-preview | 0.82 | 0.85 | 0.79 | 0.78 | 10% | 100% |
| openai/gpt-5-mini | 0.79 | 0.85 | 0.74 | 0.70 | 5% | 100% |
| xai/grok-4.20-non-reasoning | 0.75 | 0.78 | 0.72 | 0.65 | 10% | 100% |
| xai/grok-4-1-fast-non-reasoning | 0.55 | 0.60 | 0.51 | 0.50 | 20% | 95% |
| anthropic/haiku-4-5 | ? | ? | ? | ? | ? | ? |  <!-- Mastra 说不行, 看实测 -->
| groq/llama-3.3-70b | ? | ? | ? | ? | ? | ? |
```

**Table 2 — 成本延迟表**:
```markdown
| Model | $/100 fixtures | Avg latency | P95 latency | Tokens out avg |
|---|---|---|---|---|
| gemini-2.5-flash | $0.04 | 1200ms | 2100ms | 180 |
...
```

**Table 3 — 按 fixture 类别分解** (F1 by scene 类型):
```markdown
| Model | 偏好 | 状态 | 时间 | 情感 | 家居 | 纠正 | 多实体 | ✅ |
|---|---|---|---|---|---|---|---|---|
| gemini-2.5-flash | 0.92 | 0.85 | 0.78 | 0.80 | 0.90 | 0.75 | 0.80 | 1.0 |
...
```

**Table 4 — Hallucination 样例**（halluc=True 的具体行文对比）:
```markdown
### fx_001 · gemini-3-pro · halluc=True

**禁用词**: 蓝光, 冷白, 卧室

**模型输出中触发的 observation**:
* 🟡 (14:28) 用户要求调节卧室灯为暖黄  ← 触发 "卧室"

**expected 对应 observation**:
* 🔴 (14:28) 用户偏好客厅灯暖黄色
```

**Table 5 — 推荐表**:
```markdown
## 推荐

### 🥇 主 Observer: gemini-2.5-flash
- F1 0.85 (最高), Halluc 5% (最低), Tool success 100%
- $0.04 per 100 fixtures (对 Jarvis 月 ~15000 observation = $6/月)
- 符合 Mastra 官方推荐

### 🥈 Fallback: gpt-5-mini
- F1 0.79 (第三), 但 cost $0.29/100 (7× 更贵于 gemini)
- 选它做 fallback 因为 OpenAI API 通常比 Google 稳定
```

### 10.3 run_meta.json

```json
{
  "mode": "observer",  // or "observer-pilot"
  "timestamp": "2026-04-15_1430",
  "total_calls": 140,
  "errors_total": 2,
  "cost_usd_total": 1.48,
  "elapsed_sec": 892.5,
  "pricing_snapshot_date": "2026-04-14",
  "fixtures_used": ["fx_001", "fx_002", ...],
  "active_models": ["gemini-2.5-flash", ...],
  "args": { ... }
}
```

### 10.4 chart.html（可选）

- 已有 v3 的 plotly renderer
- 新增: F1 vs avg_latency **scatter plot**，每模型一个点，颜色区分 provider。一眼看出 "快 + 准确" 的帕累托前沿

---

## 11. 成本估算

| 阶段 | Calls | Cost | 时长 |
|---|---|---|---|
| Opus 生成 5 pilot fixture | 5 (Opus) | ~$1.00 | 3 min |
| Pilot 运行 7×5 | 35 | ~$0.30 | 3 min |
| Opus 生成 15 full fixture | 15 (Opus) | ~$3.00 | 10 min |
| 全量运行 7×20 | 140 | ~$1.50 | 15 min |
| **合计** | **195** | **~$5.80** | **~30 min** (不含 Allen review) |

**Allen 时间**: ~3.25 hr，分两天:
- Day 1: seeds 5 条 (30 min) + review pilot fixture (30 min) + 看 pilot 结果 (15 min)
- Day 2: seeds 扩到 20 (1 hr) + review 15 条新 fixture (1 hr) + 分析最终结果 (30 min)

---

## 12. 已知限制

1. **20 条 fixture 统计显著性弱**: 7 模型各 20 数据点，模型间差 ~5 pp F1 可能不显著。**缓解**: 看 F1 + Halluc + Tool success 三个维度的一致性，而非 single-point p 值判定
2. **Allen review 引入 bias**: Allen 的中文观感主观性影响 ground truth。**缓解**: 写清楚 `semantic_description`，future Allen 能 diff 之前 Allen 的决定
3. **Mastra prose vs 我们 function call 的能力差异**: 不测（非目标），接受此偏差
4. **`must_contain_any_of` 关键词穷举风险**: pilot 5 条跑完 review 哪些 miss，扩充 synonym
5. **Opus 当 fixture 作家可能泄漏训练分布偏好**: `tone_hint` 字段缓解但不根除，Allen review 是最终防线
6. **Gemini 2.5 Flash 定价可能过时**: 2026-03 数据为 $0.30/$2.50，上线前在 console 核对

---

## 13. 成功标准

1. **一张表拍板**: summary.md Table 1 + Table 5 清楚标出主 Observer + fallback，Allen 能直接决定
2. **总预算控制**: ≤ $6 cost, ≤ 30 min wall-clock（不含 Allen review）
3. **复刻性**: fixture git tracked + pricing snapshot + model ID 带日期 → 6 月后能一键 rerun 看有没有回归
4. **Pack 06 空白被填**: Grok 4.20-0309 中文 Observer 能力有实测数字（无论好坏）
5. **Jarvis 生产可接入**: 实验用 function call + tool schema 完全对齐 Jarvis 未来 extractor 代码，无需改造

---

## 14. 文件引用

### 设计参考
- v3 实验记录: `notes/bench-llm-v3-experiment-2026-04-14.md`
- v3 spec: `docs/superpowers/specs/2026-04-14-bench-llm-v3-design.md`

### Mastra 研究（前置调研）
- `notes/mastra-om-research-2026-04-15.md` — OM 数据结构 + prompt 骨架
- `notes/mastra-research-03-observer-instructions-2026-04-15.md` — Observer prompt 原文 (L17-L264)
- `notes/mastra-research-04-gemini-vs-gpt4o-mini-2026-04-15.md` — 中文对比空白
- `notes/mastra-research-06-grok-observer-2026-04-15.md` — Grok 可行性
- `notes/mastra-research-01-optimize-filter-2026-04-15.md` — Filter 模型
- `notes/mastra-research-02-reflector-ladder-2026-04-15.md` — Reflector 级联
- `notes/mastra-research-05-production-scale-2026-04-15.md` — 生产规模

### 实验代码（本实验）
- 主脚本: `scripts/observer_bench.py` (待实现)
- Fixture 目录: `bench_fixtures/observer_cn/` (待创建)
- 复用: `scripts/bench_llm_v3.py`（providers / cost / retry / extract_cache_metrics）

---

## 15. 后续工作

### 立即
- [ ] 写实施计划（见 `docs/superpowers/plans/2026-04-15-observer-bench.md`）
- [ ] 实现 `observer_bench.py`
- [ ] Allen 写 5 条 seed + review pilot fixture

### 扩展（不阻塞本实验）
- [ ] 测 Reflector（压缩 observation stream 的能力）
- [ ] 测 Filter（过滤 memory 检索到的 observation）
- [ ] 加英文 fixture 横向对比中文/英文 F1 差距
- [ ] 加多轮累积测试（同一用户多次 extraction 后的一致性）

---

## 16. 设计确认清单

- [x] Output format: Tool Use / Function Call（B 方案，统一路径）
- [x] Fixture 生成: Allen seeds → Opus 扩写 → Allen review（B 方案）+ `tone_hint` 防污染 + pilot 5 条优先
- [x] Fixture 分布: 20 条按 8 类加权（偏好 4 / 状态 3 / 时间 3 / 情感 3 / 家居 3 / 纠正 2 / 多实体 1 / ✅ 1）
- [x] 评测算法: pure rule-based, `must_contain_any_of` (OR of AND), `must_not_contain_globally` 触发 halluc
- [x] Model catalog: 加 Gemini 2.5 Flash, 共 7 个候选 Observer
- [x] CLI: `--observer` / `--observer-pilot` / `--observer-generate` 三主模式
- [x] 输出: CSV 20 字段 + summary.md 5 张表 + run_meta.json
- [x] 成本预算: $6 总，分 Opus 生成 $4 + 7×20 跑 $2
- [x] 代码复用: import v3，通过小改 v3 的 call_* 加 `extra_kwargs` 参数（侵入 1 行）

**下一步**: 交棒给 `writing-plans` 生成实施计划。
